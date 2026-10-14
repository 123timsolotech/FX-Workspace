"""
mcp_client.py
=============
A small Model Context Protocol client (JSON-RPC 2.0) - the `mcp` package is
not available on the host, and the protocol subset needed is small:
initialize, tools/list, tools/call, resources/list, resources/read, the
tools/list_changed notification and request cancellation.

Transports
  * stdio            the server is a local program (command + args); one JSON
                     message per line on stdin/stdout, stderr kept as its log.
  * streamable HTTP  the server is a URL; each request is a POST, answered as
                     JSON or as a short SSE stream; the Mcp-Session-Id header
                     is kept.

Servers are configured by the user in the MCP tab (data/mcp_servers.json,
git-ignored). Environment values and headers may hold API keys: they are
stored only in that file, never logged and never sent to the UI (only their
names are).

How MCP tools reach the model (non-negotiable 2): every tool of a connected
server is registered in the ONE tool registry as `mcp__<server>__<tool>` with
its own input schema. Calls go through permissions.explain (MCP tools are in
the destructive tier - they ask by default), the approval card ("Not
undoable" - an MCP call cannot be taken back), the ToolExecutor (timeout,
Stop) and Activity events; results are framed as untrusted content. Folder
rules never apply to MCP tools, and nothing about a server - its name, its
tool descriptions, its "read-only" hints - changes a permission.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

import httpx

import untrusted

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "FX Workspace", "version": "1.0"}
MAX_TOOLS_PER_SERVER = 60
CALL_TIMEOUT = 120.0
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


class MCPError(Exception):
    pass


def tool_name(server: str, tool: str) -> str:
    """The registry name: mcp__<server>__<tool>, limited to the characters
    function-calling APIs accept (64 max). A name that had to be shortened
    or cleaned ends in a short hash of the original, so two tools never
    collapse onto one name."""
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    name = f"mcp__{server}__{clean}"
    if len(name) <= 64 and clean == tool:
        return name
    tag = hashlib.sha1(tool.encode("utf-8")).hexdigest()[:6]
    return f"{name[:57]}_{tag}"


# What a stdio server process inherits from this app's environment: what
# programs need to start and find their files - never the app's API keys
# (keys.local.bat sets them for the whole process) or any other variable.
# The server's own configured env is added on top.
_ENV_ALLOW = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
              "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
              "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "PUBLIC",
              "USERNAME", "USERDOMAIN", "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
              "LANG", "LC_ALL", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"}


def child_env(extra: dict | None = None, base: dict | None = None) -> dict:
    base = os.environ if base is None else base
    env = {k: v for k, v in base.items() if k.upper() in _ENV_ALLOW}
    env.update(extra or {})
    return env


_URL_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@", re.I)
_SECRET_PARAM = re.compile(r"([?&;](?:[\w.-]*(?:key|token|secret|password|passwd|pwd|auth|signature|sig))=)[^&#\s]+", re.I)
_SECRET_FLAG = re.compile(r"^(--?[\w-]*(?:key|token|secret|password|passwd|pwd|auth)[\w-]*)(=)(.+)$", re.I)
_SECRET_FLAG_ALONE = re.compile(r"^--?[\w-]*(?:key|token|secret|password|passwd|pwd|auth)[\w-]*$", re.I)


def redact(text: str) -> str:
    """Credentials in a URL or a command-line argument, for display."""
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{m.group(2)}:***@", str(text or ""))
    text = _SECRET_PARAM.sub(lambda m: m.group(1) + "***", text)
    return _SECRET_FLAG.sub(lambda m: m.group(1) + m.group(2) + "***", text)


def redact_args(args: list) -> list:
    out, hide_next = [], False
    for a in args or []:
        a = str(a)
        out.append("***" if hide_next else redact(a))
        hide_next = bool(_SECRET_FLAG_ALONE.match(a))
    return out


def split_name(name: str) -> tuple[str, str] | None:
    if not name.startswith("mcp__"):
        return None
    server, _, tool = name[5:].partition("__")
    return (server, tool) if server and tool else None


# ==============================================================================
# Transports
# ==============================================================================
class StdioTransport:
    def __init__(self, command: str, args: list, env: dict | None = None, cwd: str | None = None):
        self.command, self.args, self.env, self.cwd = command, list(args or []), dict(env or {}), cwd
        self.proc = None
        self.log = collections.deque(maxlen=60)          # the server's stderr
        self.on_message = None
        self._write = threading.Lock()

    def start(self):
        exe = shutil.which(self.command) or self.command      # finds npx.cmd / uvx.exe on Windows
        env = child_env(self.env)
        try:
            self.proc = subprocess.Popen([exe] + self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=env, cwd=self.cwd or None,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise MCPError(f"Could not start '{self.command}': {e}")
        threading.Thread(target=self._read_out, daemon=True, name="mcp-stdout").start()
        threading.Thread(target=self._read_err, daemon=True, name="mcp-stderr").start()

    def _read_out(self):
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.log.append(f"(not JSON on stdout) {line[:300]}")
                    continue
                if self.on_message:
                    self.on_message(msg)
        except (ValueError, OSError):          # the pipe was closed by close()
            pass
        if self.on_message:
            try:
                code = self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                code = None
            self.on_message({"_closed": True, "code": code})

    def _read_err(self):
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    self.log.append(text[:500])
        except (ValueError, OSError):
            pass

    def send(self, msg: dict):
        if not self.proc or self.proc.poll() is not None:
            raise MCPError("The server process is not running.")
        with self._write:
            try:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except OSError as e:
                raise MCPError(f"Could not write to the server: {e}")

    def close(self):
        self.on_message = None                      # a deliberate close is not a crash
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            if os.name == "nt":
                # npx.cmd / uvx start cmd.exe, which starts node/python: end the whole tree.
                try:
                    subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                                   timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except (OSError, subprocess.SubprocessError):
                    pass
            if self.proc.poll() is None:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass


class HTTPTransport:
    """Streamable HTTP: POST each message; a request is answered with JSON or
    an SSE stream carrying the response (and maybe notifications). A send
    blocks until the answer arrives, so Server sends on a worker thread."""
    blocking = True

    def __init__(self, url: str, headers: dict | None = None, client: httpx.Client | None = None):
        self.url, self.headers = url, dict(headers or {})
        self.session_id = None
        self.log = collections.deque(maxlen=60)
        self.on_message = None
        self._client = client

    def start(self):
        if self._client is None:
            # Through the network layer: a server on this PC stays direct; a remote one takes the
            # "other" route (proxy / Tor), the kill switch and the connection log.
            import netlayer
            self._client = netlayer.client("other", timeout=httpx.Timeout(CALL_TIMEOUT, connect=10.0))

    def send(self, msg: dict):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            resp = self._client.post(self.url, content=json.dumps(msg), headers=headers)
        except httpx.HTTPError as e:
            raise MCPError(f"Could not reach {self.url}: {e}")
        if resp.headers.get("mcp-session-id"):
            self.session_id = resp.headers["mcp-session-id"]
        if resp.status_code == 202 or not resp.content:
            return
        if resp.status_code >= 400:
            raise MCPError(f"{self.url} answered {resp.status_code}: {resp.text[:200]}")
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for block in resp.text.split("\n\n"):
                data = "\n".join(l[5:].strip() for l in block.splitlines() if l.startswith("data:"))
                if data and self.on_message:
                    self.on_message(json.loads(data))
        elif self.on_message:
            body = resp.json()
            for item in body if isinstance(body, list) else [body]:
                self.on_message(item)

    def close(self):
        if self._client is not None:
            try:
                if self.session_id:
                    self._client.delete(self.url, headers={**self.headers, "Mcp-Session-Id": self.session_id})
            except httpx.HTTPError:
                pass


# ==============================================================================
# One server
# ==============================================================================
class Server:
    def __init__(self, name: str, config: dict, transport_factory=None):
        self.name, self.config = name, dict(config)
        self.status, self.error, self.info, self.protocol = "disconnected", "", {}, ""
        self.tools: list = []
        self.resources: list = []
        self.connected_at = None
        self._factory = transport_factory
        self._transport = None
        self._next = 0
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._connecting = threading.Lock()      # one connect at a time
        self.on_tools_changed = None

    # ---- plumbing --------------------------------------------------------------
    def _make_transport(self):
        if self._factory:
            return self._factory(self.config)
        if self.config.get("url"):
            return HTTPTransport(self.config["url"], self.config.get("headers"))
        return StdioTransport(self.config.get("command") or "", self.config.get("args") or [],
                              self.config.get("env"), self.config.get("cwd"))

    def _on_message(self, msg: dict):
        if msg.get("_closed"):
            with self._lock:
                waiting = list(self._pending.values())
                self._pending.clear()
            for slot in waiting:
                slot["error"] = {"message": f"the server exited (code {msg.get('code')})"}
                slot["event"].set()
            if self.status == "connected":
                self.status, self.error = "error", f"The server exited (code {msg.get('code')})."
                if self.on_tools_changed:
                    self.on_tools_changed(self)
            return
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._lock:
                slot = self._pending.pop(msg["id"], None)
            if slot:
                slot.update(result=msg.get("result"), error=msg.get("error"))
                slot["event"].set()
            return
        method = msg.get("method")
        if method == "notifications/tools/list_changed":
            threading.Thread(target=self._refresh_tools, daemon=True).start()
        elif "id" in msg and method:                     # a request from the server (sampling, roots...): not supported
            try:
                self._transport.send({"jsonrpc": "2.0", "id": msg["id"],
                                      "error": {"code": -32601, "message": "Not supported by this client."}})
            except MCPError:
                pass

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0, cancelled=None):
        if self._transport is None:
            raise MCPError(f"'{self.name}' is not connected.")
        with self._lock:
            self._next += 1
            rid = self._next
            slot = {"event": threading.Event(), "result": None, "error": None}
            self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, slot)
        deadline = time.time() + timeout
        while not slot["event"].wait(0.2):
            stopped = bool(cancelled and cancelled())
            if stopped or time.time() >= deadline:
                with self._lock:
                    self._pending.pop(rid, None)
                self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                            "params": {"requestId": rid, "reason": "stopped" if stopped else "timeout"}})
                raise MCPError("Stopped by you." if stopped else f"'{self.name}' did not answer {method} within {timeout:.0f}s.")
        if slot["error"]:
            raise MCPError(str(slot["error"].get("message") or slot["error"]))
        return slot["result"] or {}

    def _send(self, msg: dict, slot: dict | None = None):
        """Send one message. A blocking transport (HTTP: the POST lasts as long
        as the call) sends on a worker thread, so the caller keeps watching
        for Stop and its deadline and can send the cancellation."""
        transport = self._transport
        if transport is None:
            raise MCPError(f"'{self.name}' is not connected.")

        def run():
            try:
                transport.send(msg)
            except MCPError as e:
                if slot is None:
                    return
                with self._lock:
                    self._pending.pop(msg.get("id"), None)
                slot["error"] = {"message": str(e)}
                slot["event"].set()
        if getattr(transport, "blocking", False):
            threading.Thread(target=run, daemon=True, name=f"mcp-send-{self.name}").start()
        elif slot is None:
            try:
                transport.send(msg)
            except MCPError:
                pass
        else:
            transport.send(msg)

    def notify(self, method: str, params: dict | None = None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ---- lifecycle ------------------------------------------------------------
    def connect(self, timeout: float = 30.0):
        if not self._connecting.acquire(blocking=False):
            return False                         # a connect is already under way: don't race a second transport
        try:
            return self._connect(timeout)
        finally:
            self._connecting.release()

    def _connect(self, timeout: float):
        self.disconnect(quiet=True)
        self.status, self.error = "connecting", ""
        try:
            self._transport = self._make_transport()
            self._transport.on_message = self._on_message
            self._transport.start()
            init = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                               "clientInfo": CLIENT_INFO}, timeout=timeout)
            self.info = init.get("serverInfo") or {}
            self.protocol = init.get("protocolVersion") or ""
            caps = init.get("capabilities") or {}
            self.notify("notifications/initialized")
            self.tools = self._list("tools/list", "tools") if "tools" in caps or not caps else []
            self.resources = self._list("resources/list", "resources") if "resources" in caps else []
            self.status, self.connected_at = "connected", time.time()
        except Exception as e:
            self.status, self.error = "error", str(e)[:500]
            self._close_transport()
        if self.on_tools_changed:
            self.on_tools_changed(self)
        return self.status == "connected"

    def _list(self, method: str, key: str) -> list:
        out, cursor = [], None
        for _ in range(20):
            res = self.request(method, {"cursor": cursor} if cursor else {})
            out += [x for x in res.get(key) or [] if isinstance(x, dict)]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return out

    def _refresh_tools(self):
        try:
            self.tools = self._list("tools/list", "tools")
        except MCPError as e:
            self.error = f"Could not refresh tools: {e}"
        if self.on_tools_changed:
            self.on_tools_changed(self)

    def _close_transport(self):
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        # Calls still waiting would otherwise hang until their timeout.
        with self._lock:
            waiting = list(self._pending.values())
            self._pending.clear()
        for slot in waiting:
            slot["error"] = {"message": "the server was disconnected"}
            slot["event"].set()

    def disconnect(self, quiet: bool = False):
        self._close_transport()
        was = self.status
        self.status, self.tools, self.resources = "disconnected", [], []
        if not quiet and was != "disconnected" and self.on_tools_changed:
            self.on_tools_changed(self)

    def log(self) -> list:
        return list(getattr(self._transport, "log", []) or [])

    # ---- calls -----------------------------------------------------------------
    def call_tool(self, tool: str, arguments: dict, cancelled=None) -> dict:
        res = self.request("tools/call", {"name": tool, "arguments": arguments or {}}, timeout=CALL_TIMEOUT,
                           cancelled=cancelled)
        parts = []
        for item in res.get("content") or []:
            kind = item.get("type")
            if kind == "text":
                parts.append(str(item.get("text") or ""))
            elif kind == "resource":
                r = item.get("resource") or {}
                parts.append(f"[resource {r.get('uri')}]\n{r.get('text') or '(binary content not shown)'}")
            elif kind in ("image", "audio"):
                parts.append(f"[{kind} {item.get('mimeType') or ''} returned - not shown]")
        text = "\n".join(p for p in parts if p) or "(no content)"
        return {"ok": not res.get("isError"), "message": text[:20000], "data": res.get("structuredContent")}

    def read_resource(self, uri: str) -> dict:
        res = self.request("resources/read", {"uri": uri})
        parts = []
        for c in res.get("contents") or []:
            parts.append(c.get("text") if c.get("text") is not None else f"(binary {c.get('mimeType') or ''} content not shown)")
        return {"ok": True, "message": ("\n".join(parts) or "(empty)")[:20000]}

    def public(self) -> dict:
        cfg = self.config
        return {"name": self.name, "status": self.status, "error": self.error, "enabled": cfg.get("enabled", True),
                "transport": "http" if cfg.get("url") else "stdio",
                "command": cfg.get("command") or "", "args": redact_args(cfg.get("args") or []),
                "url": redact(cfg.get("url") or ""),
                "env_keys": sorted((cfg.get("env") or {}).keys()), "header_keys": sorted((cfg.get("headers") or {}).keys()),
                "server": self.info, "protocol": self.protocol, "connected_at": self.connected_at,
                "tools": [{"name": t.get("name"), "registered": tool_name(self.name, t.get("name") or ""),
                           "description": (t.get("description") or "")[:300]} for t in self.tools[:MAX_TOOLS_PER_SERVER]],
                "tool_count": len(self.tools), "resources": [{"uri": r.get("uri"), "name": r.get("name") or r.get("uri"),
                                                              "mimeType": r.get("mimeType")} for r in self.resources[:100]],
                "log": self.log()[-15:]}


# ==============================================================================
# The hub: configuration, servers, and their tools as registry entries
# ==============================================================================
def clean_config(name: str, cfg: dict) -> dict:
    """Validate what the user entered. Raises ValueError."""
    if not _NAME.match(name or ""):
        raise ValueError("Name: letters, digits, - and _ only (up to 32), starting with a letter or digit.")
    if "__" in name:
        raise ValueError("Name can't contain '__'.")
    out = {"enabled": bool(cfg.get("enabled", True))}
    url = str(cfg.get("url") or "").strip()
    command = str(cfg.get("command") or "").strip()
    if bool(url) == bool(command):
        raise ValueError("Give either a command (a program on this PC) or a URL (an HTTP server).")
    if url:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("The URL must start with http:// or https://.")
        out["url"] = url
        headers = cfg.get("headers") or {}
        if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ValueError("Headers must be name: value text pairs.")
        out["headers"] = headers
    else:
        out["command"] = command
        args = cfg.get("args") or []
        if isinstance(args, str):
            args = [a for a in args.split(" ") if a]
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError("Arguments must be a list of text.")
        out["args"] = args
        env = cfg.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError("Environment must be NAME: value text pairs.")
        out["env"] = env
        if cfg.get("cwd"):
            out["cwd"] = str(cfg["cwd"])
    return out


class Hub:
    def __init__(self, path: str, transport_factory=None):
        self.path = path
        self.servers: dict[str, Server] = {}
        self._factory = transport_factory
        self._lock = threading.RLock()
        self.on_change = None            # () -> None: the registry re-syncs its mcp__ tools

    # ---- config file ---------------------------------------------------------------
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        raw = data.get("servers") if isinstance(data, dict) else None
        with self._lock:
            for name, cfg in (raw or {}).items():
                try:
                    self.servers[name] = self._server(name, clean_config(name, cfg))
                except (ValueError, TypeError):
                    continue

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "servers": {n: s.config for n, s in self.servers.items()}}, f, indent=2)
        os.replace(tmp, self.path)

    def _server(self, name, cfg) -> Server:
        s = Server(name, cfg, self._factory)
        s.on_tools_changed = lambda _s: self.on_change and self.on_change()
        return s

    # ---- user actions --------------------------------------------------------------
    def upsert(self, name: str, cfg: dict, keep_secrets: bool = True) -> Server:
        """Add or change a server. Secrets not re-entered are kept (the UI never
        sees them, so an edit without them must not wipe them) - but only while
        the server stays the same program or address: a changed command or URL
        never receives the old server's keys."""
        with self._lock:
            old = self.servers.get(name)
            same_target = old is not None and (str(cfg.get("command") or "").strip(), str(cfg.get("url") or "").strip()) \
                == (old.config.get("command") or "", old.config.get("url") or "")
            if old and keep_secrets and same_target:
                for key in ("env", "headers"):
                    if key in (cfg or {}) and isinstance(cfg[key], dict) and isinstance(old.config.get(key), dict):
                        cfg[key] = {k: (v if v != "" else old.config[key].get(k, "")) for k, v in cfg[key].items()}
            clean = clean_config(name, cfg)
            if old:
                old.disconnect()
            self.servers[name] = self._server(name, clean)
            self._save()
            return self.servers[name]

    def remove(self, name: str) -> bool:
        with self._lock:
            s = self.servers.pop(name, None)
            if s is None:
                return False
            s.disconnect()
            self._save()
        if self.on_change:
            self.on_change()
        return True

    def connect(self, name: str) -> Server:
        s = self.servers.get(name)
        if s is None:
            raise KeyError(name)
        s.connect()
        return s

    def disconnect(self, name: str) -> Server:
        s = self.servers.get(name)
        if s is None:
            raise KeyError(name)
        s.disconnect()
        return s

    def connect_enabled(self):
        for s in list(self.servers.values()):
            if s.config.get("enabled", True):
                s.connect()

    def close(self):
        for s in list(self.servers.values()):
            s.disconnect(quiet=True)

    # ---- tools -------------------------------------------------------------------------
    def tool_schemas(self) -> list:
        """OpenAI-style schemas of every tool of every connected server."""
        out = []
        for s in self.servers.values():
            if s.status != "connected":
                continue
            for t in s.tools[:MAX_TOOLS_PER_SERVER]:
                name = t.get("name")
                if not name:
                    continue
                params = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object", "properties": {}}
                params = {"type": "object", "properties": dict(params.get("properties") or {}),
                          "required": [r for r in params.get("required") or [] if isinstance(r, str)]}
                params["properties"].setdefault("justification", {"type": "string"})
                desc = re.sub(r"\s+", " ", str(t.get("description") or name))[:600]
                if untrusted.find_instructions(desc):       # the description is the server's text, not ours
                    desc = "(The server's description was withheld: it contained instructions to the assistant.)"
                desc = f"[MCP server '{s.name}'] " + desc
                out.append({"type": "function", "function": {"name": tool_name(s.name, name), "description": desc,
                                                             "parameters": params},
                            "_mcp": {"server": s.name, "tool": name}})
        return out

    def call(self, registered: str, args: dict, cancelled=None) -> dict:
        parts = split_name(registered)
        s = self.servers.get(parts[0]) if parts else None
        if s is None or s.status != "connected":
            return {"ok": False, "message": f"The MCP server for '{registered}' is not connected. Connect it in the MCP tab."}
        tool = next((t.get("name") for t in s.tools if tool_name(s.name, t.get("name") or "") == registered), None)
        if tool is None:
            return {"ok": False, "message": f"'{registered}' is no longer offered by '{s.name}'."}
        try:
            return s.call_tool(tool, {k: v for k, v in (args or {}).items() if k != "justification"}, cancelled)
        except MCPError as e:
            return {"ok": False, "message": f"MCP '{s.name}' {tool} failed: {e}",
                    **({"status": "cancelled"} if "Stopped" in str(e) else {})}

    def read_resource(self, server: str, uri: str) -> dict:
        s = self.servers.get(server)
        if s is None or s.status != "connected":
            return {"ok": False, "message": f"MCP server '{server}' is not connected."}
        try:
            return s.read_resource(uri)
        except MCPError as e:
            return {"ok": False, "message": f"Could not read {uri}: {e}"}

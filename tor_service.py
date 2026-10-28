"""
tor_service.py
==============
The app's own Tor (1.4.4). The installer ships the Tor Project's Expert Bundle in {app}\\tor\\ (installer/build.ps1
checks its signature first); run from source, the build's cached copy in installer\\cache\\tor\\<version>\\x\\ is
used. netlayer routes through it when a route is "Tor" and the source is "built in".

  * Runs only while something needs it: started by the first request routed through it (ensure()), stopped when no
    route uses it any more (a watchdog) and when the app exits. Tor itself also exits if this process dies
    (__OwningControllerProcess), so it is never left running. No Windows service, nothing installed.
  * Its own ports: a free SOCKS port and a control port on 127.0.0.1 - never 9050/9150, so it never collides with
    Tor Browser or a Tor the user runs. Control is cookie-authenticated.
  * Its state lives in data\\tor\\ (guard nodes and the network consensus), so later starts take seconds.
  * Connecting: Tor's own "Bootstrapped N%" lines are read as it starts; ready() is true at 100 %. Until then netlayer
    blocks routed requests (kill switch) - nothing goes direct while Tor connects.
  * New identity: the control port's SIGNAL NEWNYM - new circuits for new requests.
  * Circuits per chat: the SOCKS port isolates by SOCKS username (IsolateSOCKSAuth) and netlayer gives each chat its
    own, so two chats never share an exit.
  * Bridges, for networks that block Tor: obfs4 (lines the user pastes) or Snowflake (the bundle's own), through the
    bundle's lyrebird.
  * Fails visibly: a Tor that exits is restarted once; after that the status carries Tor's last error lines.
"""
from __future__ import annotations

import collections
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data", "tor")
BRIDGE_MODES = ("off", "obfs4", "snowflake")
IDLE_CHECK_S = 30
_BOOT = re.compile(r"Bootstrapped (\d+)%(?: \(([\w_]+)\))?: (.*)")

_lock = threading.RLock()
_st = {"proc": None, "socks": 0, "control": 0, "progress": 0, "phase": "", "summary": "", "error": "",
       "started": 0.0, "ready_at": 0.0, "restarts": 0, "config": None, "watchdog": None, "wanted": None}
_log: collections.deque = collections.deque(maxlen=60)


def _bundle_dirs() -> list[str]:
    out = [os.path.join(ROOT, "tor")]
    try:
        ver = open(os.path.join(ROOT, "installer", "tor.version"), encoding="utf-8").read().strip()
        out.append(os.path.join(ROOT, "installer", "cache", "tor", ver, "x"))
    except OSError:
        pass
    return out


def find() -> dict | None:
    """The shipped Tor's files, or None when this install has none."""
    for base in _bundle_dirs():
        exe = os.path.join(base, "tor", "tor.exe")
        if os.path.isfile(exe):
            version = ""
            try:
                version = open(os.path.join(base, "VERSION"), encoding="utf-8").read().strip()
            except OSError:
                version = os.path.basename(os.path.dirname(base)) if base.endswith(os.sep + "x") else ""
            pt = os.path.join(base, "tor", "pluggable_transports")
            return {"base": base, "exe": exe, "geoip": os.path.join(base, "data", "geoip"),
                    "geoip6": os.path.join(base, "data", "geoip6"), "lyrebird": os.path.join(pt, "lyrebird.exe"),
                    "pt_config": os.path.join(pt, "pt_config.json"), "version": version}
    return None


def available() -> bool:
    return find() is not None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _snowflake_lines(files: dict) -> list[str]:
    try:
        with open(files["pt_config"], encoding="utf-8") as f:
            return list(json.load(f)["bridges"]["snowflake"])
    except (OSError, ValueError, KeyError, TypeError):
        return []


def clean_bridge_lines(text: str) -> list[str]:
    """obfs4 bridge lines as the user pasted them (from bridges.torproject.org or a friend): one per line, the word
    "Bridge" optional; anything that isn't an obfs4 line is dropped."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if line.lower().startswith("bridge "):
            line = line[7:].strip()
        if re.fullmatch(r"obfs4 [\w.:\[\]]+:\d+ [0-9A-Fa-f]{40} cert=\S+ iat-mode=\d", line):
            out.append(line)
    return out[:20]


def torrc(files: dict, socks: int, control: int, bridges: str = "off", bridge_lines: str = "") -> str:
    """The configuration this app runs Tor with (written to data\\tor\\torrc on each start)."""
    data = DATA_DIR.replace("\\", "/")
    lines = [
        f"SocksPort 127.0.0.1:{socks} IsolateSOCKSAuth",
        f"ControlPort 127.0.0.1:{control}",
        "CookieAuthentication 1",
        f'DataDirectory "{data}"',
        f'GeoIPFile "{files["geoip"].replace(chr(92), "/")}"',
        f'GeoIPv6File "{files["geoip6"].replace(chr(92), "/")}"',
        f"__OwningControllerProcess {os.getpid()}",
        "ClientOnly 1",
        "AvoidDiskWrites 1",
        "DormantCanceledByStartup 1",
        "Log notice stdout",
    ]
    chosen = clean_bridge_lines(bridge_lines) if bridges == "obfs4" else _snowflake_lines(files) if bridges == "snowflake" else []
    if chosen:
        pt = files["lyrebird"].replace("\\", "/")
        kind = "obfs4" if bridges == "obfs4" else "snowflake"
        lines += ["UseBridges 1", f'ClientTransportPlugin {kind} exec "{pt}"'] + [f"Bridge {b}" for b in chosen]
    return "\n".join(lines) + "\n"


def _reader(proc):
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        _log.append(line)
        m = _BOOT.search(line)
        with _lock:
            if _st["proc"] is not proc:
                return
            if m:
                _st["progress"], _st["phase"], _st["summary"] = int(m.group(1)), m.group(2) or "", m.group(3)
                if _st["progress"] >= 100 and not _st["ready_at"]:
                    _st["ready_at"] = time.time()
            elif "[err]" in line or "[warn]" in line and ("Problem bootstrapping" in line or "Could not" in line):
                _st["error"] = line.split("] ", 1)[-1][:300]


def _start(bridges: str, bridge_lines: str) -> bool:
    files = find()
    if not files:
        _st["error"] = "This install has no built-in Tor."
        return False
    os.makedirs(DATA_DIR, exist_ok=True)
    socks, control = _free_port(), _free_port()
    path = os.path.join(DATA_DIR, "torrc")
    defaults = os.path.join(DATA_DIR, "torrc-defaults")          # empty: not the bundle's, which is Tor Browser's
    with open(path, "w", encoding="utf-8") as f:
        f.write(torrc(files, socks, control, bridges, bridge_lines))
    open(defaults, "w").close()
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen([files["exe"], "-f", path, "--defaults-torrc", defaults], cwd=os.path.dirname(files["exe"]),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags)
    except OSError as e:
        _st["error"] = f"Tor couldn't start: {e}"
        return False
    _st.update(proc=proc, socks=socks, control=control, progress=0, phase="starting", summary="Starting Tor",
               error="", started=time.time(), ready_at=0.0, config=(bridges, bridge_lines))
    threading.Thread(target=_reader, args=(proc,), daemon=True, name="tor-log").start()
    _watch()
    return True


def running() -> bool:
    p = _st["proc"]
    return p is not None and p.poll() is None


def ensure(bridges: str = "off", bridge_lines: str = "") -> str:
    """Start Tor if it isn't running (or restart it for other bridge settings). Returns the SOCKS address - ready or
    still connecting; netlayer checks ready() before sending anything."""
    bridges = bridges if bridges in BRIDGE_MODES else "off"
    with _lock:
        if running() and _st["config"] != (bridges, bridge_lines):
            _stop_locked()
        if not running():
            if _st["proc"] is not None and _st["restarts"] >= 1 and _st["progress"] < 100:
                pass                                   # it failed after a restart: stay down, the status says why
            else:
                if _st["proc"] is not None:
                    _st["restarts"] += 1
                _start(bridges, bridge_lines)
        return f"socks5h://127.0.0.1:{_st['socks']}" if _st["socks"] else ""


def ready() -> bool:
    return running() and _st["progress"] >= 100


def wait_ready(timeout: float) -> bool:
    """Wait (a quarter second at a time) until Tor has connected, it fails, or timeout passes."""
    end = time.monotonic() + timeout
    while not ready():
        if not running() or time.monotonic() > end:
            return ready()
        time.sleep(.25)
    return True


def retry():
    """Try again after a failure (the status's Retry)."""
    with _lock:
        _st["restarts"] = 0
        _stop_locked()


def status() -> dict:
    files = find()
    with _lock:
        state = "off" if _st["proc"] is None else "ready" if ready() else "connecting" if running() else "failed"
        return {"available": files is not None, "version": files["version"] if files else "", "state": state,
                "progress": _st["progress"], "summary": _st["summary"], "error": _st["error"] if state == "failed" or not running() else "",
                "socks": _st["socks"] if running() else 0, "bridges": (_st["config"] or ("off", ""))[0],
                "seconds": round((_st["ready_at"] or time.time()) - _st["started"], 1) if _st["started"] else 0,
                "log": list(_log)[-8:] if state == "failed" else []}


def _control(*commands: str, timeout: float = 5.0) -> list[str]:
    """Send commands on the control port (cookie auth); returns the reply lines."""
    with open(os.path.join(DATA_DIR, "control_auth_cookie"), "rb") as f:
        cookie = f.read().hex()
    with socket.create_connection(("127.0.0.1", _st["control"]), timeout=timeout) as s:
        out = []
        for cmd in (f"AUTHENTICATE {cookie}",) + commands:
            s.sendall((cmd + "\r\n").encode())
            buf = b""
            while not re.search(rb"(^|\r\n)\d{3} [^\r\n]*\r\n$", buf):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            out.append(buf.decode("utf-8", "replace").strip())
        return out


def new_identity() -> bool:
    """New circuits for new requests (Tor rate-limits this to one every ~10 s)."""
    if not ready():
        return False
    try:
        return _control("SIGNAL NEWNYM")[-1].startswith("250")
    except (OSError, IndexError):
        return False


def _stop_locked():
    p = _st["proc"]
    _st.update(proc=None, socks=0, control=0, progress=0, phase="", summary="", ready_at=0.0, config=None)
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def stop():
    with _lock:
        _stop_locked()
        _st["restarts"] = 0


def set_wanted(fn):
    """fn() -> True while some route uses the built-in Tor (netlayer); the watchdog stops Tor when it turns False."""
    _st["wanted"] = fn


def _watch():
    if _st["watchdog"] and _st["watchdog"].is_alive():
        return

    def loop():
        while True:
            time.sleep(IDLE_CHECK_S)
            with _lock:
                if _st["proc"] is None:
                    _st["watchdog"] = None
                    return
                fn = _st["wanted"]
                try:
                    wanted = fn() if fn else True
                except Exception:
                    wanted = True
                if not wanted:
                    _stop_locked()
                    _st["restarts"] = 0
                    _st["watchdog"] = None
                    return
    _st["watchdog"] = threading.Thread(target=loop, daemon=True, name="tor-watchdog")
    _st["watchdog"].start()


import atexit  # noqa: E402 - with the functions it calls
atexit.register(stop)

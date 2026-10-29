"""
roblox_bridge.py
=================
Host-side long-poll bridge to the Roblox Studio companion plugin
(roblox_plugin/AgenticBridge.server.lua - install it into Studio's Plugins
folder, see README). Transport and sessions only; what each tool means lives
in roblox_adapter.py (the app_registry.AppAdapter for Studio).

WHY THIS SHAPE: Roblox Studio is a custom-rendered app with no Win32
accessibility tree, and a Studio plugin can only make outbound HTTP requests
(it cannot listen). So:

  1. Each plugin copy long-polls GET /api/roblox/poll for its next command.
  2. It executes the command with Roblox's own APIs (mutations wrapped in a
     ChangeHistoryService recording) and POSTs the result to /api/roblox/result.
  3. enqueue() blocks the calling tool's worker thread until that result
     lands, the command's own deadline passes, or the user presses Stop.
  4. Independently, every plugin copy PUSHES a small context snapshot every
     few seconds to /api/roblox/context: place, selection, open script, play
     state and new Output errors (with stack traces from play tests).

Sessions (protocol v3): every plugin copy sends a random instance id, its
role (edit / play-server / play-client) and the place. Studio runs one copy
per DataModel, so a playtest adds two short-lived copies, and several Studio
windows give several edit sessions. Commands go to ONE edit session - the
place in the foreground Studio window when known, else the most recently
active one - and each session has its own queue, so a command can never run
in a different place than the one it was meant for. A v2 plugin (no instance
id) is one "legacy" session, as before.

Deadlines: a command carries the time its caller stops waiting. A command
still queued at that point is dropped, and one the user stopped is dropped
too, so a timed-out or cancelled change can never run later by surprise.
Late results are discarded.

SAFETY: there is deliberately no "run arbitrary Lua" command. The command
vocabulary is fixed and mirrored in the plugin's dispatch table. Mutating
commands are classified in permissions.py and go through the approval card
like every other change.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/roblox", tags=["roblox"])

PLUGIN_VERSION_EXPECTED = 3

# ==============================================================================
# State
# ==============================================================================
_lock = threading.Lock()
_waiting: dict[str, threading.Event] = {}
_results: dict[str, dict] = {}
_cancelled: set = set()          # ids the caller gave up on (timeout / Stop)
_picked: set = set()             # ids a plugin has taken
_sessions: dict[str, dict] = {}  # instance id -> session
_focus_hint = ""                 # title of the foreground Studio window (observer)
_health: dict = {}
CONNECTION_TTL = 12.0            # the plugin polls every few seconds; 12 s = clearly gone
LEGACY = "legacy"

# Latest pushed Studio context + a rolling list of Output errors. Errors are
# numbered so consumers (awareness.py, playtests) can ask "what's new since seq N".
_context: dict = {}
_context_at = 0.0
_errors: list[dict] = []
_error_seq = 0
_ERRORS_MAX = 200

# The fixed command vocabulary. Add new entries here AND in the plugin's
# dispatch table - both sides must agree or a command times out.
COMMAND_TYPES = {
    "ping",
    "get_selection",       # -> list of {name, className, path}
    "list_children",       # {path} -> list of {name, className, childCount}
    "get_properties",      # {path, properties: [str,...]} -> {prop: value}
    "create_instance",     # {className, parentPath, name, properties?}
    "set_property",        # {path, property, value}
    "create_script",       # {className: Script|LocalScript|ModuleScript, parentPath, name, source}
    "delete_instance",     # {path}
    # v2
    "read_script",         # {path, startLine?, endLine?} -> numbered text (v2 plugins)
    "set_script_source",   # {path, source, expected?}  v3: refused if the source is not `expected`
    "find_instances",      # {root?, name?, className?, limit?} -> list of {name, className, path}
    "get_output",          # {limit?, errorsOnly?} -> list of {kind, text, at}
    "get_context",         # -> same shape as the pushed context
    # v3
    "get_script_source",   # {path?} -> {path, source (raw), lineCount}
    "get_tree",            # {root?, depth, limit} -> list of {path, className, depth, childCount}
    "run_playtest",        # {mode: play|run, seconds} -> {ended, result, mode}
}
# Commands a v2 plugin does not know.
V3_COMMANDS = {"get_script_source", "get_tree", "run_playtest"}


def _new_session(instance: str, role: str = "edit") -> dict:
    return {"instance": instance, "role": role, "version": None, "place": None, "place_id": None,
            "game_id": None, "first_seen": time.time(), "last_seen": 0.0, "last_context": 0.0,
            "queue": queue.Queue(), "running": False}


def _session(instance: str | None, role: str | None = None) -> dict:
    """Get or create; caller holds _lock. A v2 plugin sends no instance id:
    each of its roles is one legacy session."""
    key = instance or f"{LEGACY}:{role or 'edit'}"
    s = _sessions.get(key)
    if s is None:
        s = _sessions[key] = _new_session(key, role or "edit")
    elif role:
        s["role"] = role
    return s


def _alive(s: dict, now: float | None = None) -> bool:
    return ((now or time.time()) - s["last_seen"]) < CONNECTION_TTL


def _edit_sessions(now: float | None = None) -> list:
    now = now or time.time()
    return [s for s in _sessions.values() if s["role"] == "edit" and _alive(s, now)]


def _primary() -> dict | None:
    """The edit session commands go to; caller holds _lock."""
    live = _edit_sessions()
    if not live:
        return None
    if len(live) > 1 and _focus_hint:
        hint = _focus_hint.casefold()
        named = [s for s in live if s.get("place") and str(s["place"]).casefold() in hint]
        if len(named) == 1:
            return named[0]
    return max(live, key=lambda s: (s["last_context"], s["first_seen"]))


def is_connected() -> bool:
    with _lock:
        return bool(_edit_sessions())


def mark_seen(instance: str | None = None, role: str | None = None):
    with _lock:
        _session(instance, role)["last_seen"] = time.time()


def set_focus_hint(window_title: str):
    """The foreground window's title when it is Studio (from the observer):
    with several Studio windows open, commands go to the place in front."""
    global _focus_hint
    _focus_hint = str(window_title or "")[:300]


def plugin_version() -> int | None:
    with _lock:
        s = _primary()
        return s["version"] if s else None


def primary_place() -> dict:
    with _lock:
        s = _primary()
        return {"place": s["place"], "place_id": s["place_id"], "instance": s["instance"]} if s else {}


def enqueue(command_type: str, payload: dict, timeout: float = 20.0, cancelled=None) -> dict:
    """Blocks the calling thread until the Studio plugin answers this exact
    command, its deadline passes, or `cancelled()` becomes true (Stop).
    Returns {"ok", "message", "data"} (+ "cancelled"/"timeout"/"place")."""
    if command_type not in COMMAND_TYPES:
        return {"ok": False, "message": f"Unknown Roblox command '{command_type}'."}
    with _lock:
        session = _primary()
        if session is not None and command_type in V3_COMMANDS and (session["version"] or 0) < 3:
            return {"ok": False, "message": (
                "The Agentic Bridge plugin in Studio is out of date and can't do this. Copy "
                "roblox_plugin/AgenticBridge.server.lua into Studio's Plugins folder again and restart Studio.")}
        if session is None:
            return {"ok": False, "message": (
                "No Roblox Studio bridge is connected. Open Studio, make sure the 'Agentic Bridge' "
                "plugin is installed (see README) and its toolbar button shows Connected, and allow "
                "its HTTP permission prompt for 127.0.0.1.")}
        cmd_id = uuid.uuid4().hex[:12]
        event = threading.Event()
        _waiting[cmd_id] = event
        place = session["place"]
        session["queue"].put({"id": cmd_id, "type": command_type, "payload": payload or {},
                              "at": time.time(), "deadline": time.time() + timeout})

    deadline = time.time() + timeout
    while not event.wait(0.25):
        stopped = bool(cancelled and cancelled())
        if stopped or time.time() >= deadline:
            with _lock:
                _waiting.pop(cmd_id, None)
                _cancelled.add(cmd_id)
                started = cmd_id in _picked
            if stopped:
                return {"ok": False, "cancelled": True, "message": (
                    f"Stopped while Studio was running '{command_type}' - it may have finished anyway."
                    if started else f"Stopped before Studio ran '{command_type}'; nothing was sent.")}
            return {"ok": False, "timeout": True, "message": (
                f"Studio did not answer '{command_type}' within {timeout:.0f}s. "
                + ("It started the command, so check Studio before retrying." if started else
                   "It never picked the command up, so nothing ran. Studio may be busy or the bridge was turned off."))}

    with _lock:
        result = _results.pop(cmd_id, None)
        _waiting.pop(cmd_id, None)
        _picked.discard(cmd_id)
    result = result or {"ok": False, "message": "No result was recorded for this command."}
    if place:
        result.setdefault("place", place)
    return result


def health_check(timeout: float = 5.0) -> dict:
    """An active round trip (ping) to the primary edit session."""
    started = time.time()
    res = enqueue("ping", {}, timeout=timeout)
    ok = bool(res.get("ok"))
    info = {"ok": ok, "at": time.time(), "latency_ms": int((time.time() - started) * 1000) if ok else None,
            "message": "Studio answered." if ok else res.get("message", "")}
    _health.clear()
    _health.update(info)
    return dict(info)


# ==============================================================================
# Context (pushed by the plugin) - read by awareness.py and /api/state
# ==============================================================================
def _fold_errors(items: list, tag: str = "") -> list:
    """Caller holds _lock. Returns the new error records."""
    global _error_seq
    added = []
    for item in (items or [])[:50]:
        if isinstance(item, dict):
            text, trace, script = str(item.get("text") or ""), str(item.get("trace") or ""), item.get("script")
        else:
            text, trace, script = str(item), "", None
        if not text:
            continue
        _error_seq += 1
        rec = {"seq": _error_seq, "text": (f"[{tag}] " if tag else "") + text[:600], "at": time.time()}
        if trace:
            rec["trace"] = trace[:1200]
        if script:
            rec["script"] = str(script)[:200]
        _errors.append(rec)
        added.append(rec)
    del _errors[:-_ERRORS_MAX]
    return added


def record_context(snapshot: dict) -> None:
    """Store a pushed context snapshot and fold its new errors into the
    rolling, sequence-numbered error list. Play-test copies of the plugin
    (role play-server / play-client) only add their errors and mark the
    place as running; the edit copy stays the source of place/selection."""
    global _context, _context_at
    snapshot = dict(snapshot or {})
    new_errors = snapshot.pop("errors", None) or []
    role = snapshot.pop("role", None) or "edit"
    instance = snapshot.pop("instance", None)
    with _lock:
        s = _session(instance, role)
        s["last_seen"] = time.time()
        s["last_context"] = time.time()
        s["place"] = snapshot.get("place") or s["place"]
        s["place_id"] = snapshot.get("placeId", s["place_id"])
        s["game_id"] = snapshot.get("gameId", s["game_id"])
        if snapshot.get("version") is not None:
            s["version"] = snapshot.get("version")
        if role != "edit":
            _fold_errors(new_errors, role)
            s["running"] = True
            return
        s["running"] = bool(snapshot.get("running"))
        _fold_errors(new_errors)
        primary = _primary()
        if primary is None or primary is s:
            _context = {k: v for k, v in snapshot.items() if k != "version"}
            _context_at = time.time()


def _running() -> bool:
    """Caller holds _lock: a live play-test copy, or the edit copy says so."""
    now = time.time()
    return any(s["role"] != "edit" and _alive(s, now) for s in _sessions.values()) or bool(_context.get("running"))


def latest_context() -> dict:
    """Most recent Studio context, or {} when the plugin is not connected."""
    with _lock:
        if not _edit_sessions():
            return {}
        ctx = dict(_context)
        ctx["age"] = round(time.time() - _context_at, 1) if _context_at else None
        ctx["error_seq"] = _error_seq
        ctx["running"] = _running()
    return ctx


def errors_since(seq: int, limit: int = 20) -> list[dict]:
    with _lock:
        return [dict(e) for e in _errors if e["seq"] > seq][-limit:]


def recent_errors(limit: int = 20) -> list[dict]:
    with _lock:
        return [dict(e) for e in _errors[-limit:]]


def error_seq() -> int:
    return _error_seq


def status_dict() -> dict:
    ctx = latest_context()
    now = time.time()
    with _lock:
        primary = _primary()
        live = [s for s in _sessions.values() if _alive(s, now)]
        last_seen = max((s["last_seen"] for s in _sessions.values()), default=0.0)
        version = primary["version"] if primary else None
        sessions = [{"place": s["place"], "role": s["role"], "version": s["version"],
                     "primary": s is primary} for s in sorted(live, key=lambda s: s["first_seen"])]
    return {
        "connected": primary is not None,
        "last_seen_ago": round(now - last_seen, 1) if last_seen else None,
        "plugin_version": version,
        "outdated_plugin": bool(version is not None and version < PLUGIN_VERSION_EXPECTED),
        "place": ctx.get("place"),
        "active_script": ctx.get("activeScript"),
        "running": ctx.get("running"),
        "selection": (ctx.get("selection") or [])[:5],
        "error_count": len(_errors),
        # The Studio Overlay: the Output's warning count this session and the newest errors.
        "warning_count": (ctx.get("counts") or {}).get("warnings") if isinstance(ctx.get("counts"), dict) else None,
        "recent_errors": [{"text": e["text"][:200], "at": e["at"], "script": e.get("script")} for e in recent_errors(3)],
        "sessions": sessions,
        "health": dict(_health) if _health else None,
    }


def _reset_for_tests():
    global _context, _context_at, _errors, _error_seq, _focus_hint
    with _lock:
        _sessions.clear()
        _waiting.clear()
        _results.clear()
        _cancelled.clear()
        _picked.clear()
        _context, _context_at, _errors, _error_seq, _focus_hint = {}, 0.0, [], 0, ""
        _health.clear()


# ==============================================================================
# HTTP surface the Studio plugin talks to
# ==============================================================================
class ResultIn(BaseModel):
    id: str
    ok: bool
    message: str = ""
    data: object = None
    instance: str = None


class ContextIn(BaseModel):
    version: int = 1
    role: str = "edit"
    instance: str = None
    place: str = None
    placeId: int = None
    gameId: int = None
    selection: list = None
    activeScript: str = None
    running: bool = None
    errors: list = None
    counts: dict = None


@router.get("/poll")
def poll(wait: float = 20.0, version: int = None, instance: str = None):
    """Long-polled by each edit-mode plugin copy. Returns its next command as
    soon as one is queued, or {} after `wait` seconds so it can poll again."""
    with _lock:
        s = _session(instance)
        s["last_seen"] = time.time()
        if version is not None:
            s["version"] = version
        q = s["queue"]
    deadline = time.time() + max(0.1, min(wait, 25.0))
    while time.time() < deadline:
        try:
            cmd = q.get(timeout=max(0.05, min(0.5, deadline - time.time())))
        except queue.Empty:
            mark_seen(instance)
            continue
        with _lock:
            if cmd["id"] in _cancelled or time.time() > cmd.get("deadline", 0):
                _cancelled.discard(cmd["id"])
                continue     # its caller stopped waiting; running it now would surprise the user
            _picked.add(cmd["id"])
        return {k: cmd[k] for k in ("id", "type", "payload", "at")}
    return {}


@router.post("/result")
def post_result(body: ResultIn):
    with _lock:
        _session(body.instance)["last_seen"] = time.time()
        event = _waiting.get(body.id)
        if event is None:                   # the caller gave up: drop it, don't keep it forever
            _cancelled.discard(body.id)
            _picked.discard(body.id)
            return {"status": "late"}
        _results[body.id] = {"ok": body.ok, "message": body.message, "data": body.data}
    event.set()
    return {"status": "ok"}


@router.post("/context")
def post_context(body: ContextIn):
    data = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    record_context(data)
    return {"status": "ok"}


@router.get("/status")
def status():
    return status_dict()

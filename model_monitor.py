"""
model_monitor.py
================
Every local model call, and where it came from (1.4.2): on a local PC the model's time is the budget, so anything
that uses it outside a reply you asked for is listed in Settings > This PC - nothing should run unasked.

  where()          the source of the call on this thread: a reply, a tool inside one, or something else
  record(...)      one finished call (agent/models._local_complete; the warm-up load in api/server)
  recent()         the newest calls outside replies, for the Settings list
  blocked(source)  the "Only for my replies" switch: automatic sources are refused while it is on

Sources the user starts themselves (Compare, a benchmark, Labs) are listed but never blocked.
"""
from __future__ import annotations

import collections
import threading
import time

# Thread name prefix -> what the user sees. Replies run on "turn-<id>", their tools on "tool-<name>".
_SOURCES = (("turn-", "Your reply"), ("tool-", "A tool in your reply"), ("model-warm", "Loading ahead while you type"),
            ("compare", "Compare"), ("benchmark", "Benchmark"), ("labs", "Labs"), ("awareness", "Proactive help"),
            ("knowledge", "Knowledge index"))
AUTOMATIC = {"Loading ahead while you type", "Proactive help"}     # started by the app, not by a click or a message
_log: collections.deque = collections.deque(maxlen=200)
_lock = threading.Lock()


def where(thread_name: str | None = None) -> tuple[str, bool]:
    """(source, part of a reply?) for the calling thread."""
    name = thread_name if thread_name is not None else threading.current_thread().name
    for prefix, label in _SOURCES:
        if name.startswith(prefix):
            return label, prefix in ("turn-", "tool-")
    return f"Other ({name})", False


def record(model: str | None, tokens_in: int = 0, tokens_out: int = 0, seconds: float = 0.0, label: str = "",
           source: str | None = None, ok: bool = True) -> None:
    src, in_reply = where() if source is None else (source, False)
    with _lock:
        _log.append({"at": time.time(), "source": src, "in_reply": in_reply, "label": label[:80], "model": model or "",
                     "tokens_in": int(tokens_in or 0), "tokens_out": int(tokens_out or 0),
                     "seconds": round(float(seconds or 0), 1), "ok": bool(ok)})


def recent(limit: int = 50, include_replies: bool = False) -> list:
    with _lock:
        rows = [dict(r) for r in _log if include_replies or not r["in_reply"]]
    return rows[-limit:][::-1]


def totals() -> dict:
    """Calls and tokens outside replies since the app started, by source."""
    out: dict = {}
    with _lock:
        for r in _log:
            if r["in_reply"]:
                continue
            e = out.setdefault(r["source"], {"calls": 0, "tokens": 0})
            e["calls"] += 1
            e["tokens"] += r["tokens_in"] + r["tokens_out"]
    return out


def blocked(source: str, settings: dict | None) -> bool:
    return bool((settings or {}).get("model_replies_only")) and source in AUTOMATIC

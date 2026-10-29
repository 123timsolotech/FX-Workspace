"""
model_monitor.py
================
Every local model call, and where it came from (1.4.2): on a local PC the model's time is the budget, so anything
that uses it outside a reply you asked for is listed in Settings > This PC - nothing should run unasked.

  where()          the source of the call on this thread: a reply, a tool inside one, or something else
  record(...)      one finished call (agent/models._local_complete; the warm-up load in api/server)
  recent()         the newest calls outside replies
  grouped()        the same, repeats in a row folded into one line - the Settings list
  blocked(source)  the "Only for my replies" switch: automatic sources are refused while it is on
  skipped()        how many calls the switch has refused, by source

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


def grouped(limit: int = 12) -> list:
    """recent(), with runs of the same call in a row folded into one line (1.4.3): twelve chat titles in a row are
    "Chat titles ×12" with their tokens and time added up, at the newest one's time."""
    out: list = []
    for r in recent(200):
        last = out[-1] if out else None
        if last and (last["source"], last["label"], last["model"], last["ok"]) == (r["source"], r["label"], r["model"], r["ok"]):
            last["count"] += 1
            last["tokens_in"] += r["tokens_in"]
            last["tokens_out"] += r["tokens_out"]
            last["seconds"] = round(last["seconds"] + r["seconds"], 1)
            continue
        if len(out) == limit:
            break
        out.append({**r, "count": 1})
    return out


_skipped: dict = {}                   # source -> calls the switch stopped, since the app started


def blocked(source: str, settings: dict | None) -> bool:
    stop = bool((settings or {}).get("model_replies_only")) and source in AUTOMATIC
    if stop:
        with _lock:
            _skipped[source] = _skipped.get(source, 0) + 1
    return stop


def skipped() -> dict:
    """What the "Only for my replies" switch has stopped so far - the switch's effect, shown under it."""
    with _lock:
        return dict(_skipped)

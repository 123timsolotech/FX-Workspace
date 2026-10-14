"""
recorder.py
===========
Records real local-model runs in the replay fixture format, so a failure seen
in daily use becomes a replay test (fixtures/replay/) instead of a guess.

Off by default (setting `record_model_io`). When on, each orchestrated run is
written to data/recordings/<timestamp>-<task>.json with provenance "recorded":
the request, the settings that shaped it, every raw model reply exactly as the
server sent it (content, reasoning, tool-call argument strings, finish reason,
usage) and the approval answers. Recordings contain the conversation, so they
stay on this computer (data/ is git-ignored); nothing is uploaded.

To turn one into a regression test: copy it to fixtures/replay/, add an
"expect" block, and replace machine-specific paths with {sandbox}.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import privacy

REC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "recordings")
_lock = threading.Lock()
_current: dict | None = None


def start(request: str, settings: dict, model: str | None, mode: str, task_id: str):
    global _current
    if privacy.active():                      # a temporary chat is never recorded
        return
    with _lock:
        _current = {
            "format": "agentic-replay/1",
            "meta": {"name": f"Recorded run {time.strftime('%Y-%m-%d %H:%M')}", "provenance": "recorded",
                     "model": model or "", "evidence": f"Recorded live from llama-server on {time.strftime('%Y-%m-%d %H:%M:%S')}.",
                     "task_id": task_id},
            "setup": {"mode": mode, "entry": "agent",
                      "settings": {k: settings.get(k) for k in (
                          "effort", "prompt_enhancement", "effort_overrides", "permission_mode",
                          "permission_categories", "permission_overrides") if k in settings}},
            "request": request, "responses": [], "approvals": [], "expect": {},
            "tools": {"mode": "stub", "results": {}},
        }


def tool_result(name: str, result: dict):
    """What a tool returned, so the replay can serve the same result."""
    with _lock:
        if _current is not None:
            keep = {k: result.get(k) for k in ("ok", "message", "status", "verified") if k in result}
            _current["tools"]["results"].setdefault(name, []).append(keep)


def cancelled():
    """The user pressed Stop while this reply was streaming: replay presses
    Stop at the same point (setup.stop_at_response)."""
    with _lock:
        if _current is not None:
            _current["setup"]["stop_at_response"] = len(_current["responses"])
            _current["responses"].append({"finish_reason": "stop", "content": "",
                                          "note": "Stopped by the user while this reply was being written."})


def response(message, finish_reason, usage=None):
    """One raw model reply, exactly as received (argument strings unparsed)."""
    with _lock:
        if _current is None:
            return
        calls = [{"id": getattr(tc, "id", None), "name": getattr(tc.function, "name", ""),
                  "arguments": getattr(tc.function, "arguments", "")} for tc in (getattr(message, "tool_calls", None) or [])]
        entry = {"finish_reason": finish_reason, "content": getattr(message, "content", "") or "",
                 "tool_calls": calls}
        if getattr(message, "reasoning_content", None):
            entry["reasoning_content"] = message.reasoning_content
        if usage is not None:
            entry["usage"] = {"prompt_tokens": getattr(usage, "prompt_tokens", None),
                              "completion_tokens": getattr(usage, "completion_tokens", None)}
        _current["responses"].append(entry)


def approval(answer: bool):
    with _lock:
        if _current is not None:
            _current["approvals"].append(bool(answer))


def finish(result: dict | None = None) -> str | None:
    """Write the recording; returns its path (None when nothing was recorded)."""
    global _current
    with _lock:
        rec, _current = _current, None
    if not rec or not rec["responses"] or privacy.active():
        return None
    rec["meta"]["result_status"] = (result or {}).get("status")
    os.makedirs(REC_DIR, exist_ok=True)
    slug = re.sub(r"[^\w-]+", "-", rec["request"][:40]).strip("-").lower() or "run"
    path = os.path.join(REC_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1, ensure_ascii=False)
    return path


def active() -> bool:
    return _current is not None

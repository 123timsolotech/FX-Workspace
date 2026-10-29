"""
idle_unload.py
==============
While the desktop app's window is hidden in the tray and nothing has used the model for a while (Settings >
Appearance, idle_unload: off / 5 / 15 / 30 / 60 minutes), the local model leaves the GPU - a game you started
after closing the window isn't fighting the AI for VRAM. The next message loads it again.

  window(visible)   the desktop app says when its main window is shown or hidden (POST /api/shell/window)
  check()           one decision (the watcher calls it every 30 s); returns why it did or didn't unload
  start()           the watcher thread
"""
from __future__ import annotations

import threading
import time

import core.state as shared

_state = {"hidden_at": None, "unloaded_at": None}
_lock = threading.Lock()
_started = False


def window(visible: bool) -> None:
    with _lock:
        _state["hidden_at"] = None if visible else (_state["hidden_at"] or time.time())


def _last_model_use() -> float:
    import model_monitor
    rows = model_monitor.recent(limit=1, include_replies=True)
    return rows[0]["at"] if rows else 0.0


def check(now: float | None = None) -> str:
    now = time.time() if now is None else now
    setting = str((shared.app_state.get("settings") or {}).get("idle_unload", "15"))
    with _lock:
        hidden_at, unloaded_at = _state["hidden_at"], _state["unloaded_at"]
    if setting == "off":
        return "off"
    if hidden_at is None:
        return "window shown"
    import local_runtime
    if local_runtime.busy():
        return "model busy"
    idle_since = max(hidden_at, _last_model_use())
    if unloaded_at and unloaded_at >= idle_since:
        return "already unloaded"
    if now - idle_since < int(setting) * 60:
        return "not idle long enough"
    import llama_local
    gone = llama_local.unload_all()
    with _lock:
        _state["unloaded_at"] = now
    if gone:
        import events as event_log
        event_log.log_action(f"Unloaded {', '.join(gone)}: the window was hidden and the model unused for "
                             f"{setting} minutes - the GPU is free. The next message loads it again.")
        return "unloaded"
    return "nothing loaded"


def start() -> None:
    global _started
    if _started:
        return
    _started = True

    def watch():
        while True:
            time.sleep(30)
            try:
                check()
            except Exception:
                pass
    threading.Thread(target=watch, daemon=True, name="idle-unload").start()

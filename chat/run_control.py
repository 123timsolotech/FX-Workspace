"""
run_control.py
==============
Lets the user pause, resume or stop the task that is running.

The agent calls checkpoint() before each model call and each tool action:
  * paused  -> checkpoint() blocks until resumed (or stopped)
  * stopped -> checkpoint() raises RunStopped
  * past the run's deadline -> checkpoint() raises RunStopped (reason "time")

RunStopped derives from BaseException, not Exception, so the many
`except Exception` blocks around tool execution cannot swallow it - it
unwinds straight to the request handler, like KeyboardInterrupt.

A model call already in flight is not cut off; the stop takes effect as soon
as it returns, and its tool calls are not run.

begin()/end() nest: the orchestrator opens the run around planning,
execution, review and revision, and chat_with_agent opens it again inside.
Only the outermost end() clears a stop, so Stop pressed during review is not
forgotten when the inner execution stage finishes.
"""
from __future__ import annotations

import threading
import time


class RunStopped(BaseException):
    """The user pressed Stop (or the run hit its time limit)."""

    def __init__(self, reason: str = "user"):
        super().__init__(reason)
        self.reason = reason


class RunControl:
    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()
        self._depth = 0
        self._deadline = None
        self._reason = "user"
        self._clock = clock

    # ------------------------------------------------------ run lifecycle
    def begin(self, deadline_s: float | None = None):
        with self._lock:
            self._depth += 1
            if self._depth == 1:
                self._stop.clear()
                self._resume.set()
                self._reason = "user"
                self._deadline = None
            if deadline_s:
                limit = self._clock() + deadline_s
                self._deadline = limit if self._deadline is None else min(self._deadline, limit)

    def set_deadline(self, seconds_from_now: float):
        """Move the running task's time limit (an effort change mid-run)."""
        with self._lock:
            if self._depth > 0:
                self._deadline = self._clock() + max(1.0, float(seconds_from_now))

    def end(self):
        with self._lock:
            self._depth = max(0, self._depth - 1)
            if self._depth == 0:
                self._stop.clear()
                self._resume.set()
                self._deadline = None
                self._reason = "user"

    @property
    def _active(self) -> bool:
        return self._depth > 0

    # ---------------------------------------------------------- user side
    def stop(self, reason: str = "user") -> bool:
        with self._lock:
            if not self._active:
                return False
            if not self._stop.is_set():
                self._reason = reason
            self._stop.set()
            self._resume.set()      # a paused run must wake up to stop
            return True

    def pause(self) -> bool:
        with self._lock:
            if not self._active or self._stop.is_set():
                return False
            self._resume.clear()
            return True

    def resume(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            self._resume.set()
            return True

    # --------------------------------------------------------- agent side
    def _check_deadline(self):
        if self._deadline is not None and self._clock() >= self._deadline and not self._stop.is_set():
            self.stop("time")

    def is_stopped(self) -> bool:
        if not self._active:
            return False
        self._check_deadline()
        return self._stop.is_set()

    def stop_reason(self) -> str:
        return self._reason

    def checkpoint(self, on_pause=None):
        """Block while paused; raise RunStopped once stopped. No-op outside a run."""
        if not self._active:
            return
        self._check_deadline()
        if not self._resume.is_set():
            if on_pause:
                on_pause()
            while not self._resume.wait(0.25):
                self._check_deadline()
        if self._stop.is_set():
            raise RunStopped(self._reason)

    def state(self) -> str:
        with self._lock:
            if not self._active:
                return "idle"
            if self._stop.is_set():
                return "stopping"
            if not self._resume.is_set():
                return "paused"
            return "running"


run_control = RunControl()

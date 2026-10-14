"""
approval_broker.py
==================
Routes an approval request to wherever the user actually is.

  * Workspace tab focused  -> an inline "Allow / Deny" card in the chat, the
    way a permission prompt appears in a chat assistant. No window pops up
    over what the user is looking at.
  * Workspace not focused  -> the native desktop popup (approval.py), placed
    beside the mouse, exactly as before.
  * Card shown but the user leaves the tab for 15 s, or the tab closes ->
    the card is withdrawn and the native popup takes over, so a request can
    never get stuck waiting on a page nobody is looking at.

The browser reports focus on every /api/state poll (heartbeat()).
"""
from __future__ import annotations

import threading
import time
import uuid

HEARTBEAT_STALE = 8.0       # no poll for this long = tab closed / asleep
FOCUS_GRACE = 3.0           # focus must have been reported this recently
UNFOCUSED_ESCALATE = 15.0   # card unanswered while tab unfocused this long -> popup

# Voice mode can approve by saying "approve" - but only requests where a misheard word could do no harm:
# every action opens, shows or reads something (the "benign" tier), or is one of VOICE_EXTRA, with no
# warning on it, at most MAX_VOICE_ACTIONS of them. Anything that types, clicks, runs code, changes or
# deletes files, sends, downloads or is an MCP tool needs a click or a key. Saying "cancel" always works.
VOICE_EXTRA = frozenset({"media_control", "snap_windows"})       # play/pause, arranging windows
MAX_VOICE_ACTIONS = 3


def voice_ok(actions: list) -> bool:
    import permissions
    if not actions or len(actions) > MAX_VOICE_ACTIONS:
        return False
    for a in actions:
        tool = str(a.get("tool") or a.get("tool_name") or "")
        # The tier is worked out again here, not taken from the card's description of the action.
        if a.get("warning") or not tool or not (permissions.classify(tool) == "benign" or tool in VOICE_EXTRA):
            return False
    return True


class ApprovalBroker:
    def __init__(self, native_ask, clock=None, sleep=None, native_available=None):
        self.native_ask = native_ask
        # When the desktop popup can't be shown (no Tk), an open-but-unfocused
        # tab is still a far better place to ask than silently rejecting.
        self.native_available = native_available or (lambda: True)
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._last_poll = 0.0
        self._last_focused = 0.0
        self._focused = False
        self._pending: dict | None = None
        # How the last request was answered: "once", or "request" - "Allow all like this for this
        # request" (the caller then lets the same tools run unasked until the request ends).
        self.last_scope = "once"

    # ------------------------------------------------------------- browser
    def heartbeat(self, focused: bool):
        now = self.clock()
        with self._lock:
            self._last_poll = now
            self._focused = bool(focused)
            if focused:
                self._last_focused = now

    def web_available(self) -> bool:
        now = self.clock()
        with self._lock:
            return (now - self._last_poll) < HEARTBEAT_STALE and self._focused \
                and (now - self._last_focused) < FOCUS_GRACE

    def pending(self) -> dict | None:
        with self._lock:
            if not self._pending or self._pending["answer"] is not None:
                return None
            p = self._pending
            import approval_view
            return {"id": p["id"], "actions": p["actions"], "context": p["context"], "created_at": p["created_at"],
                    "title": approval_view.title(len(p["actions"])), "wording": approval_view.WORDING,
                    "voice_ok": voice_ok(p["actions"]), "request_ok": _request_ok(p["actions"])}

    def answer(self, request_id: str, approved: bool, via: str = "", scope: str = "") -> bool:
        """via "voice": said in voice mode - an approval counts only for a request voice_ok() allows.
        scope "request": allow these tools for the rest of the user's request - only when every action
        may be granted that way (request_ok, decided by the caller when the card was built)."""
        with self._lock:
            p = self._pending
            if not p or p["id"] != request_id or p["answer"] is not None:
                return False
            if approved and via == "voice" and not voice_ok(p["actions"]):
                return False
            if approved and scope == "request" and not _request_ok(p["actions"]):
                return False
            p["scope"] = "request" if approved and scope == "request" else "once"
            p["answer"] = bool(approved)
            p["event"].set()
            return True

    # -------------------------------------------------------------- server
    def _native(self, actions, context_line, cancelled):
        """The desktop popup, closed by Stop when the popup supports it."""
        import inspect
        try:
            takes_cancel = "cancelled" in inspect.signature(self.native_ask).parameters
        except (TypeError, ValueError):
            takes_cancel = False
        if takes_cancel:
            return self.native_ask(actions, context_line, cancelled=cancelled)
        return self.native_ask(actions, context_line)

    def _tab_alive(self) -> bool:
        with self._lock:
            return (self.clock() - self._last_poll) < HEARTBEAT_STALE

    def request(self, actions: list, context_line: str = "", timeout: float = 600, cancelled=None) -> bool:
        """cancelled: optional callable; when it returns True (the user pressed
        Stop) the card is withdrawn and the request counts as declined."""
        cancelled = cancelled or (lambda: False)
        self.last_scope = "once"                    # the desktop popup and a withdrawn card allow once at most
        native_ok = bool(self.native_available())
        if not self.web_available() and (native_ok or not self._tab_alive()):
            return bool(self._native(actions, context_line, cancelled))
        event = threading.Event()
        entry = {"id": uuid.uuid4().hex[:10], "actions": _public_actions(actions), "context": context_line,
                 "created_at": self.clock(), "answer": None, "event": event}
        with self._lock:
            self._pending = entry
        deadline = self.clock() + timeout
        unfocused_since = None
        try:
            while self.clock() < deadline:
                if event.wait(0.25):
                    self.last_scope = entry.get("scope") or "once"
                    return bool(entry["answer"])
                if cancelled():
                    with self._lock:
                        if entry["answer"] is None:
                            entry["answer"] = False
                    return bool(entry["answer"])
                now = self.clock()
                with self._lock:
                    stale = now - self._last_poll >= HEARTBEAT_STALE
                    focused = self._focused
                if focused:
                    unfocused_since = None
                elif unfocused_since is None:
                    unfocused_since = now
                if native_ok and (stale or (unfocused_since is not None and now - unfocused_since >= UNFOCUSED_ESCALATE)):
                    with self._lock:
                        if entry["answer"] is not None:
                            return bool(entry["answer"])
                        entry["answer"] = False      # withdraw the card
                    return bool(self._native(actions, context_line, cancelled))
            return False
        finally:
            with self._lock:
                if self._pending is entry:
                    self._pending = None


def _request_ok(actions: list) -> bool:
    """Every action on the card may be allowed for the rest of the request (its view says so -
    tools/approvals: permissions.request_grantable when the card was built)."""
    return bool(actions) and all(isinstance(a, dict) and a.get("request_ok") is True for a in actions)


def _public_actions(actions: list) -> list:
    """JSON-safe copy for the web card: the full approval_view description
    (nothing clipped - the card scrolls). Actions without a precomputed view
    (direct callers) get one here."""
    import approval_view
    import permissions
    out = []
    for a in actions or []:
        view = a.get("view") or approval_view.describe(
            a.get("tool_name") or "", a.get("args") or {}, a.get("justification") or "",
            tier=permissions.classify(a.get("tool_name") or ""))
        out.append(view)
    return out

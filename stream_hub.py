"""
stream_hub.py
=============
Relays live model output (llm_stream fragments) to the browser.

publish() appends an event to a bounded in-memory buffer; subscribers (the
GET /api/stream server-sent-events endpoint) wait on a condition and receive
everything newer than the last sequence number they saw, with consecutive
text fragments of the same call and channel merged into one message.

Events
  start    {call, step, model}                      a model call began
  delta    {call, channel: content|reasoning|tool, text, name?}
  restart  {call, reason}                           the same call is being re-asked (e.g. cut off)
  end      {call, finish, has_tools, aborted?}      the reply is complete
Nothing here is persisted.
"""
from __future__ import annotations

import threading
import time

import privacy

MAX_EVENTS = 6000


class StreamHub:
    def __init__(self):
        self._cond = threading.Condition()
        self._events: list = []
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, kind: str, **data):
        temp = privacy.current()
        with self._cond:
            self._seq += 1
            self._events.append({"seq": self._seq, "kind": kind, "t": time.time(), **data, **({"temp": temp} if temp else {})})
            if len(self._events) > MAX_EVENTS:
                del self._events[:-MAX_EVENTS]
            self._cond.notify_all()

    def since(self, seq: int, timeout: float = 15.0) -> tuple[list, int]:
        """Events newer than `seq` (waits up to `timeout` for some), merged.
        Returns (events, newest_seq)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._seq <= seq:
                left = deadline - time.monotonic()
                if left <= 0:
                    return [], self._seq
                self._cond.wait(left)
            fresh = [e for e in self._events if e["seq"] > seq]
            newest = self._seq
        return merge(fresh), newest

    def forget(self, chat_id: str) -> int:
        """A temporary chat closed: drop what it streamed. Returns how many events."""
        with self._cond:
            before = len(self._events)
            self._events[:] = [e for e in self._events if e.get("temp") != chat_id]
            return before - len(self._events)


def merge(events: list) -> list:
    """Collapse runs of text deltas for the same call/channel/tool index."""
    out = []
    for e in events:
        prev = out[-1] if out else None
        if (prev and e["kind"] == "delta" and prev["kind"] == "delta" and prev.get("call") == e.get("call")
                and prev.get("channel") == e.get("channel") and prev.get("index") == e.get("index")):
            merged = dict(prev)
            merged["text"] = (prev.get("text") or "") + (e.get("text") or "")
            merged["seq"] = e["seq"]
            if e.get("name"):
                merged["name"] = e["name"]
            out[-1] = merged
        else:
            out.append(dict(e))
    return out


hub = StreamHub()
privacy.on_discard(hub.forget)

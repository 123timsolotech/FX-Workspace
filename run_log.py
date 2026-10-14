"""
run_log.py
==========
Past runs for the Activity panel ("searchable past runs, not just the latest
one"): each finished turn is saved as data/runs/<run id>.json with what the
Activity feed showed for it - the real runtime events, slimmed - plus its
request, effort, model, outcome and usage against its limits.

    {"version": 1, "id", "request", "chat", "project", "started", "ended",
     "status", "effort", "model", "usage": {...} | null, "events": [...]}

Retention: the newest 300 runs, none older than 30 days (oldest removed
first); a run is deleted with its chat, one at a time, or all at once
(Settings > Privacy). Search matches every word of the query against the
request and the events' labels, details and stated reasons. Nothing here is
ever sent to a model.

What is NOT kept (Phase 4 review): the brief's text (it can hold project file
lines and saved memory - only its size is kept) and the text of the saved
memory entries a run used (only how many); every kept string goes through
secret_mask, the one masking rule.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import atomic_io
import privacy
import secret_mask

MAX_RUNS = 300
MAX_AGE_S = 30 * 24 * 3600
MAX_EVENTS = 400
_ID = re.compile(r"^[A-Za-z0-9_-]{6,40}$")
# What a saved event keeps (the same fields the Activity feed renders).
_KEEP = ("id", "seq", "t", "ended", "kind", "label", "status", "ok", "phase", "level", "tool", "detail", "note",
         "reasoning", "reasoning_full", "reasoning_words", "next_tools", "undo_id", "verified", "attempts", "result",
         "steps", "findings", "items", "snippets", "verdict", "handoff", "model", "duration_ms", "tokens_in",
         "tokens_out", "reasoning_tokens", "finish", "plan_step", "why", "thought_id", "routine_step", "effort", "limits",
         "requested", "source", "prep", "prep_label", "prep_source", "text", "project", "folders", "budget", "routine",
         "trigger", "change_id", "conflict", "source_label", "text_chars")
_LIMITS = {"detail": 2000, "text": 20000, "reasoning_full": 20100, "note": 600, "reasoning": 1200}


def _masked(value):
    if isinstance(value, str):
        return secret_mask.mask(value)
    if isinstance(value, list):
        return [_masked(v) for v in value]
    if isinstance(value, dict):
        return {k: _masked(v) for k, v in value.items()}
    return value


def slim(event: dict) -> dict:
    event = dict(event)
    if event.get("phase") == "brief_sent":            # project file lines, memory: only the size is kept
        event["text_chars"] = len(event.get("text") or "")
        event.pop("text", None)
    if event.get("kind") == "memory":                 # which memories: only how many
        event.pop("items", None)
        event["detail"] = "The entries' text isn't kept in run history."
    out = {}
    for key in _KEEP:
        if key in event and event[key] not in (None, "", []):
            value = event[key]
            if isinstance(value, str) and key in _LIMITS:
                value = value[:_LIMITS[key]]
            out[key] = _masked(value)
    return out


class RunLog:
    def __init__(self, root: str):
        self.root = root
        self._lock = threading.Lock()
        self._summary: dict = {}          # id -> (mtime, summary, search text)

    def _path(self, run_id: str) -> str:
        if not _ID.match(run_id or ""):
            raise KeyError(run_id)
        return os.path.join(self.root, f"{run_id}.json")

    def save(self, record: dict) -> dict | None:
        """Save one finished run. Never raises: a failure only loses the history entry.
        A temporary chat's runs are not kept (privacy.py): None."""
        if privacy.active() or privacy.is_temp_id(record.get("chat")):
            return None
        try:
            record = dict(record)
            record["version"] = 1
            record["events"] = [slim(e) for e in (record.get("events") or [])][-MAX_EVENTS:]
            record["request"] = secret_mask.mask(str(record.get("request") or "")[:2000])
            path = self._path(record["id"])
            with self._lock:
                os.makedirs(self.root, exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(record, f, ensure_ascii=False, default=str)
                atomic_io.replace(tmp, path)
                self._prune()
            return record
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def get(self, run_id: str) -> dict | None:
        try:
            with open(self._path(run_id), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) and data.get("id") == run_id else None
        except (OSError, ValueError, KeyError):
            return None

    def _files(self) -> list:
        try:
            return [n for n in os.listdir(self.root) if n.endswith(".json") and _ID.match(n[:-5])]
        except OSError:
            return []

    def _summaries(self) -> list:
        out = []
        for name in self._files():
            run_id = name[:-5]
            path = os.path.join(self.root, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            cached = self._summary.get(run_id)
            if not cached or cached[0] != mtime:
                run = self.get(run_id)
                if run is None:
                    continue
                events = run.get("events") or []
                tools = [e for e in events if e.get("kind") == "tool"]
                summary = {k: run.get(k) for k in ("id", "request", "chat", "project", "started", "ended", "status",
                                                   "effort", "model", "usage")}
                summary.update(tools=len(tools), failed=sum(1 for e in tools if e.get("ok") is False),
                               approvals=sum(1 for e in events if e.get("kind") == "approval" and e.get("ok") is not None))
                text = " ".join([str(run.get("request") or "")] + [
                    f"{e.get('label') or ''} {e.get('detail') or ''} {e.get('tool') or ''} {e.get('why') or ''} "
                    f"{(e.get('plan_step') or {}).get('text') or '' if isinstance(e.get('plan_step'), dict) else ''} "
                    f"{e.get('routine_step') or ''}" for e in events])
                cached = (mtime, summary, text.casefold())
                self._summary[run_id] = cached
            out.append(cached)
        return out

    def list(self, query: str = "", limit: int = 50) -> list:
        words = [w for w in re.findall(r"\w+", (query or "").casefold()) if w]
        rows = []
        for _mtime, summary, text in self._summaries():
            if words and not all(w in text for w in words):
                continue
            row = dict(summary)
            if words:
                i = text.find(words[0])
                row["snippet"] = text[max(0, i - 50): i + 110].strip()
            rows.append(row)
        rows.sort(key=lambda r: r.get("started") or 0, reverse=True)
        return rows[:limit]

    def delete(self, run_id: str) -> bool:
        try:
            path = self._path(run_id)
        except KeyError:
            return False
        with self._lock:
            try:
                os.remove(path)
            except OSError:
                return False
            self._summary.pop(run_id, None)
        return True

    def delete_where(self, chat: str | None = None) -> int:
        """Every run of a chat (the chat was deleted), or every run (chat=None)."""
        gone = 0
        for _mtime, summary, _text in self._summaries():
            if chat is None or summary.get("chat") == chat:
                gone += self.delete(summary["id"])
        return gone

    def _prune(self):
        """Caller holds the lock."""
        now = time.time()
        entries = []
        for name in self._files():
            path = os.path.join(self.root, name)
            try:
                entries.append((os.path.getmtime(path), path, name[:-5]))
            except OSError:
                continue
        entries.sort()
        doomed = [e for e in entries if now - e[0] > MAX_AGE_S]
        keep = [e for e in entries if e not in doomed]
        doomed += keep[:max(0, len(keep) - MAX_RUNS)]
        for _mt, path, run_id in doomed:
            try:
                os.remove(path)
            except OSError:
                pass
            self._summary.pop(run_id, None)

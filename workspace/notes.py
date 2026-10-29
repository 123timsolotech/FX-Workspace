"""
notes.py
========
Notes & tasks (the Notes place, and the assistant's notes_* / task_* tools): notes in Markdown, and tasks with an
optional due time. A task that comes due shows a Windows notification while the app runs (due_check(), called
every minute by the server), and "Do it with the assistant" sends its text to a new chat - so a to-do can be
handed to the agent, or saved as a routine from there.

Stored in data/notes.json:
  {"version": 1,
   "notes": [{id, title, body, pinned, created, updated}],
   "tasks": [{id, text, done, due ("YYYY-MM-DD" or "YYYY-MM-DDTHH:MM", local time, or ""), note_id, created,
              done_at, notified}]}
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime

import atomic_io

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "notes.json")
MAX_NOTES, MAX_TASKS = 2000, 2000
MAX_BODY = 200_000
_lock = threading.Lock()
_DUE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")


def _load() -> dict:
    try:
        with open(DATA, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("notes", []); d.setdefault("tasks", [])
            return d
    except (OSError, ValueError):
        pass
    return {"version": 1, "notes": [], "tasks": []}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    atomic_io.replace(tmp, DATA)


def _title_of(body: str) -> str:
    first = next((ln.strip().lstrip("#").strip() for ln in (body or "").splitlines() if ln.strip()), "")
    return first[:80] or "Untitled note"


def clean_due(due) -> str:
    due = str(due or "").strip()
    if not due:
        return ""
    due = due.replace(" ", "T")[:16]
    if not _DUE_RX.match(due):
        raise ValueError("A due time is a date (2026-10-30) or a date and time (2026-10-30T09:00).")
    datetime.fromisoformat(due)              # a real date
    return due


def due_at(task: dict) -> float | None:
    """The due moment as a timestamp: a date alone is due at 09:00 that day."""
    d = task.get("due") or ""
    if not d:
        return None
    try:
        return datetime.fromisoformat(d if "T" in d else d + "T09:00").timestamp()
    except ValueError:
        return None


# ---- notes -----------------------------------------------------------------------------------------------------
def all_data() -> dict:
    with _lock:
        d = _load()
    d["notes"].sort(key=lambda n: (not n.get("pinned"), -n.get("updated", 0)))
    now = time.time()
    for t in d["tasks"]:
        at = due_at(t)
        t["overdue"] = bool(at and at < now and not t.get("done"))
    d["tasks"].sort(key=lambda t: (bool(t.get("done")), due_at(t) or 9e12, -t.get("created", 0)))
    return d


def save_note(note_id: str | None, body: str, title: str | None = None, pinned: bool | None = None) -> dict:
    body = (body or "")[:MAX_BODY]
    with _lock:
        d = _load()
        n = next((x for x in d["notes"] if x["id"] == note_id), None) if note_id else None
        if n is None:
            if note_id:
                raise KeyError("No such note.")
            if len(d["notes"]) >= MAX_NOTES:
                raise ValueError("That's the most notes this keeps - delete a few first.")
            n = {"id": uuid.uuid4().hex[:10], "created": time.time(), "pinned": False}
            d["notes"].append(n)
        n["body"] = body
        n["title"] = (title or "").strip()[:120] or _title_of(body)
        if pinned is not None:
            n["pinned"] = bool(pinned)
        n["updated"] = time.time()
        _save(d)
        return dict(n)


def delete_note(note_id: str) -> bool:
    with _lock:
        d = _load()
        n = len(d["notes"])
        d["notes"] = [x for x in d["notes"] if x["id"] != note_id]
        for t in d["tasks"]:
            if t.get("note_id") == note_id:
                t["note_id"] = None
        _save(d)
        return len(d["notes"]) != n


def search(query: str, limit: int = 20) -> list[dict]:
    words = [w for w in re.split(r"\s+", (query or "").lower()) if w]
    d = all_data()
    hits = []
    for n in d["notes"]:
        hay = (n.get("title", "") + "\n" + n.get("body", "")).lower()
        if all(w in hay for w in words):
            hits.append({"kind": "note", "id": n["id"], "title": n["title"], "text": n.get("body", "")[:400]})
    for t in d["tasks"]:
        if all(w in t["text"].lower() for w in words):
            hits.append({"kind": "task", "id": t["id"], "title": t["text"][:80], "text": t["text"], "done": t.get("done"),
                         "due": t.get("due")})
    return hits[:limit]


# ---- tasks -----------------------------------------------------------------------------------------------------
def add_task(text: str, due: str = "", note_id: str | None = None) -> dict:
    text = (text or "").strip()[:500]
    if not text:
        raise ValueError("A task needs some text.")
    due = clean_due(due)
    with _lock:
        d = _load()
        if len(d["tasks"]) >= MAX_TASKS:
            raise ValueError("That's the most tasks this keeps - clear done ones first.")
        t = {"id": uuid.uuid4().hex[:10], "text": text, "done": False, "due": due, "note_id": note_id or None,
             "created": time.time(), "done_at": None, "notified": False}
        d["tasks"].append(t)
        _save(d)
        return dict(t)


def update_task(task_id: str, **fields) -> dict:
    with _lock:
        d = _load()
        t = next((x for x in d["tasks"] if x["id"] == task_id), None)
        if t is None:
            raise KeyError("No such task.")
        if "text" in fields and fields["text"] is not None:
            text = str(fields["text"]).strip()[:500]
            if not text:
                raise ValueError("A task needs some text.")
            t["text"] = text
        if "due" in fields and fields["due"] is not None:
            t["due"] = clean_due(fields["due"])
            t["notified"] = False
        if "done" in fields and fields["done"] is not None:
            t["done"] = bool(fields["done"])
            t["done_at"] = time.time() if t["done"] else None
        _save(d)
        return dict(t)


def delete_task(task_id: str) -> bool:
    with _lock:
        d = _load()
        n = len(d["tasks"])
        d["tasks"] = [x for x in d["tasks"] if x["id"] != task_id]
        _save(d)
        return len(d["tasks"]) != n


def clear_done() -> int:
    with _lock:
        d = _load()
        n = len(d["tasks"])
        d["tasks"] = [x for x in d["tasks"] if not x.get("done")]
        _save(d)
        return n - len(d["tasks"])


def due_check(notify=None, now: float | None = None) -> list[dict]:
    """Tasks that just came due: marked notified and handed to notify(title, message) once each."""
    now = now or time.time()
    fired = []
    with _lock:
        d = _load()
        for t in d["tasks"]:
            at = due_at(t)
            if t.get("done") or t.get("notified") or at is None or at > now:
                continue
            t["notified"] = True
            fired.append(dict(t))
        if fired:
            _save(d)
    for t in fired:
        if notify:
            try:
                notify("Task due", t["text"][:200])
            except Exception:
                pass
    return fired


# ---- the assistant's tools -------------------------------------------------------------------------------------
TOOLS = [
    {"type": "function", "function": {"name": "notes_search", "description": "Searches the user's notes and tasks (the Notes place). Empty query lists recent ones.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "note_save", "description": "Saves a note in the user's Notes (Markdown). When they ask you to note, jot or write something down, call this yourself - don't only write it in the chat. Give note_id to replace an existing note's text.", "parameters": {"type": "object", "properties": {"body": {"type": "string"}, "title": {"type": "string"}, "note_id": {"type": "string"}, "justification": {"type": "string"}}, "required": ["body"]}}},
    {"type": "function", "function": {"name": "task_add", "description": "Adds a task to the user's to-do list, optionally due at a date (YYYY-MM-DD) or date and time (YYYY-MM-DDTHH:MM, local) - they get a notification then.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "due": {"type": "string"}, "justification": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "task_done", "description": "Marks one of the user's tasks done (or not done with done=false).", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}, "done": {"type": "boolean"}}, "required": ["task_id"]}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def run_tool(name: str, args: dict) -> dict:
    try:
        if name == "notes_search":
            q = str(args.get("query") or "")
            if q.strip():
                hits = search(q)
            else:
                d = all_data()
                hits = ([{"kind": "task", "id": t["id"], "title": t["text"][:80], "text": t["text"], "done": t["done"], "due": t["due"]}
                         for t in d["tasks"] if not t.get("done")][:15]
                        + [{"kind": "note", "id": n["id"], "title": n["title"], "text": n.get("body", "")[:300]} for n in d["notes"][:10]])
            lines = [f"{h['kind']} {h['id']}: {h['title']}" + (f" (due {h['due']})" if h.get("due") else "")
                     + (" [done]" if h.get("done") else "") + ("" if h["kind"] == "task" else f"\n  {h['text'][:300]}") for h in hits]
            return {"ok": True, "data": hits, "message": "\n".join(lines) or "Nothing matches."}
        if name == "note_save":
            n = save_note(args.get("note_id") or None, str(args.get("body") or ""), args.get("title"))
            return {"ok": True, "verified": True, "data": n, "message": f"Saved note {n['id']}: {n['title']}"}
        if name == "task_add":
            t = add_task(str(args.get("text") or ""), str(args.get("due") or ""))
            return {"ok": True, "verified": True, "data": t, "message": f"Added task {t['id']}: {t['text']}" + (f" (due {t['due']})" if t["due"] else "")}
        if name == "task_done":
            t = update_task(str(args.get("task_id") or ""), done=args.get("done", True) is not False)
            return {"ok": True, "verified": True, "data": t, "message": f"Task {t['id']} {'done' if t['done'] else 'not done'}: {t['text']}"}
    except (KeyError, ValueError) as e:
        return {"ok": False, "status": "permanent_failure", "message": str(e).strip("'\"")}
    return {"ok": False, "status": "permanent_failure", "message": f"Unknown tool {name}."}

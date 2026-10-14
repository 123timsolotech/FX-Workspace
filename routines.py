"""
routines.py
===========
Routines: the action steps of a run that worked, saved under a name
("Morning setup", "Start Roblox session") and re-run in one click or on a
schedule. Stored in data/routines.json:

    {"version": 1, "routines": [
        {"id", "name", "request", "project", "created", "updated", "runs",
         "steps": [{"tool", "args", "label"}],
         "last_run": {"at", "status", "message", "trigger"} | null,
         "history": [{"at", "trigger", "status"}] (last 30 runs),
         "schedule": {"repeat", "time", "date", "task"} | null}]}

A routine is the list of exact tool calls that succeeded, not a prompt: a
re-run needs no model and does the same thing again. The other kind,
kind "prompt", is a request the assistant answers afresh each run - with the
composer's tools it names ("tools", e.g. grounded: search the web and cite), so
"a daily digest of AI news" on a schedule gives a new, cited answer every day
(agent/routine_runner.run_prompt). It carries no authority:
agent/routine_runner.py sends every step through permissions.explain and,
where that says ask, the approval card - exactly like a model's call - and
file changes are journaled for undo as always (non-negotiable 7). Steps that
only informed the model (reads, plans, questions) are not kept.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import atomic_io

MAX_ROUTINES = 100
MAX_STEPS = 30
HISTORY_KEEP = 30
MAX_PROMPT = 2000
PROMPT_TOOLS = ("grounded", "biascheck", "summarize", "critique")      # composer tools a prompt routine may use
MAX_NAME = 60
_ID = re.compile(r"^[a-f0-9]{12}$")
# Never replayed: host bookkeeping, questions, memory (a re-run would only
# duplicate the entry), screen-coordinate clicks (where they land depends on
# what was on screen at the time) and browser clicks/fills (their element
# numbers belong to one reading of one page).
SKIP_TOOLS = {"set_plan", "update_plan", "ask_user", "save_memory", "click_at", "observe", "inspect_screen",
              "browser_click", "browser_fill"}
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def steps_from_history(history: list, read_only: set, describe=None) -> list:
    """The replayable steps of a finished run (the executor's history): the
    calls that succeeded and changed something - model-called tools and
    deterministic desktop routes - in order, without their justification."""
    describe = describe or (lambda tool, args: tool)
    steps = []
    for h in history or []:
        tool = h.get("tool") or ""
        if not h.get("ok") or tool in SKIP_TOOLS or tool in read_only:
            continue
        if not (h.get("mutating") or h.get("internal")):
            continue
        args = {k: v for k, v in (h.get("args") or {}).items() if k not in ("justification", "force")}
        step = {"tool": tool, "args": args, "label": describe(tool, args)}
        if steps and steps[-1]["tool"] == tool and steps[-1]["args"] == args:
            continue                                # a retried identical call is one step
        steps.append(step)
    return steps[:MAX_STEPS]


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:MAX_NAME]
    if not name:
        raise ValueError("Give the routine a name.")
    return name


def clean_schedule(body: dict) -> dict:
    """{repeat, time "HH:MM", date "YYYY-MM-DD" (once/weekly)} -> validated.
    Raises ValueError with a readable reason."""
    repeat = str((body or {}).get("repeat") or "daily").lower()
    if repeat not in ("once", "daily", "weekdays", "weekly"):
        raise ValueError("Pick once, daily, weekdays or weekly.")
    at = str((body or {}).get("time") or "").strip()
    if not TIME.match(at):
        raise ValueError("Time must look like 08:30.")
    date = str((body or {}).get("date") or "").strip()
    if repeat in ("once", "weekly"):
        if not DATE.match(date):
            raise ValueError("Pick a date (YYYY-MM-DD)" + (" - its weekday is the day it repeats." if repeat == "weekly" else "."))
        try:
            when = time.strptime(f"{date} {at}", "%Y-%m-%d %H:%M")
        except ValueError:
            raise ValueError("That date doesn't exist.")
        if repeat == "once" and time.mktime(when) < time.time() + 30:
            raise ValueError("Pick a time at least a minute from now.")
    else:
        date = ""
    return {"repeat": repeat, "time": at, "date": date}


class RoutineStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> list:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        rows = data.get("routines") if isinstance(data, dict) else None
        return [r for r in rows or [] if isinstance(r, dict) and _ID.match(str(r.get("id") or ""))]

    def _save(self, rows: list):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "routines": rows}, f, indent=2, ensure_ascii=False)
        atomic_io.replace(tmp, self.path)

    def list(self) -> list:
        with self._lock:
            rows = self._load()
        return sorted(rows, key=lambda r: r.get("updated") or 0, reverse=True)

    def get(self, rid: str) -> dict | None:
        return next((r for r in self.list() if r["id"] == rid), None)

    def create(self, name: str, steps: list, request: str = "", project: str | None = None,
               tainted: bool = False) -> dict:
        """tainted: the run it was saved from read content that tried to
        instruct the assistant - every run of it keeps that caution."""
        name = clean_name(name)
        if not steps:
            raise ValueError("That run has no actions to repeat.")
        now = time.time()
        routine = {"id": uuid.uuid4().hex[:12], "name": name, "request": str(request or "")[:500],
                   "project": project, "steps": list(steps)[:MAX_STEPS], "created": now, "updated": now,
                   "runs": 0, "last_run": None, "schedule": None, "tainted": bool(tainted)}
        with self._lock:
            rows = self._load()
            if len(rows) >= MAX_ROUTINES:
                raise ValueError(f"You have {MAX_ROUTINES} routines - delete one first.")
            if any(r["name"].casefold() == name.casefold() for r in rows):
                raise ValueError(f"A routine called '{name}' already exists.")
            rows.append(routine)
            self._save(rows)
        return routine

    def create_prompt(self, name: str, prompt: str, tools: list | None = None, project: str | None = None) -> dict:
        """A routine that sends a request (not recorded steps): answered afresh on every run."""
        name = clean_name(name)
        prompt = str(prompt or "").strip()[:MAX_PROMPT]
        if not prompt:
            raise ValueError("Write what the routine should ask, e.g. 'The top AI news of the last day, with sources'.")
        now = time.time()
        routine = {"id": uuid.uuid4().hex[:12], "name": name, "kind": "prompt", "prompt": prompt,
                   "tools": [t for t in (tools or []) if t in PROMPT_TOOLS], "request": prompt[:500], "project": project,
                   "steps": [], "created": now, "updated": now, "runs": 0, "last_run": None, "schedule": None,
                   "tainted": False}
        with self._lock:
            rows = self._load()
            if len(rows) >= MAX_ROUTINES:
                raise ValueError(f"You have {MAX_ROUTINES} routines - delete one first.")
            if any(r["name"].casefold() == name.casefold() for r in rows):
                raise ValueError(f"A routine called '{name}' already exists.")
            rows.append(routine)
            self._save(rows)
        return routine

    def update(self, rid: str, **fields) -> dict:
        """name / keep (indexes of the steps to keep, in order) / schedule /
        last_run + runs. Steps can be removed, never added or edited: a
        routine only ever repeats calls that already ran."""
        with self._lock:
            rows = self._load()
            routine = next((r for r in rows if r["id"] == rid), None)
            if routine is None:
                raise KeyError(rid)
            if fields.get("name") is not None:
                name = clean_name(fields["name"])
                if any(r["id"] != rid and r["name"].casefold() == name.casefold() for r in rows):
                    raise ValueError(f"A routine called '{name}' already exists.")
                routine["name"] = name
            if routine.get("kind") == "prompt":
                if fields.get("prompt") is not None:
                    prompt = str(fields["prompt"]).strip()[:MAX_PROMPT]
                    if not prompt:
                        raise ValueError("The routine needs something to ask.")
                    routine["prompt"] = routine["request"] = prompt
                if fields.get("tools") is not None:
                    routine["tools"] = [t for t in fields["tools"] if t in PROMPT_TOOLS]
            elif fields.get("keep") is not None:
                keep = [i for i in fields["keep"] if isinstance(i, int) and 0 <= i < len(routine["steps"])]
                if not keep:
                    raise ValueError("A routine needs at least one step.")
                routine["steps"] = [routine["steps"][i] for i in sorted(set(keep))]
            if "schedule" in fields:
                routine["schedule"] = fields["schedule"]
            if fields.get("last_run") is not None:
                routine["last_run"] = fields["last_run"]
                routine["runs"] = int(routine.get("runs") or 0) + 1
                # When it ran (the proactive observer offers a routine at its usual time).
                # With what it said (the run history in the routine editor).
                history = list(routine.get("history") or []) + [{k: fields["last_run"].get(k) for k in ("at", "trigger", "status", "message", "chat")}]
                routine["history"] = history[-HISTORY_KEEP:]
            routine["updated"] = time.time()
            self._save(rows)
            return dict(routine)

    def delete(self, rid: str) -> bool:
        with self._lock:
            rows = self._load()
            kept = [r for r in rows if r["id"] != rid]
            if len(kept) == len(rows):
                return False
            self._save(kept)
            return True


def task_name(routine: dict) -> str:
    """The Task Scheduler name (without schedule_task's prefix)."""
    return f"Routine {routine['id']}"


def trigger_command(routine: dict, port: int) -> str:
    """What Task Scheduler runs: a local POST that starts the routine as if
    clicked. The app must be running; the run still asks for every approval."""
    return (f"powershell -NoProfile -WindowStyle Hidden -Command \"Invoke-RestMethod -Method Post "
            f"-Uri 'http://127.0.0.1:{int(port)}/api/routines/{routine['id']}/run?trigger=schedule'\"")

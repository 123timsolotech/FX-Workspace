"""
prompt_prep.py
==============
Prompt Prep: what happens to a request before the assistant acts on it.

  off      Run exactly as typed. Nothing is added.
  clarify  At most one question, and only when the request cannot be done
           correctly without the answer. Otherwise the quick notes
           (constraints, what "done" means) go along with the request.
  expand   A written spec (goal, constraints, done-when, assumptions) is
           shown as an editable card; the run starts when the user sends it.
  plan     A step plan is shown as an editable card. No tool runs until the
           user accepts it - execution has not started, so this is enforced
           by the runtime, not asked of the model.

The mode follows the same rules as effort (effort.choose_level): session >
project > this model > the setting; it is read once when a run starts, so a
change applies from the next run; the run's Activity says which mode applied
and why. The card shows the prepared text exactly as it will be sent, and
Activity shows the whole brief the model received.

Nothing here grants anything: accepting a spec or a plan is not approving the
actions in it. Every action still goes through the permission and approval
path (non-negotiable 7).
"""
from __future__ import annotations

import re
import threading
import time
import uuid

MODES = ("off", "clarify", "expand", "plan")
LABELS = {"off": "Off", "clarify": "Clarify", "expand": "Expand", "plan": "Plan first"}
# Settings before Prompt Prep had four "prompt_enhancement" levels. None of
# them stopped a run to ask anything, so they all map to the non-blocking
# modes; the user opts into Expand or Plan first themselves.
LEGACY = {"off": "off", "minimal": "clarify", "standard": "clarify", "detailed": "clarify"}
DEFAULT = "clarify"


def migrated(settings: dict) -> str:
    """The stored mode, or the one an older settings file implies."""
    mode = (settings or {}).get("prompt_prep")
    if mode in MODES:
        return mode
    return LEGACY.get((settings or {}).get("prompt_enhancement"), DEFAULT)


def choose_mode(settings: dict, model: str | None = None, project: dict | None = None,
                session: str | None = None) -> tuple[str, str]:
    """(mode, source): session > project > this model > the setting."""
    if session in MODES:
        return session, "session"
    if isinstance(project, dict) and project.get("prompt_prep") in MODES:
        return project["prompt_prep"], "project"
    by_model = (settings or {}).get("prompt_prep_by_model") or {}
    if model and by_model.get(model) in MODES:
        return by_model[model], "model"
    return migrated(settings), "setting"


# Repeated work: the same operation over many items ("stack 10 parts", "make 5 rows of trees", "color
# every part red"). Done one tool call per item, it is one step and one approval card per item and the
# run stalls - so, whatever the Prompt Prep mode, the request goes out with the "one script, one loop"
# wording (agent/models.build_messages) and Expand/Plan cards say it too (orchestrator: PromptPreparer,
# Planner). Units and quantities of text aren't repeated operations ("wait 10 minutes", "50 lines").
_NUMBER_WORDS = ("two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|"
                 "hundred|dozen|several|many|lots of|a bunch of")
_NOT_ITEMS = (r"(?:seconds?|minutes?|mins?|hours?|days?|weeks?|months?|years?|ms|px|pixels?|studs?|percent|dollars?|"
              r"euros?|shekels?|mb|gb|kb|fps|degrees?|lines?|words?|characters?|chars?|pages?|sentences?|paragraphs?|"
              r"steps?|times?|ways?|reasons?|examples?|ideas?|tips?|questions?)\b")
_REPEAT_RE = re.compile(
    rf"\b(?:\d{{1,4}}|{_NUMBER_WORDS})\s+(?:more\s+|new\s+|other\s+|of\s+(?:these|those|them)\b)?(?!{_NOT_ITEMS})[a-z]+s\b"
    rf"|\b(?:\d{{1,4}}|{_NUMBER_WORDS})\s+of\s+(?:these|those|them)\b"
    r"|\b(?:stack|pile|tower|rows?\s+of|grid\s+of|columns?\s+of|line\s+of|array\s+of)\b"
    r"|\b(?:each|every)\s+(?!other\b|time\b|day\b|week\b|now\b)[a-z]+"
    r"|\ball\s+(?:the|of\s+the|my|these|those)\s+[a-z]+s\b"
    rf"|\b(?:\d{{1,4}}|{_NUMBER_WORDS})\s+times\b", re.I)


def repeat_hint(text: str) -> str:
    """The "one script, one loop" line for a repeat-style request, or ""."""
    if not text or not _REPEAT_RE.search(str(text)):
        return ""
    return ("REPEATED WORK: this request repeats one operation over many items - if one tool call can do all of it "
            "(a Luau script with a loop, a batch call, one script), make exactly ONE such call, never one call per item.")


def repeat_constraint(text: str) -> str:
    """The same rule as Prompt Prep writes it on the Expand / Plan card, or ""."""
    return ("Do the repeated part in ONE tool call - one script with a loop (or one batch) - not one call per item."
            if repeat_hint(text) else "")


def spec_text(enhanced: dict | None) -> str:
    """The editable spec for Expand: plain lines the user can change."""
    e = enhanced or {}
    lines = [f"Goal: {e.get('intent') or ''}".rstrip()]
    for title, key in (("Constraints", "constraints"), ("Done when", "success_criteria"),
                       ("Assumptions / open questions", "ambiguities")):
        items = [str(i).strip() for i in e.get(key) or [] if str(i).strip()]
        if items:
            lines.append(f"{title}:")
            lines += [f"- {i}" for i in items]
    return "\n".join(lines).strip()


def steps_from_text(text: str, limit: int = 20) -> list:
    """Plan card text -> steps (one per line; list markers and numbers dropped)."""
    import re
    out = []
    for line in str(text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if line:
            out.append(line[:300])
    return out[:limit]


class PrepGate:
    """Holds the run thread until the user sends, edits or cancels the card.
    One card at a time (runs are serialised). Stop ends the wait."""

    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._pending = None
        self._clock = clock

    def request(self, kind: str, payload: dict, cancelled=lambda: False, poll: float = 0.1) -> dict | None:
        """Blocks until answered. Returns {"action": "send"|"raw"|"cancel", "text": str},
        or None when the run was stopped while waiting."""
        card = {"id": uuid.uuid4().hex[:12], "kind": kind, "created_at": self._clock(), **payload,
                "answer": None, "event": threading.Event()}
        with self._lock:
            self._pending = card
        try:
            while not card["event"].wait(poll):
                if cancelled():
                    return None
            return card["answer"]
        finally:
            with self._lock:
                if self._pending is card:
                    self._pending = None

    def pending(self) -> dict | None:
        with self._lock:
            p = self._pending
            if not p or p["answer"] is not None:
                return None
            return {k: v for k, v in p.items() if k not in ("answer", "event")}

    def answer(self, card_id: str, action: str, text: str | None = None) -> bool:
        if action not in ("send", "raw", "cancel"):
            return False
        with self._lock:
            p = self._pending
            if not p or p["id"] != card_id or p["answer"] is not None:
                return False
            p["answer"] = {"action": action, "text": str(text or "")[:8000]}
            p["event"].set()
            return True


gate = PrepGate()

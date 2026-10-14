"""
agent/routine_runner.py
=======================
Runs a saved routine (routines.py) as one turn: its steps in order, each
through the same gates as a model's call - permissions.explain, the approval
card when that says ask, the "changed since approved" check, and the guarded
executor (registry spec, timeouts, undo journal, Activity events). Desktop
routes replay through run_desktop_intent, which gates its own actions the
same way. No model is involved and nothing is skipped: a routine grants no
permission (non-negotiable 7).

The run stops at the first step that fails, is declined or is set to Deny;
steps already done stay done (each file change is its own undoable change),
and the reply lists what ran. Stop and Pause work between steps.

Candidates: when a turn finishes successfully with at least one replayable
action, its steps are kept (in memory, last few turns) so the chat can offer
"Save as routine".
"""
from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from types import SimpleNamespace

import effort
import intent_router
import permissions
import routines
from run_control import RunStopped, run_control

import core.state as shared
import events as event_log
import tools.approvals as approvals
import tools.runtime as runtime
import tools.schemas as schemas

_candidates: "OrderedDict[str, dict]" = OrderedDict()
MAX_CANDIDATES = 8


def offer_candidate(task_id: str, request: str, history: list) -> dict | None:
    """Keep this run's replayable steps; returns what the UI shows, or None."""
    steps = routines.steps_from_history(history, schemas.READ_ONLY_TOOLS, event_log.describe_tool)
    if not task_id or not steps:
        return None
    project = shared.app_state.get("active_project")
    _candidates[task_id] = {"request": request, "steps": steps,
                            "project": project.get("id") if isinstance(project, dict) else None,
                            # the run read content that tried to instruct the assistant
                            "tainted": bool(shared.app_state.get("untrusted_seen"))}
    while len(_candidates) > MAX_CANDIDATES:
        _candidates.popitem(last=False)
    return {"task_id": task_id, "steps": [s["label"] for s in steps]}


def save_candidate(task_id: str, name: str) -> dict:
    """Raises KeyError (no such run) or ValueError (bad name, duplicate)."""
    cand = _candidates.get(task_id)
    if cand is None:
        raise KeyError(task_id)
    routine = shared.routine_store.create(name, cand["steps"], cand["request"], cand["project"],
                                          tainted=cand.get("tainted", False))
    event_log.log_action(f"Routine saved: {routine['name']} ({len(routine['steps'])} steps).")
    return routine


def _record(routine: dict, status: str, message: str, trigger: str):
    try:
        chat = (shared.app_state.get("active_chat") or {}).get("id")      # where its answer was saved
        shared.routine_store.update(routine["id"], last_run={"at": time.time(), "status": status,
                                                            "message": message[:300], "trigger": trigger, "chat": chat})
    except (KeyError, OSError) as e:
        event_log.log_action(f"Could not record the routine run: {e}")


def _run_step(i: int, n: int, name: str, step: dict) -> tuple[bool, str, bool]:
    """(ok, message, declined) for one step, through the one gate path."""
    import agent.tasks as tasks                 # call-time: tasks imports this module
    tool, args = step["tool"], dict(step.get("args") or {})
    context = f"Routine '{name}' · step {i} of {n}: {step.get('label') or tool}"
    if tool == "desktop_route":
        intent = intent_router.DesktopIntent(str(args.get("app") or ""), [str(a) for a in args.get("actions") or []],
                                             f"Routine {name}: {step.get('label') or ''}")
        res = tasks.run_desktop_intent(intent)
        ok = res.get("status") == "success" and not res.get("rejected")
        return ok, str(res.get("message") or ""), bool(res.get("rejected"))
    if runtime.TOOL_REGISTRY.get(tool) is None or tool in schemas.INTERNAL_TOOL_NAMES:
        return False, f"'{tool}' is no longer available, so this step can't run.", False
    decision, why = permissions.explain(tool, shared.app_state["settings"],
                                        tainted=bool(shared.app_state.get("untrusted_seen")), args=args)
    if decision == "deny":
        event_log.log_action(f"BLOCKED {tool}: set to 'deny' in your permission settings.")
        return False, f"'{tool}' is set to Deny in Settings > Permissions.", False
    if decision == "ask":
        action = {"tool_name": tool, "args": args, "justification": f"Step {i} of your routine '{name}'."}
        shared.app_state["waiting_for_os_approval"] = True
        try:
            approved = approvals.ask_batch_approval_os([action], context_line=context)
        finally:
            shared.app_state["waiting_for_os_approval"] = False
        if not approved:
            return False, "You declined this step.", True
        changed = approvals._changed_since_approval(action.get("view"))
        if changed:
            return False, changed, False
    else:
        event_log.log_action(f"Auto-running {tool} ({why}).")
    res = runtime.execute_tool_guarded(tool, args, links={"routine_step": context})
    return bool(res.get("ok")), str(res.get("message") or ""), False


def prompt_request(routine: dict, trigger: str = "click"):
    """The request a prompt routine sends: its prompt, with its composer tools."""
    return SimpleNamespace(message=routine["prompt"], with_vision=False, command=None, tools=list(routine.get("tools") or []),
                           files=[], instructions="", compare_model=None, routine_id=routine["id"], trigger=trigger,
                           mode=shared.app_state.get("mode"), local_model=shared.app_state.get("local_model"))


def run_prompt(routine: dict, req, runner) -> dict:
    """A prompt routine's run: the request goes through the normal pipeline
    (runner - the orchestrator), and what it answered is kept in its history."""
    result = runner(req) or {}
    status = result.get("status")
    answer = str(result.get("message") or "")
    first = next((l.strip("# ") for l in answer.splitlines() if l.strip()), "")
    _record(routine, "done" if status == "success" else "stopped" if status == "stopped" else "failed",
            first[:300] or status or "", getattr(req, "trigger", "click"))
    if status == "success":
        event_log.emit_activity("routine_finished", f"Routine finished · {routine['name']}")
    return {**result, "routine": routine["id"], "routine_name": routine["name"]}


def run(routine_id: str, trigger: str = "click") -> dict:
    """One routine run (a turn on the TurnManager thread)."""
    routine = shared.routine_store.get(routine_id)
    if routine is None:
        return {"status": "error", "message": "That routine no longer exists."}
    if not shared.state_lock.acquire(blocking=False):
        _record(routine, "skipped", "Another task was running.", trigger)
        return {"status": "error", "message": "Busy - another task is running."}
    name, steps = routine["name"], list(routine.get("steps") or [])
    task_id = uuid.uuid4().hex[:12]
    profile = effort.PROFILES["normal"]
    run_control.begin(profile.time_limit_s)
    runtime.tool_exec.begin_task(task_id, len(steps) + 4)
    done: list = []
    try:
        who = "its schedule" if trigger == "schedule" else "you"
        # Saved from a run that read injected instructions: its steps keep asking,
        # exactly as they did in that run (only local reads run by themselves).
        shared.app_state["untrusted_seen"] = ([{"source": "the run this routine was saved from",
                                                "snippet": "content it read tried to instruct the assistant"}]
                                              if routine.get("tainted") else [])
        shared.app_state["current_user_request"] = f"Run routine: {name}"
        shared.app_state["turn_policy"] = {"mode": "action", "allow_mutation": True,
                                           "reason": f"routine '{name}' started by {who}"}
        shared.app_state["intent_guard"] = None
        shared.app_state["turn_engine"] = "routine"
        shared.task_board.clear()
        # A re-run is a new, explicit request: the duplicate guard (meant to
        # stop a model redoing work) must not refuse the routine's steps.
        shared.ledger.clear()
        event_log.emit_activity("run_started", f"Routine · {name}", task_id=task_id, routine=routine["id"],
                                trigger=trigger, steps=[s.get("label") for s in steps], effort="routine",
                                limits={"steps": len(steps), "time_s": profile.time_limit_s})
        failed = None
        for i, step in enumerate(steps, 1):
            run_control.checkpoint(on_pause=event_log._announce_pause)
            ok, message, declined = _run_step(i, len(steps), name, step)
            done.append((step, ok, message))
            if not ok:
                failed = (i, step, message, declined)
                break
        lines = [f"- {'✓' if ok else '✗'} {s.get('label') or s['tool']}" + ("" if ok else f" - {msg[:200]}")
                 for s, ok, msg in done]
        if failed is None:
            status, head = "success", f"Ran routine **{name}**: all {len(steps)} step{'s' if len(steps) != 1 else ''} done."
        else:
            i, _step, _msg, declined = failed
            status = "success" if declined else "error"
            head = (f"Stopped routine **{name}** at step {i} of {len(steps)}"
                    + (" because you declined it." if declined else " because it failed."))
            if len(steps) > i:
                lines.append(f"- Not run: {len(steps) - i} later step{'s' if len(steps) - i != 1 else ''}.")
        message = head + "\n\n" + "\n".join(lines)
        _record(routine, "done" if failed is None else ("declined" if failed[3] else "failed"), head, trigger)
        # "routine_finished", not "verification_complete": nothing here verified the result.
        event_log.emit_activity("routine_finished" if failed is None else "run_failed",
                                "Routine finished" if failed is None else head.replace("**", ""),
                                level="info" if failed is None else "warn")
        conv = shared.app_state["conversation"]
        conv.append({"role": "user", "content": f"Run routine: {name}"})
        conv.append({"role": "assistant", "content": message})
        return {"status": status, "message": message, "task_id": task_id, "routine": routine["id"],
                "rejected": bool(failed and failed[3])}
    except RunStopped as e:
        reason = "Time limit reached" if e.reason == "time" else "Stopped by you"
        _record(routine, "stopped", reason, trigger)
        event_log.emit_activity("run_cancelled", reason, level="warn")
        ran = ", ".join(s.get("label") or s["tool"] for s, ok, _m in done if ok) or "nothing"
        return {"status": "stopped", "message": f"{reason}. Routine '{name}' ran: {ran}.", "task_id": task_id}
    except Exception as e:
        _record(routine, "failed", str(e)[:200], trigger)
        event_log.log_action(f"Routine failed: {e}")
        return {"status": "error", "message": f"Routine failed: {str(e)[:300]}", "task_id": task_id}
    finally:
        shared.app_state["turn_policy"] = {"mode": "none", "allow_mutation": False, "reason": "no active request"}
        runtime.tool_exec.begin_task(None, None)
        run_control.end()
        shared.state_lock.release()

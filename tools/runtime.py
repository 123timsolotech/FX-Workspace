"""
tools/runtime.py
================
The tool registry, the executor, and execute_tool_guarded (gates, events, ledger).
(Split out of main.py in Phase 2 - see ARCHITECTURE.md D14.)
"""
from __future__ import annotations

import re
import threading
import context as ctx
import permissions
import recorder
from run_control import run_control
import tool_executor
import tool_registry

import core.state as shared
import events as event_log
import tools.dispatch as dispatch
import tools.files as files
import tools.schemas as schemas


def execute_tool_guarded(tool_name: str, args: dict, tool_call_id: str = None, links: dict | None = None) -> dict:
    """Turn/intent gates, duplicate suppression and ledger recording around
    every execution; the call itself goes through tool_exec (validation,
    classification, bounded retries, cooldowns, timeouts, cancellation).
    `links` (plan_step / why / thought_id) ride on the Activity event only."""
    run_control.checkpoint(on_pause=event_log._announce_pause)
    if not schemas.tool_allowed_current_turn(tool_name):
        policy = shared.app_state.get("turn_policy") or {}
        message = (f"CURRENT-TURN GATE: blocked '{tool_name}'. The current request grants "
                   f"{policy.get('mode', 'no')} tool access ({policy.get('reason', 'no reason')}).")
        event_log.log_action(message)
        return {"ok": False, "blocked": True, "message": message}
    # A real tool of a skill not loaded yet (skills.py only shrinks what is shown; the mode grants authority).
    import skills
    skills.note_used(tool_name)
    guard = shared.app_state.get("intent_guard") or {}
    expected = (guard.get("expected_app") or "").strip()
    if expected and tool_name in ("open_application", "open_app_and_list"):
        requested = (args.get("app_name") or "").strip()
        norm = lambda value: re.sub(r"[^a-z0-9]+", "", value.lower())
        if requested and norm(requested) != norm(expected):
            message = (f"INTENT GUARD: blocked '{requested}'. The user explicitly requested "
                       f"'{expected}'. Open that exact installed app; do not substitute a website, "
                       "search engine, or unrelated application.")
            event_log.log_action(message)
            return {"ok": False, "message": message}
    if expected and tool_name in ("execute_command", "run_powershell", "run_python"):
        message = (f"INTENT GUARD: command execution is disabled for this app-opening step. Use the "
                   f"installed-app resolver for '{expected}'; never search the web as a substitute.")
        event_log.log_action(message)
        return {"ok": False, "message": message}

    blocked = ctx.duplicate_block(shared.ledger, tool_name, args)
    if blocked:
        event_log.log_action(f"BLOCKED duplicate {tool_name} - already executed recently.")
        shared.ledger.record(tool_name, args, False, "blocked as a duplicate")
        return blocked

    # The note must be computed BEFORE this call is recorded, otherwise the
    # freshly-written ledger entry is itself the "identical earlier call" and
    # every single action gets falsely flagged as a repeat.
    note = ctx.soft_repeat_note(shared.ledger, tool_name, args) if tool_name not in schemas.READ_ONLY_TOOLS else ""

    started = event_log.emit_event("tool", event_log.describe_tool(tool_name, args), tool=tool_name, status="running",
                         tool_call_id=tool_call_id, **(links or {}))
    shared.app_state["running_tool_event"] = started          # event_log.progress (web searches)
    try:
        res = tool_exec.execute(tool_name, args, tool_call_id=tool_call_id)
    finally:
        shared.app_state["running_tool_event"] = None
    _finish(tool_name, started, res)

    # Pure reads and plan bookkeeping are left out: the ledger exists to stop
    # WORK being redone, and set_plan/update_plan results are already visible
    # verbatim in the CURRENT TASK block, so recording them would double the
    # cost of the live block for no added information.
    if tool_name not in ("set_plan", "update_plan", "observe", "list_windows",
                         "list_processes", "read_file", "list_dir", "get_active_window"):
        shared.ledger.record(tool_name, args, res.get("ok", False), res.get("message", ""), res.get("verified"))

    if note:
        res = {**res, "message": (res.get("message", "") + note)}
    return res


def _finish(tool_name: str, started: dict, res: dict):
    """What every tool call does after it ran - model-called or internal:
    the recorder, the Activity event, and the verified-result link."""
    if recorder.active():
        recorder.tool_result(tool_name, res)
    event_log.finish_event(started, ok=bool(res.get("ok")), detail=(res.get("message") or "")[:400],
                           verified=res.get("verified"), result=res.get("status"), attempts=res.get("attempts"),
                           undo_id=res.get("undo_id"), **({"world": res["world"]} if tool_name == "world_monitor" and isinstance(res.get("world"), dict) else {}), **({"web": res["event"]["web"]} if isinstance(
                               res.get("event"), dict) and isinstance(res["event"].get("web"), dict) else {}))
    # This is the grounding link between what a tool actually confirmed and
    # what update_plan is later allowed to claim. Only tools that explicitly
    # checked real machine state return a 'verified' key at all (act_element,
    # click_at, generic_actions-backed desktop actions, ...); informational
    # reads don't, and are correctly left out of this check entirely.
    if "verified" in res:
        shared.app_state["last_verifiable_result"] = {
            "tool": tool_name, "verified": bool(res.get("verified")), "message": res.get("message", ""),
        }


def execute_internal(tool_name: str, args: dict, runner) -> dict:
    """An internal tool (schemas.INTERNAL_TOOLS - never offered to models):
    a deterministic desktop route whose actions the user already approved via
    approvals.gate_batch. It goes through the same registry spec, executor,
    event stream and journal as every other tool (no side channel).

    The caller gets exactly what the route returned (its own message, or no
    message so the caller's fallback applies), and an exception the route
    raises still propagates - the executor's model-facing "[status: ...]" note
    and error wrapping are for model-called tools; no model reads this result.
    The executor also keeps internal calls out of the run's tool budget and
    marks them internal in its history (the reviewer counts model work only)."""
    run_control.checkpoint(on_pause=event_log._announce_pause)
    started = event_log.emit_event("tool", event_log.describe_tool(tool_name, args), tool=tool_name, status="running")
    box = {}

    def run():
        try:
            box["raw"] = runner()
        except Exception as e:
            box["exc"] = e
            raise
        return box["raw"]

    done = tool_exec.execute(tool_name, args, runner=run)
    if isinstance(box.get("raw"), dict):
        res = dict(box["raw"])
    else:                                   # not run (cancelled, invalid) or returned nothing usable
        res = {**done, "message": re.sub(r"\n\[status: [^\]]*\]$", "", str(done.get("message") or ""))}
    _finish(tool_name, started, {**res, "status": done.get("status"), "attempts": done.get("attempts"),
                                 "undo_id": done.get("undo_id")})
    if "exc" in box:
        raise box["exc"]
    return res


# Typed tool catalogue + executor (tool_registry.py / tool_executor.py).
TOOL_REGISTRY = tool_registry.build_registry(schemas.TOOLS + schemas.INTERNAL_TOOLS, schemas.READ_ONLY_TOOLS,
                                             permissions.classify)


def sync_mcp_tools():
    """Keep the registry's mcp__ entries equal to the tools of the connected
    MCP servers (called whenever a server connects, disconnects or changes
    its tool list), so MCP calls use the same spec/validation/executor path."""
    with _mcp_sync:                   # servers connect on their own threads
        wanted = {t["function"]["name"]: t for t in shared.mcp_hub.tool_schemas()}
        for name in [n for n in TOOL_REGISTRY.names() if n.startswith("mcp__") and n not in wanted]:
            TOOL_REGISTRY.unregister(name)
        for name, schema in wanted.items():
            fn = schema["function"]
            TOOL_REGISTRY.register(tool_registry.ToolSpec(
                name=name, description=fn.get("description", ""), input_schema=fn.get("parameters"),
                tier=permissions.classify(name), read_only=False, **tool_registry.MCP_TOOL_POLICY),
                {k: v for k, v in schema.items() if k != "_mcp"})


_mcp_sync = threading.Lock()


shared.mcp_hub.on_change = sync_mcp_tools


# Late-bound (lambda) so patching/replacing execute_tool still takes effect.
tool_exec = tool_executor.ToolExecutor(TOOL_REGISTRY, lambda name, args: dispatch.execute_tool(name, args),
                                       is_cancelled=run_control.is_stopped, undo=files.undo)

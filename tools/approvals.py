"""
tools/approvals.py
==================
The approval path: views, the broker (web card / desktop popup), gate_batch.
(Split out of main.py in Phase 2 - see ARCHITECTURE.md D14.)
"""
from __future__ import annotations

import os
import app_registry
import approval
import approval_broker
import approval_view
import patch_edit
import permissions
import recorder
from run_control import run_control
import undo_store

import core.state as shared
import events as event_log
import tools.files as files
import tools.runtime as runtime


# ==============================================================================
# Batched approval popup (see approval.py for the dialog itself)
# ==============================================================================
approval.start()


broker = approval_broker.ApprovalBroker(native_ask=approval.ask,
                                        native_available=lambda: not approval._gui_failed.is_set())


def _patch_preview(args: dict) -> dict:
    """The approval preview for apply_patch: the exact diff a dry run produces."""
    path = files.safe_path(args.get("filename") or "")
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {"kind": "text", "text": f"Cannot preview: {e}. The patch will fail."}
    result = patch_edit.dry_run(text, args.get("edits"), os.path.basename(path))
    if not result["ok"]:
        return {"kind": "text", "text": f"This patch will fail and change nothing: {result['error']}"}
    return {"kind": "diff", "text": result["diff"], "note": patch_edit.describe_matches(result["matches"])}


def _adapter_preview(view: dict, name: str, args: dict) -> dict:
    """An app adapter's own preview (e.g. the diff of a Roblox script edit,
    from the script as it is in Studio now) and the fingerprint that lets
    _changed_since_approval catch a change made after the card was shown."""
    adapter = app_registry.adapter_for_tool(name)
    shown = adapter.preview(name, args) if adapter else None
    if shown:
        view["preview"] = {k: shown[k] for k in ("kind", "text", "note") if k in shown}
        if shown.get("ref"):
            view["target"] = shown.get("target") or shown["ref"]
            view["fingerprint"] = shown.get("fingerprint")
            view["external"] = {"adapter": adapter.app_id, "ref": shown["ref"]}
    return view


def _approval_view(action: dict) -> dict:
    """The complete, untruncated description both approval surfaces render."""
    name = action.get("tool_name") or ""
    spec = runtime.TOOL_REGISTRY.get(name)
    try:
        return _adapter_preview(approval_view.describe(
            name, action.get("args") or {}, action.get("justification") or "",
            tier=permissions.classify(name), undoable=bool(spec and spec.undoable),
            mutating=bool(spec.mutating) if spec else True,
            patch_preview=_patch_preview), name, action.get("args") or {})
    except Exception as e:                 # a preview must never block the approval itself
        event_log.log_action(f"Approval preview failed for {name}: {e}")
        return approval_view.describe(name, action.get("args") or {}, action.get("justification") or "",
                                      tier=permissions.classify(name), undoable=bool(spec and spec.undoable))


def _changed_since_approval(view) -> str | None:
    """The file an approved action targets changed after its preview was shown:
    what the user approved is no longer what would happen, so it is not run."""
    if view and view.get("external"):
        adapter = app_registry.get_adapter(view["external"]["adapter"])
        try:
            now = adapter.fingerprint(view["external"]["ref"]) if adapter else None
        except OSError as e:
            return f"NOT EXECUTED - could not check '{view.get('target')}' before changing it: {e}"
        if now == view.get("fingerprint"):
            return None
        event_log.log_action(f"Not run: {view.get('target')} changed after it was approved.")
        return (f"NOT EXECUTED - '{view.get('target')}' changed after the user approved this action, so the "
                "approved preview no longer matches. Read it again and propose the change again.")
    if not view or not view.get("target") or view.get("fingerprint") in (None, "dir"):
        return None
    now = undo_store.fingerprint(view["target"])
    if now == view["fingerprint"]:
        return None
    event_log.log_action(f"Not run: {view['target']} changed after it was approved.")
    return (f"NOT EXECUTED - '{view['target']}' changed after the user approved this action, so the approved "
            "preview no longer matches. Read the file again and propose the change again.")


def untrusted_warning(seen: list) -> str:
    """The line an approval card shows once this request read content that
    tried to instruct the assistant: the user should check the action is
    theirs, not the content's."""
    first = seen[0]
    more = f" (and {len(seen) - 1} more)" if len(seen) > 1 else ""
    return (f"Earlier in this request, content from {first['source']}{more} tried to give the assistant "
            f"instructions: \"{first['snippet'][:140]}\". Make sure this action is something you asked for.")


def ask_batch_approval_os(actions: list, context_line: str | None = None) -> bool:
    """Inline card in the web UI when it is focused, native popup otherwise
    (see approval_broker.py). The name is kept for the existing call sites.
    `context_line` (optional) says where in a run this is, e.g. a routine step."""
    if not context_line:
        active = shared.task_board.active_step()
        context_line = (f"Step {active['n']} of {len(shared.task_board.steps)}: {active['text']}" if active
                        else "")      # the card needs no generic line (D48); the desktop popup has its own
    seen = shared.app_state.get("untrusted_seen") or []
    for a in actions:                        # kept on the caller's dicts: checked again just before running
        a["view"] = _approval_view(a)
        # The same words the step row uses ("Write notes.txt"): an answered card folds to them, and a declined
        # action keeps them in its work line under the reply (D49).
        a["view"]["brief"] = event_log.describe_tool(a.get("tool_name") or "", a.get("args") or {})
        if seen:
            a["view"]["warning"] = untrusted_warning(seen)
        # "Allow all like this for this request" is offered only where it may apply (agent/loop._decide).
        a["view"]["request_ok"] = permissions.request_grantable(a.get("tool_name") or "", shared.app_state["settings"],
                                                                tainted=bool(seen), args=a.get("args") or {})
    event_log.emit_event("approval", "Waiting for your approval", detail=", ".join(a.get("tool_name", "?") for a in actions))
    approved = broker.request(actions, context_line, cancelled=run_control.is_stopped)
    if recorder.active():
        recorder.approval(approved)
    run_control.checkpoint(on_pause=event_log._announce_pause)   # Stop while the card was open ends the run here
    if approved and broker.last_scope == "request":
        names = {a.get("tool_name") for a in actions if a.get("tool_name")}
        shared.app_state["request_grants"] = set(shared.app_state.get("request_grants") or ()) | names
        event_log.log_action(f"Allowed for the rest of this request: {', '.join(sorted(names))}.")
    event_log.emit_event("approval", ("Approved for the rest of this request" if broker.last_scope == "request" else "Approved")
                         if approved else "Declined", ok=approved,
                         asked=[a["view"]["brief"] for a in actions][:8])
    return approved


def gate_batch(actions: list):
    """For a FIXED, indivisible batch of desktop-router actions (one logical
    step, e.g. 'open app + click Connect'): returns (decision, approved)
    where decision is 'auto'/'ask'/'deny' and approved is the bool the
    caller should act on ('auto' -> True without a popup, 'deny' -> False
    without a popup, 'ask' -> whatever the popup returns). This is where
    god mode, lockdown mode, and per-tool overrides reach the deterministic
    desktop-router fast path, not just the free-form agent loop."""
    names = [a["tool_name"] for a in actions]
    decision = permissions.batch_decision(names, shared.app_state["settings"],
                                          tainted=bool(shared.app_state.get("untrusted_seen")))
    if decision == "auto":
        event_log.log_action(f"Auto-approved (permission: auto): {', '.join(names)}.")
        return decision, True
    if decision == "deny":
        event_log.log_action(f"BLOCKED by permission settings (deny): {', '.join(names)}.")
        return decision, False
    shared.app_state["waiting_for_os_approval"] = True
    try:
        approved = ask_batch_approval_os(actions)
    finally:
        shared.app_state["waiting_for_os_approval"] = False
    return decision, approved

"""
roblox_adapter.py
=================
Roblox Studio as an app adapter (app_registry.AppAdapter): what every
roblox_* tool does, on top of the transport in roblox_bridge.py.

  * Reads: status (with an active ping), the place tree, selection, children,
    properties, search, the Output (plus errors from play tests, with stack
    traces), and scripts - in windows of lines that SAY what was left out, so
    a model never mistakes part of a script for all of it.
  * Script edits: roblox_edit_script applies multi-hunk patches with the same
    engine as files (patch_edit.py); roblox_set_script_source replaces a whole
    script; roblox_create_script adds one. All three are journaled in the undo
    trail (undo_store "external" ops: the old source is kept on this PC) and
    written compare-and-set: the plugin refuses the write if the script
    changed since it was read, so nothing newer is overwritten. The approval
    card shows the real diff, and a change made after approval is caught
    before running.
  * Other changes (create / delete instance, set property) are one Ctrl+Z step
    in Studio (ChangeHistoryService); the app's own undo trail does not cover
    them and the approval card says so.
  * Play tests: roblox_run_playtest starts a Play or Run session for a bounded
    time and reports the errors the play-test copies of the plugin pushed.

Studio must be open with the Agentic Bridge plugin turned on; otherwise every
tool says so and nothing is queued.
"""
from __future__ import annotations

import difflib
import json

import app_registry
import patch_edit
import roblox_bridge as bridge
import undo_store

READ_LINES = 400                 # at most this many lines per roblox_read_script call
READ_CHARS = 16_000              # ... and at most this many characters
TREE_LIMIT = 400
PLAYTEST_MAX_S = 120
MUTATING_COMMANDS = {"set_property", "create_instance", "create_script", "delete_instance", "set_script_source"}


def _ref(path: str, place: dict | None = None) -> str:
    """Undo reference: the place plus the script path, so an undo can never
    land in a different place that happens to have the same path."""
    place = place or bridge.primary_place()
    return f"{place.get('place_id') or 0}|{place.get('place') or '?'}::{path}"


def _split_ref(ref: str) -> tuple[str, str]:
    key, _, path = ref.partition("::")
    return key, path


def _place_key() -> str:
    place = bridge.primary_place()
    return f"{place.get('place_id') or 0}|{place.get('place') or '?'}"


def _rows(data) -> str:
    rows = []
    for item in (data or [])[:150]:
        if isinstance(item, dict) and "path" in item:
            rows.append(f"- {item.get('path')} ({item.get('className', '')}"
                        + (f", {item['childCount']} children" if item.get("childCount") else "") + ")")
        elif isinstance(item, dict) and "text" in item:
            rows.append(f"[{item.get('kind', '')}] {item.get('text', '')}")
        else:
            rows.append(json.dumps(item, default=str) if isinstance(item, (dict, list)) else str(item))
    return "\n".join(rows)


# Script source only changes through roblox_edit_script / roblox_set_script_source
# (checked against what was read, shown as a diff, in the undo trail) and new
# scripts through roblox_create_script - never as a property (Phase 3 review).
_SCRIPT_CLASSES = {"Script", "LocalScript", "ModuleScript"}
_SOURCE_PROPS = {"source", "linkedsource"}
_SOURCE_REFUSAL = ("Refused: script code is never changed through properties. Use roblox_edit_script to change a "
                   "script (a checked edit that can be undone) or roblox_create_script to make a new one.")

# roblox_batch: many changes, one call, one approval. The bridge deliberately runs no Luau (roblox_bridge.py),
# so repetition is data - repeat + step on a create_instance - expanded here into the plugin's own commands.
BATCH_MAX = 200
REPEAT_MAX = 100
BATCH_ACTIONS = ("create_instance", "set_property", "delete_instance")


def _vec(value):
    """[x, y, z] from [x, y, z] or {x, y, z} / {X, Y, Z}; None for anything else."""
    if isinstance(value, dict):
        value = [value.get("x", value.get("X")), value.get("y", value.get("Y")), value.get("z", value.get("Z"))]
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return [float(v) for v in value]
    return None


def _stepped(base, step, k: int):
    """The value of copy k (0-based): base + step * k, for numbers and [x, y, z]."""
    if isinstance(step, (int, float)) and not isinstance(step, bool):
        start = base if isinstance(base, (int, float)) and not isinstance(base, bool) else 0
        return round(start + step * k, 6)
    inc = _vec(step)
    if inc is not None:
        start = _vec(base) or [0.0, 0.0, 0.0]
        return [round(start[j] + inc[j] * k, 6) for j in range(3)]
    return base


def expand_batch(actions) -> tuple[list, str | None]:
    """roblox_batch's actions as single changes in order ([{action, ...}], None), or ([], why not)."""
    if not isinstance(actions, list) or not actions:
        return [], "actions must be a non-empty list"
    ops = []
    for i, a in enumerate(actions, 1):
        if not isinstance(a, dict) or a.get("action") not in BATCH_ACTIONS:
            return [], f"item {i} needs action create_instance, set_property or delete_instance"
        kind = a["action"]
        if kind == "create_instance":
            if not a.get("class_name"):
                return [], f"item {i} (create_instance) needs class_name"
            try:
                count = int(a.get("repeat") or 1)
            except (TypeError, ValueError):
                return [], f"item {i}: repeat must be a whole number"
            if not 1 <= count <= REPEAT_MAX:
                return [], f"item {i}: repeat must be 1-{REPEAT_MAX}"
            props = a.get("properties") if isinstance(a.get("properties"), dict) else {}
            step = a.get("step") if isinstance(a.get("step"), dict) else {}
            base = str(a.get("name") or "").strip() or str(a["class_name"])
            for k in range(count):
                copy = dict(props)
                for prop, inc in step.items():
                    copy[prop] = _stepped(props.get(prop), inc, k)
                ops.append({"action": kind, "class_name": a["class_name"], "parent_path": a.get("parent_path") or "Workspace",
                            "name": f"{base}{k + 1}" if count > 1 else (a.get("name") or None), "properties": copy})
        elif kind == "set_property":
            if not a.get("path") or not a.get("property") or "value" not in a:
                return [], f"item {i} (set_property) needs path, property and value"
            ops.append({"action": kind, "path": a["path"], "property": a["property"], "value": a["value"]})
        else:
            if not a.get("path"):
                return [], f"item {i} (delete_instance) needs path"
            ops.append({"action": kind, "path": a["path"]})
        if len(ops) > BATCH_MAX:
            return [], f"that is more than {BATCH_MAX} changes - split the work into several calls"
    return ops, None


def describe_op(op: dict) -> str:
    if op["action"] == "create_instance":
        props = ", ".join(f"{k}={json.dumps(v)}" for k, v in (op.get("properties") or {}).items())
        return (f"create {op['class_name']} '{op.get('name') or op['class_name']}' in {op['parent_path']}"
                + (f" ({props})" if props else ""))
    if op["action"] == "set_property":
        return f"set {op['path']}.{op['property']} = {json.dumps(op['value'])}"
    return f"delete {op['path']}"


class RobloxAdapter(app_registry.AppAdapter):
    app_id = "roblox"
    display_name = "Roblox Studio"
    process_names = ("RobloxStudioBeta.exe", "RobloxStudio.exe")
    tool_prefix = "roblox_"

    # ---- connection -------------------------------------------------------
    def status(self) -> dict:
        st = bridge.status_dict()
        if not st["connected"]:
            detail = "Not connected - turn on the Agentic Bridge plugin in Studio (Plugins tab > Bridge)."
        else:
            detail = f"Connected to '{st.get('place') or '?'}'"
            if len(st.get("sessions") or []) > 1:
                detail += f" ({sum(1 for s in st['sessions'] if s['role'] == 'edit')} Studio windows open; commands go to this one)"
        return {**st, "detail": detail}

    def health_check(self, timeout: float = 5.0) -> dict:
        if not bridge.is_connected():
            return {"ok": False, "latency_ms": None, "message": self.status()["detail"]}
        return bridge.health_check(timeout)

    # ---- helpers ----------------------------------------------------------
    def _send(self, command: str, payload: dict, cancelled=None, timeout: float = 20.0) -> dict:
        return bridge.enqueue(command, payload, timeout=timeout, cancelled=cancelled)

    def _source(self, path: str | None, cancelled=None) -> tuple[bool, str, str]:
        """(ok, path or error message, raw source)."""
        res = self._send("get_script_source", {"path": path or None}, cancelled)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        if not res.get("ok") or not isinstance(data.get("source"), str):
            return False, res.get("message") or "Could not read the script.", ""
        return True, str(data.get("path") or path), data["source"]

    # ---- tools ------------------------------------------------------------
    def call(self, tool_name: str, args: dict, journal=None, cancelled=None) -> dict:
        handler = getattr(self, "_t_" + tool_name[len(self.tool_prefix):], None)
        if handler is None:
            return {"ok": False, "message": f"Unknown Roblox tool '{tool_name}'."}
        return handler(dict(args or {}), journal or undo_store.NO_JOURNAL, cancelled)

    def _t_status(self, args, journal, cancelled):
        st = self.status()
        if not st["connected"]:
            return {"ok": True, "message": st["detail"], "data": st}
        health = self.health_check()
        parts = [st["detail"],
                 f"script open in editor: {st.get('active_script') or 'none'}",
                 f"selection: {', '.join(st.get('selection') or []) or 'nothing'}",
                 "playtest running" if st.get("running") else "not playtesting",
                 f"{st.get('error_count', 0)} Output error(s) recorded this session",
                 f"ping: {health['latency_ms']} ms" if health.get("ok") else f"ping failed: {health.get('message')}"]
        if st.get("outdated_plugin"):
            parts.append("the plugin is outdated - ask the user to copy roblox_plugin/AgenticBridge.server.lua "
                         "into Studio's Plugins folder again")
        return {"ok": True, "message": "; ".join(parts) + ".", "data": {**st, "health": health}}

    def _t_get_tree(self, args, journal, cancelled):
        depth = max(1, min(int(args.get("depth") or 3), 6))
        limit = max(10, min(int(args.get("limit") or 200), TREE_LIMIT))
        res = self._send("get_tree", {"root": args.get("root") or None, "depth": depth, "limit": limit}, cancelled)
        if not res.get("ok"):
            return {"ok": False, "message": res.get("message", "")}
        items = res.get("data") or []
        lines = [("  " * int(i.get("depth") or 0)) + f"{str(i.get('path') or '').rsplit('/', 1)[-1]} "
                 f"({i.get('className', '')}{', ' + str(i['childCount']) + ' children' if i.get('childCount') else ''})"
                 f"  [{i.get('path')}]" for i in items if isinstance(i, dict)]
        more = len(items) >= limit
        note = (f"\n[Stopped at {limit} items - call roblox_get_tree with a deeper root or roblox_list_children "
                "to see more.]") if more else ""
        return {"ok": True, "message": f"Place tree ({len(items)} items, depth {depth}):\n" + "\n".join(lines) + note,
                "data": items}

    def _passthrough(self, command, payload, cancelled, mutating=False):
        res = self._send(command, payload, cancelled)
        data, message = res.get("data"), res.get("message") or ""
        if isinstance(data, list):
            rows = _rows(data)
            message += ("\n" + rows) if rows else ""
        elif isinstance(data, dict) and command in ("get_properties", "set_property"):
            message += "\n" + json.dumps(data, default=str)[:3000]
        out = {"ok": bool(res.get("ok")), "message": message, "data": data}
        if res.get("cancelled"):
            out["status"] = "cancelled"
        if mutating:
            out["verified"] = bool(res.get("ok"))
        return out

    def _t_get_selection(self, args, journal, cancelled):
        return self._passthrough("get_selection", {}, cancelled)

    def _t_list_children(self, args, journal, cancelled):
        return self._passthrough("list_children", {"path": args.get("path", "")}, cancelled)

    def _t_get_properties(self, args, journal, cancelled):
        return self._passthrough("get_properties", {"path": args.get("path", ""), "properties": args.get("properties") or []}, cancelled)

    def _t_find(self, args, journal, cancelled):
        return self._passthrough("find_instances", {"name": args.get("name") or None, "className": args.get("class_name") or None,
                                                    "root": args.get("root") or None, "limit": args.get("limit") or 50}, cancelled)

    def _t_create_instance(self, args, journal, cancelled):
        props = args.get("properties") if isinstance(args.get("properties"), dict) else {}
        if str(args.get("class_name") or "") in _SCRIPT_CLASSES or any(str(k).lower() in _SOURCE_PROPS for k in props):
            return {"ok": False, "status": "permanent_failure", "message": _SOURCE_REFUSAL}
        return self._passthrough("create_instance", {"className": args.get("class_name"), "parentPath": args.get("parent_path", ""),
                                                     "name": args.get("name"), "properties": args.get("properties") or {}},
                                 cancelled, mutating=True)

    def _t_set_property(self, args, journal, cancelled):
        if str(args.get("property") or "").lower() in _SOURCE_PROPS:
            return {"ok": False, "status": "permanent_failure", "message": _SOURCE_REFUSAL}
        return self._passthrough("set_property", {"path": args.get("path", ""), "property": args.get("property"),
                                                  "value": args.get("value")}, cancelled, mutating=True)

    def _t_delete_instance(self, args, journal, cancelled):
        return self._passthrough("delete_instance", {"path": args.get("path", "")}, cancelled, mutating=True)

    def _t_batch(self, args, journal, cancelled):
        """Every change in order, each one the plugin's own command (and its own Ctrl+Z in Studio). Stops at the
        first one that fails - and says how far it got - rather than carrying on from a broken state."""
        ops, why = expand_batch(args.get("actions"))
        if why:
            return {"ok": False, "status": "permanent_failure", "message": f"Nothing ran: {why}."}
        run = {"create_instance": self._t_create_instance, "set_property": self._t_set_property,
               "delete_instance": self._t_delete_instance}
        done = []
        for i, op in enumerate(ops, 1):
            if cancelled is not None and cancelled():
                return {"ok": False, "status": "cancelled", "verified": False, "data": {"done": done},
                        "message": f"Stopped after {len(done)} of {len(ops)} changes."}
            res = run[op["action"]](op, journal, cancelled)
            if not res.get("ok"):
                return {"ok": False, "verified": False, "data": {"done": done},
                        "message": (f"Made {len(done)} of {len(ops)} changes; change {i} ({describe_op(op)}) failed: "
                                    f"{res.get('message') or 'no reason given'}. Nothing after it ran.")}
            done.append(describe_op(op))
        shown = "\n".join(f"- {d}" for d in done[:25]) + (f"\n- ... and {len(done) - 25} more" if len(done) > 25 else "")
        return {"ok": True, "verified": True, "data": {"count": len(done)},
                "message": f"Made {len(done)} change{'s' if len(done) != 1 else ''} in Studio:\n{shown}"}

    def _t_get_output(self, args, journal, cancelled):
        limit = max(1, min(int(args.get("limit") or 40), 300))
        res = self._send("get_output", {"limit": limit, "errorsOnly": bool(args.get("errors_only"))}, cancelled)
        lines = [f"[{i.get('kind', '')}] {i.get('text', '')}" for i in (res.get("data") or []) if isinstance(i, dict)]
        play = [e for e in bridge.recent_errors(50) if e["text"].startswith("[play-")]
        if args.get("include_playtest", True) and play:
            lines.append("-- errors from play tests (server/client copies of your scripts) --")
            for e in play[-20:]:
                lines.append(e["text"] + (f"\n    {e['trace'].strip()}" if e.get("trace") else ""))
        if not res.get("ok") and not play:
            return {"ok": False, "message": res.get("message", "")}
        return {"ok": True, "message": f"{len(lines)} Output line(s):\n" + "\n".join(lines[-(limit + 25):]),
                "data": res.get("data")}

    def _t_read_script(self, args, journal, cancelled):
        if (bridge.plugin_version() or 0) < 3:          # older plugin: its own numbered read, with its own notice
            return self._passthrough("read_script", {"path": args.get("path") or None, "startLine": args.get("start_line"),
                                                     "endLine": args.get("end_line")}, cancelled)
        ok, path, source = self._source(args.get("path"), cancelled)
        if not ok:
            return {"ok": False, "message": path}
        lines = source.split("\n")
        total = len(lines)
        start = max(1, int(args.get("start_line") or 1))
        end = min(total, int(args.get("end_line") or total))
        if start > total:
            return {"ok": False, "message": f"{path} has only {total} lines."}
        shown, used, last = [], 0, start - 1
        for n in range(start, end + 1):
            row = f"{n:4d}  {lines[n - 1]}"
            if len(shown) >= READ_LINES or used + len(row) + 1 > READ_CHARS:
                break
            shown.append(row)
            used += len(row) + 1
            last = n
        head = f"{path} - lines {start}-{last} of {total}:"
        tail = ""
        if last < total:
            tail = (f"\n[NOT SHOWN: lines {last + 1}-{total}. The script continues - read them with "
                    f"roblox_read_script start_line={last + 1} before judging the whole script. To change it, "
                    "use roblox_edit_script: it edits only the lines you name, so lines you have not read stay as they are.]")
        return {"ok": True, "message": head + "\n" + "\n".join(shown) + tail,
                "data": {"path": path, "line_count": total, "start_line": start, "end_line": last}}

    def _write(self, path: str, before: str, after: str, journal, cancelled) -> dict:
        journal.external("roblox", _ref(path), before, after, display=f"Roblox: {path}")
        res = self._send("set_script_source", {"path": path, "source": after, "expected": before}, cancelled)
        if not res.get("ok"):
            return {"ok": False, "message": res.get("message") or "Studio did not accept the change.",
                    **({"status": "cancelled"} if res.get("cancelled") else {})}
        ok, _p, now = self._source(path, cancelled)
        verified = ok and now == after
        return {"ok": True, "verified": verified,
                "message": f"Updated {path} ({len(after.split(chr(10)))} lines)." + (
                    "" if verified else " Could not read it back to confirm."),
                "data": {"path": path}}

    def _t_edit_script(self, args, journal, cancelled):
        ok, path, before = self._source(args.get("path"), cancelled)
        if not ok:
            return {"ok": False, "message": path}
        result = patch_edit.dry_run(before, args.get("edits"), path)
        if not result["ok"]:
            return {"ok": False, "message": f"Nothing was changed: {result['error']}"}
        if result["text"] == before:
            return {"ok": True, "message": f"{path} already contains that; nothing to change.", "verified": True}
        out = self._write(path, before, result["text"], journal, cancelled)
        if out.get("ok"):
            out["message"] += " " + patch_edit.describe_matches(result["matches"]) + "."
        return out

    def _t_set_script_source(self, args, journal, cancelled):
        ok, path, before = self._source(args.get("path"), cancelled)
        if not ok:
            return {"ok": False, "message": path}
        after = str(args.get("source") or "")
        if after == before:
            return {"ok": True, "message": f"{path} already has exactly that source.", "verified": True}
        return self._write(path, before, after, journal, cancelled)

    def _t_create_script(self, args, journal, cancelled):
        class_name = args.get("class_name") or "Script"
        parent = str(args.get("parent_path") or "").strip("/")
        name = args.get("name") or class_name
        path = f"{parent}/{name}" if parent else name
        exists, _p, _src = self._source(path, cancelled)
        if exists:
            return {"ok": False, "message": (f"{path} already exists. Pick another name, or change that script "
                                             "with roblox_edit_script.")}
        source = str(args.get("source") or "")
        journal.external("roblox", _ref(path), None, source, display=f"Roblox: {path} (new)")
        res = self._send("create_script", {"className": class_name, "parentPath": parent, "name": name, "source": source},
                         cancelled)
        out = {"ok": bool(res.get("ok")), "message": res.get("message") or "", "data": res.get("data"),
               "verified": bool(res.get("ok"))}
        if res.get("cancelled"):
            out["status"] = "cancelled"
        return out

    def _t_run_playtest(self, args, journal, cancelled):
        mode = "run" if args.get("mode") == "run" else "play"
        seconds = max(3, min(int(args.get("seconds") or 15), PLAYTEST_MAX_S))
        first = bridge.error_seq()
        res = self._send("run_playtest", {"mode": mode, "seconds": seconds}, cancelled, timeout=seconds + 45)
        if not res.get("ok"):
            return {"ok": False, "message": res.get("message", ""), **({"status": "cancelled"} if res.get("cancelled") else {})}
        errors = bridge.errors_since(first, limit=40)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        head = f"Playtest ({mode}, up to {seconds} s) finished"
        if not errors:
            return {"ok": True, "message": head + " with no errors in Output.", "data": {**data, "errors": []}}
        lines = [e["text"] + (f"\n    {e['trace'].strip()}" if e.get("trace") else "") for e in errors]
        return {"ok": True, "message": f"{head}: {len(errors)} error(s):\n" + "\n".join(lines),
                "data": {**data, "errors": errors}}

    # ---- approvals ----------------------------------------------------------
    def preview(self, tool_name: str, args: dict) -> dict | None:
        """The diff the approval card shows, from the script as it is now - or, for roblox_batch,
        every single change it will make, spelled out."""
        if tool_name == "roblox_batch":
            ops, why = expand_batch((args or {}).get("actions"))
            if why:
                return {"kind": "text", "text": f"This will fail and change nothing: {why}."}
            lines = "\n".join(f"{i}. {describe_op(op)}" for i, op in enumerate(ops, 1))
            return {"kind": "text", "text": lines,
                    "note": f"{len(ops)} change{'s' if len(ops) != 1 else ''} in Studio, in this order."}
        if tool_name not in ("roblox_edit_script", "roblox_set_script_source", "roblox_create_script"):
            return None
        if not bridge.is_connected():
            return {"kind": "text", "text": "Roblox Studio is not connected, so this can't be previewed - it will fail."}
        if tool_name == "roblox_create_script":
            path = f"{str(args.get('parent_path') or '').strip('/')}/{args.get('name') or args.get('class_name') or 'Script'}"
            body = str(args.get("source") or "")
            diff = "".join(difflib.unified_diff([], [l + "\n" for l in body.split("\n")], fromfile=f"{path} (new)",
                                                tofile=f"{path} (after)"))
            return {"kind": "diff", "text": diff or "(empty script)\n", "note": "Creates a new script.",
                    "target": path, "ref": _ref(path), "fingerprint": None}
        ok, path, before = self._source(args.get("path"))
        if not ok:
            return {"kind": "text", "text": f"Can't preview: {path} It will fail and change nothing."}
        if tool_name == "roblox_edit_script":
            result = patch_edit.dry_run(before, args.get("edits"), path)
            if not result["ok"]:
                return {"kind": "text", "text": f"This edit will fail and change nothing: {result['error']}",
                        "target": path, "ref": _ref(path), "fingerprint": undo_store.content_fingerprint(before)}
            text, note = result["diff"], patch_edit.describe_matches(result["matches"])
        else:
            after = str(args.get("source") or "")
            text = "".join(difflib.unified_diff([l + "\n" for l in before.split("\n")], [l + "\n" for l in after.split("\n")],
                                                fromfile=f"{path} (now)", tofile=f"{path} (after)")) or "(no change)\n"
            note = f"Replaces the whole script ({len(before.split(chr(10)))} lines now)."
        return {"kind": "diff", "text": text, "note": note, "target": path, "ref": _ref(path),
                "fingerprint": undo_store.content_fingerprint(before)}

    # ---- undo ---------------------------------------------------------------
    def _check_place(self, ref: str):
        key, _path = _split_ref(ref)
        if not bridge.is_connected():
            raise app_registry.AdapterUnavailable(
                "Roblox Studio isn't connected - open that place, turn on the Agentic Bridge, then Undo again.")
        if key != _place_key():
            raise app_registry.AdapterUnavailable(
                f"Studio has a different place open ({_place_key().split('|', 1)[-1]}); open "
                f"'{key.split('|', 1)[-1]}' and Undo again.")

    def fingerprint(self, ref: str) -> str | None:
        self._check_place(ref)
        ok, _path, source = self._source(_split_ref(ref)[1])
        return undo_store.content_fingerprint(source) if ok else None

    def restore(self, ref: str, content: str | None, expected: str | None):
        self._check_place(ref)
        path = _split_ref(ref)[1]
        ok, _p, now = self._source(path)
        if content is None:                                   # the change created this script
            if not ok:
                return
            res = self._send("delete_instance", {"path": path}, None)
        elif not ok:
            raise undo_store.UndoConflict(f"{path} no longer exists in Studio; not recreating it.")
        else:
            res = self._send("set_script_source", {"path": path, "source": content, "expected": now}, None)
        if not res.get("ok"):
            raise app_registry.AdapterUnavailable(f"Studio refused: {res.get('message')}")


adapter = app_registry.register_adapter(RobloxAdapter())
undo_store.register_external("roblox", adapter)

"""
approval_view.py
================
What an approval shows, computed once on the server and rendered the same way
by the in-chat card (index.html) and the desktop popup (approval.py).

  * Nothing is truncated: every argument is shown in full (the popup and the
    card scroll instead of cutting text).
  * File changes carry a preview computed from the real file: a unified diff
    for writes/edits/patches, what exactly would be deleted, what an overwrite
    would replace.
  * Every action says honestly whether it can be undone (the tool registry's
    `undoable` flag - file tools are journaled by undo_store) or not
    ("Not undoable": shell commands, scripts, MCP calls, browser submits...).
  * One wording, used everywhere: "Allow" / "Deny".
"""
from __future__ import annotations

import difflib
import json
import os

WORDING = {
    "allow": "Allow",
    "deny": "Deny",
    "hint": "Enter allows · Esc denies",
    "undoable": "Can be undone",
    "not_undoable": "Not undoable",
    "no_reason": "No reason given.",
}


def title(n: int) -> str:
    return "Allow this action?" if n == 1 else f"Allow these {n} actions?"


FRIENDLY = {
    "execute_command": "Run a command (cmd)", "run_powershell": "Run PowerShell", "run_python": "Run Python code",
    "write_file": "Write a file", "edit_file": "Edit a file", "apply_patch": "Edit a file (patch)",
    "copy_path": "Copy", "move_path": "Move", "rename_path": "Rename", "delete_path": "Delete",
    "make_dir": "Create a folder", "zip_paths": "Create a zip", "unzip_file": "Extract a zip",
    "download_file": "Download a file", "screenshot_to_file": "Save a screenshot",
    "open_application": "Open an app", "open_app_and_list": "Open an app", "open_url": "Open a web page",
    "close_window": "Close a window", "kill_process": "End a process", "click_at": "Click on the screen",
    "click_by_name": "Click a control", "act_element": "Use a control", "type_text": "Type text",
    "keyboard_action": "Press keys", "set_clipboard": "Set the clipboard", "service_control": "Control a service",
    "set_env_var": "Set an environment variable", "schedule_task": "Schedule a task",
    "messages_send": "Send a message", "media_control": "Control media",
    "save_memory": "Save to memory", "messages_draft": "Draft a message",
    "note_save": "Save a note", "task_add": "Add a task", "task_done": "Tick off a task", "doc_write": "Write a document", "skill_save": "Save a skill",
    "roblox_edit_script": "Edit a Roblox script", "roblox_set_script_source": "Replace a Roblox script",
    "roblox_create_script": "Create a Roblox script", "roblox_create_instance": "Create in Roblox Studio",
    "roblox_set_property": "Change a property in Roblox Studio", "roblox_delete_instance": "Delete in Roblox Studio",
    "roblox_batch": "Make several changes in Roblox Studio", "roblox_run_playtest": "Playtest in Roblox Studio",
    "browser_open": "Open a page in the assistant's browser", "browser_click": "Click in the browser",
    "browser_fill": "Fill in a web form field", "mcp_read_resource": "Read an MCP resource",
}

# tool -> (args) -> preview dict; registered by the tool's module (browser.py).
EXTRA_PREVIEWS: dict = {}

# Not undoable from this app, but the app they run in can undo them. Said next
# to "Not undoable" so the card stays honest about this app's own undo.
UNDO_ELSEWHERE = {
    "roblox_create_instance": "Ctrl+Z in Studio reverts it",
    "roblox_set_property": "Ctrl+Z in Studio reverts it",
    "roblox_delete_instance": "Ctrl+Z in Studio restores it",
    "roblox_batch": "Ctrl+Z in Studio reverts each change",
}


def friendly(tool: str) -> str:
    if tool.startswith("mcp__"):
        server, _, name = tool[5:].partition("__")
        return f"Use '{name}' from MCP server '{server}'"
    return FRIENDLY.get(tool) or tool.replace("_", " ").capitalize()


PREVIEW_MAX_FILE = 4 * 1024 ** 2      # larger files are described, not diffed
PREVIEW_MAX_CHARS = 200_000           # a preview longer than this is shortened (and says so)


class _NotText(Exception):
    pass


def _read_text(path: str) -> str | None:
    """The file's text, None when it does not exist; raises _NotText for a
    binary or very large file (those are described instead of diffed)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > PREVIEW_MAX_FILE:
        raise _NotText(f"'{os.path.basename(path)}' is {size / 1024 ** 2:.1f} MB - too large to show as a diff.")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        raise _NotText(f"'{os.path.basename(path)}' is a binary file ({size / 1024:.0f} KB) - no text diff.")
    return raw.decode("utf-8", errors="replace")


def _shorten(pv: dict | None) -> dict | None:
    if pv and len(pv.get("text") or "") > PREVIEW_MAX_CHARS:
        pv = dict(pv, text=pv["text"][:PREVIEW_MAX_CHARS],
                  note=((pv.get("note") + " ") if pv.get("note") else "")
                  + f"Preview shortened to the first {PREVIEW_MAX_CHARS:,} characters; the full content is in the arguments above.")
    return pv


def unified_diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile=f"{os.path.basename(path)} (now)", tofile=f"{os.path.basename(path)} (after)")
    out = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return out or "(no change to the file's text)\n"


def match_line_endings(content: str, old: str, new: str) -> tuple:
    """Same rule as edit_file: '\\n' in the model's text matches a CRLF file."""
    if "\r\n" in content and "\r\n" not in old and "\n" in old:
        return old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
    return old, new


FILE_TARGET = {"write_file": "filename", "edit_file": "filename", "apply_patch": "filename",
               "delete_path": "path", "move_path": "source", "rename_path": "path", "copy_path": "destination"}


def target_path(tool: str, args: dict) -> str | None:
    key = FILE_TARGET.get(tool)
    value = (args or {}).get(key) if key else None
    return os.path.abspath(os.path.expanduser(value)) if isinstance(value, str) and value else None


def _describe_path(path: str) -> str:
    if not os.path.lexists(path):
        return f"'{path}' does not exist."
    if os.path.isdir(path):
        files = dirs = size = 0
        for root, dnames, fnames in os.walk(path):
            dirs += len(dnames)
            files += len(fnames)
            for name in fnames:
                try:
                    size += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return f"Folder '{path}': {files} file(s), {dirs} subfolder(s), {size / 1024:.0f} KB."
    return f"File '{path}': {os.path.getsize(path) / 1024:.1f} KB."


def preview(tool: str, args: dict, patch_preview=None) -> dict | None:
    """A {kind, text} preview of what the action will change, or None.
    `patch_preview(args)` returns the dry-run diff for apply_patch."""
    a = args or {}
    try:
        return _shorten(_preview(tool, a, patch_preview))
    except _NotText as e:
        return {"kind": "text", "text": str(e)}


def _preview(tool: str, a: dict, patch_preview) -> dict | None:
    if tool in EXTRA_PREVIEWS:
        try:
            return EXTRA_PREVIEWS[tool](tool, a)
        except Exception as e:
            return {"kind": "text", "text": f"Can't preview this: {e}"}
    try:
        if tool == "write_file" and a.get("filename"):
            path = os.path.abspath(os.path.expanduser(a["filename"]))
            old = _read_text(path)
            if old is None:
                return {"kind": "diff", "text": unified_diff(path, "", a.get("content") or ""),
                        "note": "Creates a new file."}
            new = (old + (a.get("content") or "")) if a.get("append") else (a.get("content") or "")
            return {"kind": "diff", "text": unified_diff(path, old, new),
                    "note": "Adds to the end of the file." if a.get("append") else "Replaces the file's contents."}
        if tool == "edit_file" and a.get("filename"):
            path = os.path.abspath(os.path.expanduser(a["filename"]))
            old = _read_text(path)
            if old is None:
                return {"kind": "text", "text": f"'{path}' could not be read - the edit will fail."}
            find, repl = match_line_endings(old, a.get("old_text") or "", a.get("new_text") or "")
            if not find or find not in old:
                return {"kind": "text", "text": "The text to replace was not found - the edit will fail."}
            return {"kind": "diff", "text": unified_diff(path, old, old.replace(find, repl, 1))}
        if tool == "apply_patch" and patch_preview:
            return patch_preview(a)
        if tool == "delete_path" and a.get("path"):
            return {"kind": "text", "text": "Will delete: " + _describe_path(os.path.abspath(os.path.expanduser(a["path"])))}
        if tool in ("move_path", "copy_path") and a.get("destination"):
            dst = os.path.abspath(os.path.expanduser(a["destination"]))
            if os.path.lexists(dst):
                return {"kind": "text", "text": ("Replaces: " if a.get("overwrite") else "Already exists (will fail unless overwrite): ")
                        + _describe_path(dst)}
    except OSError as e:
        return {"kind": "text", "text": f"Preview unavailable: {e}"}
    return None


def _show(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def describe(tool: str, args: dict, justification: str = "", tier: str = "benign",
             undoable: bool = False, mutating: bool = True, patch_preview=None) -> dict:
    """Everything the approval UI shows for one action - complete, untruncated."""
    shown = {k: _show(v) for k, v in (args or {}).items() if k != "justification" and v not in (None, "", [])}
    target = target_path(tool, args)
    return {
        "tool": tool,
        "label": friendly(tool),
        "args": shown,
        "justification": justification or (args or {}).get("justification") or "",
        "tier": tier,
        "undoable": bool(undoable),
        "undo_text": WORDING["undoable"] if undoable else (
            (WORDING["not_undoable"] + (f" here - {UNDO_ELSEWHERE[tool]}" if tool in UNDO_ELSEWHERE else ""))
            if mutating else ""),
        "preview": preview(tool, args, patch_preview),
        # What the file looked like when the preview was made: if it changes
        # before the approved action runs, the action is not run (tools/approvals.py _changed_since_approval).
        "target": target,
        "fingerprint": _fingerprint(target) if target else None,
    }


def _fingerprint(path: str):
    import undo_store
    return undo_store.fingerprint(path)

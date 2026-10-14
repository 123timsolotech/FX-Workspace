"""
tools/files.py
==============
File helpers shared by tools: paths, the undo journal, atomic writes, secret files.
(Split out of main.py in Phase 2 - see ARCHITECTURE.md D14.)
"""
from __future__ import annotations

import os
import re
import undo_store

import core.config as config


def safe_path(filename: str) -> str:
    return os.path.abspath(os.path.expanduser(filename))


# ---- undo trail (undo_store.py) -------------------------------------------------
UNDO_DIR = os.path.join(config.BASE_DIR, "data", "undo")
undo = undo_store.UndoStore(UNDO_DIR)


def _journal():
    """The undo journal of the tool call running right now (opened by the
    ToolExecutor), or a no-op journal outside the executor."""
    return undo.active() or undo_store.NO_JOURNAL


_SECRET_FILE_RE = re.compile(
    r"^(keys?[._-].*\.(bat|cmd|ps1|txt)|\.env(\..*)?|.*\.env|.*secrets?.*|.*credentials?.*|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?"
    r"|.*\.(pem|key|pfx|p12|kdbx|keystore))$", re.I)


# The app's own private files: keys, settings, MCP server secrets (env values,
# headers) and the assistant browser's profile (cookies, sessions).
_APP_PRIVATE_FILES = ("keys.local.bat", "keys_local.bat", "settings.json", os.path.join("data", "mcp_servers.json"),
                      os.path.join("data", "search_keys.json"))
_APP_PRIVATE_DIRS = (os.path.join("data", "browser-profile"),)


def _is_secret_file(path: str) -> bool:
    """Files read_file (an auto-approved tool) must not hand to the model -
    and content searches must not probe."""
    path = path or "."
    # NTFS stream suffixes (including ::$DATA) name the same protected file.
    # Check both the supplied name and the resolved target of a link.
    if os.name == "nt":
        drive, tail = os.path.splitdrive(path)
        path = drive + tail.split(":", 1)[0]
    full = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    if any(_SECRET_FILE_RE.match(os.path.basename(p)) for p in (path, full)):
        return True
    base = os.path.normcase(os.path.realpath(config.BASE_DIR))
    if any(full == os.path.join(base, os.path.normcase(f)) for f in _APP_PRIVATE_FILES):
        return True
    return any(full == d or full.startswith(d + os.sep)
               for d in (os.path.join(base, os.path.normcase(x)) for x in _APP_PRIVATE_DIRS))


def _match_line_endings(content: str, old: str, new: str) -> tuple:
    """edit_file on a CRLF file: the model writes '\\n'; match the file's '\\r\\n'
    (files are read with newline='' so their endings are kept as they are)."""
    if "\r\n" in content and "\r\n" not in old and "\n" in old:
        return old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
    return old, new


def _atomic_write(path: str, text: str):
    """Write the whole file or nothing: a failure mid-write leaves the old file."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

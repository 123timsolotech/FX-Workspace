"""
data_files.py
=============
Settings > Data and files (1.4.4): everything this app keeps on this PC, folder by folder - how big it is, a button
that opens it in File Explorer (to look, back up or customise by hand), and Clear for the kinds that can go.

  * Clear sends the files to the Windows Recycle Bin by default (Windows' own delete, so they can be put back);
    "skip the Recycle Bin" deletes them for good. Nothing outside data\\ is ever touched: every path is resolved and
    checked to be inside it first.
  * Chats: all of them, or all but the pinned ones; the chat open now is closed with them. The app's own trash of
    deleted chats is a kind of its own.
  * Kinds the app needs while it runs (memory, the window's cache, settings) are open-only here; their own pages
    clear them (Memory, Privacy).
  * Never while a reply runs, never from the phone (remote_access blocks these endpoints).
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# id: (label, what it is, paths inside data\ (globs), clearable, note shown when clearing)
KINDS = {
    "chats": ("Chats", "Every saved conversation.", ["chats/*.json"], True, "Pinned chats can be kept."),
    "chat_trash": ("Deleted chats", "Chats you deleted - kept here until cleared.", ["chats/trash/*"], True, ""),
    "images": ("Images", "Pictures made on this PC and the gallery.", ["images/**"], True, ""),
    "videos": ("Videos", "Videos made on this PC.", ["videos/**"], True, ""),
    "music": ("Music", "Songs made in the Music studio.", ["music/**"], True, ""),
    "uploads": ("Attached files", "Files you attached to messages.", ["uploads/**"], True, "Chats that used them keep their text."),
    "research": ("Research notes", "Notes of Research runs, used to resume one.", ["research/**"], True, ""),
    "runs": ("Run history", "What each task did, step by step.", ["runs/**"], True, ""),
    "osint": ("Lookups", "Saved results of OSINT lookups.", ["osint/**"], True, ""),
    "logs": ("Logs", "The app's and llama-server's logs.", ["logs/**"], True, "Logs in use start again empty."),
    "update_backups": ("Update backups", "Copies kept from before updates.", ["update-backups/**"], True, ""),
    "browser": ("The assistant's browser", "Its cookies, sign-ins and cache.", ["browser-profile/**"], True,
                "Sites it was signed in to will ask again."),
    "voice": ("Voice", "Speech models and recordings.", ["voice/**"], True, "Voice models download again when next used."),
    "memory": ("Memory and profile", "What it remembers about you (Settings > Memory clears it).",
               ["memory/**", "memory.json", "profile.json", "identity/**"], False, ""),
    "window": ("App window cache", "The desktop window's own cache (in use while the app runs).",
               ["app-window-profile/**", "overlay-window-profile/**"], False, ""),
}
ORDER = list(KINDS)


def _inside(path: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(DATA)
    return real != base and os.path.commonpath([real, base]) == base


def files_of(kind: str) -> list[str]:
    out = []
    for pattern in KINDS[kind][2]:
        for p in glob.glob(os.path.join(DATA, pattern.replace("/", os.sep)), recursive=True):
            if os.path.isfile(p) and _inside(p):
                out.append(p)
    if kind == "chats":
        out = [p for p in out if os.path.dirname(p) == os.path.join(DATA, "chats")]
    return out


def folder_of(kind: str) -> str:
    first = KINDS[kind][2][0].split("/")[0]
    path = os.path.join(DATA, first)
    return path if os.path.isdir(path) else DATA


def summary() -> dict:
    kinds, total = [], 0
    for k in ORDER:
        label, what, _, clearable, note = KINDS[k]
        size, n, newest = 0, 0, 0.0
        for p in files_of(k):
            try:
                st = os.stat(p)
            except OSError:
                continue
            size += st.st_size
            n += 1
            newest = max(newest, st.st_mtime)
        total += size
        kinds.append({"id": k, "label": label, "what": what, "bytes": size, "files": n, "updated": newest,
                      "clearable": clearable, "note": note, "folder": folder_of(k)})
    return {"data_dir": DATA, "kinds": kinds, "bytes": total}


def open_folder(kind: str | None = None) -> bool:
    path = folder_of(kind) if kind in KINDS else DATA
    os.makedirs(path, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)                                 # File Explorer
        return True
    subprocess.Popen(["xdg-open", path])
    return True


def _recycle(paths: list[str]) -> bool:
    """Windows' own delete to the Recycle Bin (SHFileOperation with FOF_ALLOWUNDO), no dialogs."""
    if sys.platform != "win32" or not paths:
        return not paths

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    for i in range(0, len(paths), 200):                   # one call per batch: pFrom is one long string
        op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom="\0".join(paths[i:i + 200]) + "\0\0",
                             fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI)
        if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) != 0 or op.fAnyOperationsAborted:
            return False
    return True


def _pinned(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            return bool(json.load(f).get("pinned"))
    except (OSError, ValueError, AttributeError):
        return False


def clear(kind: str, permanent: bool = False, keep_pinned: bool = False) -> dict:
    """Remove a kind's files. Returns {"ok", "files", "bytes", "skipped", "message"}."""
    if kind not in KINDS or not KINDS[kind][3]:
        return {"ok": False, "message": "That kind of data can't be cleared here."}
    paths = files_of(kind)
    if kind == "chats" and keep_pinned:
        paths = [p for p in paths if not _pinned(p)]
    busy, ok_paths = [], []
    for p in paths:                                         # a file another program holds open can't go
        try:
            with open(p, "ab"):
                pass
            ok_paths.append(p)
        except OSError:
            busy.append(p)
    size = sum(os.path.getsize(p) for p in ok_paths if os.path.exists(p))
    if permanent:
        for p in ok_paths:
            try:
                os.remove(p)
            except OSError:
                busy.append(p)
    elif not _recycle(ok_paths):
        return {"ok": False, "message": "Windows couldn't move the files to the Recycle Bin. Nothing was deleted - "
                                        "try again, or tick \"skip the Recycle Bin\"."}
    _prune_empty(kind)
    gone = len(ok_paths) - (len(busy) if permanent else 0)
    where = "deleted for good" if permanent else "moved to the Recycle Bin"
    msg = f"{gone} file{'s' if gone != 1 else ''} ({size / 2 ** 20:.1f} MB) {where}."
    if busy:
        msg += f" {len(busy)} in use stayed."
    return {"ok": True, "files": gone, "bytes": size, "skipped": len(busy), "message": msg, "at": time.time()}


def _prune_empty(kind: str):
    """Empty folders a clear left behind (inside the kind's own folder, never data\\ itself)."""
    top = folder_of(kind)
    if top == DATA or not _inside(top):
        return
    for dirpath, dirnames, filenames in os.walk(top, topdown=False):
        if dirpath != top and not os.listdir(dirpath) and _inside(dirpath):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

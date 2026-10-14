"""
fs_tools.py
===========
General-purpose filesystem and archive tools: copy/move/rename/delete,
zip/unzip, search, and file/drive info. Independent of the desktop-
automation layers (elements.py/desktop_router.py) - these operate on the
filesystem directly, never through a UI, so they work identically whether
or not any window is open.

Every function returns {"ok": bool, "message": str}, matching the
convention every tool handler in tools/dispatch.py execute_tool() already uses.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import time
import zipfile

from undo_store import NO_JOURNAL

IS_WINDOWS = hasattr(os, "startfile")


def _safe_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


# Every mutating function takes a `journal` (undo_store.Change) that records
# what it is about to change, so the tool call can be undone - and rolled back
# if it fails halfway. Without one (direct calls) behaviour is unchanged.
def _parents_to_create(path: str) -> list:
    """Folders that os.makedirs(path) would create, outermost first."""
    missing = []
    while path and not os.path.exists(path):
        missing.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return list(reversed(missing))


def _makedirs(path: str, journal) -> None:
    for folder in _parents_to_create(path):
        os.mkdir(folder)
        journal.created_dir(folder)


def copy_path(source: str, destination: str, overwrite: bool = False, journal=None) -> dict:
    journal = journal or NO_JOURNAL
    src, dst = _safe_path(source), _safe_path(destination)
    if not os.path.exists(src):
        return {"ok": False, "message": f"Source not found: '{src}'."}
    if os.path.exists(dst) and not overwrite:
        return {"ok": False, "message": f"Destination already exists: '{dst}'. Pass overwrite=true to replace it."}
    try:
        # The old destination is moved aside (kept for undo), never destroyed
        # before the copy has succeeded.
        if os.path.exists(dst):
            journal.trash(dst)
        _makedirs(os.path.dirname(dst) or ".", journal)
        if os.path.isdir(src):
            journal.created_tree(dst)                    # recorded first: a half-done copy is rolled back too
            shutil.copytree(src, dst)
        else:
            journal.backup_file(dst)                     # absent now: undo removes the copy
            shutil.copy2(src, dst)
        return {"ok": True, "message": f"Copied '{src}' to '{dst}'."}
    except Exception as e:
        return {"ok": False, "message": f"Copy failed: {e}"}


def move_path(source: str, destination: str, overwrite: bool = False, journal=None) -> dict:
    journal = journal or NO_JOURNAL
    src, dst = _safe_path(source), _safe_path(destination)
    if not os.path.exists(src):
        return {"ok": False, "message": f"Source not found: '{src}'."}
    if os.path.exists(dst):
        if not overwrite:
            return {"ok": False, "message": f"Destination already exists: '{dst}'. Pass overwrite=true to replace it."}
        try:
            journal.trash(dst)
        except Exception as e:
            return {"ok": False, "message": f"Could not clear the existing destination: {e}"}
    try:
        _makedirs(os.path.dirname(dst) or ".", journal)
        shutil.move(src, dst)
        journal.record_move(src, dst)
        return {"ok": True, "message": f"Moved '{src}' to '{dst}'."}
    except Exception as e:
        # A move across drives copies then deletes; if it failed partway, the
        # source is still there and a partial copy sits at dst - the rollback
        # sets that partial copy aside.
        if os.path.exists(src) and os.path.lexists(dst):
            journal.created_tree(dst)
        return {"ok": False, "message": f"Move failed: {e}"}


def rename_path(path: str, new_name: str, journal=None) -> dict:
    journal = journal or NO_JOURNAL
    if not new_name or os.sep in new_name or (os.altsep and os.altsep in new_name):
        return {"ok": False, "message": "new_name must be a plain file/folder name, not a path."}
    src = _safe_path(path)
    if not os.path.exists(src):
        return {"ok": False, "message": f"Not found: '{src}'."}
    dst = os.path.join(os.path.dirname(src), new_name)
    if os.path.exists(dst):
        return {"ok": False, "message": f"'{dst}' already exists."}
    try:
        os.rename(src, dst)
        journal.record_move(src, dst)
        return {"ok": True, "message": f"Renamed '{src}' to '{dst}'."}
    except Exception as e:
        return {"ok": False, "message": f"Rename failed: {e}"}


def delete_path(path: str, force: bool = False, journal=None) -> dict:
    journal = journal or NO_JOURNAL
    target = _safe_path(path)
    if not os.path.exists(target):
        return {"ok": False, "message": f"Not found: '{target}'."}
    is_dir = os.path.isdir(target)
    if is_dir and not force:
        try:
            non_empty = bool(os.listdir(target))
        except Exception:
            non_empty = True
        if non_empty:
            return {"ok": False, "message": (
                f"'{target}' is a non-empty folder. Pass force=true to delete it and everything inside.")}
    try:
        # With a journal the item is moved into the undo area, so it can be restored.
        kept = journal.trash(target)
        return {"ok": True, "message": f"Deleted '{target}'." + (" It can be restored with Undo." if kept else "")}
    except Exception as e:
        return {"ok": False, "message": f"Delete failed: {e}"}


def make_dir(path: str, journal=None) -> dict:
    journal = journal or NO_JOURNAL
    target = _safe_path(path)
    try:
        _makedirs(target, journal)
        return {"ok": True, "message": f"Ensured directory exists: '{target}'."}
    except Exception as e:
        return {"ok": False, "message": f"Could not create '{target}': {e}"}


def zip_paths(paths: list, output_zip: str, journal=None) -> dict:
    journal = journal or NO_JOURNAL
    if not paths:
        return {"ok": False, "message": "No paths given to archive."}
    out = _safe_path(output_zip)
    resolved = [_safe_path(p) for p in paths]
    missing = [p for p in resolved if not os.path.exists(p)]
    if missing:
        return {"ok": False, "message": "Not found: " + ", ".join(missing)}
    try:
        _makedirs(os.path.dirname(out) or ".", journal)
        journal.backup_file(out)
        count = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for full in resolved:
                if os.path.isdir(full):
                    base = os.path.dirname(full)
                    for root, _dirs, files in os.walk(full):
                        for name in files:
                            file_path = os.path.join(root, name)
                            zf.write(file_path, os.path.relpath(file_path, base))
                            count += 1
                else:
                    zf.write(full, os.path.basename(full))
                    count += 1
        return {"ok": True, "message": f"Wrote {count} file(s) into '{out}'."}
    except Exception as e:
        return {"ok": False, "message": f"Zip failed: {e}"}


def unzip_file(zip_path: str, destination: str, journal=None) -> dict:
    journal = journal or NO_JOURNAL
    src, dst = _safe_path(zip_path), _safe_path(destination)
    if not os.path.isfile(src):
        return {"ok": False, "message": f"Zip not found: '{src}'."}
    try:
        _makedirs(dst, journal)
        with zipfile.ZipFile(src) as zf:
            names = zf.namelist()
            # A zip is untrusted input: refuse any entry that would escape
            # 'dst' via '..' or an absolute path (a 'zip slip' attack)
            # instead of extracting it and finding out afterward.
            dst_abs = os.path.abspath(dst)
            for name in names:
                member = os.path.abspath(os.path.join(dst, name))
                if member != dst_abs and not member.startswith(dst_abs + os.sep):
                    return {"ok": False, "message": f"Refused to extract unsafe entry '{name}' (path traversal)."}
            # Journal every folder and file the extraction creates or replaces.
            for info in zf.infolist():
                target = os.path.abspath(os.path.join(dst, info.filename))
                _makedirs(target if info.is_dir() else os.path.dirname(target), journal)
                if not info.is_dir():
                    journal.backup_file(target)
            zf.extractall(dst)
        return {"ok": True, "message": f"Extracted {len(names)} entr{'y' if len(names) == 1 else 'ies'} from '{src}' to '{dst}'."}
    except zipfile.BadZipFile:
        return {"ok": False, "message": f"'{src}' is not a valid zip file."}
    except Exception as e:
        return {"ok": False, "message": f"Unzip failed: {e}"}


def search_files(directory: str, pattern: str = "*", content_query: str = None, max_results: int = 50,
                 private=None) -> dict:
    """`private(path)` -> True for files whose contents must not be searched
    (secrets): a content query would otherwise probe them a guess at a time."""
    root = _safe_path(directory)
    if not os.path.isdir(root):
        return {"ok": False, "message": f"Not a directory: '{root}'."}
    max_results = max(1, min(int(max_results or 50), 500))
    query = (content_query or "").strip()
    hits = []
    try:
        for base, _dirs, files in os.walk(root):
            for name in files:
                if not fnmatch.fnmatch(name.lower(), (pattern or "*").lower()):
                    continue
                full = os.path.join(base, name)
                if query:
                    if private is not None and private(full):
                        continue
                    try:
                        with open(full, "r", encoding="utf-8", errors="ignore") as f:
                            if query.lower() not in f.read(200_000).lower():
                                continue
                    except Exception:
                        continue
                hits.append(full)
                if len(hits) >= max_results:
                    break
            if len(hits) >= max_results:
                break
    except Exception as e:
        return {"ok": False, "message": f"Search failed: {e}"}
    if not hits:
        suffix = f" containing '{content_query}'" if query else ""
        return {"ok": False, "message": f"No files matched '{pattern}'{suffix} under '{root}'."}
    return {"ok": True, "message": f"{len(hits)} match(es):\n" + "\n".join(hits)}


def file_info(path: str) -> dict:
    target = _safe_path(path)
    if not os.path.exists(target):
        return {"ok": False, "message": f"Not found: '{target}'."}
    st = os.stat(target)
    is_dir = os.path.isdir(target)
    if is_dir:
        size = 0
        for d, _dirs, files in os.walk(target):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(d, f))
                except OSError:
                    pass
    else:
        size = st.st_size
    kind = "directory" if is_dir else "file"
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
    return {"ok": True, "message": f"'{target}' is a {kind}, {size:,} bytes, modified {when}."}


def list_drives() -> dict:
    parts = []
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil:
        for p in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(p.mountpoint)
                parts.append(f"{p.device} ({p.mountpoint}): {usage.free / (1024**3):.1f} GB free of {usage.total / (1024**3):.1f} GB")
            except Exception:
                parts.append(f"{p.device} ({p.mountpoint})")
    elif IS_WINDOWS:
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                try:
                    usage = shutil.disk_usage(drive)
                    parts.append(f"{drive}: {usage.free / (1024**3):.1f} GB free of {usage.total / (1024**3):.1f} GB")
                except Exception:
                    parts.append(drive)
    if not parts:
        return {"ok": False, "message": "No drives could be enumerated."}
    return {"ok": True, "message": "\n".join(parts)}


def disk_usage(path: str = ".") -> dict:
    target = _safe_path(path)
    try:
        usage = shutil.disk_usage(target)
        return {"ok": True, "message": (
            f"'{target}': {usage.free / (1024**3):.1f} GB free, "
            f"{usage.used / (1024**3):.1f} GB used, {usage.total / (1024**3):.1f} GB total.")}
    except Exception as e:
        return {"ok": False, "message": f"Could not read disk usage for '{target}': {e}"}


def compare_files(path_a: str, path_b: str) -> dict:
    a, b = _safe_path(path_a), _safe_path(path_b)
    for p in (a, b):
        if not os.path.isfile(p):
            return {"ok": False, "message": f"Not a file: '{p}'."}
    size_a, size_b = os.path.getsize(a), os.path.getsize(b)
    if size_a != size_b:
        return {"ok": True, "message": f"Different: sizes differ ({size_a:,} vs {size_b:,} bytes)."}

    def digest(p):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    same = digest(a) == digest(b)
    return {"ok": True, "message": "Identical (same size and SHA-256)." if same else "Different: same size, different content."}

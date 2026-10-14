"""
undo_store.py
=============
The undo trail for every tool that changes files.

How it works
  * The ToolExecutor opens a Change before an undoable tool runs and makes it
    the *active journal*. The tool records what it is about to do through it:
        backup_file(path)   copy the current file first (or note that it is absent)
        trash(path)         move a file/folder into the backup area instead of deleting it
        record_move(a, b)   note that a was moved/renamed to b
        created_dir(path)   note a folder that did not exist before
        external(adapter, ref, before, after)
                            an artifact inside another app (a Roblox script): its
                            content before the change is kept here, and the app's
                            adapter (app_registry.AppAdapter) fingerprints and
                            restores it
  * Tool succeeded  -> commit(): fingerprints of the result are stored, so a later
                       undo can tell whether someone changed the file since.
  * Tool failed / was cancelled / raised -> rollback(): the journal is replayed in
                       reverse, so nothing is left half-applied.
  * undo(change_id) replays a committed change in reverse. If a path changed after
    the tool ran, undo refuses unless force=True (it never silently overwrites
    newer work).

Where the backups live (documented for the user)
  data/undo/<change_id>/...      the backed-up files and trashed items
  data/undo/manifest.json        one record per change (newest last)

Retention
  Oldest changes are pruned first once there are more than 300 changes, a change
  is older than 14 days, or the backups exceed 2 GB. Pruning a change deletes its
  backups for good, like emptying a recycle bin. The newest change is never
  pruned, so its Undo always works (even when it alone exceeds 2 GB).
  Deleting on the same drive as data/undo moves the item (instant); on another
  drive it is copied then removed, which takes as long as copying it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid

MAX_CHANGES = 300
MAX_AGE_S = 14 * 24 * 3600
MAX_BYTES = 2 * 1024 ** 3
HASH_LIMIT = 64 * 1024 ** 2          # files larger than this are fingerprinted by size+mtime
BIG_FILE = 1024 ** 3                 # backing up a file larger than this moves it instead of copying


class UndoConflict(Exception):
    """The path changed after the tool ran; undo would overwrite newer work."""


# External artifacts: adapter id -> object with fingerprint(ref) and
# restore(ref, content, expected) (app_registry.AppAdapter). Registered by the
# adapter module; an undo whose adapter is missing or disconnected fails with
# a message and changes nothing.
EXTERNAL: dict = {}


def register_external(adapter_id: str, handler):
    EXTERNAL[adapter_id] = handler


def content_fingerprint(text: str | None) -> str | None:
    """The fingerprint adapters use for text content (e.g. script source)."""
    if text is None:
        return None
    return "sha1:" + hashlib.sha1(text.encode("utf-8")).hexdigest()


def _external(op: dict):
    handler = EXTERNAL.get(op.get("adapter"))
    if handler is None:
        raise OSError(f"the {op.get('adapter')} integration is not loaded")
    return handler


def fingerprint(path: str):
    try:
        if not os.path.lexists(path):
            return None
        if os.path.isdir(path):
            return "dir"
        st = os.stat(path)
        if st.st_size > HASH_LIMIT:
            return f"size:{st.st_size}:mtime:{int(st.st_mtime)}"
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return "sha1:" + h.hexdigest()
    except OSError:
        return "unreadable"


def _size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


class Change:
    """One tool call's journal. Operations are applied by the tool itself;
    the journal only makes them reversible."""

    def __init__(self, store: "UndoStore", change_id: str, tool: str, summary: str):
        self.store, self.id, self.tool, self.summary = store, change_id, tool, summary
        self.ops: list[dict] = []
        self.dir = os.path.join(store.root, change_id)

    def _slot(self) -> str:
        os.makedirs(self.dir, exist_ok=True)
        return os.path.join(self.dir, f"{len(self.ops):03d}")

    def backup_file(self, path: str, replacing: bool = True):
        """Call before changing a file. Idempotent per path. `replacing=False`
        (an append, an in-place edit) keeps the original where it is."""
        path = os.path.abspath(path)
        if any(op["op"] == "file" and op["path"] == path for op in self.ops):
            return
        if os.path.isdir(path):
            raise IsADirectoryError(f"'{path}' is a folder, not a file.")
        backup = None
        if os.path.lexists(path):
            backup = self._slot()
            if replacing and os.path.getsize(path) > BIG_FILE:
                shutil.move(path, backup)       # it is about to be replaced anyway: no copy
                self.ops.append({"op": "file", "path": path, "backup": backup, "moved": True})
                return
            shutil.copy2(path, backup)
        self.ops.append({"op": "file", "path": path, "backup": backup})

    def trash(self, path: str) -> str:
        """Move path into the backup area instead of deleting it. Returns the backup location."""
        path = os.path.abspath(path)
        backup = self._slot()
        shutil.move(path, backup)
        self.ops.append({"op": "trash", "path": path, "backup": backup})
        return backup

    def record_move(self, src: str, dst: str):
        self.ops.append({"op": "move", "src": os.path.abspath(src), "dst": os.path.abspath(dst)})

    def created_dir(self, path: str):
        self.ops.append({"op": "mkdir", "path": os.path.abspath(path)})

    def created_tree(self, path: str):
        """A whole folder tree the tool created (e.g. a copied folder)."""
        self.ops.append({"op": "tree", "path": os.path.abspath(path)})

    def external(self, adapter: str, ref: str, before: str | None, after: str | None, display: str = ""):
        """Call before changing an artifact that lives in another app.
        `before`: its content now (None: the change creates it). `after`: the
        content the change will write (None: the change removes it). Only the
        fingerprint of `after` is kept - it tells a rollback whether the
        write actually landed, so a failed call never overwrites anything."""
        backup = None
        if before is not None:
            backup = self._slot() + ".txt"
            with open(backup, "w", encoding="utf-8", newline="") as f:
                f.write(before)
        self.ops.append({"op": "external", "adapter": adapter, "ref": ref, "backup": backup,
                         "after_fp": content_fingerprint(after), "display": display or ref})

    # ---- replay -----------------------------------------------------------
    def paths(self) -> list:
        out = []
        for op in self.ops:
            out += [op[k] for k in ("path", "src", "dst") if k in op]
        return out

    def _after_state(self) -> dict:
        state = {p: fingerprint(p) for p in self.paths()}
        for op in self.ops:
            if op["op"] == "external":          # what was written is what the tool meant to write
                state[f"external:{op['adapter']}:{op['ref']}"] = op.get("after_fp")
        return state

    def _reverse(self, check: dict | None, force: bool):
        """Undo every op, newest first. `check` = fingerprints recorded at commit."""
        if check and not force:
            changed = []
            for key, fp in check.items():
                if key.startswith("external:"):
                    _, adapter, ref = key.split(":", 2)
                    now = _external({"adapter": adapter}).fingerprint(ref)
                else:
                    now = fingerprint(key)
                if now != fp:
                    changed.append(key.split(":", 2)[2] if key.startswith("external:") else key)
            if changed:
                raise UndoConflict("Changed since the tool ran: " + ", ".join(changed[:5]))
        for op in reversed(self.ops):
            kind = op["op"]
            if kind == "external":
                handler = _external(op)
                current = handler.fingerprint(op["ref"])
                if check is None and current != op.get("after_fp"):
                    continue                    # rollback: the write never landed - leave it alone
                content = None
                if op.get("backup"):
                    with open(op["backup"], "r", encoding="utf-8", newline="") as f:
                        content = f.read()
                handler.restore(op["ref"], content, current)
                continue
            if kind == "file":
                path, backup = op["path"], op["backup"]
                if backup is None:                          # the tool created it
                    if os.path.lexists(path):
                        shutil.move(path, self._slot() + ".created")   # kept, not destroyed
                else:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    tmp = path + ".undo-tmp"
                    shutil.copy2(backup, tmp)
                    os.replace(tmp, path)
            elif kind == "trash":
                if os.path.lexists(op["path"]):
                    raise UndoConflict(f"'{op['path']}' exists again; not overwriting it.")
                os.makedirs(os.path.dirname(op["path"]) or ".", exist_ok=True)
                shutil.move(op["backup"], op["path"])
            elif kind == "move":
                if os.path.lexists(op["src"]):
                    raise UndoConflict(f"'{op['src']}' exists again; not overwriting it.")
                if not os.path.lexists(op["dst"]):
                    raise UndoConflict(f"'{op['dst']}' is gone; nothing to move back.")
                shutil.move(op["dst"], op["src"])
            elif kind == "mkdir":
                try:
                    os.rmdir(op["path"])                     # only if still empty
                except OSError:
                    pass
            elif kind == "tree":
                if os.path.lexists(op["path"]):
                    shutil.move(op["path"], self._slot() + ".created")   # kept, not destroyed


class UndoStore:
    def __init__(self, root: str):
        self.root = root
        self.manifest_path = os.path.join(root, "manifest.json")
        self._lock = threading.RLock()
        # One journaled tool call at a time: held from begin() to commit()/rollback().
        self._run_lock = threading.Lock()
        self._active: Change | None = None
        self._records: list | None = None

    # ---- manifest -----------------------------------------------------------
    def _load(self) -> list:
        if self._records is None:
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._records = data if isinstance(data, list) else []
            except (OSError, ValueError):
                self._records = []
        return self._records

    def _save(self):
        os.makedirs(self.root, exist_ok=True)
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._records or [], f, indent=1)
        os.replace(tmp, self.manifest_path)

    def states(self, change_ids) -> dict:
        """change id -> "applied" / "undone" / other status, or None when it is
        no longer in the trail (pruned). For past runs' Undo buttons."""
        wanted = set(change_ids)
        with self._lock:
            found = {r["id"]: r.get("status") for r in self._load() if r["id"] in wanted}
        return {cid: found.get(cid) for cid in wanted}

    def _find(self, change_id: str) -> dict | None:
        return next((r for r in self._load() if r["id"] == change_id), None)

    # ---- lifecycle (called by ToolExecutor) ------------------------------------
    def begin(self, tool: str, summary: str) -> Change:
        if not self._run_lock.acquire(timeout=120):
            raise TimeoutError("another file change is still in progress")
        change = Change(self, time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6], tool, summary)
        with self._lock:
            self._active = change
        return change

    def active(self) -> Change | None:
        return self._active

    def _end(self, change: Change):
        with self._lock:
            if self._active is change:
                self._active = None
                try:
                    self._run_lock.release()
                except RuntimeError:
                    pass

    def commit(self, change: Change) -> str | None:
        """Keep the change if the tool touched anything; returns its id."""
        self._end(change)
        with self._lock:
            if not change.ops:
                shutil.rmtree(change.dir, ignore_errors=True)
                return None
            size = _size(change.dir) if os.path.isdir(change.dir) else 0
            self._load().append({"id": change.id, "at": time.time(), "tool": change.tool,
                                 "summary": change.summary, "ops": change.ops, "bytes": size,
                                 "after": change._after_state(), "status": "applied"})
            self._prune(keep=change.id)
            self._save()
            return change.id

    def rollback(self, change: Change) -> bool:
        """Undo whatever a failed/cancelled tool already did. Never raises."""
        self._end(change)
        ok = True
        try:
            change._reverse(None, force=True)
        except Exception:
            ok = False
        if ok:
            shutil.rmtree(change.dir, ignore_errors=True)
        else:                                            # keep the backups for manual recovery
            with self._lock:
                self._load().append({"id": change.id, "at": time.time(), "tool": change.tool,
                                     "summary": change.summary + " (rollback incomplete)", "ops": change.ops,
                                     "after": {}, "status": "rollback_failed"})
                self._save()
        return ok

    # ---- user-facing -----------------------------------------------------------
    def undo(self, change_id: str, force: bool = False) -> dict:
        if not self._run_lock.acquire(timeout=30):
            return {"ok": False, "message": "A file change is in progress; try Undo again in a moment."}
        try:
            return self._undo(change_id, force)
        finally:
            self._run_lock.release()

    def _undo(self, change_id: str, force: bool) -> dict:
        with self._lock:
            rec = self._find(change_id)
            if rec is None:
                return {"ok": False, "message": "That change is no longer in the undo trail."}
            if rec["status"] != "applied":
                return {"ok": False, "message": f"That change is already {rec['status'].replace('_', ' ')}."}
            change = Change(self, rec["id"], rec["tool"], rec["summary"])
            change.ops = rec["ops"]
            try:
                change._reverse(rec.get("after") or {}, force)
            except UndoConflict as e:
                return {"ok": False, "conflict": True, "message": f"Not undone: {e} Use force to undo anyway."}
            except OSError as e:
                return {"ok": False, "message": f"Undo failed: {e}"}
            rec["status"] = "undone"
            rec["undone_at"] = time.time()
            self._save()
            return {"ok": True, "message": f"Undid: {rec['summary']}", "id": change_id}

    def undo_last(self, force: bool = False) -> dict:
        with self._lock:
            rec = next((r for r in reversed(self._load()) if r["status"] == "applied"), None)
        if rec is None:
            return {"ok": False, "message": "There is nothing to undo."}
        return self.undo(rec["id"], force)

    def list(self, limit: int = 50) -> list:
        with self._lock:
            out = []
            for r in reversed(self._load()[-limit:]):
                out.append({k: r.get(k) for k in ("id", "at", "tool", "summary", "status", "undone_at")}
                           | {"paths": sorted({p for op in r["ops"] for k, p in op.items() if k in ("path", "src", "dst")}
                                              | {op.get("display") or op["ref"] for op in r["ops"] if op["op"] == "external"})})
            return out

    def _prune(self, keep: str | None = None):
        """Drop the oldest changes past the limits. The change just committed
        (`keep`) is never dropped: its Undo button must work even when that one
        change alone is larger than the size limit."""
        records = self._load()
        now = time.time()

        def oldest_droppable():
            return next((i for i, r in enumerate(records) if r["id"] != keep), None)
        while len(records) > MAX_CHANGES or (records and now - records[0]["at"] > MAX_AGE_S and records[0]["id"] != keep):
            i = oldest_droppable()
            if i is None:
                break
            self._drop(records.pop(i))
        total = sum(r.get("bytes", 0) for r in records)
        while total > MAX_BYTES:
            i = oldest_droppable()
            if i is None:
                break
            gone = records.pop(i)
            total -= gone.get("bytes", 0)
            self._drop(gone)

    def _drop(self, rec: dict):
        shutil.rmtree(os.path.join(self.root, rec["id"]), ignore_errors=True)


class _NoJournal:
    """Used when a file tool runs outside the executor (unit tests, scripts):
    operations behave exactly as before, just without a backup."""

    def backup_file(self, path, replacing=True): pass
    def record_move(self, src, dst): pass
    def created_dir(self, path): pass
    def created_tree(self, path): pass
    def external(self, adapter, ref, before, after, display=""): pass

    def trash(self, path):
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        return None


NO_JOURNAL = _NoJournal()

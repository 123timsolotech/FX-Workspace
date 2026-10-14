"""
projects.py
===========
Projects: a named place for related chats, a folder of files, standing
instructions, notes and (optionally) a Roblox place. Stored one JSON file per
project in data/projects/<id>.json.

    {"version": 1, "id", "name", "folder", "files": [pinned file paths],
     "instructions", "notes", "roblox_place", "effort": level | null,
     "prompt_prep": mode | null, "created", "updated"}

A project gives the assistant CONTEXT, never authority: its folder is only
read for the file index (index_folder), its instructions are the user's own
words, and its effort / Prompt Prep are just the defaults for its runs. It
does not widen folder access, grant a permission or skip an approval
(non-negotiable 7) - per-folder permission rules are separate and explicit
(permissions.py).

Context assembly (build_context) picks, within a size budget set by effort:
the instructions and notes, the file index (tree, README, languages), the
lines around words from the request in the project's files, and a short
summary of the project's recent chats. File text is framed as untrusted data
(untrusted.py). The same cached index serves any folder a request mentions
outside a project.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid

import effort as effort_mod
import prompt_prep
import secret_mask
import atomic_io

_ID = re.compile(r"^[a-f0-9]{12}$")
MAX_TEXT = 8000                       # instructions / notes

# Characters of project context per run, by effort level (= budget_for(the
# level's default profile); used when no profile is at hand).
BUDGET = {"low": 1500, "normal": 4000, "high": 8000, "extra": 12000, "max": 16000, "ultra": 16000}

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".idea", ".vscode",
             "dist", "build", ".next", ".cache", "target", "bin", "obj", ".pytest_cache", ".mypy_cache"}
TEXT_EXT = {".py", ".lua", ".luau", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".html", ".css", ".toml",
            ".yaml", ".yml", ".ini", ".cfg", ".cs", ".cpp", ".c", ".h", ".hpp", ".java", ".kt", ".go", ".rs", ".rb",
            ".php", ".sh", ".ps1", ".bat", ".sql", ".xml", ".rbxmx", ".csv"}
LANG = {".py": "Python", ".lua": "Lua", ".luau": "Luau", ".js": "JavaScript", ".ts": "TypeScript",
        ".tsx": "TypeScript", ".jsx": "JavaScript", ".html": "HTML", ".css": "CSS", ".cs": "C#", ".cpp": "C++",
        ".c": "C", ".h": "C/C++", ".java": "Java", ".kt": "Kotlin", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
        ".php": "PHP", ".ps1": "PowerShell", ".sh": "Shell", ".sql": "SQL", ".rbxl": "Roblox place",
        ".rbxlx": "Roblox place", ".rbxm": "Roblox model", ".rbxmx": "Roblox model", ".md": "Markdown"}
MAX_INDEX_FILES = 4000
MAX_DEPTH = 6
INDEX_TTL_S = 300                     # a cached index is rebuilt at most every 5 minutes
MAX_SCAN_BYTES = 256 * 1024


# ==============================================================================
# Project records
# ==============================================================================
class ProjectStore:
    def __init__(self, root: str):
        self.root = root
        self.trash = os.path.join(root, "trash")
        self._lock = threading.Lock()

    def _path(self, pid: str) -> str:
        if not _ID.match(pid or ""):
            raise KeyError(pid)
        return os.path.join(self.root, f"{pid}.json")

    def get(self, pid: str) -> dict | None:
        try:
            path = self._path(pid)
            with self._lock:            # never read half a replace (and never block one on Windows)
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            return data if isinstance(data, dict) and data.get("id") == pid else None
        except (OSError, ValueError, KeyError):
            return None

    def list(self) -> list:
        try:
            names = sorted(n for n in os.listdir(self.root) if n.endswith(".json"))
        except OSError:
            return []
        rows = [p for p in (self.get(n[:-5]) for n in names if _ID.match(n[:-5])) if p]
        rows.sort(key=lambda p: p.get("updated") or 0, reverse=True)
        return rows

    def _write(self, project: dict) -> dict:
        path = self._path(project["id"])
        with self._lock:
            os.makedirs(self.root, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(project, f, indent=2, ensure_ascii=False)
            atomic_io.replace(tmp, path)
        return project

    def create(self, fields: dict) -> dict:
        now = time.time()
        project = {"version": 1, "id": uuid.uuid4().hex[:12], "created": now, "updated": now,
                   "name": "", "folder": "", "files": [], "instructions": "", "notes": "", "roblox_place": "",
                   "effort": None, "prompt_prep": None}
        project.update(clean_fields(fields, creating=True))
        return self._write(project)

    def update(self, pid: str, fields: dict) -> dict:
        project = self.get(pid)
        if project is None:
            raise KeyError(pid)
        project.update(clean_fields(fields))
        project["updated"] = time.time()
        return self._write(project)

    def search(self, query: str = "", limit: int = 200) -> list:
        """Newest first; every word of `query` must appear in the name, folder,
        instructions, notes or Roblox place."""
        words = [w for w in re.findall(r"\w+", (query or "").casefold()) if w]
        rows = []
        for p in self.list():
            full = " ".join(str(p.get(k) or "") for k in ("name", "folder", "roblox_place", "instructions", "notes"))
            text = full.casefold()
            if words and not all(w in text for w in words):
                continue
            row = dict(p)
            if words:
                i = text.find(words[0])
                row["snippet"] = full[max(0, i - 60): i + 120].strip()
            rows.append(row)
        return rows[:limit]

    def delete(self, pid: str) -> bool:
        """Moves the project file to data/projects/trash/. Its chats stay, and
        its folder is never touched."""
        try:
            path = self._path(pid)
        except KeyError:
            return False
        with self._lock:
            if not os.path.exists(path):
                return False
            os.makedirs(self.trash, exist_ok=True)
            os.replace(path, os.path.join(self.trash, f"{pid}-{int(time.time())}.json"))
        return True


def clean_fields(fields: dict, creating: bool = False) -> dict:
    """Validate what the user typed. Raises ValueError with a readable reason."""
    out = {}
    f = fields or {}
    if "name" in f or creating:
        name = re.sub(r"\s+", " ", str(f.get("name") or "")).strip()[:80]
        if not name:
            raise ValueError("Give the project a name.")
        out["name"] = name
    if "folder" in f:
        folder = str(f.get("folder") or "").strip().strip('"')
        if folder:
            folder = os.path.abspath(os.path.expanduser(folder))
            if not os.path.isdir(folder):
                raise ValueError(f"{folder} is not an existing folder.")
            if not os.path.splitdrive(folder)[1].strip("\\/"):
                raise ValueError("A whole drive is too big for a project folder - pick a folder inside it.")
        out["folder"] = folder
    if "files" in f:
        files = [os.path.abspath(os.path.expanduser(str(p).strip().strip('"'))) for p in (f.get("files") or []) if str(p).strip()]
        out["files"] = [p for p in files if os.path.isfile(p)][:20]
    for key in ("instructions", "notes"):
        if key in f:
            out[key] = str(f.get(key) or "")[:MAX_TEXT]
    if "roblox_place" in f:
        out["roblox_place"] = str(f.get("roblox_place") or "").strip().strip("\"'“”").strip()[:200]   # "Scratch Test" typed with quotes
    if "effort" in f:
        out["effort"] = f["effort"] if f.get("effort") in effort_mod.CHOICES else None
    if "prompt_prep" in f:
        out["prompt_prep"] = f["prompt_prep"] if f.get("prompt_prep") in prompt_prep.MODES else None
    return out


# ==============================================================================
# Folder index (on demand, cached in data/index/)
# ==============================================================================
class FolderIndex:
    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self._mem: dict = {}

    def _cache_path(self, folder: str) -> str:
        key = hashlib.sha1(os.path.normcase(folder).encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.cache_dir, f"{key}.json")

    def get(self, folder: str, max_age: float = INDEX_TTL_S) -> dict | None:
        folder = os.path.abspath(folder)
        if not os.path.isdir(folder):
            return None
        now = time.time()
        cached = self._mem.get(folder)
        if cached is None:
            try:
                with open(self._cache_path(folder), "r", encoding="utf-8") as f:
                    cached = json.load(f)
            except (OSError, ValueError):
                cached = None
        if cached and now - (cached.get("built") or 0) < max_age:
            self._mem[folder] = cached
            return cached
        index = build_index(folder)
        self._mem[folder] = index
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = self._cache_path(folder) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False)
            os.replace(tmp, self._cache_path(folder))
        except OSError:
            pass
        return index


def build_index(folder: str, time_limit: float = 3.0) -> dict:
    """Walk the folder (bounded by depth, file count and time): relative
    paths with sizes, language counts and the README's opening."""
    started = time.time()
    files, langs, truncated = [], {}, False
    for dirpath, dirnames, filenames in os.walk(folder):
        rel_dir = os.path.relpath(dirpath, folder)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if depth >= MAX_DEPTH:
            dirnames[:] = []
        for name in sorted(filenames):
            if len(files) >= MAX_INDEX_FILES or time.time() - started > time_limit:
                truncated = True
                break
            path = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            rel = os.path.normpath(os.path.join(rel_dir, name)) if rel_dir != "." else name
            ext = os.path.splitext(name)[1].lower()
            files.append({"path": rel, "size": size})
            if ext in LANG:
                langs[LANG[ext]] = langs.get(LANG[ext], 0) + 1
        if truncated:
            break
    readme = ""
    for name in ("README.md", "README.txt", "README", "readme.md"):
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    readme = f.read(3000)
            except OSError:
                pass
            break
    return {"folder": folder, "built": time.time(), "files": files, "languages": langs,
            "readme": readme, "truncated": truncated}


def _tree(index: dict, budget: int) -> str:
    lines, used = [], 0
    for f in index["files"]:
        line = f"{f['path']} ({_size(f['size'])})"
        if used + len(line) + 1 > budget:
            lines.append(f"... and {len(index['files']) - len(lines)} more files")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:.1f} MB"


_STOP = set("the and for with that this from into what when your you are was can could would should please make "
            "file files folder project open fix add use using need want".split())


def _snippets(index: dict, request: str, budget: int, max_files: int = 3) -> list:
    """Lines around the request's words in the project's text files."""
    words = [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", request or "") if w.lower() not in _STOP][:8]
    if not words:
        return []
    pattern = re.compile("|".join(re.escape(w) for w in words), re.I)
    scored = []
    from tools.files import _is_secret_file         # call-time: keys files never reach the brief
    for f in index["files"]:
        ext = os.path.splitext(f["path"])[1].lower()
        if ext not in TEXT_EXT or f["size"] > MAX_SCAN_BYTES:
            continue
        name_hits = len(pattern.findall(f["path"]))
        path = os.path.join(index["folder"], f["path"])
        if _is_secret_file(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        hits = pattern.findall(text)
        if not hits and not name_hits:
            continue
        scored.append((name_hits * 5 + len(set(h.lower() for h in hits)) * 3 + min(len(hits), 20), f["path"], text))
    scored.sort(key=lambda s: -s[0])
    out, used = [], 0
    for _score, rel, text in scored[:max_files]:
        lines = text.splitlines()
        hit = next((i for i, l in enumerate(lines) if pattern.search(l)), 0)
        lo, hi = max(0, hit - 6), min(len(lines), hit + 14)
        # A key pasted into a project file is still hidden (secret_mask - the one rule).
        body = secret_mask.mask("\n".join(f"{i + 1}: {lines[i]}" for i in range(lo, hi)))
        if used + len(body) > budget:
            break
        out.append({"path": rel, "lines": f"{lo + 1}-{hi}", "text": body})
        used += len(body)
    return out


_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s\"'<>|?*]+")


def mentioned_folders(request: str) -> list:
    """Existing folders named with a full path in the request."""
    out = []
    for raw in _WIN_PATH.findall(request or ""):
        path = raw.rstrip(".,;:)")
        if os.path.isdir(path) and os.path.splitdrive(path)[1].strip("\\/") and path not in out:
            out.append(path)
    return out[:2]


def budget_for(profile) -> int:
    """Characters of project context for an effort profile: a quarter of its
    context-selection size (tokens), so a per-model override of
    `context_tokens` moves it too; capped so the brief never crowds out the
    conversation."""
    tokens = getattr(profile, "context_tokens", None)
    if not isinstance(tokens, int) or tokens <= 0:
        return BUDGET.get(getattr(profile, "name", "normal"), BUDGET["normal"])
    return max(1000, min(16000, tokens // 4))


def build_context(project: dict | None, request: str, level: str, index: FolderIndex,
                  recent_chats: list | None = None, frame=None, budget: int | None = None) -> tuple[str, dict]:
    """(text for the task brief, report of what was included). `frame(source,
    text)` wraps file content as untrusted data. `budget` (characters)
    defaults to the level's BUDGET."""
    budget = budget or BUDGET.get(level, BUDGET["normal"])
    frame = frame or (lambda source, text: text)
    parts, report = [], {"project": None, "included": [], "folders": []}
    if project:
        report["project"] = {"id": project.get("id"), "name": project.get("name")}
        head = [f"ACTIVE PROJECT: {project.get('name')}"]
        if project.get("roblox_place"):
            head.append(f"Roblox place: {project['roblox_place']}")
        if project.get("folder"):
            head.append(f"Project folder: {project['folder']}")
        parts.append("\n".join(head))
        if project.get("instructions"):
            text = project["instructions"][: budget // 3]
            parts.append("PROJECT INSTRUCTIONS (written by the user):\n" + text)
            report["included"].append("instructions")
        if project.get("notes"):
            text = project["notes"][: budget // 5]
            parts.append("PROJECT NOTES (written by the user):\n" + text)
            report["included"].append("notes")
        # Pinned files: in every chat of the project, sharing a third of the budget.
        pinned = [p for p in project.get("files") or [] if os.path.isfile(p)]
        if pinned:
            each = max(400, (budget // 3) // len(pinned))
            for path in pinned:
                try:
                    with open(path, "rb") as fh:
                        text = fh.read(each * 2).decode("utf-8", errors="replace")[:each]
                except OSError:
                    continue
                parts.append(f"PINNED FILE {path}:\n" + frame(f"the pinned file {os.path.basename(path)}", text))
                report["included"].append(f"pinned: {os.path.basename(path)}")
    folders = []
    if project and project.get("folder"):
        folders.append(project["folder"])
    for f in mentioned_folders(request):
        if os.path.normcase(f) not in {os.path.normcase(x) for x in folders}:
            folders.append(f)
    for folder in folders:
        remaining = budget - sum(len(p) for p in parts)
        if remaining < 300:
            break
        idx = index.get(folder)
        if not idx:
            continue
        langs = ", ".join(f"{k} ({v})" for k, v in sorted(idx["languages"].items(), key=lambda kv: -kv[1])[:6])
        tree = _tree(idx, min(remaining // 3, 2500))
        block = [f"FILE INDEX of {folder}: {len(idx['files'])} files{' (partial)' if idx['truncated'] else ''}"
                 + (f"; languages: {langs}" if langs else ""), tree]
        report["folders"].append({"folder": folder, "files": len(idx["files"])})
        if idx.get("readme"):
            block.append(frame(f"README in {folder}", idx["readme"][: min(1500, remaining // 4)]))
            report["included"].append("readme")
        snips = _snippets(idx, request, max(0, remaining // 3))
        for s in snips:
            block.append(frame(f"{s['path']} lines {s['lines']}", s["text"]))
        if snips:
            report["included"].append("snippets: " + ", ".join(s["path"] for s in snips))
        parts.append("\n".join(block))
    if project and recent_chats:
        lines = [f"- {c.get('title')}: {str(c.get('last_reply') or '').splitlines()[0][:160] if c.get('last_reply') else ''}"
                 for c in recent_chats[:3]]
        text = "RECENT CHATS IN THIS PROJECT:\n" + "\n".join(lines)
        if sum(len(p) for p in parts) + len(text) <= budget:
            parts.append(text)
            report["included"].append(f"{len(lines)} recent chats")
    text = "\n\n".join(parts)
    return text[:budget + 2000], report

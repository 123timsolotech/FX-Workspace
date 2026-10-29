"""
knowledge.py
============
Your files as a knowledge base: pick folders in Settings > Knowledge, and the assistant can search them
(kb_search) and answer with the file and lines it used - the feature every comparable app leads with
(AnythingLLM, Open WebUI, Jan), done fully offline.

  Index    data/knowledge.db - SQLite's full-text index (FTS5, BM25 ranking). No model is needed to build or
           search it, so it never takes the GPU from the chat model, and an 8 GB card loses nothing. Files are
           cut into passages of about CHUNK characters along paragraph lines, each with its line numbers.
  Files    text and code (.md .txt .py .lua ...), PDF (pdf_text.py), Word (.docx) and HTML. Big files, binaries,
           .git / node_modules / venvs are skipped. Re-indexing reads only files whose size or time changed.
  Search   every word must match (AND), falling back to any word (OR) when that finds nothing; prefix matching for
           longer words. Results are passages with path, lines and a highlighted snippet.

Everything the index returns is the user's own file content - still framed as untrusted data when the model reads
it (agent/loop.py frames every tool result), since a downloaded file can carry instructions.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import zipfile

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "knowledge.db")
CHUNK = 1400
MAX_FILE = 15 * 2 ** 20
MAX_FILES = 50_000
TEXT_EXT = {".md", ".markdown", ".txt", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini",
            ".cfg", ".xml", ".py", ".js", ".ts", ".tsx", ".jsx", ".lua", ".luau", ".java", ".c", ".h", ".cpp", ".hpp",
            ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".ps1", ".bat", ".sql", ".css", ".scss", ".tex", ".srt", ".vtt"}
DOC_EXT = {".pdf", ".docx", ".html", ".htm"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".idea", ".vs", "dist", "build", ".next",
             "$recycle.bin", "system volume information", "appdata"}
_lock = threading.RLock()
_state = {"state": "idle", "done": 0, "total": 0, "folder": "", "error": "", "at": None}
_cancel = threading.Event()


def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, folder TEXT, size INTEGER, mtime REAL, chunks INTEGER, error TEXT)")
    c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(text, path UNINDEXED, line_from UNINDEXED, "
              "line_to UNINDEXED, title, tokenize='unicode61 remove_diacritics 2')")
    return c


# ---- reading files -----------------------------------------------------------------------------------------------
def _docx_text(data: bytes) -> str:
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html
    return html.unescape(text)


def _html_text(raw: str) -> str:
    import html
    t = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", "", raw)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", t)
    return html.unescape(re.sub(r"<[^>]+>", "", t))


def read_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        data = f.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("over 15 MB")
    if ext == ".pdf":
        import pdf_text
        return pdf_text.extract(data).get("text", "")
    if ext == ".docx":
        return _docx_text(data)
    if b"\x00" in data[:4096]:
        raise ValueError("binary")
    text = data.decode("utf-8", "replace")
    return _html_text(text) if ext in (".html", ".htm") else text


def chunks(text: str) -> list[tuple[int, int, str]]:
    """(first line, last line, text) passages of about CHUNK characters, cut at blank lines when possible."""
    lines = text.replace("\r\n", "\n").split("\n")
    out, buf, start, size = [], [], 1, 0
    for i, line in enumerate(lines, 1):
        if not buf:
            start = i
        buf.append(line)
        size += len(line) + 1
        if size >= CHUNK and (not line.strip() or size >= CHUNK * 1.6):
            body = "\n".join(buf).strip()
            if body:
                out.append((start, i, body))
            buf, size = [], 0
    body = "\n".join(buf).strip()
    if body:
        out.append((start, len(lines), body))
    return out


def _wanted(name: str) -> bool:
    ext = os.path.splitext(name)[1].lower()
    return ext in TEXT_EXT or ext in DOC_EXT


def _allowed(path: str, folder: str | None = None) -> bool:
    from tools.files import _is_secret_file
    if _is_secret_file(path):
        return False
    if folder:
        root, target = os.path.realpath(folder), os.path.realpath(path)
        try:
            return os.path.commonpath([root, target]) == root
        except ValueError:
            return False
    return True


# ---- indexing ----------------------------------------------------------------------------------------------------
def status() -> dict:
    with _lock:
        st = dict(_state)
    try:
        c = _db()
        rows = c.execute("SELECT folder, COUNT(*), SUM(chunks), SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) FROM files GROUP BY folder").fetchall()
        c.close()
    except sqlite3.Error:
        rows = []
    st["folders"] = {r[0]: {"files": r[1], "passages": r[2] or 0, "skipped": r[3] or 0} for r in rows}
    return st


def _walk(folder: str):
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for n in names:
            path = os.path.join(root, n)
            if _wanted(n) and _allowed(path, folder):
                yield path


def index(folders: list[str]) -> None:
    """Brings the index in line with these folders (run on a thread: start())."""
    folders = [os.path.abspath(f) for f in folders if f]
    c = _db()
    try:
        known = {r[0]: (r[1], r[2], r[3]) for r in c.execute("SELECT path, size, mtime, folder FROM files")}
        # Files of folders no longer chosen, or deleted since: out of the index.
        keep = set()
        todo = []
        for folder in folders:
            if not os.path.isdir(folder):
                continue
            for p in _walk(folder):
                if _cancel.is_set():
                    with _lock:
                        _state.update(state="cancelled", at=time.time())
                    return  # an incomplete scan must not purge unvisited files
                if len(keep) >= MAX_FILES:
                    break
                if p in keep:
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size > MAX_FILE:
                    continue
                keep.add(p)
                old = known.get(p)
                if not old or old[0] != st.st_size or old[1] != st.st_mtime:
                    todo.append((folder, p, st.st_size, st.st_mtime))
        gone = [p for p in known if p not in keep]
        with c:
            for p in gone:
                c.execute("DELETE FROM chunks WHERE path = ?", (p,))
                c.execute("DELETE FROM files WHERE path = ?", (p,))
        with _lock:
            _state.update(state="running", done=0, total=len(todo), error="")
        completed = 0
        for i, (folder, p, size, mtime) in enumerate(todo):
            if _cancel.is_set():
                break
            with _lock:
                _state.update(done=i, folder=folder, file=os.path.basename(p))
            err, parts = "", []
            try:
                if not _allowed(p, folder):
                    raise ValueError("File is outside the selected folder or protected.")
                parts = chunks(read_text(p))
            except Exception as e:
                err = str(e)[:120] or type(e).__name__
            title = os.path.basename(p)
            with c:
                c.execute("DELETE FROM chunks WHERE path = ?", (p,))
                c.executemany("INSERT INTO chunks (text, path, line_from, line_to, title) VALUES (?, ?, ?, ?, ?)",
                              [(t, p, a, b, title) for a, b, t in parts])
                c.execute("INSERT OR REPLACE INTO files (path, folder, size, mtime, chunks, error) VALUES (?, ?, ?, ?, ?, ?)",
                          (p, folder, size, mtime, len(parts), err))
            completed += 1
        with _lock:
            _state.update(state="idle" if not _cancel.is_set() else "cancelled", done=completed, at=time.time(), file="")
    except Exception as e:
        with _lock:
            _state.update(state="failed", error=str(e)[:300])
    finally:
        c.close()


def start(folders: list[str]) -> dict:
    with _lock:
        if _state["state"] == "running":
            return {"ok": False, "message": "Already indexing."}
        _state.update(state="running", done=0, total=0, error="")
        _cancel.clear()
    threading.Thread(target=index, args=(list(folders),), name="knowledge-index", daemon=True).start()
    return {"ok": True}


def cancel() -> None:
    _cancel.set()


def forget(folder: str) -> None:
    folder = os.path.abspath(folder)
    c = _db()
    try:
        with c:
            paths = [r[0] for r in c.execute("SELECT path FROM files WHERE folder = ?", (folder,))]
            for p in paths:
                c.execute("DELETE FROM chunks WHERE path = ?", (p,))
            c.execute("DELETE FROM files WHERE folder = ?", (folder,))
    finally:
        c.close()


# ---- searching ---------------------------------------------------------------------------------------------------
def _fts_query(q: str, mode: str) -> str:
    words = re.findall(r"\w+", q.lower(), re.UNICODE)
    stop = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "are", "what", "how", "my", "me", "i",
            "do", "does", "where", "which", "about", "with", "from", "that", "this", "it", "find", "show", "file", "files"}
    terms = [w for w in words if w not in stop] or words
    terms = [f'"{w}"*' if len(w) >= 4 else f'"{w}"' for w in terms[:12]]
    return (" AND " if mode == "and" else " OR ").join(terms)


def search(query: str, limit: int = 8, folder: str | None = None) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    c = _db()
    try:
        for mode in ("and", "or"):
            q = _fts_query(query, mode)
            if not q:
                return []
            sql = ("SELECT chunks.path, line_from, line_to, title, snippet(chunks, 0, '[[', ']]', ' … ', 40), bm25(chunks, 1.0, 0, 0, 0, 2.0), text, files.folder "
                   "FROM chunks JOIN (SELECT path, folder FROM files) AS files ON chunks.path = files.path WHERE chunks MATCH ?"
                   + (" AND chunks.path LIKE ? ESCAPE '\\'" if folder else "") + " ORDER BY bm25(chunks, 1.0, 0, 0, 0, 2.0) LIMIT ?")
            prefix = (os.path.join(os.path.abspath(folder), "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                      if folder else "")
            args = [q] + ([prefix + "%"] if folder else []) + [max(1, min(int(limit), 100))]
            try:
                rows = c.execute(sql, args).fetchall()
            except sqlite3.OperationalError:
                rows = []
            rows = [r for r in rows if os.path.isfile(r[0]) and _allowed(r[0], r[7])]
            if rows:
                return [{"path": r[0], "lines": [r[1], r[2]], "file": r[3], "snippet": r[4], "score": round(-r[5], 2),
                         "text": r[6]} for r in rows]
        return []
    finally:
        c.close()


# ---- the assistant's tool ------------------------------------------------------------------------------------------
TOOLS = [
    {"type": "function", "function": {"name": "kb_search", "description": "Searches the user's knowledge base - the folders of their own files they chose in Settings > Knowledge (notes, documents, PDFs, code). Returns passages with the file path and line numbers. Use it for questions about their files, projects or notes; cite the file you used.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Key words to find, not a whole question."}, "limit": {"type": "integer"}}, "required": ["query"]}}},
]
TOOL_NAMES = {"kb_search"}


def run_tool(name: str, args: dict) -> dict:
    hits = search(str(args.get("query") or ""), max(1, min(int(args.get("limit") or 6), 12)))
    if not hits:
        return {"ok": True, "data": [], "message": "Nothing in the knowledge base matches. Try other words."}
    parts = [f"[{i}] {h['path']} (lines {h['lines'][0]}-{h['lines'][1]})\n{h['text'][:1600]}" for i, h in enumerate(hits, 1)]
    return {"ok": True, "data": [{k: v for k, v in h.items() if k != "text"} for h in hits],
            "message": "\n\n".join(parts) + "\n\nCite the file (and lines) you use."}

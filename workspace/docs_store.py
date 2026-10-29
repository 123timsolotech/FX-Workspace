"""
docs_store.py
=============
Documents (the Docs place): writing in Markdown with the model as an editor - select text and say what to change,
proofread the whole document, or continue from the cursor. Every AI change is shown as a suggestion (old -> new)
that you accept or reject; nothing is rewritten behind your back. Versions are kept like Odysseus' documents:
saves within VERSION_COALESCE seconds of the last one update that version (typing doesn't make a version per
keystroke); otherwise a new one. Restoring copies an old version forward.

Stored under data/docs/:
  index.json            {"version": 1, "docs": [{id, title, created, updated, words, versions}]}
  <id>.md               the current text
  <id>.versions.json    [{n, at, summary, content}] - the last MAX_VERSIONS
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

import atomic_io

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "docs")
VERSION_COALESCE = 60
MAX_VERSIONS = 50
MAX_DOC = 1_000_000
_ID_RX = re.compile(r"^[0-9a-f]{12}$")
_lock = threading.Lock()

EDIT_SYSTEM = ("You are an editor working inside a document. You get the text around a selection, the selected text "
               "and an instruction. Reply with ONLY the replacement for the selected text - no quotes, no preamble, no "
               "explanation - in the same language and Markdown style as the document, unless the instruction asks "
               "otherwise.")
CONTINUE_SYSTEM = ("You are a co-writer. Continue the document from where the text before the cursor ends, in the same "
                   "voice, language and Markdown style. Reply with ONLY the new text to insert (a paragraph or two "
                   "unless the instruction says otherwise).")
PROOF_SYSTEM = ("You proofread documents. Find spelling, grammar, punctuation and clear wording mistakes. Reply with JSON: "
                '{"suggestions": [{"find": "<exact text from the document, short but unique>", "replace": "<corrected '
                'text>", "why": "<a few words>"}]}. "find" must be copied exactly from the document. At most 25 '
                "suggestions; an empty list when it's fine. Don't change meaning or style.")


def _p(*parts) -> str:
    return os.path.join(ROOT, *parts)


def _check(doc_id: str) -> str:
    if not _ID_RX.match(doc_id or ""):
        raise KeyError("No such document.")
    return doc_id


def _write(path: str, text: str) -> None:
    os.makedirs(ROOT, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    atomic_io.replace(tmp, path)


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _index() -> dict:
    d = _read_json(_p("index.json"), None)
    return d if isinstance(d, dict) and isinstance(d.get("docs"), list) else {"version": 1, "docs": []}


def _title_of(text: str) -> str:
    first = next((ln.strip().lstrip("#").strip() for ln in (text or "").splitlines() if ln.strip()), "")
    return first[:80] or "Untitled document"


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def list_docs() -> list[dict]:
    with _lock:
        return sorted(_index()["docs"], key=lambda x: -x.get("updated", 0))


def get(doc_id: str) -> dict:
    _check(doc_id)
    with _lock:
        meta = next((x for x in _index()["docs"] if x["id"] == doc_id), None)
        if meta is None:
            raise KeyError("No such document.")
        try:
            with open(_p(doc_id + ".md"), encoding="utf-8") as f:
                content = f.read()
        except OSError:
            content = ""
    return {**meta, "content": content}


def create(content: str = "", title: str = "") -> dict:
    content = (content or "")[:MAX_DOC]
    doc_id = uuid.uuid4().hex[:12]
    now = time.time()
    meta = {"id": doc_id, "title": (title or "").strip()[:120] or _title_of(content), "created": now, "updated": now,
            "words": _words(content), "versions": 1, "auto_title": not (title or "").strip()}
    with _lock:
        idx = _index()
        idx["docs"].append(meta)
        _write(_p(doc_id + ".md"), content)
        _write(_p(doc_id + ".versions.json"), json.dumps([{"n": 1, "at": now, "summary": "Created", "content": content}], ensure_ascii=False))
        _write(_p("index.json"), json.dumps(idx, ensure_ascii=False, indent=1))
    return {**meta, "content": content}


def save(doc_id: str, content: str | None = None, title: str | None = None, summary: str = "Edit",
         force_version: bool = False) -> dict:
    _check(doc_id)
    with _lock:
        idx = _index()
        meta = next((x for x in idx["docs"] if x["id"] == doc_id), None)
        if meta is None:
            raise KeyError("No such document.")
        now = time.time()
        if title is not None and title.strip():
            meta["title"], meta["auto_title"] = title.strip()[:120], False
        if content is not None:
            content = content[:MAX_DOC]
            vers = _read_json(_p(doc_id + ".versions.json"), [])
            last = vers[-1] if vers else None
            if last is None or last.get("content") != content or force_version:
                if last and not force_version and now - last["at"] < VERSION_COALESCE and last.get("summary") == summary:
                    last.update(at=now, content=content)
                else:
                    vers.append({"n": (last["n"] + 1) if last else 1, "at": now, "summary": summary[:80], "content": content})
                vers = vers[-MAX_VERSIONS:]
                _write(_p(doc_id + ".versions.json"), json.dumps(vers, ensure_ascii=False))
            _write(_p(doc_id + ".md"), content)
            meta["words"] = _words(content)
            meta["versions"] = vers[-1]["n"] if vers else 1
            if meta.get("auto_title", True) and title is None:
                meta["title"] = _title_of(content)
        meta["updated"] = now
        _write(_p("index.json"), json.dumps(idx, ensure_ascii=False, indent=1))
    return get(doc_id)


def delete(doc_id: str) -> bool:
    _check(doc_id)
    with _lock:
        idx = _index()
        n = len(idx["docs"])
        idx["docs"] = [x for x in idx["docs"] if x["id"] != doc_id]
        _write(_p("index.json"), json.dumps(idx, ensure_ascii=False, indent=1))
        for suffix in (".md", ".versions.json"):
            try:
                os.remove(_p(doc_id + suffix))
            except OSError:
                pass
        return len(idx["docs"]) != n


def versions(doc_id: str) -> list[dict]:
    _check(doc_id)
    with _lock:
        vers = _read_json(_p(doc_id + ".versions.json"), [])
    return [{"n": v["n"], "at": v["at"], "summary": v.get("summary", ""), "words": _words(v.get("content", "")),
             "content": v.get("content", "")} for v in reversed(vers)]


def restore(doc_id: str, n: int) -> dict:
    v = next((x for x in versions(doc_id) if x["n"] == int(n)), None)
    if v is None:
        raise KeyError("No such version.")
    return save(doc_id, v["content"], summary=f"Restored from version {n}", force_version=True)


# ---- the model as an editor ------------------------------------------------------------------------------------
def ai_edit(text_model, selection: str, instruction: str, before: str = "", after: str = "", model=None) -> dict:
    instruction = (instruction or "").strip()[:1000]
    if not instruction:
        raise ValueError("Say what to change.")
    if not (selection or "").strip():
        user = f"Text before the cursor:\n\"\"\"\n{(before or '')[-4000:]}\n\"\"\"\n\nInstruction: {instruction}"
        out = text_model(CONTINUE_SYSTEM, user, max_tokens=900, model=model, label="Writing in the document")
    else:
        user = (f"Text before the selection:\n\"\"\"\n{(before or '')[-2500:]}\n\"\"\"\n\nSELECTED TEXT:\n\"\"\"\n{selection[:12000]}\n\"\"\"\n\n"
                f"Text after the selection:\n\"\"\"\n{(after or '')[:1500]}\n\"\"\"\n\nInstruction: {instruction}")
        out = text_model(EDIT_SYSTEM, user, max_tokens=min(4000, 400 + len(selection) // 2), model=model, label="Editing the document")
    text = _unwrap(out.get("text", ""))
    return {"replacement": text, "model": out.get("model")}


def _unwrap(text: str) -> str:
    t = (text or "").strip("\n")
    m = re.fullmatch(r'\s*(?:"""|```(?:\w+)?)\n?(.*?)\n?(?:"""|```)\s*', t, re.S)
    return m.group(1) if m else t


def proofread(json_model, content: str) -> list[dict]:
    if not (content or "").strip():
        return []
    out = json_model(PROOF_SYSTEM, content[:20000], max_tokens=1800) or {}
    sugg = out.get("suggestions") if isinstance(out, dict) else None
    keep = []
    for s in sugg if isinstance(sugg, list) else []:
        if not isinstance(s, dict):
            continue
        find, rep = str(s.get("find") or ""), str(s.get("replace") or "")
        if find and find != rep and find in content:            # only edits that point at real text
            keep.append({"find": find, "replace": rep, "why": str(s.get("why") or "")[:120]})
    return keep[:25]


# ---- the assistant's tools -------------------------------------------------------------------------------------
TOOLS = [
    {"type": "function", "function": {"name": "doc_list", "description": "Lists the user's documents in the Docs place (id, title, words).", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "doc_read", "description": "Reads one of the user's documents by id.", "parameters": {"type": "object", "properties": {"doc_id": {"type": "string"}}, "required": ["doc_id"]}}},
    {"type": "function", "function": {"name": "doc_write", "description": "Creates a document in the Docs place (no doc_id), or replaces one's whole text (doc_id). The old text stays as a version. When the user asks for something as a doc or in Docs, write it here yourself - then say it's in Docs.", "parameters": {"type": "object", "properties": {"content": {"type": "string"}, "title": {"type": "string"}, "doc_id": {"type": "string"}, "justification": {"type": "string"}}, "required": ["content"]}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def run_tool(name: str, args: dict) -> dict:
    try:
        if name == "doc_list":
            docs = list_docs()
            return {"ok": True, "data": docs, "message": "\n".join(f"{d['id']}: {d['title']} ({d['words']} words)" for d in docs[:50]) or "No documents yet."}
        if name == "doc_read":
            d = get(str(args.get("doc_id") or ""))
            return {"ok": True, "data": {"id": d["id"], "title": d["title"]}, "message": f"{d['title']}\n\n{d['content'][:30000]}"}
        if name == "doc_write":
            doc_id = str(args.get("doc_id") or "").strip()
            if doc_id:
                d = save(doc_id, str(args.get("content") or ""), title=args.get("title") or None, summary="Written by the assistant", force_version=True)
            else:
                d = create(str(args.get("content") or ""), str(args.get("title") or ""))
            return {"ok": True, "verified": True, "data": {"id": d["id"], "title": d["title"]}, "message": f"Saved document {d['id']}: {d['title']} ({d['words']} words). It's in Docs."}
    except (KeyError, ValueError) as e:
        return {"ok": False, "status": "permanent_failure", "message": str(e).strip("'\"")}
    return {"ok": False, "status": "permanent_failure", "message": f"Unknown tool {name}."}

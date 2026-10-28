"""
memory_graph.py
===============
Saved memory as a linked set of notes (1.4.2), built on memory.py - which stays the store (approvals, undo and
relevance picking keep working exactly as before; data/memory.json is kept).

  notes        one plain-markdown note per topic in data/memory/ (front matter: title, topic, updated, links), each
               fact a bullet; notes that mention the same thing (a place, a project, a name) link to each other with
               [[Topic]]. Rewritten from the store after every change and on start - the first run is the move-over.
  write rules  in memory.MemoryStore.add: an exact repeat is not added again, and a fact about the same subject
               ("my GPU is ...") replaces the old value instead of sitting beside it (memory.subject_key).
  overview     overview_block(): topics and one line each - the start of every chat sees what it remembers about,
               never the whole memory (requests still get the matching entries, memory.relevant).
  upkeep       maintain(): drops duplicates and stale contradictions, unlinks memories from deleted projects, and
               rewrites the notes so no link is broken.
  graph        graph(): topics and the things they share, for the view in Settings > Memory.
"""
from __future__ import annotations

import os
import re
import time

import core.state as shared

GENERATOR = "fx-memory-notes"
OVERVIEW_MAX = 600
_STOPWORDS = {"I", "My", "The", "A", "An", "It", "This", "That", "When", "Always", "Never", "Use", "Don't", "Do",
              "Please", "Every", "In", "On", "At", "For", "And", "Or", "But", "If", "Our", "We", "You", "Your"}


def _entries(include_sensitive: bool | None = None) -> list:
    settings = shared.app_state.get("settings") or {}
    sens = bool(settings.get("memory_sensitive")) if include_sensitive is None else include_sensitive
    return [e for e in shared.memory_store.list_with_topics() if sens or not e.get("sensitive")]


def entities(text: str) -> set:
    """The names a fact mentions: Capitalised words and phrases, quoted names, file-ish names."""
    out = set()
    for m in re.finditer(r"\"([^\"]{2,40})\"|'([^']{2,40})'|\b([A-Z][\w-]+(?:\s+[A-Z][\w-]+){0,3})\b|\b([\w-]+\.(?:lua|py|rbxl|json|md))\b", text or ""):
        name = next(g for g in m.groups() if g)
        if name not in _STOPWORDS and not (m.start() == 0 and " " not in name and m.group(3)):
            out.add(name.strip())
    return out


def graph(include_sensitive: bool | None = None) -> dict:
    """{"nodes": [{id, label, kind, size}], "edges": [{a, b, weight}]} - topics, and the names shared by facts."""
    by_topic: dict = {}
    mentions: dict = {}
    for e in _entries(include_sensitive):
        t = e.get("topic") or "Other"
        by_topic.setdefault(t, []).append(e)
        for name in entities(e["text"]):
            mentions.setdefault(name, set()).add(t)
    nodes = [{"id": f"t:{t}", "label": t, "kind": "topic", "size": len(xs)} for t, xs in by_topic.items()]
    edges = []
    for name, topics in sorted(mentions.items()):
        count = sum(1 for e in _entries(include_sensitive) if name in entities(e["text"]))
        if count < 2 and len(topics) < 2:
            continue                                  # a name mentioned once links nothing
        nodes.append({"id": f"e:{name}", "label": name, "kind": "name", "size": count})
        edges += [{"a": f"e:{name}", "b": f"t:{t}", "weight": 1} for t in sorted(topics)]
    return {"nodes": nodes, "edges": edges}


def _links(topic: str, by_topic: dict) -> list:
    mine = set().union(*(entities(e["text"]) for e in by_topic.get(topic, []))) if by_topic.get(topic) else set()
    out = []
    for other, xs in by_topic.items():
        if other != topic and mine & set().union(*(entities(e["text"]) for e in xs)):
            out.append(other)
    return sorted(out)


def notes_dir() -> str:
    """data/memory/, beside the store's memory.json."""
    return os.path.join(os.path.dirname(os.path.abspath(shared.memory_store.path)), "memory")


def write_notes_if_stale() -> bool:
    """Rewrite the notes when memory.json changed since they were written (the first time: the move-over)."""
    try:
        src = os.path.getmtime(shared.memory_store.path)
    except OSError:
        return False
    marker = os.path.join(notes_dir(), ".written")
    try:
        with open(marker, encoding="utf-8") as f:
            if float(f.read().strip() or 0) >= src:
                return False
    except (OSError, ValueError):
        pass
    write_notes()
    with open(marker, "w", encoding="utf-8") as f:
        f.write(str(src))
    return True


def _slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-") or "other"


def write_notes() -> list:
    """Rewrite data/memory/*.md from the store (only the notes this module made). Returns the files written."""
    by_topic: dict = {}
    for e in _entries(include_sensitive=True):
        by_topic.setdefault(e.get("topic") or "Other", []).append(e)
    DIR = notes_dir()
    os.makedirs(DIR, exist_ok=True)
    written = []
    for topic, xs in sorted(by_topic.items()):
        links = _links(topic, by_topic)
        updated = max((e.get("updated") or 0) for e in xs)
        lines = ["---", f"title: {topic}", f"topic: {topic}", f"generator: {GENERATOR}",
                 f"updated: {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(updated))}",
                 f"links: [{', '.join(links)}]", "---", "", f"# {topic}", ""]
        for e in sorted(xs, key=lambda e: (not e.get("pinned"), -(e.get("updated") or 0))):
            flags = (" (pinned)" if e.get("pinned") else "") + (" (sensitive)" if e.get("sensitive") else "")
            lines.append(f"- {e['text']}{flags} <!-- id:{e['id']} -->")
        if links:
            lines += ["", "Related: " + ", ".join(f"[[{l}]]" for l in links)]
        path = os.path.join(DIR, _slug(topic) + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        written.append(path)
    keep = {os.path.basename(p) for p in written}
    for name in os.listdir(DIR):                     # a topic that emptied: its note goes, so no link points nowhere
        p = os.path.join(DIR, name)
        if name.endswith(".md") and name not in keep:
            try:
                with open(p, encoding="utf-8") as f:
                    mine = f"generator: {GENERATOR}" in f.read(400)
                if mine:
                    os.remove(p)
            except OSError:
                pass
    return written


def overview_block() -> str:
    """Topics and one line each, for the start of every chat - what it remembers about, not all of it."""
    settings = shared.app_state.get("settings") or {}
    if settings.get("memory_enabled") is False or settings.get("memory_overview") is False:
        return ""
    try:
        entries = _entries()
        write_notes_if_stale()
    except Exception:
        return ""
    if not entries:
        return ""
    by_topic: dict = {}
    for e in entries:
        by_topic.setdefault(e.get("topic") or "Other", []).append(e)
    lines, used = [], 0
    for topic, xs in sorted(by_topic.items(), key=lambda kv: -len(kv[1])):
        # Names and counts only: a memory's text goes with a request only when it matches it (memory.relevant) -
        # pinned ones excepted, which every request carries anyway.
        pinned = [e["text"] for e in xs if e.get("pinned")]
        line = f"- {topic}: {len(xs)} memor{'y' if len(xs) == 1 else 'ies'}" + (f" (pinned: {pinned[0][:90]})" if pinned else "")
        if used + len(line) > OVERVIEW_MAX:
            break
        lines.append(line)
        used += len(line)
    return ("MEMORY OVERVIEW - topics you have saved memories about (facts about the user and their work, not "
            "about you; the ones that match a request come with it):\n" + "\n".join(lines))


def maintain() -> dict:
    """Upkeep: duplicates and outdated contradictions out, memories of deleted projects kept but unlinked, notes
    rewritten (no broken links). Returns what it did."""
    import memory
    store = shared.memory_store
    entries = store.list()
    seen_text, seen_key = {}, {}
    removed, unlinked = [], 0
    projects = set()
    try:
        projects = {p["id"] for p in shared.project_store.list()}
    except Exception:
        projects = None
    for e in sorted(entries, key=lambda e: -(e.get("updated") or 0)):      # newest first: it wins
        norm = re.sub(r"[^\w]+", " ", e["text"].casefold()).strip()
        key = memory.subject_key(e["text"])
        scope = e.get("project") or None
        if (norm, scope) in seen_text or (key and (key, scope) in seen_key):
            removed.append(e)
            continue
        seen_text[(norm, scope)] = e["id"]
        if key:
            seen_key[(key, scope)] = e["id"]
    for e in removed:
        store.delete(e["id"])
    if projects is not None:
        for e in store.list():
            if e.get("project") and e["project"] not in projects:
                with store._lock:
                    all_ = store._load()
                    for x in all_:
                        if x["id"] == e["id"]:
                            x["project"] = None
                    store._save(all_)
                unlinked += 1
    notes = write_notes()
    return {"removed": [e["text"] for e in removed], "unlinked": unlinked, "notes": len(notes)}

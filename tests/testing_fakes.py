"""
testing_fakes.py
================
Test doubles. Imported by tests only - nothing in the shipped app imports this
module, and no setting or environment variable switches a fake on.

  * MockMessagingProvider - a deterministic in-memory messaging service (moved
    here from messaging.py in Phase 2, see ARCHITECTURE.md).
  * FakeStudio - a Roblox Studio + Agentic Bridge plugin stand-in: it speaks
    the plugin's v3 protocol to the real roblox_bridge endpoints (poll,
    result, context) over an in-memory place, so the Roblox tools, approvals
    and undo can be tested end to end without Studio.
"""
from __future__ import annotations

import itertools
import threading
import time

from messaging import CAPABILITIES, MessagingProvider


class MockMessagingProvider(MessagingProvider):
    """Deterministic in-memory provider. 'Sent' messages are appended to the
    conversation and to self.sent; nothing leaves the process."""
    id = "mock"
    name = "Demo inbox (local, fake)"
    capabilities = CAPABILITIES

    def __init__(self, conversations: dict | None = None, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self._ids = itertools.count(1)
        self.conversations = conversations if conversations is not None else {
            "c1": {"id": "c1", "title": "Alex", "messages": [
                {"from": "Alex", "text": "Are we still on for the playtest at 6?", "at": 1_700_000_000}]},
            "c2": {"id": "c2", "title": "Team build", "messages": [
                {"from": "Sam", "text": "KillBrick script is throwing errors again", "at": 1_700_000_100}]},
        }
        self.drafts: dict[str, dict] = {}
        self.sent: list[dict] = []

    def list_conversations(self, limit=20):
        with self._lock:
            return [{"id": c["id"], "title": c["title"],
                     "last": (c["messages"][-1]["text"] if c["messages"] else "")}
                    for c in list(self.conversations.values())[:limit]]

    def search(self, query, limit=20):
        q = (query or "").lower()
        with self._lock:
            hits = []
            for c in self.conversations.values():
                for m in c["messages"]:
                    if q and (q in m["text"].lower() or q in c["title"].lower()):
                        hits.append({"conversation_id": c["id"], "title": c["title"], "from": m["from"], "text": m["text"]})
            return hits[:limit]

    def read(self, conversation_id, limit=30):
        with self._lock:
            c = self.conversations.get(conversation_id)
            if not c:
                raise KeyError(f"No conversation '{conversation_id}'.")
            return {"id": c["id"], "title": c["title"], "messages": c["messages"][-limit:]}

    def create_draft(self, conversation_id, text):
        with self._lock:
            if conversation_id not in self.conversations:
                raise KeyError(f"No conversation '{conversation_id}'.")
            draft = {"id": f"d{next(self._ids)}", "conversation_id": conversation_id, "text": text,
                     "created": self._clock(), "sent": False}
            self.drafts[draft["id"]] = draft
            return dict(draft)

    def send_draft(self, draft_id):
        with self._lock:
            draft = self.drafts.get(draft_id)
            if not draft:
                raise KeyError(f"No draft '{draft_id}'.")
            if draft["sent"]:
                return {"id": draft_id, "already_sent": True}
            draft["sent"] = True
            msg = {"from": "You", "text": draft["text"], "at": self._clock()}
            self.conversations[draft["conversation_id"]]["messages"].append(msg)
            self.sent.append(dict(draft))
            return {"id": draft_id, "conversation_id": draft["conversation_id"], "sent": True}



# ==============================================================================
# FakeStudio
# ==============================================================================
class FakeStudio:
    """An edit-mode plugin copy over an in-memory place.

        studio = FakeStudio({"ServerScriptService/Lava": ("Script", "print(1)")}, place="Obby")
        studio.start(); ...; studio.stop()

    `play_errors` is what a playtest reports (list of {text, trace}), pushed
    the way the play-server copy of the plugin does. `edit_in_studio(path,
    text)` simulates the user typing in Studio."""

    SERVICES = ("Workspace", "ServerScriptService", "ReplicatedStorage", "StarterGui", "StarterPlayer")

    def __init__(self, scripts: dict | None = None, place: str = "Obby World", place_id: int = 42,
                 version: int = 3, instance: str | None = None, play_errors: list | None = None):
        import roblox_bridge
        self.bridge = roblox_bridge
        self.place, self.place_id, self.version = place, place_id, version
        self.instance = instance or f"fake-{place_id}"
        self.nodes = {name: {"className": name, "source": None} for name in self.SERVICES}
        for path, (cls, source) in (scripts or {}).items():
            self._add_parents(path)
            self.nodes[path] = {"className": cls, "source": source}
        self.play_errors = list(play_errors or [])
        self.output = []
        self.commands: list = []
        self._stop = threading.Event()
        self._thread = None

    def _add_parents(self, path):
        parts = path.split("/")
        for i in range(1, len(parts)):
            self.nodes.setdefault("/".join(parts[:i]), {"className": "Folder", "source": None})

    def _children(self, path):
        prefix = path + "/"
        return [p for p in self.nodes if p.startswith(prefix) and "/" not in p[len(prefix):]]

    def edit_in_studio(self, path, text):
        self.nodes[path]["source"] = text

    def source(self, path):
        node = self.nodes.get(path)
        return node["source"] if node else None

    # ---- the plugin's command handlers -------------------------------------
    def handle(self, cmd):
        t, p = cmd["type"], cmd.get("payload") or {}
        self.commands.append(t)
        if t == "ping":
            return True, {"version": self.version, "place": self.place}, "pong"
        if t == "get_script_source":
            node = self.nodes.get(p.get("path") or "")
            if not node or node["source"] is None:
                return False, None, f"No script at '{p.get('path')}'."
            return True, {"path": p["path"], "source": node["source"], "lineCount": node["source"].count("\n") + 1}, "ok"
        if t == "set_script_source":
            node = self.nodes.get(p.get("path") or "")
            if not node or node["source"] is None:
                return False, None, f"No script at '{p.get('path')}'."
            if isinstance(p.get("expected"), str) and node["source"] != p["expected"]:
                return False, None, "The script changed since the assistant read it (it was edited in Studio). Nothing was written - read it again."
            node["source"] = p["source"]
            return True, {"path": p["path"]}, "Updated " + p["path"]
        if t == "create_script":
            path = f"{p['parentPath']}/{p['name']}" if p.get("parentPath") else p["name"]
            self._add_parents(path)
            self.nodes[path] = {"className": p.get("className") or "Script", "source": p.get("source") or ""}
            return True, {"path": path}, "Created " + path
        if t == "delete_instance":
            path = p.get("path") or ""
            if path not in self.nodes:
                return False, None, f"No instance at path '{path}'."
            for k in [k for k in self.nodes if k == path or k.startswith(path + "/")]:
                del self.nodes[k]
            return True, {"path": path}, f"Deleted {path} (Ctrl+Z restores it)."
        if t == "list_children":
            kids = self._children(p.get("path") or "")
            return True, [{"name": k.rsplit("/", 1)[-1], "className": self.nodes[k]["className"], "path": k,
                           "childCount": len(self._children(k))} for k in kids], f"{len(kids)} children"
        if t == "get_tree":
            depth_max, items = int(p.get("depth") or 3), []
            roots = [p["root"]] if p.get("root") else list(self.SERVICES)

            def walk(path, depth):
                items.append({"path": path, "className": self.nodes[path]["className"], "depth": depth,
                              "childCount": len(self._children(path))})
                if depth + 1 < depth_max:
                    for kid in sorted(self._children(path)):
                        walk(kid, depth + 1)
            for r in roots:
                if r in self.nodes:
                    walk(r, 0)
            return True, items[: int(p.get("limit") or 200)], f"{len(items)} item(s)."
        if t == "get_output":
            return True, list(self.output), f"{len(self.output)} output line(s)."
        if t == "run_playtest":
            # What the play-server copy does: push its errors as role play-server, then the test ends.
            self.bridge.record_context({"role": "play-server", "instance": self.instance + "-server",
                                        "version": self.version, "place": self.place, "errors": self.play_errors})
            return True, {"ended": True, "mode": p.get("mode"), "result": {"errors": len(self.play_errors)}}, "Play test finished."
        return False, None, f"Plugin does not know command '{t}'. Update the plugin."

    # ---- transport (what the Lua pollLoop / contextLoop do) ------------------
    def push_context(self, **extra):
        self.bridge.record_context({"version": self.version, "role": "edit", "instance": self.instance,
                                    "place": self.place, "placeId": self.place_id, "selection": [],
                                    "running": False, **extra})

    def _loop(self):
        while not self._stop.is_set():
            cmd = self.bridge.poll(wait=0.2, version=self.version, instance=self.instance)
            if cmd and cmd.get("id"):
                ok, data, message = self.handle(cmd)
                self.bridge.post_result(self.bridge.ResultIn(id=cmd["id"], ok=ok, message=message, data=data,
                                                             instance=self.instance))

    def start(self):
        self.push_context()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"fake-studio-{self.instance}")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

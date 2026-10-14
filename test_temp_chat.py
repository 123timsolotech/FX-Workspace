"""Temporary chats (privacy.py): a canary string sent in one must not be on
disk afterwards - not in the chats, the session file, memory, usage, past
runs, recordings, attachments or the search caches - not after the chat
closes, not after a simulated crash and restart. The stores refuse it
themselves (not the page), and the page keeps nothing of it either."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main  # noqa: F401  (wires the app together)
import chat_store
import composer_tools
import events as event_log
import memory
import privacy
import recorder
import run_log
import search_engine
import stream_hub
import usage
import web_tools

import agent.sessions as sessions
import agent.tasks as tasks
import core.state as shared

CANARY = "canary-7f3a9c2e-temporary"


class Isolated(unittest.TestCase):
    """Every store rooted in a temp folder, so the whole disk footprint can be searched."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tempchat-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        r = lambda *p: os.path.join(self.root, *p)
        stores = {"chat_store": chat_store.ChatStore(r("chats")), "memory_store": memory.MemoryStore(r("memory.json")),
                  "usage_store": usage.UsageStore(r("usage.json")), "run_log": run_log.RunLog(r("runs")),
                  "file_store": composer_tools.FileStore(r("uploads"))}
        for name, store in stores.items():
            p = patch.object(shared, name, store)
            p.start()
            self.addCleanup(p.stop)
        for target, attr, value in ((sessions, "SESSION_PATH", r("session.json")), (privacy, "ROOT", r("tempchats")),
                                    (recorder, "REC_DIR", r("recordings"))):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        keys = ("active_chat", "active_project", "conversation", "context_summary", "mode", "temp_file_store")
        saved = {k: shared.app_state.get(k) for k in keys}
        self.addCleanup(lambda: [shared.app_state.__setitem__(k, v) for k, v in saved.items()])
        shared.app_state.update(active_chat=None, active_project=None, mode="local",
                                conversation=shared.app_state["conversation"][:1])
        self.addCleanup(sessions.close_temporary)      # runs before the state is restored: nothing lingers in memory
        search_engine.reset()
        web_tools.reset_search_state()
        self.addCleanup(search_engine.reset)
        self.addCleanup(web_tools.reset_search_state)

    def on_disk(self, needle: str = CANARY) -> list:
        """Every file under the test root that contains needle."""
        hits = []
        for folder, _dirs, files in os.walk(self.root):
            for name in files:
                path = os.path.join(folder, name)
                with open(path, "rb") as f:
                    if needle.encode() in f.read():
                        hits.append(os.path.relpath(path, self.root))
        return hits

    def in_memory(self, needle: str = CANARY) -> list:
        where = []
        if needle in json.dumps(shared.app_state.get("events") or [], default=str):
            where.append("events")
        if needle in json.dumps(shared.app_state.get("audit_log") or [], default=str):
            where.append("audit log")
        if needle in json.dumps(stream_hub.hub._events, default=str):
            where.append("stream buffer")
        if needle in json.dumps([tasks.turns.get(t) for t in list(tasks.turns._turns)], default=str):
            where.append("turn records")
        if needle in repr(search_engine._cache) + repr(web_tools._search_cache) + repr(web_tools._page_cache):
            where.append("search/page caches")
        return where


class StoresRefuseTests(Isolated):
    def test_every_store_refuses_or_skips_inside_a_temporary_chat(self):
        chat = sessions.new_temporary_chat()
        with self.assertRaises(privacy.TemporaryChatError):
            shared.chat_store.save(chat)                                   # the type itself is refused
        with privacy.scope(chat["id"]):
            with self.assertRaises(privacy.TemporaryChatError):
                shared.chat_store.save({"id": shared.chat_store.new_id(), "title": CANARY, "transcript": []})
            with self.assertRaises(privacy.TemporaryChatError):
                shared.memory_store.add(CANARY)
            entry = None
            shared.usage_store.add({"in": 5, "out": 5, "model": CANARY})
            self.assertIsNone(shared.run_log.save({"id": "abcdef123456", "request": CANARY, "events": []}))
            recorder.start(CANARY, {}, "m", "local", "t1")
            self.assertFalse(recorder.active())
        self.assertEqual(self.on_disk(), [])
        # Outside the chat everything works as before.
        entry = shared.memory_store.add("a normal fact")
        shared.memory_store.mark_used([entry["id"]])
        self.assertEqual(shared.memory_store.list()[0]["uses"], 1)
        with privacy.scope(chat["id"]):
            shared.memory_store.mark_used([entry["id"]])                  # reading memory leaves no trace
        self.assertEqual(shared.memory_store.list()[0]["uses"], 1)

    def test_the_console_never_gets_its_content(self):
        chat = sessions.new_temporary_chat()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), privacy.scope(chat["id"]):
            event_log.log_action(f"Searched the web for {CANARY}")
        self.assertNotIn(CANARY, out.getvalue())
        self.assertIn("temporary chat", out.getvalue())

    def test_the_marker_follows_thread_pools(self):
        from concurrent.futures import ThreadPoolExecutor
        with privacy.scope("tmp-abc123def456"):
            fn = privacy.wrap(lambda: privacy.current())
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(pool.submit(fn).result(), "tmp-abc123def456")
            self.assertIsNone(pool.submit(privacy.current).result())


class TemporaryTurnTests(Isolated):
    def run_turn(self, message: str):
        """A whole turn through tasks._run_turn - the orchestrator replaced by one that
        does what a real turn does with the text: events, a stream, a web search with
        results cached, a memory save attempt, a log line."""
        tried = {}

        def fake_orchestrator(req, board=None):
            event_log.emit_event("tool", f"Search the web for {message}", status="done", ok=True)
            stream_hub.hub.publish("delta", call=1, channel="content", text=f"Looking up {message}")
            event_log.log_action(f"web_search: {message}")
            with patch.object(web_tools, "_duckduckgo", return_value=(
                    [{"title": "Result", "url": "https://example.org/a", "snippet": message, "date": ""}], "")):
                search_engine.search(message, settings={})
            try:
                shared.memory_store.add(f"remember {message}")
            except privacy.TemporaryChatError as e:
                tried["memory"] = str(e)
            return {"status": "success", "message": f"Here is what I found about {message}."}

        req = SimpleNamespace(message=message, with_vision=False, command=None, tools=[], files=[], instructions=None)
        with patch.object(tasks.agent, "run", side_effect=fake_orchestrator):
            result = tasks._run_turn(req)
        return result, tried

    def test_a_temporary_turn_leaves_nothing_on_disk(self):
        chat = sessions.new_temporary_chat()
        result, tried = self.run_turn(f"what is {CANARY}")
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["chat"]["temporary"])
        self.assertIn("memory", tried)                                    # the store said no
        self.assertEqual(self.on_disk(), [])                              # chats, session, memory, usage, runs ...
        self.assertEqual(shared.chat_store.list(), [])                    # not in the sidebar / history
        self.assertEqual(sessions.list_chats("*", CANARY), [])            # not in chat search (retrieval)
        self.assertEqual(shared.memory_store.relevant(CANARY), [])        # not in memory retrieval
        self.assertNotIn("search/page caches", self.in_memory())          # the search cache was bypassed
        live = shared.app_state["active_chat"]
        self.assertEqual(live["id"], chat["id"])
        self.assertIn(CANARY, json.dumps(live["transcript"]))             # it's there - in memory, for the page
        # Closing it forgets what memory still held of it.
        sessions.close_temporary()
        self.assertEqual(self.in_memory(), [])
        self.assertIsNone(shared.app_state["active_chat"])

    def test_attachments_live_in_its_folder_and_go_with_it(self):
        chat = sessions.new_temporary_chat()
        store = sessions.file_store()
        self.assertIsNot(store, shared.file_store)
        meta = store.get(store.save("notes.txt", base64.b64encode(f"secret {CANARY}".encode()).decode())["id"])
        folder = privacy.temp_dir(chat["id"], create=False)
        self.assertTrue(os.path.isfile(meta["path"]) and meta["path"].startswith(folder))
        self.assertEqual(os.listdir(shared.file_store.root) if os.path.isdir(shared.file_store.root) else [], [])
        sessions.close_temporary()
        self.assertFalse(os.path.exists(folder))
        self.assertEqual(self.on_disk(), [])

    def test_nothing_comes_back_after_a_crash_or_a_restart(self):
        # A normal chat was open before; then a temporary one with a file and a turn.
        shared.chat_store.save({"id": "a1b2c3d4e5f6", "title": "Normal", "transcript": [], "conversation": []})
        sessions.open_chat("a1b2c3d4e5f6")
        chat = sessions.new_temporary_chat()
        sessions.file_store().save("f.txt", base64.b64encode(CANARY.encode()).decode())
        self.run_turn(f"tell me about {CANARY}")
        with open(sessions.SESSION_PATH, encoding="utf-8") as f:
            self.assertIsNone(json.load(f)["chat"])                      # the session file never names it
        hits = self.on_disk()
        self.assertTrue(hits and all(h.startswith(os.path.join("tempchats", chat["id"])) for h in hits), hits)   # only its own folder
        # The process dies here (no close). Next start: sweep, then restore the session.
        shared.app_state["active_chat"] = None
        shared.app_state["conversation"] = shared.app_state["conversation"][:1]
        self.assertEqual(privacy.sweep(), 1)
        sessions.restore()
        self.assertIsNone(shared.app_state["active_chat"])               # nothing reopens
        self.assertEqual(self.on_disk(), [])

    def test_a_message_for_an_ended_temporary_chat_is_refused(self):
        from fastapi.testclient import TestClient
        from api.server import app
        client = TestClient(app, base_url="http://127.0.0.1")
        with patch.object(tasks.turns, "start") as start:
            r = client.post("/api/chat", json={"message": CANARY, "temporary_chat": "tmp-000000000000"}).json()
        start.assert_not_called()
        self.assertTrue(r["temporary_ended"])
        self.assertEqual(self.on_disk(), [])

    def test_save_this_chat_makes_it_a_normal_chat(self):
        sessions.new_temporary_chat()
        meta = sessions.file_store().save("a.txt", base64.b64encode(b"hello").decode())
        self.run_turn("keep this one")
        old_id = shared.app_state["active_chat"]["id"]
        saved = sessions.save_temporary()
        self.assertFalse(privacy.is_temporary(saved))
        self.assertEqual(shared.chat_store.load(saved["id"])["title"], "keep this one")
        self.assertFalse(os.path.exists(privacy.temp_dir(old_id, create=False)))
        self.assertTrue(os.listdir(shared.file_store.root))              # the attachment moved into uploads
        self.assertNotEqual(meta["id"], os.listdir(shared.file_store.root)[0])

    def test_message_actions_work_on_it_in_memory(self):
        chat = sessions.new_temporary_chat()
        self.run_turn("first")
        self.run_turn("second")
        edited = sessions.edit_messages(chat["id"], "truncate", 2)
        self.assertEqual([e["t"] for e in edited["transcript"] if e["r"] == "u"], ["first"])
        with self.assertRaises(ValueError):
            sessions.edit_messages(chat["id"], "branch", 0)
        self.assertEqual(shared.chat_store.list(), [])


class PageKeepsNothingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")

    def test_no_transcript_draft_or_id_of_it_in_local_storage(self):
        persist = re.search(r"function persistTranscript\(\) \{(.*?)\n\}", self.html, re.S).group(1)
        self.assertLess(persist.index("if (isTemp())"), persist.index("store.set('transcript'"))
        self.assertIn("if (isTempId(chatOf)) { inMemory[chatOf] = v; return; }", self.html)     # drafts
        self.assertIn("store.set('chatId', isTempId(chat.id) ? null : chat.id)", self.html)
        self.assertIn("...(isTemp() ? { temporary_chat: S.chatId } : {})", self.html)
        self.assertIn("Actions performed outside the chat may still persist.", self.html)


if __name__ == "__main__":
    unittest.main()

"""
test_polish.py
==============
Reliability and organisation: usage and cost (usage.py), pinned and archived
chats (chat_store), crash-safe saving of a message before its turn
(agent/sessions.mark_pending), prompt routines (routines / routine_runner),
pinned project files in the brief (projects.build_context), and the cloud
retry round (agent/models.call_cloud).
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main  # noqa: F401  (wires the app together)
import chat_store
import projects
import routines
import usage

import agent.models as llm
import agent.routine_runner as routine_runner
import agent.sessions as sessions
import core.state as shared


class UsageTests(unittest.TestCase):
    def test_cost_is_known_zero_or_unknown(self):
        self.assertGreater(usage.cost("gemini/gemini-2.5-flash", 1000, 1000), 0)
        self.assertEqual(usage.cost("Qwen-3.5-4B-Q4_K_M", 10 ** 6, 10 ** 6), 0.0)          # local: free
        self.assertIsNone(usage.cost("gemini/not-a-real-model-xyz", 10, 10))               # never guessed

    def test_days_and_chats_add_up(self):
        store = usage.UsageStore(os.path.join(tempfile.mkdtemp(), "usage.json"))
        store.add({"in": 100, "out": 50, "searches": 2, "reads": 3, "model": "gemini/gemini-2.5-flash"})
        store.add({"in": 10, "out": 5, "model": "local-model"})
        store.add({})                                                                        # nothing used: not a reply
        today = store.days(3)[-1]
        self.assertEqual((today["in"], today["out"], today["searches"], today["reads"], today["replies"]), (110, 55, 2, 3, 2))
        self.assertEqual(len(store.days(3)), 3)
        totals = usage.chat_totals([{"r": "u", "t": "q"}, {"r": "a", "m": {"in": 5, "out": 1, "model": "x/unpriced"}}])
        self.assertEqual((totals["in"], totals["unpriced"]), (5, 1))


class ChatFlagTests(unittest.TestCase):
    def setUp(self):
        self.store = chat_store.ChatStore(tempfile.mkdtemp())
        self.a = self.store.save({"id": self.store.new_id(), "title": "A", "transcript": [], "conversation": []})
        time.sleep(0.02)
        self.b = self.store.save({"id": self.store.new_id(), "title": "B", "transcript": [], "conversation": []})

    def test_pinned_first_and_not_touched(self):
        self.store.set_flags(self.a["id"], pinned=True)
        self.assertEqual([r["title"] for r in self.store.list()], ["A", "B"])
        self.assertEqual(self.store.load(self.a["id"])["updated"], self.a["updated"])        # pinning isn't activity

    def test_flags_survive_a_save_from_memory(self):
        self.store.set_flags(self.a["id"], pinned=True, archived=True)
        self.store.save({"id": self.a["id"], "title": "A", "transcript": [{"r": "u", "t": "x"}], "conversation": []})
        saved = self.store.load(self.a["id"])
        self.assertTrue(saved["pinned"] and saved["archived"])
        self.store.set_flags(self.a["id"], pinned=False)
        self.store.save({"id": self.a["id"], "title": "A", "transcript": [], "conversation": []})
        self.assertFalse(self.store.load(self.a["id"])["pinned"])


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = (shared.chat_store, shared.app_state.get("active_chat"), sessions.SESSION_PATH)
        shared.chat_store = chat_store.ChatStore(os.path.join(self.dir.name, "chats"))
        sessions.SESSION_PATH = os.path.join(self.dir.name, "session.json")
        shared.app_state["active_chat"] = None

    def tearDown(self):
        shared.chat_store, shared.app_state["active_chat"], sessions.SESSION_PATH = self.saved
        self.dir.cleanup()

    def test_a_message_is_on_disk_before_its_turn_and_cleared_after(self):
        req = SimpleNamespace(message="Summarize my notes", command="tldr", with_vision=False)
        sessions.mark_pending(req)
        cid = shared.app_state["active_chat"]["id"]
        pending = shared.chat_store.load(cid)["pending"]
        self.assertEqual((pending["t"], pending["c"]), ("Summarize my notes", "tldr"))
        sessions.record_turn(req, {"status": "success", "message": "Done."}, shared.app_state.get("event_seq", 0))
        saved = shared.chat_store.load(cid)
        self.assertNotIn("pending", saved)
        self.assertEqual([e["r"] for e in saved["transcript"]], ["u", "a"])


class PromptRoutineTests(unittest.TestCase):
    def setUp(self):
        self.store = routines.RoutineStore(os.path.join(tempfile.mkdtemp(), "routines.json"))

    def test_create_update_and_history(self):
        r = self.store.create_prompt("AI news", "Top AI news of the day, with sources.", ["grounded", "research", "bogus"])
        self.assertEqual((r["kind"], r["tools"], r["steps"]), ("prompt", ["grounded"], []))
        with self.assertRaises(ValueError):
            self.store.create_prompt("Empty", "  ")
        with self.assertRaises(ValueError):
            self.store.create_prompt("ai NEWS", "again")                                     # names are unique
        r = self.store.update(r["id"], prompt="Top 3 items only.", tools=["summarize"])
        self.assertEqual((r["prompt"], r["tools"]), ("Top 3 items only.", ["summarize"]))
        r = self.store.update(r["id"], last_run={"at": 1, "status": "done", "message": "News, 26 Sep", "trigger": "schedule", "chat": "c1"})
        self.assertEqual(r["history"][-1], {"at": 1, "trigger": "schedule", "status": "done", "message": "News, 26 Sep", "chat": "c1"})

    def test_a_run_answers_the_prompt_with_its_tools_and_records_it(self):
        r = self.store.create_prompt("Digest", "The top AI news of the day.", ["grounded"])
        req = routine_runner.prompt_request(r, "schedule")
        self.assertEqual((req.message, req.tools, req.trigger), ("The top AI news of the day.", ["grounded"], "schedule"))
        seen = {}
        with patch.object(shared, "routine_store", self.store):
            result = routine_runner.run_prompt(r, req, lambda q: seen.setdefault("req", q) and {
                "status": "success", "message": "# AI news, 26 September\n\n- One [1]"})
        self.assertIs(seen["req"], req)
        self.assertEqual((result["routine"], result["routine_name"]), (r["id"], "Digest"))
        last = self.store.get(r["id"])["last_run"]
        self.assertEqual((last["status"], last["message"], last["trigger"]), ("done", "AI news, 26 September", "schedule"))


class PinnedFileTests(unittest.TestCase):
    def test_pinned_files_go_in_the_brief(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "STYLE.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("Use tabs. Name things clearly.")
            project = {"id": "p1", "name": "Game", "files": [path, os.path.join(d, "gone.md")]}
            text, report = projects.build_context(project, "hello", "normal", projects.FolderIndex(os.path.join(d, "idx")),
                                                  frame=lambda source, t: f"<<{source}>>{t}")
        self.assertIn("PINNED FILE", text)
        self.assertIn("<<the pinned file STYLE.md>>Use tabs.", text)
        self.assertIn("pinned: STYLE.md", report["included"])


class CloudRetryTests(unittest.TestCase):
    def test_a_transient_failure_gets_one_visible_retry(self):
        calls = []

        def complete(**kw):
            calls.append(kw["model"])
            if len(calls) == 1:
                raise RuntimeError("429 Too Many Requests")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
        with patch.object(llm, "candidate_pairs", return_value=[("gemini/x", "k")]), \
             patch.object(llm.llm_compat, "completion", side_effect=complete), \
             patch.object(llm.time, "sleep"), patch.object(llm.event_log, "emit_event") as note:
            res = llm.call_cloud([{"role": "user", "content": "hi"}], False)
        self.assertEqual(res.choices[0].message.content, "ok")
        self.assertEqual(len(calls), 2)
        self.assertIn("retrying", note.call_args[0][1])

    def test_a_bad_key_is_not_retried(self):
        with patch.object(llm, "candidate_pairs", return_value=[("gemini/x", "k")]), \
             patch.object(llm.llm_compat, "completion", side_effect=RuntimeError("401 invalid api key")) as c, \
             patch.object(llm.time, "sleep"):
            with self.assertRaises(RuntimeError):
                llm.call_cloud([{"role": "user", "content": "hi"}], False)
        self.assertEqual(c.call_count, 1)


if __name__ == "__main__":
    unittest.main()

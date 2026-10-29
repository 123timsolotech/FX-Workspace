"""Phase 4 server side of the Activity panel: every tool event says which
plan step it served, the model's stated reason and the model note it came
from; the model's full reasoning is kept (only when it emitted some); each
finished turn is saved as a past run with its usage against the effort
limits, and /api/runs lists, searches and returns it."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import replay_harness as rh
import run_log


def _call(cid, name, args, **extra):
    return {"finish_reason": "tool_calls", "tool_calls": [{"id": cid, "name": name, "arguments": json.dumps(args)}],
            **extra}


def _fixture(target, reasoning):
    return {"format": "agentic-replay/1",
            "meta": {"name": "run history", "provenance": "reconstructed", "model": "unrecorded", "evidence": "Phase 4"},
            "setup": {"mode": "local", "entry": "turn", "settings": {"effort": "high", "prompt_prep": "off"},
                      "model_caps": {"context": 131072, "tool_protocol": "native"}},
            "request": "Create lava.lua in my game folder that prints hello, then say done",
            "responses": [
                _call("p1", "set_plan", {"goal": "Make lava.lua", "steps": ["Write lava.lua", "Tell the user"]}),
                _call("w1", "write_file", {"filename": target, "content": "print('hello')\n",
                                           "justification": "Step 1 needs the script file"},
                      content="Writing the script now.", reasoning_content=reasoning),
                _call("u1", "update_plan", {"step": 1, "status": "done"}),
                _call("u2", "update_plan", {"step": 2, "status": "done"}),
                {"finish_reason": "stop", "content": "Done - lava.lua prints hello."}],
            "aux_responses": [], "approvals": [True], "tools": {"mode": "real"}}


class RunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.join(self.tmp.name, "game", "lava.lua")
        os.makedirs(os.path.dirname(self.target))
        # Longer than the 600-character excerpt, so the full text is kept separately.
        self.reasoning = "The plan says write the file first. " * 40 + "FINAL-THOUGHT"
        self.run = rh.run_fixture(_fixture(self.target, self.reasoning), sandbox=self.tmp.name)

    def test_tool_events_link_to_their_step_reason_and_thought(self):
        self.assertTrue(os.path.exists(self.target), self.run.message)
        [write] = [e for e in self.run.events if e.get("kind") == "tool" and e.get("tool") == "write_file"]
        self.assertEqual(write["plan_step"]["n"], 1)
        self.assertEqual(write["plan_step"]["text"], "Write lava.lua")
        self.assertEqual(write["why"], "Step 1 needs the script file")
        thought = next(e for e in self.run.events if e.get("id") == write["thought_id"])
        self.assertEqual((thought["kind"], thought["note"]), ("thought", "Writing the script now."))
        self.assertTrue(thought["reasoning_full"].endswith("FINAL-THOUGHT"))
        self.assertLessEqual(len(thought["reasoning"]), 601)                # the excerpt stays short

    def test_no_reasoning_means_no_reasoning_fields(self):
        """Nothing is invented: a step without model reasoning carries none."""
        [plan] = [e for e in self.run.events if e.get("kind") == "tool" and e.get("tool") == "set_plan"]
        self.assertNotIn("thought_id", plan)
        self.assertNotIn("why", plan)

    def test_the_run_is_saved_with_usage_and_found_by_search(self):
        log = run_log.RunLog(os.path.join(self.tmp.name, ".runs"))
        [row] = log.list()
        self.assertEqual(row["id"], self.run.result["run_id"])
        self.assertEqual((row["effort"], row["status"]), ("high", "success"))
        self.assertIn("lava.lua", row["request"])
        self.assertGreaterEqual(row["usage"]["steps"], 3)
        self.assertEqual(row["usage"]["level"], "high")
        self.assertGreaterEqual(row["tools"], 2)
        self.assertEqual([r["id"] for r in log.list("script file")], [row["id"]])   # the reason is searchable
        self.assertEqual(log.list("grocery"), [])
        full = log.get(row["id"])
        write = next(e for e in full["events"] if e.get("tool") == "write_file")
        self.assertTrue(write.get("undo_id"))
        self.assertEqual(write["why"], "Step 1 needs the script file")
        self.assertTrue(any(e.get("reasoning_full") for e in full["events"]))


class RunHistoryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import main
        import core.state as shared
        cls.shared = shared
        cls.client = TestClient(main.app, base_url="http://127.0.0.1:8000")

    def test_list_search_get_and_live_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            log = run_log.RunLog(os.path.join(folder, "runs"))
            log.save({"id": "abc123def456", "request": "fix the lava damage", "started": 5, "ended": 9, "status": "success",
                      "effort": "normal", "events": [{"id": 1, "kind": "tool", "tool": "roblox_edit_script",
                                                      "label": "Edit script ServerScriptService/Lava", "ok": True}]})
            with patch.object(self.shared, "run_log", log):
                rows = self.client.get("/api/runs", params={"q": "lava"}).json()["runs"]
                self.assertEqual([r["id"] for r in rows], ["abc123def456"])
                self.assertEqual(self.client.get("/api/runs", params={"q": "grocery"}).json()["runs"], [])
                got = self.client.get("/api/runs/abc123def456").json()
                self.assertTrue(got["ok"])
                self.assertEqual(got["run"]["events"][0]["tool"], "roblox_edit_script")
                self.assertFalse(self.client.get("/api/runs/..%2Fsecret").json().get("ok"))
        import effort
        budget = effort.RunBudget(effort.PROFILES["high"], local=True)
        budget.record_call(1000, 50)
        with patch.dict(self.shared.app_state, {"run_budget": budget}):
            usage = self.client.get("/api/state").json()["usage"]
        self.assertEqual((usage["steps"], usage["level"]), (1, "high"))
        self.assertEqual(usage["step_limit"], effort.PROFILES["high"].local_iterations)
        self.assertIsNone(self.client.get("/api/state").json()["usage"])


class ReviewFixTests(unittest.TestCase):
    """Phase 4 review: secrets never reach saved runs (or the brief), the
    session override can be cleared, the slider answers for the next
    message's model, a past run's Undo says what the trail says now, and
    runs can be deleted (with their chat, one by one, or all)."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import main
        import core.state as shared
        cls.shared = shared
        cls.client = TestClient(main.app, base_url="http://127.0.0.1:8000")

    def test_keys_in_a_project_never_reach_the_brief_or_the_saved_run(self):
        with tempfile.TemporaryDirectory() as sandbox:
            folder = os.path.join(sandbox, "Obby")
            os.makedirs(folder)
            with open(os.path.join(folder, "keys.local.bat"), "w") as f:
                f.write("set GEMINI_API_KEY=AIzaSyA0123456789012345678901234567\nset OPENAI_API_KEY=sk-live0123456789abcdef\n")
            with open(os.path.join(folder, "config.lua"), "w") as f:
                f.write("-- GEMINI_API_KEY is read here\nlocal api_key = 'sk-live0123456789abcdef'\n")
            project = {"id": "e" * 12, "name": "Obby", "folder": folder, "files": [], "instructions": "",
                       "notes": "", "roblox_place": "", "effort": None, "prompt_prep": None}
            fx = {"format": "agentic-replay/1",
                  "meta": {"name": "secrets", "provenance": "reconstructed", "model": "unrecorded", "evidence": "Phase 4 review H1"},
                  "setup": {"mode": "local", "entry": "turn", "settings": {"effort": "normal", "prompt_prep": "off"},
                            "model_caps": {"context": 131072, "tool_protocol": "native"},
                            "memory": [{"text": "My home address is 12 Lava Road", "pinned": True}],
                            "app_state": {"active_project": project}},
                  "request": "Explain how GEMINI_API_KEY is set in my project, near my home address",
                  "responses": [{"finish_reason": "stop", "content": "It is read in config.lua."}],
                  "aux_responses": [], "approvals": [], "tools": {"mode": "real"}}
            run = rh.run_fixture(fx, sandbox=sandbox)
            sent = json.dumps([r.get("messages") for r in run.requests])
            self.assertIn("GEMINI_API_KEY is read here", sent)              # the file's lines did reach the brief
            self.assertNotIn("sk-live0123456789abcdef", sent)               # but no key: masked in snippets
            self.assertNotIn("AIzaSyA0123456789", sent)                     # and keys.local.bat is never read
            saved = json.dumps(run_log.RunLog(os.path.join(sandbox, ".runs")).get(run.result["run_id"]))
            for secret in ("sk-live0123456789abcdef", "AIzaSyA0123456789", "12 Lava Road"):
                self.assertNotIn(secret, saved)
            self.assertIn("text_chars", saved)                              # the brief's size, not its text

    def test_the_session_override_can_be_cleared(self):
        c = self.client
        self.addCleanup(lambda: self.shared.app_state.update(effort_session=None, prep_session=None))
        self.assertEqual(c.post("/api/effort/session", json={"level": "max"}).json()["session_effort"], "max")
        r = c.post("/api/effort/session", json={"level": None})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["session_effort"])
        self.assertEqual(c.post("/api/prep/session", json={"mode": None}).status_code, 200)

    def test_the_slider_answers_for_the_model_the_next_message_uses(self):
        import model_caps
        saved = (model_caps.CAPS_PATH, model_caps._cache)
        with tempfile.TemporaryDirectory() as folder:
            model_caps.CAPS_PATH, model_caps._cache = os.path.join(folder, "caps.json"), {}
            try:
                model_caps.update("tiny-ctx", context=4096, tool_protocol="native")
                local = self.client.get("/api/effort", params={"mode": "local", "model": "tiny-ctx"}).json()
                cloud = self.client.get("/api/effort", params={"mode": "cloud"}).json()
            finally:
                model_caps.CAPS_PATH, model_caps._cache = saved
        self.assertEqual((local["model"], local["local"]), ("tiny-ctx", True))
        self.assertIn("ultra", local["unsupported"])                        # its 4k window rules Workflow out
        self.assertFalse(cloud["local"])
        self.assertEqual(cloud["unsupported"], {})

    def test_past_undo_states_and_deleting_runs(self):
        import tools.files as files
        import undo_store
        with tempfile.TemporaryDirectory() as folder:
            log = run_log.RunLog(os.path.join(folder, "runs"))
            undo = undo_store.UndoStore(os.path.join(folder, "undo"))
            target = os.path.join(folder, "a.txt")
            change = undo.begin("write_file", "Write a.txt")
            change.backup_file(target)
            with open(target, "w") as f:
                f.write("x")
            applied = undo.commit(change)
            change = undo.begin("write_file", "Write b.txt")
            change.backup_file(os.path.join(folder, "b.txt"))
            open(os.path.join(folder, "b.txt"), "w").close()
            undone = undo.commit(change)
            undo.undo(undone)
            events = [{"id": i, "kind": "tool", "tool": "write_file", "label": "Write", "ok": True, "undo_id": u}
                      for i, u in enumerate((applied, undone, "20200101-000000-gone00"), 1)]
            for rid, chat in (("run0000001", "chatA"), ("run0000002", "chatB"), ("run0000003", "chatB")):
                log.save({"id": rid, "request": "write", "chat": chat, "started": 1, "events": events})
            with patch.object(self.shared, "run_log", log), patch.object(files, "undo", undo):
                got = self.client.get("/api/runs/run0000001").json()["run"]["events"]
                self.assertEqual([e["undo_state"] for e in got], ["applied", "undone", "gone"])
                self.assertTrue(self.client.delete("/api/runs/run0000001").json()["ok"])
                self.assertEqual(log.delete_where(chat="chatB"), 2)          # what deleting a chat does
                self.assertEqual(log.list(), [])
                log.save({"id": "run0000004", "request": "x", "started": 1, "events": []})
                self.assertEqual(self.client.delete("/api/runs").json()["deleted"], 1)

    def test_a_deleted_chat_takes_its_runs_with_it(self):
        import chat_store
        with tempfile.TemporaryDirectory() as folder:
            log = run_log.RunLog(os.path.join(folder, "runs"))
            chats = chat_store.ChatStore(os.path.join(folder, "chats"))
            chat = chats.save({"id": chats.new_id(), "title": "t", "transcript": [], "conversation": []})
            log.save({"id": "run0000009", "request": "x", "chat": chat["id"], "started": 1, "events": []})
            import agent.sessions as sessions
            with patch.object(self.shared, "run_log", log), patch.object(self.shared, "chat_store", chats), \
                    patch.object(sessions, "SESSION_PATH", os.path.join(folder, "session.json")):   # never data/
                self.assertTrue(self.client.delete(f"/api/chats/{chat['id']}").json()["ok"])
            self.assertEqual(log.list(), [])


if __name__ == "__main__":
    unittest.main()

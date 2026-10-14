"""Phase 4 exit walkthrough, end to end through the real API and pipeline:

  new project (effort High) -> a Roblox task -> the approval card (real diff,
  "Can be undone") -> the user raises effort to Max while it runs -> the run
  is saved and found in past runs -> undo the script edit -> restart -> the
  project and its chat reopen.

Only the model's replies are scripted (replay harness) and Roblox Studio is
testing_fakes.FakeStudio; everything else - routes, orchestrator, effort,
permissions, approval view, executor, undo trail, run log, sessions - is the
shipped code. All stores live in a temporary folder."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import replay_harness as rh
import roblox_bridge
import run_log
import undo_store
from testing_fakes import FakeStudio

LAVA = "local function onTouch(hit)\n\thit.Parent.Humanoid.Health = 0\nend\n"
FIXED = "local function onTouch(hit)\n\tlocal h = hit.Parent:FindFirstChildOfClass('Humanoid')\n\tif h then h.Health = 0 end\nend\n"


def _call(cid, name, args):
    return {"finish_reason": "tool_calls", "tool_calls": [{"id": cid, "name": name, "arguments": json.dumps(args)}]}


class WalkthroughTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import main
        cls.client = TestClient(main.app, base_url="http://127.0.0.1:8000")

    def setUp(self):
        import agent.sessions as sessions
        import chat_store
        import core.state as shared
        import projects
        import tools.files as files
        import tools.runtime as runtime
        self.shared, self.sessions = shared, sessions
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        box = self.tmp.name
        self.game = os.path.join(box, "ObbyWorld")
        os.makedirs(self.game)
        # The same folders the replay harness uses for this sandbox, so what the
        # API writes before and after the run is what the run itself sees.
        undo = undo_store.UndoStore(os.path.join(box, ".undo"))
        for p in (patch.object(shared, "project_store", projects.ProjectStore(os.path.join(box, ".projects"))),
                  patch.object(shared, "chat_store", chat_store.ChatStore(os.path.join(box, ".chats"))),
                  patch.object(shared, "run_log", run_log.RunLog(os.path.join(box, ".runs"))),
                  patch.object(sessions, "SESSION_PATH", os.path.join(box, ".session.json")),
                  patch.object(files, "undo", undo), patch.object(runtime.tool_exec, "undo", undo)):
            p.start()
            self.addCleanup(p.stop)
        keys = ("active_project", "active_chat", "effort_session", "prep_session", "conversation")
        saved = {k: shared.app_state.get(k) for k in keys}
        self.addCleanup(lambda: shared.app_state.update(saved))
        roblox_bridge._reset_for_tests()
        self.addCleanup(roblox_bridge._reset_for_tests)
        self.studio = FakeStudio({"ServerScriptService/Lava": ("Script", LAVA)}, play_errors=[]).start()
        self.addCleanup(self.studio.stop)

    def test_project_roblox_task_approval_effort_change_undo_restart(self):
        c, shared = self.client, self.shared
        # 1. A new project, High effort, opened.
        made = c.post("/api/projects", json={"name": "Obby World", "folder": self.game, "effort": "high",
                                             "instructions": "Lua style: tabs."}).json()
        self.assertTrue(made["ok"], made)
        pid = made["project"]["id"]
        self.assertTrue(c.post(f"/api/projects/{pid}/activate").json()["ok"])
        self.assertEqual((c.get("/api/effort").json()["requested"], c.get("/api/effort").json()["source"]),
                         ("high", "project"))

        # 2-4. The Roblox task. While the model writes its second reply, the user
        # moves the effort slider to Max: the project sets High, so the UI makes
        # it a session override - the same API call the slider makes.
        moved = []

        def raise_effort():
            moved.append(c.post("/api/effort/session", json={"level": "max"}).json())
        fx = {"format": "agentic-replay/1",
              "meta": {"name": "walkthrough", "provenance": "reconstructed", "model": "unrecorded",
                       "evidence": "Phase 4 exit walkthrough"},
              "setup": {"mode": "local", "entry": "turn", "roblox": "fake",
                        "settings": {"prompt_prep": "off", "effort_auto_escalate": False},
                        "model_caps": {"context": 131072, "tool_protocol": "native"},
                        "app_state": {"active_project": shared.app_state["active_project"]},
                        "settings_at_response": {"1": raise_effort}},          # while the 2nd reply streams
              "request": "Fix the lava script so it doesn't error when a part without a Humanoid touches it",
              "responses": [
                  _call("r1", "roblox_read_script", {"path": "ServerScriptService/Lava"}),
                  _call("e1", "roblox_edit_script", {"path": "ServerScriptService/Lava", "justification": "Guard the Humanoid lookup",
                                                     "edits": [{"find": "\thit.Parent.Humanoid.Health = 0",
                                                                "replace": "\tlocal h = hit.Parent:FindFirstChildOfClass('Humanoid')\n\tif h then h.Health = 0 end"}]}),
                  {"finish_reason": "stop", "content": "Fixed: the lava now checks for a Humanoid first."}],
              "aux_responses": [], "approvals": [True], "tools": {"mode": "real"}}
        run = rh.run_fixture(fx, sandbox=self.tmp.name)
        self.assertEqual(run.status, "success", run.message)
        [started] = [e for e in run.events if e.get("phase") == "run_started"]
        self.assertEqual((started["effort"], started["source"]), ("high", "project"))
        self.assertEqual(moved[0]["session_effort"], "max")
        changed = [e for e in run.events if e.get("phase") == "effort_changed"]
        self.assertTrue(changed and changed[0]["effort"] == "max", [e.get("phase") for e in run.events])
        [card] = run.approvals                                    # the approval card: real diff, honest undo
        self.assertEqual(card[0]["tool_name"], "roblox_edit_script")
        self.assertEqual(card[0]["view"]["undo_text"], "Can be undone")
        self.assertIn("FindFirstChildOfClass", json.dumps(card[0]["view"]))
        self.assertEqual(self.studio.source("ServerScriptService/Lava"), FIXED)
        [edit] = [e for e in run.events if e.get("kind") == "tool" and e.get("tool") == "roblox_edit_script"]
        self.assertEqual(edit["why"], "Guard the Humanoid lookup")

        # 5. The run is kept: found by searching past runs, with its usage.
        rows = c.get("/api/runs", params={"q": "lava humanoid"}).json()["runs"]
        self.assertEqual([r["id"] for r in rows], [run.result["run_id"]])
        self.assertEqual(rows[0]["usage"]["level"], "max")               # the limits it finished under
        saved = c.get(f"/api/runs/{rows[0]['id']}").json()["run"]
        self.assertTrue(any(e.get("undo_id") == edit["undo_id"] for e in saved["events"]))

        # 6. Undo the script edit from the undo history.
        history = c.get("/api/undo").json()["changes"]
        self.assertEqual(history[0]["id"], edit["undo_id"])
        undone = c.post(f"/api/undo/{edit['undo_id']}", json={"force": False}).json()
        self.assertTrue(undone["ok"], undone)
        self.assertEqual(self.studio.source("ServerScriptService/Lava"), LAVA)

        # 7. Restart: memory is gone; the session file reopens the project and its chat.
        chat_id = run.result["chat"]["id"]
        shared.app_state.update(active_project=None, active_chat=None, effort_session=None)
        self.sessions.restore()
        state = c.get("/api/state").json()
        self.assertEqual(state["project"]["id"], pid)
        self.assertEqual(state["chat"]["id"], chat_id)
        chat = c.get(f"/api/chats/{chat_id}").json()["chat"]
        self.assertIn("lava script", chat["transcript"][0]["t"])
        self.assertEqual(chat["project"], pid)
        self.assertEqual(c.get("/api/effort").json()["requested"], "high")    # the session override ended with it


if __name__ == "__main__":
    unittest.main()

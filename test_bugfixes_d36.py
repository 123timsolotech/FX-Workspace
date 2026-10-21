"""Regressions for D36 (2026-09-25): Roblox tools never reached the model,
the sliders were locked before a model loaded, and every model-settings save
failed with "Save failed"."""
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import intent_router as ir
import llama_local
import main
import replay_harness as rh
import roblox_bridge
import agent.loop as loop
import agent.prompts as prompts
import core.config as config
import core.state as shared
import tools.schemas as schemas

QUESTION = "what do you see in Test1 roblox place?"


def _names(tools):
    return {t if isinstance(t, str) else t["function"]["name"] for t in tools or []}


class RobloxToolsReachTheModel(unittest.TestCase):
    def test_the_question_is_a_read_and_gets_the_tree_tool(self):
        self.assertEqual(ir.classify_turn(QUESTION)["mode"], "read")
        with patch.object(roblox_bridge, "is_connected", return_value=True):
            offered = _names(schemas.tools_for_mode(True, "read"))
        self.assertTrue({"roblox_get_tree", "roblox_status", "roblox_find", "roblox_list_children"} <= offered)
        self.assertFalse(offered & {"roblox_edit_script", "roblox_delete_instance"})      # still read-only
        for text in ("what is bonsai 2 27B?", "tell me a joke"):
            self.assertEqual(ir.classify_turn(text)["mode"], "none", text)

    def test_the_prompt_names_only_tools_that_are_offered(self):
        known = schemas.TOOL_NAMES
        tool_re = re.compile(r"\b(" + "|".join(map(re.escape, known)) + r")\b")
        for offered in (set(), {"roblox_status", "web_search"}, {"read_file", "write_file", "roblox_get_tree"}):
            for base in (prompts.SYSTEM_PROMPT, prompts.SYSTEM_PROMPT_COMPACT):
                text = prompts.for_tools(base, offered, known)
                self.assertEqual(set(tool_re.findall(text)) - offered, set(), offered)
                if not any(n.startswith("roblox_") for n in offered):
                    self.assertNotIn("roblox_*", text)
        # nothing is lost when everything is offered
        self.assertEqual(prompts.for_tools(prompts.SYSTEM_PROMPT_COMPACT, known, known).count("roblox_"),
                         prompts.SYSTEM_PROMPT_COMPACT.count("roblox_"))

    def test_an_invented_name_gets_the_real_list(self):
        offered = [{"type": "function", "function": {"name": n}} for n in ("roblox_get_tree", "roblox_status")]
        with patch.object(schemas, "tools_for_current_turn", return_value=offered):
            msg = loop._invalid_call_reason("roblox_list", {})
        self.assertIn("Your tools are exactly: roblox_get_tree, roblox_status", msg)
        with patch.object(schemas, "tools_for_current_turn", return_value=[]):
            self.assertIn("you have no tools for this message", loop._invalid_call_reason("roblox_inspect", {}))

    def test_replay_of_the_failing_chat(self):
        """The 2026-09-24 chat: the model guessed roblox_list / roblox_inspect
        because it was given no tools. Now it is given the tree tool, an
        invented name is answered with the real list, and the tree call runs."""
        fx = {"format": "agentic-replay/1",
              "meta": {"name": "roblox place question", "provenance": "reconstructed", "model": "unrecorded",
                       "evidence": "2026-09-24 chat: 'what do you see in Test1 roblox place?' looped on invented names"},
              # "fake": the harness leaves the bridge to the test, which says it is connected (tools are stubbed)
              "setup": {"mode": "local", "entry": "agent", "roblox": "fake",
                        "settings": {"effort": "low", "prompt_prep": "off"}},
              "request": QUESTION,
              "responses": [
                  {"finish_reason": "tool_calls", "tool_calls": [{"id": "c1", "name": "roblox_list", "arguments": "{}"}]},
                  {"finish_reason": "tool_calls", "tool_calls": [{"id": "c2", "name": "roblox_get_tree", "arguments": "{}"}]},
                  {"finish_reason": "stop", "content": "Test1 has a Baseplate and a SpawnLocation."}],
              "approvals": [],
              "tools": {"mode": "stub", "results": {"roblox_get_tree": [
                  {"ok": True, "message": "Workspace\n  Baseplate (Part)\n  SpawnLocation (SpawnLocation)"}]}}}
        with patch.object(roblox_bridge, "is_connected", return_value=True):
            run = rh.run_fixture(fx)
        self.assertIn("roblox_get_tree", _names(run.requests[0].get("tools")))
        self.assertEqual([n for n, _a in run.executed], ["roblox_get_tree"])
        sent = json.dumps([r.get("messages") for r in run.requests])
        self.assertIn("Your tools are exactly:", sent)
        system = run.requests[0]["messages"][0]["content"]
        self.assertNotIn("roblox_edit_script", system)          # not offered on a read turn, so not named


class _Api(unittest.TestCase):
    def setUp(self):
        self.saved = json.loads(json.dumps(shared.app_state["settings"]))
        self.state = {k: shared.app_state.get(k) for k in ("mode", "local_model", "local_engaged", "last_working_model")}
        p = patch.object(config, "save_settings")          # never touch the real settings.json
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._restore)
        self.client = TestClient(main.app, base_url="http://127.0.0.1:8000")

    def _restore(self):
        shared.app_state["settings"] = self.saved
        shared.app_state.update(self.state)


class SlidersBeforeAModelLoads(_Api):
    def test_a_change_is_saved_for_the_model_the_slider_shows(self):
        """Before a local model has answered, the server's 'current model' was
        the last cloud model: the choice went under that name and the slider,
        which reads the pinned local model's entry, snapped back to it."""
        shared.app_state.update(mode=config.MODE_CLOUD, last_working_model="cloud/some-model",
                                local_model=None, local_engaged=None)
        shared.app_state["settings"]["effort_by_model"] = {"Bonsai": "max"}
        shared.app_state["settings"]["prompt_prep_by_model"] = {"Bonsai": "plan"}
        c = self.client
        before = c.get("/api/effort", params={"mode": "local", "model": "Bonsai"}).json()
        self.assertEqual((before["requested"], before["source"]), ("max", "model"))
        c.post("/api/settings", json={"effort": "high", "for_mode": "local", "for_model": "Bonsai"})
        c.post("/api/settings", json={"prompt_prep": "clarify", "for_mode": "local", "for_model": "Bonsai"})
        after = c.get("/api/effort", params={"mode": "local", "model": "Bonsai"}).json()
        prep = c.get("/api/prep", params={"mode": "local", "model": "Bonsai"}).json()
        self.assertEqual((after["requested"], after["source"]), ("high", "model"))
        self.assertEqual((prep["mode"], prep["source"], prep["model"]), ("clarify", "model", "Bonsai"))
        s = shared.app_state["settings"]
        self.assertNotIn("for_mode", s)
        self.assertNotIn("cloud/some-model", s["effort_by_model"])

    def test_the_ui_sends_the_shown_model(self):
        with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        # The save names the mode and the model shown (the variable names changed - the behaviour is what's checked;
        # pointed out in PR #1 by 123timsolotech).
        self.assertRegex(html, r"for_mode: \w+(\.mode)?, for_model: ")
        self.assertIn("your choice for ${d.model}", html)


class ModelSettingsSave(_Api):
    def setUp(self):
        super().setUp()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.ini = os.path.join(tmp, "models.ini")
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write("[*]\nc = 8192\n\n[Bonsai]\nmodel = x.gguf\nngl = 99\n")
        p = patch.object(llama_local, "_ini_path", return_value=self.ini)
        p.start()
        self.addCleanup(p.stop)

    def test_blank_boxes_are_sent_as_null_and_the_save_works(self):
        """The UI's exact request: blank boxes are null. That was a 422, shown as 'Save failed'."""
        r = self.client.post("/api/local-models/config",
                             json={"name": "Bonsai", "ngl": None, "ctx_size": 16384, "batch_size": None, "threads": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "success")
        with open(self.ini, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("16384", text)
        self.assertNotIn("ngl", text.split("[Bonsai]")[1])          # null cleared the override

    def test_a_bad_value_says_what_is_wrong(self):
        r = self.client.post("/api/local-models/config", json={"name": "Bonsai", "ctx_size": 100}).json()
        self.assertEqual(r["status"], "error")
        self.assertIn("ctx size must be at least 512", r["message"])
        r = self.client.post("/api/local-models/config", json={"name": "Nope", "ctx_size": 4096}).json()
        self.assertIn("No model named 'Nope'", r["message"])

    def test_the_ui_shows_the_server_message(self):
        with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("toast('Save failed.')", html)
        self.assertIn("Array.isArray(data.detail)", html)


if __name__ == "__main__":
    unittest.main()

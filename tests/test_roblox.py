"""Roblox Studio integration (Phase 3.7): the bridge's sessions, deadlines and
cancellation; the adapter's tools (tree, windowed reads, patch edits with
compare-and-set, playtests); script edits in the undo trail; approval
previews; and an end-to-end replay of a Roblox flow against FakeStudio (a test
double speaking the plugin's protocol to the real bridge)."""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app_registry
import approval_view
import permissions
import replay_harness as rh
import roblox_adapter
import roblox_bridge
import undo_store
from testing_fakes import FakeStudio

LONG = "\n".join(f"local v{i} = {i}" for i in range(1, 1001))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        roblox_bridge._reset_for_tests()
        self.addCleanup(roblox_bridge._reset_for_tests)

    def _serve_once(self, instance, answer="pong"):
        got = {}

        def plugin():
            for _ in range(20):
                cmd = roblox_bridge.poll(wait=1, version=3, instance=instance)
                if cmd:
                    got["cmd"] = cmd
                    roblox_bridge.post_result(roblox_bridge.ResultIn(id=cmd["id"], ok=True, message=answer, instance=instance))
                    return
        t = threading.Thread(target=plugin)
        t.start()
        return t, got

    def test_commands_go_to_the_place_in_front(self):
        roblox_bridge.record_context({"instance": "a", "version": 3, "place": "Obby World"})
        roblox_bridge.record_context({"instance": "b", "version": 3, "place": "Tycoon"})
        roblox_bridge.set_focus_hint("Obby World - Roblox Studio")
        self.assertEqual(roblox_bridge.primary_place()["place"], "Obby World")
        t, got = self._serve_once("a")
        self.assertTrue(roblox_bridge.enqueue("ping", {}, timeout=5)["ok"])
        t.join()
        self.assertEqual(got["cmd"]["type"], "ping")
        self.assertEqual(roblox_bridge.poll(wait=1, instance="b"), {})           # the other window got nothing
        self.assertEqual(len(roblox_bridge.status_dict()["sessions"]), 2)

    def test_a_timed_out_command_never_runs_later(self):
        roblox_bridge.record_context({"instance": "a", "version": 3, "place": "P"})
        res = roblox_bridge.enqueue("delete_instance", {"path": "Workspace/Part"}, timeout=0.3)
        self.assertTrue(res["timeout"])
        self.assertIn("nothing ran", res["message"])
        self.assertEqual(roblox_bridge.poll(wait=1, instance="a"), {})

    def test_stop_cancels_a_queued_command(self):
        roblox_bridge.record_context({"instance": "a", "version": 3, "place": "P"})
        res = roblox_bridge.enqueue("set_property", {"path": "x"}, timeout=10, cancelled=lambda: True)
        self.assertTrue(res["cancelled"])
        self.assertIn("nothing was sent", res["message"])
        self.assertEqual(roblox_bridge.poll(wait=1, instance="a"), {})

    def test_late_results_are_dropped(self):
        self.assertEqual(roblox_bridge.post_result(roblox_bridge.ResultIn(id="gone", ok=True))["status"], "late")
        self.assertEqual(roblox_bridge._results, {})

    def test_v3_commands_need_a_v3_plugin(self):
        roblox_bridge.record_context({"instance": "old", "version": 2, "place": "P"})
        res = roblox_bridge.enqueue("get_script_source", {"path": "x"})
        self.assertIn("out of date", res["message"])
        self.assertTrue(roblox_bridge.status_dict()["outdated_plugin"])

    def test_playtest_copies_mark_running_and_tag_errors(self):
        roblox_bridge.record_context({"instance": "a", "version": 3, "place": "P", "running": False})
        roblox_bridge.record_context({"instance": "srv", "role": "play-server", "version": 3, "place": "P",
                                      "errors": [{"text": "boom", "trace": "Script:3", "script": "ServerScriptService/S"}]})
        self.assertTrue(roblox_bridge.latest_context()["running"])
        self.assertEqual(roblox_bridge.latest_context()["place"], "P")
        [err] = roblox_bridge.recent_errors()
        self.assertEqual((err["text"], err["trace"]), ("[play-server] boom", "Script:3"))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        roblox_bridge._reset_for_tests()
        self.studio = FakeStudio({"ServerScriptService/Lava": ("Script", "local lava = 1\nprint('touch')\n"),
                                  "ServerScriptService/Long": ("Script", LONG)}).start()
        self.addCleanup(roblox_bridge._reset_for_tests)
        self.addCleanup(self.studio.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.undo = undo_store.UndoStore(os.path.join(self.tmp.name, "undo"))
        self.a = roblox_adapter.adapter

    def _journaled(self, tool, args):
        change = self.undo.begin(tool, tool)
        try:
            res = self.a.call(tool, args, journal=change)
        except Exception:
            self.undo.rollback(change)
            raise
        if res.get("ok"):
            return res, self.undo.commit(change)
        self.undo.rollback(change)
        return res, None

    def test_registered_as_the_only_adapter(self):
        self.assertIs(app_registry.adapter_for_tool("roblox_edit_script"), self.a)
        self.assertEqual([a.app_id for a in app_registry.adapters()], ["roblox"])     # Blender/Figma: interface only
        with self.assertRaises(TypeError):
            app_registry.AppAdapter()

    def test_reads_say_what_they_left_out(self):
        res = self.a.call("roblox_read_script", {"path": "ServerScriptService/Long"})
        self.assertIn("lines 1-400 of 1000", res["message"])
        self.assertIn("NOT SHOWN: lines 401-1000", res["message"])
        self.assertIn("start_line=401", res["message"])
        more = self.a.call("roblox_read_script", {"path": "ServerScriptService/Long", "start_line": 401, "end_line": 420})
        self.assertIn(" 401  local v401 = 401", more["message"])
        self.assertNotIn("NOT SHOWN", self.a.call("roblox_read_script", {"path": "ServerScriptService/Lava"})["message"])

    def test_script_code_never_changes_through_properties(self):
        """Phase 3 review: set_property("Source") and a Script made by
        create_instance would skip the diff, the compare-and-set and undo."""
        for tool, args in (("roblox_set_property", {"path": "ServerScriptService/Lava", "property": "Source", "value": "x"}),
                           ("roblox_set_property", {"path": "ServerScriptService/Lava", "property": "source", "value": "x"}),
                           ("roblox_create_instance", {"class_name": "Script", "parent_path": "ServerScriptService"}),
                           ("roblox_create_instance", {"class_name": "Part", "parent_path": "Workspace",
                                                       "properties": {"Source": "x"}})):
            res = self.a.call(tool, args)
            self.assertFalse(res["ok"], (tool, args))
            self.assertIn("roblox_edit_script", res["message"])
        self.assertEqual(self.studio.source("ServerScriptService/Lava"), "local lava = 1\nprint('touch')\n")

    def test_tree(self):
        res = self.a.call("roblox_get_tree", {"depth": 2})
        self.assertIn("Lava (Script)", res["message"])
        self.assertIn("[ServerScriptService/Lava]", res["message"])

    def test_edit_is_patched_journaled_and_undone(self):
        res, change = self._journaled("roblox_edit_script", {"path": "ServerScriptService/Lava",
                                                            "edits": [{"find": "local lava = 1", "replace": "local lava = 2"}]})
        self.assertTrue(res["ok"] and res["verified"], res)
        self.assertEqual(self.studio.source("ServerScriptService/Lava"), "local lava = 2\nprint('touch')\n")
        self.assertTrue(change)
        [rec] = self.undo.list()
        self.assertIn("Roblox: ServerScriptService/Lava", rec["paths"])
        self.assertTrue(self.undo.undo(change)["ok"])
        self.assertEqual(self.studio.source("ServerScriptService/Lava"), "local lava = 1\nprint('touch')\n")

    def test_undo_refuses_to_overwrite_a_later_edit(self):
        _res, change = self._journaled("roblox_edit_script", {"path": "ServerScriptService/Lava",
                                                             "edits": [{"find": "print('touch')", "replace": "print('ouch')"}]})
        self.studio.edit_in_studio("ServerScriptService/Lava", "-- the user rewrote it\n")
        res = self.undo.undo(change)
        self.assertTrue(res["conflict"])
        self.assertEqual(self.studio.source("ServerScriptService/Lava"), "-- the user rewrote it\n")
        self.assertTrue(self.undo.undo(change, force=True)["ok"])

    def test_undo_in_another_place_is_refused(self):
        _res, change = self._journaled("roblox_edit_script", {"path": "ServerScriptService/Lava",
                                                             "edits": [{"find": "1", "replace": "3"}]})
        self.studio.stop()
        roblox_bridge._reset_for_tests()
        other = FakeStudio({"ServerScriptService/Lava": ("Script", "local lava = 3\nprint('touch')\n")},
                           place="Other Game", place_id=7).start()
        self.addCleanup(other.stop)
        res = self.undo.undo(change)
        self.assertFalse(res["ok"])
        self.assertIn("Obby World", res["message"])
        self.assertEqual(other.source("ServerScriptService/Lava"), "local lava = 3\nprint('touch')\n")

    def test_a_write_refused_by_studio_rolls_nothing_back(self):
        """Studio refuses the write (the script changed after it was read):
        the rollback must not put the old text over the user's edit."""
        real = self.studio.handle

        def edit_between(cmd):
            if cmd["type"] == "set_script_source":
                self.studio.edit_in_studio("ServerScriptService/Lava", "-- typed meanwhile\n")
            return real(cmd)
        self.studio.handle = edit_between
        res, change = self._journaled("roblox_set_script_source", {"path": "ServerScriptService/Lava", "source": "x = 1\n"})
        self.assertFalse(res["ok"])
        self.assertIn("changed since", res["message"])
        self.assertIsNone(change)
        self.assertEqual(self.studio.source("ServerScriptService/Lava"), "-- typed meanwhile\n")

    def test_created_script_is_undoable_and_names_are_unique(self):
        res, change = self._journaled("roblox_create_script", {"class_name": "Script", "parent_path": "ServerScriptService",
                                                              "name": "Spawner", "source": "print('spawn')"})
        self.assertTrue(res["ok"])
        self.assertFalse(self.a.call("roblox_create_script", {"parent_path": "ServerScriptService", "name": "Spawner",
                                                              "source": "x"})["ok"])
        self.assertTrue(self.undo.undo(change)["ok"])
        self.assertIsNone(self.studio.source("ServerScriptService/Spawner"))

    def test_playtest_reports_errors_with_traces(self):
        self.studio.play_errors = [{"text": "ServerScriptService.Lava:2: attempt to index nil with 'Humanoid'",
                                    "trace": "ServerScriptService.Lava, line 2"}]
        res = self.a.call("roblox_run_playtest", {"mode": "play", "seconds": 5})
        self.assertIn("1 error(s)", res["message"])
        self.assertIn("attempt to index nil", res["message"])
        self.assertIn("line 2", res["message"])
        self.assertIn("Humanoid", self.a.call("roblox_get_output", {})["message"])

    def test_preview_is_the_real_diff(self):
        shown = self.a.preview("roblox_edit_script", {"path": "ServerScriptService/Lava",
                                                      "edits": [{"find": "local lava = 1", "replace": "local lava = 5"}]})
        self.assertEqual(shown["kind"], "diff")
        self.assertIn("-local lava = 1", shown["text"])
        self.assertIn("+local lava = 5", shown["text"])
        self.assertEqual(shown["fingerprint"], undo_store.content_fingerprint("local lava = 1\nprint('touch')\n"))


class ApprovalWordingTests(unittest.TestCase):
    def test_undo_honesty(self):
        edit = approval_view.describe("roblox_edit_script", {"path": "x"}, undoable=True)
        prop = approval_view.describe("roblox_set_property", {"path": "x"}, undoable=False, mutating=True)
        play = approval_view.describe("roblox_run_playtest", {}, undoable=False, mutating=True)
        self.assertEqual(edit["undo_text"], "Can be undone")
        self.assertTrue(prop["undo_text"].startswith("Not undoable"))
        self.assertIn("Ctrl+Z in Studio", prop["undo_text"])
        self.assertEqual(play["undo_text"], "Not undoable")

    def test_tiers(self):
        self.assertEqual(permissions.classify("roblox_edit_script"), "destructive")
        self.assertEqual(permissions.classify("roblox_run_playtest"), "input")
        self.assertEqual(permissions.classify("roblox_get_tree"), "benign")
        self.assertEqual(permissions.decide("roblox_edit_script", {}), "ask")
        self.assertEqual(permissions.decide("roblox_run_playtest", {}), "ask")

    def test_registry_marks_script_edits_undoable(self):
        import tools.runtime as runtime
        for name in ("roblox_edit_script", "roblox_set_script_source", "roblox_create_script"):
            self.assertTrue(runtime.TOOL_REGISTRY.get(name).undoable, name)
        for name in ("roblox_set_property", "roblox_delete_instance", "roblox_create_instance", "roblox_run_playtest"):
            self.assertFalse(runtime.TOOL_REGISTRY.get(name).undoable, name)


class OfferedToolsTests(unittest.TestCase):
    def test_only_status_is_offered_while_studio_is_not_connected(self):
        import core.state as shared
        import tools.schemas as schemas
        keys = ("turn_policy", "current_user_request", "chat_skills", "turn_skills", "chat_skill_seen", "chat_turn_no")
        saved = {k: shared.app_state.get(k) for k in keys}
        self.addCleanup(lambda: shared.app_state.update(saved))
        shared.app_state["turn_policy"] = {"mode": "action", "allow_mutation": True, "reason": "t"}
        # A Studio request (the roblox skill, skills.py) - what is offered of it depends on the bridge.
        shared.app_state.update(current_user_request="check my Roblox Studio place", chat_skills=[], turn_skills=None)
        with patch.object(roblox_bridge, "is_connected", return_value=False):
            names = {t["function"]["name"] for t in schemas.tools_for_current_turn()}
        self.assertEqual({n for n in names if n.startswith("roblox_")}, {"roblox_status"})
        with patch.object(roblox_bridge, "is_connected", return_value=True):
            names = {t["function"]["name"] for t in schemas.tools_for_current_turn()}
        self.assertTrue({"roblox_edit_script", "roblox_run_playtest", "roblox_get_tree"} <= names)


def _call(cid, name, args):
    return {"finish_reason": "tool_calls", "tool_calls": [{"id": cid, "name": name, "arguments": json.dumps(args)}]}


class RobloxFlowReplayTests(unittest.TestCase):
    """End to end: the agent reads a script, edits it (approval card with the
    real diff, "Can be undone"), playtests (approval, "Not undoable"), and the
    edit is undone from the undo trail."""

    def setUp(self):
        roblox_bridge._reset_for_tests()
        self.addCleanup(roblox_bridge._reset_for_tests)
        self.studio = FakeStudio({"ServerScriptService/Lava": ("Script", "local function onTouch(hit)\n\thit.Parent.Humanoid.Health = 0\nend\n")},
                                 play_errors=[]).start()
        self.addCleanup(self.studio.stop)

    def test_read_edit_playtest_then_undo(self):
        fixed = "local function onTouch(hit)\n\tlocal h = hit.Parent:FindFirstChildOfClass('Humanoid')\n\tif h then h.Health = 0 end\nend\n"
        fx = {"format": "agentic-replay/1",
              "meta": {"name": "roblox flow", "provenance": "reconstructed", "model": "unrecorded",
                       "evidence": "Illustrative Roblox flow for the Phase 3.7 exit criterion; FakeStudio stands in for Studio."},
              "setup": {"mode": "local", "entry": "agent", "roblox": "fake",
                        "settings": {"effort": "low", "prompt_enhancement": "off"}},
              "request": "Fix the lava script so it doesn't error when a part without a Humanoid touches it, then playtest",
              "responses": [
                  _call("r1", "roblox_read_script", {"path": "ServerScriptService/Lava"}),
                  _call("e1", "roblox_edit_script", {"path": "ServerScriptService/Lava", "justification": "Guard the Humanoid lookup",
                                                     "edits": [{"find": "\thit.Parent.Humanoid.Health = 0",
                                                                "replace": "\tlocal h = hit.Parent:FindFirstChildOfClass('Humanoid')\n\tif h then h.Health = 0 end"}]}),
                  _call("p1", "roblox_run_playtest", {"mode": "play", "seconds": 5, "justification": "Check for errors"}),
                  {"finish_reason": "stop", "content": "Fixed and playtested with no errors."}],
              "approvals": [True, True], "tools": {"mode": "real"}}
        with tempfile.TemporaryDirectory() as sandbox:
            run = rh.run_fixture(fx, sandbox=sandbox)
            self.assertEqual(run.status, "success", run.message)
            self.assertEqual(self.studio.source("ServerScriptService/Lava"), fixed)
            cards = [a[0] for a in run.approvals]
            self.assertEqual([c["tool_name"] for c in cards], ["roblox_edit_script", "roblox_run_playtest"])
            edit_view, play_view = cards[0]["view"], cards[1]["view"]
            self.assertEqual(edit_view["preview"]["kind"], "diff")
            self.assertIn("+\tif h then h.Health = 0 end", edit_view["preview"]["text"])
            self.assertEqual(edit_view["undo_text"], "Can be undone")
            self.assertEqual(play_view["undo_text"], "Not undoable")
            self.assertIn("run_playtest", self.studio.commands)
            tool_events = [e for e in run.events if e.get("kind") == "tool" and e.get("tool") == "roblox_edit_script"]
            undo_id = tool_events[-1].get("undo_id")
            self.assertTrue(undo_id, "the script edit must be in the undo trail")
            res = undo_store.UndoStore(os.path.join(sandbox, ".undo")).undo(undo_id)
            self.assertTrue(res["ok"], res)
            self.assertEqual(self.studio.source("ServerScriptService/Lava"),
                             "local function onTouch(hit)\n\thit.Parent.Humanoid.Health = 0\nend\n")

    def test_a_change_in_studio_after_approval_is_not_overwritten(self):
        real = self.studio.handle
        state = {"reads": 0}

        def handle(cmd):
            # The user types in Studio after the card was shown, before the edit runs.
            if cmd["type"] == "get_script_source":
                state["reads"] += 1
                if state["reads"] == 2:
                    self.studio.edit_in_studio("ServerScriptService/Lava", "-- my own fix\n")
            return real(cmd)
        self.studio.handle = handle
        fx = {"format": "agentic-replay/1",
              "meta": {"name": "roblox changed after approval", "provenance": "reconstructed", "model": "unrecorded", "evidence": "test"},
              "setup": {"mode": "local", "entry": "agent", "roblox": "fake", "settings": {"effort": "low", "prompt_enhancement": "off"}},
              "request": "Change the lava damage script",
              "responses": [_call("e1", "roblox_edit_script", {"path": "ServerScriptService/Lava", "justification": "x",
                                                               "edits": [{"find": "Health = 0", "replace": "Health -= 50"}]}),
                            {"finish_reason": "stop", "content": "Stopped."}],
              "approvals": [True], "tools": {"mode": "real"}}
        run = rh.run_fixture(fx)
        self.assertEqual(self.studio.source("ServerScriptService/Lava"), "-- my own fix\n")
        tool_msgs = [m for m in run.conversation if m.get("role") == "tool"]
        self.assertIn("changed after the user approved", tool_msgs[-1]["content"])



class ProjectLinkTests(unittest.TestCase):
    def test_the_open_projects_folder_is_searched_for_places(self):
        import roblox_places
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "Obby World.rbxl"), "w").close()
            roblox_places._cache.update(at=0.0, dirs=(), places=[])
            with patch.object(roblox_places, "extra_dirs", lambda: [folder]):
                match, _ = roblox_places.resolve_place("obby world")
            roblox_places._cache.update(at=0.0, dirs=(), places=[])
        self.assertEqual(match["path"], os.path.join(folder, "Obby World.rbxl"))

    def test_the_brief_notes_a_different_place_in_studio(self):
        roblox_bridge._reset_for_tests()
        self.addCleanup(roblox_bridge._reset_for_tests)
        studio = FakeStudio({}, place="Tycoon").start()
        self.addCleanup(studio.stop)
        fx = {"format": "agentic-replay/1",
              "meta": {"name": "place mismatch", "provenance": "reconstructed", "model": "unrecorded", "evidence": "test"},
              "setup": {"mode": "local", "entry": "agent", "roblox": "fake", "settings": {"effort": "low", "prompt_enhancement": "off"},
                        "app_state": {"active_project": {"id": "c" * 12, "name": "Obby", "folder": "", "files": [],
                                                         "instructions": "", "notes": "", "roblox_place": "Obby World",
                                                         "effort": None, "prompt_prep": None}}},
              "request": "what should I work on next in my game?", "responses": [{"finish_reason": "stop", "content": "Ok."}]}
        run = rh.run_fixture(fx)
        self.assertIn("Roblox Studio has 'Tycoon' open, not this project's place 'Obby World'",
                      json.dumps([r.get("messages") for r in run.requests]))


if __name__ == "__main__":
    unittest.main()

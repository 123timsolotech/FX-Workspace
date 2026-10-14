"""llama_builds.py (ARCHITECTURE D51): the newest llama.cpp runs by default; a model only PrismML's fork loads
(Ternary Bonsai 2) restarts the router on the fork with the same command line, and the user's next pick of
another model moves it back. Helper calls never move it back, a pinned build stays, a build that doesn't come
up brings the old one back, and nothing here starts a real process or writes into data/."""
import io
import os
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import core.config as config
import core.state as shared
import llama_builds
import llama_local

HERE = os.path.dirname(os.path.abspath(__file__))
LATEST = r"E:\ClankerLocalCpp\llama-server.exe"
PRISM = r"E:\ClankerLocalCpp\prism\llama-server.exe"
CMD = [PRISM, "--host", "127.0.0.1", "--port", "8090", "--models-preset", r"C:\app\models.ini", "--models-max", "1",
       "--jinja", "--parallel", "1"]


def installed(*paths):
    return patch.object(llama_builds, "_exists", side_effect=lambda p: p in paths)


class BuildTests(unittest.TestCase):
    def setUp(self):
        llama_builds.forget()
        self.addCleanup(llama_builds.forget)
        settings = patch.dict(shared.app_state["settings"], {"llama_build": "auto"})
        settings.start()
        self.addCleanup(settings.stop)
        started = dict(llama_builds._started)
        self.addCleanup(llama_builds._started.update, started)

    def running_on(self, exe, pid=4242, cmdline=None):
        return patch.object(llama_builds, "_find_router",
                            return_value={"pid": pid, "exe": exe, "cmdline": cmdline or [exe] + CMD[1:]})

    def test_builds_are_told_apart_by_their_folder(self):
        self.assertEqual(llama_builds.kind_of(PRISM), "prism")
        self.assertEqual(llama_builds.kind_of(LATEST), "latest")
        self.assertEqual(llama_builds.kind_of(r"C:\llama.cpp\llama-server.exe"), "latest")
        self.assertIsNone(llama_builds.kind_of(None))
        with installed(LATEST, PRISM):
            self.assertEqual((llama_builds.exe_of("latest"), llama_builds.exe_of("prism")), (LATEST, PRISM))
        with installed(r"C:\llama.cpp\llama-server.exe"):
            self.assertEqual(llama_builds.exe_of("latest"), r"C:\llama.cpp\llama-server.exe")
            self.assertIsNone(llama_builds.exe_of("prism"))

    def test_auto_wants_prism_only_for_the_models_only_it_loads(self):
        with installed(LATEST, PRISM):
            self.assertEqual(llama_builds.wanted("Ternary-Bonsai-2-27B"), "prism")       # via its models.ini path
            self.assertEqual(llama_builds.wanted("Mistral-7B"), "latest")
            self.assertEqual(llama_builds.wanted("Mistral-7B", "prism"), "prism")         # a pinned build stays
            self.assertEqual(llama_builds.wanted("Ternary-Bonsai-2-27B", "latest"), "latest")
        with installed(LATEST):
            self.assertIsNone(llama_builds.wanted("Ternary-Bonsai-2-27B"))               # nothing to switch to

    def test_bonsai_moves_the_router_to_prism_and_the_next_pick_moves_it_back(self):
        told = []
        with installed(LATEST, PRISM), self.running_on(LATEST), patch.object(llama_builds, "switch") as sw:
            self.assertEqual(llama_builds.ensure_for("Ternary-Bonsai-2-27B", on_switch=told.append), "prism")
            sw.assert_called_once()
            self.assertEqual(sw.call_args[0][0], "prism")
            self.assertEqual(told, ["prism"])
        llama_builds.forget()
        with installed(LATEST, PRISM), self.running_on(PRISM), patch.object(llama_builds, "switch") as sw:
            self.assertIsNone(llama_builds.ensure_for("Mistral-7B", back=False))           # a helper call: stays
            sw.assert_not_called()
            self.assertIsNone(llama_builds.ensure_for("Ternary-Bonsai-2-27B"))            # already there
            sw.assert_not_called()
            self.assertEqual(llama_builds.ensure_for("Mistral-7B"), "latest")             # the user's pick: back
            self.assertEqual(sw.call_args[0][0], "latest")

    def test_no_switch_when_the_running_build_is_unknown_or_pinned(self):
        with installed(LATEST, PRISM), patch.object(llama_builds, "_find_router", return_value=None), \
                patch.object(llama_local, "server_executable", return_value=None), \
                patch.object(llama_builds, "switch") as sw:
            self.assertIsNone(llama_builds.ensure_for("Ternary-Bonsai-2-27B"))
            sw.assert_not_called()
        llama_builds.forget()
        with installed(LATEST, PRISM), self.running_on(LATEST), patch.object(llama_builds, "switch") as sw, \
                patch.dict(shared.app_state["settings"], {"llama_build": "latest"}):
            self.assertIsNone(llama_builds.ensure_for("Ternary-Bonsai-2-27B"))
            sw.assert_not_called()

    def test_a_switch_restarts_with_the_old_routers_own_command_line(self):
        popen = MagicMock()
        popen.return_value.poll.return_value = None
        with installed(LATEST, PRISM), patch.object(llama_builds.subprocess, "run") as run, \
                patch.object(llama_builds.subprocess, "Popen", popen), \
                patch.object(llama_builds, "_wait_ready", return_value=True), \
                patch.object(llama_builds, "_answers", return_value=False), \
                patch.dict(os.environ, {"AGENTIC_WINDOWLESS": "1"}), \
                patch.object(llama_builds, "_log", return_value=io.StringIO()):
            llama_builds.switch("latest", {"kind": "prism", "exe": PRISM, "pid": 4242, "cmdline": CMD})
        self.assertEqual(run.call_args_list[0][0][0], ["taskkill", "/PID", "4242", "/T", "/F"])
        args = popen.call_args[0][0]
        self.assertEqual(args, [LATEST] + CMD[1:])                     # same models.ini, port and flags
        self.assertNotIn("--ctx-size", args)
        self.assertEqual(popen.call_args[1]["creationflags"], llama_builds.NO_WINDOW)

    def test_under_run_bat_the_new_router_gets_its_own_minimized_window(self):
        with installed(LATEST, PRISM), patch.object(llama_builds.subprocess, "run"), \
                patch.object(llama_builds.subprocess, "Popen") as popen, \
                patch.object(llama_builds, "_wait_ready", return_value=True), \
                patch.object(llama_builds, "_answers", return_value=False), \
                patch.dict(os.environ, {"AGENTIC_WINDOWLESS": ""}):
            llama_builds.switch("prism", {"kind": "latest", "exe": LATEST, "pid": 7, "cmdline": [LATEST] + CMD[1:]})
        args = popen.call_args[0][0]
        self.assertEqual(args[:6], ["cmd", "/c", "start", "llama-server (router)", "/min", PRISM])
        self.assertEqual(args[6:], CMD[1:])
        self.assertIsNone(llama_builds._started["proc"])               # it outlives the app, like run.bat's

    def test_without_the_old_command_line_it_uses_run_bats(self):
        args = llama_builds._args_like({"cmdline": []})
        self.assertIn("--models-preset", args)
        self.assertEqual(args[args.index("--models-max") + 1], "1")
        self.assertNotIn("--ctx-size", args)

    def test_a_build_that_does_not_come_up_brings_the_old_one_back(self):
        starts = []
        with installed(LATEST, PRISM), patch.object(llama_builds.subprocess, "run"), \
                patch.object(llama_builds, "_start", side_effect=lambda exe, args: starts.append(exe)), \
                patch.object(llama_builds, "_wait_ready", side_effect=[False, True]), \
                patch.object(llama_builds, "_answers", return_value=False):
            with self.assertRaises(RuntimeError) as cm:
                llama_builds.switch("prism", {"kind": "latest", "exe": LATEST, "pid": 7, "cmdline": [LATEST] + CMD[1:]})
        self.assertEqual(starts, [PRISM, LATEST])
        self.assertIn("didn't start", str(cm.exception))

    def test_a_router_it_cannot_find_is_left_alone(self):
        with installed(LATEST, PRISM), patch.object(llama_builds.subprocess, "run") as run, \
                patch.object(llama_builds, "_start") as start, patch.object(llama_builds, "_answers", return_value=True):
            with self.assertRaises(RuntimeError) as cm:
                llama_builds.switch("prism", {"kind": "latest", "exe": LATEST, "pid": None, "cmdline": []})
        run.assert_not_called()
        start.assert_not_called()                                       # no second router fighting for the port
        self.assertIn("Couldn't find", str(cm.exception))

    def test_only_the_router_this_app_started_stops_with_it(self):
        proc = MagicMock(pid=99)
        proc.poll.return_value = None
        llama_builds._started["proc"] = proc
        with patch.object(llama_builds.subprocess, "run") as run:
            llama_builds.stop_started()
        self.assertEqual(run.call_args[0][0], ["taskkill", "/PID", "99", "/T", "/F"])
        self.assertIsNone(llama_builds._started["proc"])
        with patch.object(llama_builds.subprocess, "run") as run:
            llama_builds.stop_started()                                 # nothing started: nothing stopped
        run.assert_not_called()

    def test_the_version_is_read_from_both_output_styles(self):
        llama_builds._versions.clear()
        self.addCleanup(llama_builds._versions.clear)
        for text, want in (("version: 1 (build 11541, commit c5ccdd2)\nbuilt with MSVC", "b11541"),
                           ("version: 10770 (6684606)\nbuilt with MSVC", "b10770"), ("nonsense", "")):
            llama_builds._versions.clear()
            done = subprocess.CompletedProcess([], 0, stdout="", stderr=text)
            with patch.object(llama_builds.subprocess, "run", return_value=done):
                self.assertEqual(llama_builds.version(LATEST), want)
        self.assertEqual(llama_builds.version(None), "")

    def test_force_load_switches_first_and_an_eviction_never_moves_back(self):
        resp = MagicMock()
        with patch.object(llama_builds, "ensure_for") as ensure, patch.object(llama_local.requests, "post", return_value=resp):
            llama_local.force_load("Ternary-Bonsai-2-27B", timeout=1)
            ensure.assert_called_with("Ternary-Bonsai-2-27B", back=True)
            llama_local.force_load("Mistral-7B", timeout=1, back=False)
            ensure.assert_called_with("Mistral-7B", back=False)

    def test_the_chat_shows_the_engine_starting(self):
        import agent.models as llm
        ev = {"status": "running", "kind": "model"}

        def fake(model, back=True, on_switch=None):
            on_switch("prism")
            self.assertEqual(ev.get("engine"), "PrismML's llama.cpp")
            self.assertEqual((ev.get("phase"), ev.get("loading")), ("loading", model))
            return "prism"
        with patch.dict(shared.app_state, {"running_model_event": ev}), patch.object(llama_builds, "ensure_for", side_effect=fake):
            self.assertEqual(llm._ensure_build("Ternary-Bonsai-2-27B"), "prism")
        self.assertIsNone(ev.get("engine"))

    def test_the_setting_is_cleaned(self):
        self.assertEqual(config.DEFAULT_SETTINGS["llama_build"], "auto")
        self.assertEqual(config.clean_agent_settings({"llama_build": "prism"})["llama_build"], "prism")
        self.assertEqual(config.clean_agent_settings({"llama_build": "rm -rf"})["llama_build"], "auto")

    def test_newer_routers_say_which_models_read_images(self):
        st = llama_local._router_status({"data": [
            {"id": "Ornith", "status": {"value": "unloaded"}, "architecture": {"input_modalities": ["text", "image"]}},
            {"id": "Mistral-7B", "status": {"value": "unloaded"}, "architecture": {"input_modalities": ["text"]}},
            {"id": "Old", "status": {"value": "loaded", "args": [LATEST, "--mmproj", "x.mmproj"]}}]})
        self.assertTrue(st["Ornith"]["mmproj"])
        self.assertFalse(st["Mistral-7B"]["mmproj"])
        self.assertTrue(st["Old"]["mmproj"])

    def test_settings_page_offers_the_choice(self):
        with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn("function llamaBuildGroup()", page)
        self.assertIn("/api/llama/builds/apply", page)
        self.assertIn("Starting ${ev.engine} for", page)


if __name__ == "__main__":
    unittest.main()

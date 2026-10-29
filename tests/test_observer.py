"""The proactive observer (Phase 3.10): off means off, quiet hours, precise and
persistent "Don't suggest this again", feedback that never silently mutes,
opt-in signals that can't be forced on, no window-title fragments saved, the
new signals (repeated switching, idle after an error, project folders, a
project's Roblox place, routines at their usual time), a traceable one-line
reason on every suggestion, and no activity summary for cloud models."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import awareness
from test_awareness import Clock, FakeRoblox, make_engine


class OffAndQuietTests(unittest.TestCase):
    def test_off_reads_nothing_and_forgets(self):
        clock = Clock()
        reads = {"fg": 0, "idle": 0}
        settings = dict(awareness.DEFAULT_SETTINGS)

        def fg():
            reads["fg"] += 1
            return {"exe": "code.exe", "title": "secret.py - VS Code", "hwnd": 1}

        def idle():
            reads["idle"] += 1
            return 0.5
        roblox = FakeRoblox()
        roblox.set_focus_hint = lambda title: reads.__setitem__("hint", title)
        engine = awareness.AwarenessEngine(clock=clock, foreground_fn=fg, idle_fn=idle, settings_fn=lambda: settings,
                                           roblox=roblox)
        for _ in range(3):
            clock.advance(2)
            engine.tick()
        self.assertTrue(engine.sessions)
        settings["proactive_enabled"] = False
        before = dict(reads)
        for _ in range(5):
            clock.advance(2)
            self.assertEqual(engine.tick(), [])
        self.assertEqual((reads["fg"], reads["idle"]), (before["fg"], before["idle"]))    # nothing sampled
        self.assertFalse(engine.sessions)                                                 # dropped from memory
        self.assertIsNone(engine.obs)
        self.assertEqual(engine.activity_line(), "")

    def test_quiet_hours_hold_suggestions(self):
        clock = Clock(time.mktime((2026, 9, 21, 23, 30, 0, 0, 0, -1)))
        settings = dict(awareness.DEFAULT_SETTINGS, proactive_quiet_hours="23:00-08:00")
        engine, _, _ = make_engine(clock, settings=settings, fg={"exe": "explorer.exe", "title": "Desktop", "hwnd": 2},
                                   disks_fn=lambda: [{"mount": "C:\\", "free_gb": 2, "percent_free": 1}])
        self.assertEqual(engine.tick(), [])
        settings["proactive_quiet_hours"] = ""
        engine.scratch.clear()
        self.assertTrue([s for s in engine.tick() if s.detector == "low_disk"])


class FeedbackTests(unittest.TestCase):
    def _download_engine(self, clock, folder, store=None):
        return make_engine(clock, downloads_dir=folder, store_path=store,
                           fg={"exe": "explorer.exe", "title": "Desktop", "hwnd": 2})[0]

    def _new_file(self, engine, clock, folder, name):
        with open(os.path.join(folder, name), "wb") as f:
            f.write(b"x" * 100)
        clock.advance(16)
        engine.tick()
        clock.advance(16)
        return [s for s in engine.tick() if s.detector == "new_download"]

    def test_dismissing_spaces_out_but_never_silently_mutes(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            engine = self._download_engine(clock, folder)
            engine.tick()
            for i in range(6):
                [s] = self._new_file(engine, clock, folder, f"f{i}.zip")
                engine.dismiss(s.id)
                clock.advance(3 * 3600)                   # longer than the (lengthened) cooldown
            self.assertEqual(engine.detector_state("new_download"), "less_often")
            self.assertTrue(self._new_file(engine, clock, folder, "last.zip"))     # still offered

    def test_mutes_and_dismissals_survive_a_restart_and_can_be_undone(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            store = os.path.join(folder, "data", "awareness.json")
            windows = ["Game (Not Responding)"]
            engine, _, _ = make_engine(clock, store_path=store, windows_fn=lambda: windows,
                                       fg={"exe": "explorer.exe", "title": "Desktop", "hwnd": 2})
            engine.tick(); clock.advance(13)
            s = engine.tick()[0]
            engine.dismiss(s.id, forever=True)
            again, _, _ = make_engine(clock, store_path=store, windows_fn=lambda: windows,
                                      fg={"exe": "explorer.exe", "title": "Desktop", "hwnd": 2})
            [muted] = again.muted()
            self.assertEqual(muted["key"], awareness._pk("not_responding:Game"))
            self.assertEqual(muted["title"], "Frozen apps")              # no window title kept
            with open(store, encoding="utf-8") as f:
                self.assertNotIn("Game", f.read())                       # nor anywhere else on disk
            clock.advance(7 * 3600)
            again.tick(); clock.advance(13)
            self.assertEqual(again.tick(), [])
            self.assertTrue(again.unmute(muted["key"]))
            again.last_fired.clear()
            again.key_block_until.clear()
            clock.advance(13)
            self.assertTrue(again.tick())

    def test_keys_saved_in_the_clear_by_older_versions_are_hashed(self):
        """Stores written before the Phase 3 review held keys (with window titles) in the clear."""
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            store = os.path.join(folder, "awareness.json")
            with open(store, "w", encoding="utf-8") as f:
                json.dump({"muted": {"error_dialog:Error opening Salary_Review.pdf": {
                               "at": 1, "title": "Error opening Salary_Review.pdf", "detector": "error_dialog"}},
                           "blocked": {"not_responding:Payroll": clock() + 3600}}, f)
            engine, _, _ = make_engine(clock, store_path=store)
            with open(store, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("Salary", text)
            self.assertNotIn("Payroll", text)
            self.assertIn(awareness._pk("error_dialog:Error opening Salary_Review.pdf"), engine.store["muted"])
            self.assertGreater(engine.key_block_until[awareness._pk("not_responding:Payroll")], clock())

    def test_old_detector_wide_mutes_still_hold(self):
        clock = Clock()
        engine, _, _ = make_engine(clock)
        engine.store["weights"]["not_responding"] = 0.0          # saved by the old "Don't suggest this"
        self.assertFalse(engine.detector_enabled(awareness.DETECTORS["not_responding"], engine.settings()))
        self.assertEqual(engine.detector_state("not_responding"), "muted")

    def test_the_clipboard_cannot_be_forced_on(self):
        clock = Clock()
        settings = dict(awareness.DEFAULT_SETTINGS, proactive_clipboard=False,
                        proactive_detectors={"clipboard_error": True, "habit": True}, proactive_learn_habits=False)
        engine, _, _ = make_engine(clock, settings=settings)
        self.assertFalse(engine.detector_enabled(awareness.DETECTORS["clipboard_error"], engine.settings()))
        self.assertFalse(engine.detector_enabled(awareness.DETECTORS["habit"], engine.settings()))


class PrivacyTests(unittest.TestCase):
    def test_habits_keep_program_names_not_window_titles(self):
        clock = Clock()
        engine, state, _ = make_engine(clock, fg={"exe": "pdfviewer.exe", "title": "Bank Statement 2026 - Private Viewer", "hwnd": 5})
        engine.tick()
        state["fg"] = {"exe": "explorer.exe", "title": "Desktop", "hwnd": 2}
        engine.tick()
        self.assertEqual(list(engine.store["habits"]), ["pdfviewer"])
        self.assertNotIn("Bank", repr(engine.store))

    def test_cloud_models_get_no_activity_summary(self):
        import agent.models as llm
        import core.config as config
        import core.observer as observer
        import core.state as shared
        saved = shared.app_state.get("mode")
        self.addCleanup(lambda: shared.app_state.update(mode=saved))
        with patch.object(observer, "_activity_line", return_value="User activity: working in Roblox Studio"):
            shared.app_state["mode"] = config.MODE_LOCAL
            local = str(llm.build_messages())
            shared.app_state["mode"] = config.MODE_CLOUD
            cloud = str(llm.build_messages())
        self.assertIn("User activity: working in Roblox Studio", local)
        self.assertNotIn("User activity: working in Roblox Studio", cloud)

    def test_every_signal_is_declared(self):
        declared = set(awareness.SIGNALS)
        for d in awareness.DETECTORS.values():
            self.assertTrue(d.signals, d.id)
            self.assertLessEqual(set(d.signals), declared, d.id)
        used = {sig for d in awareness.DETECTORS.values() for sig in d.signals}
        self.assertEqual(used, declared)                          # nothing is read without a detector using it


class NewSignalTests(unittest.TestCase):
    def test_studio_errors_reason_names_the_signal(self):
        clock, roblox = Clock(), FakeRoblox()
        engine, _, _ = make_engine(clock, roblox=roblox)
        engine.tick()
        for line in (12, 14, 20):
            roblox.add_error(f"ServerScriptService.Main:{line}: attempt to index nil with 'Humanoid'", clock())
        clock.advance(5)
        [s] = [x for x in engine.tick() if x.detector == "studio_errors"]
        self.assertEqual(s.reason, "Studio's Output shows 3 nil-index errors in ServerScriptService.Main")

    def test_repeated_switching(self):
        clock = Clock()
        engine, state, _ = make_engine(clock, fg={"exe": "a.exe", "title": "A", "hwnd": 1})
        apps = [("robloxstudiobeta.exe", "Obby - Roblox Studio"), ("chrome.exe", "Docs - Chrome"), ("code.exe", "x - VS Code")]
        found = []
        for _ in range(3):
            for exe, title in apps:
                state["fg"] = {"exe": exe, "title": title, "hwnd": 1}
                clock.advance(20)
                found += [s for s in engine.tick() if s.detector == "repeated_actions"]
        [s] = found
        self.assertIn("three times", s.reason)
        self.assertEqual(s.actions[0]["kind"], "prompt")

    def test_idle_after_an_error(self):
        clock, roblox = Clock(), FakeRoblox()
        engine, state, _ = make_engine(clock, roblox=roblox)
        engine.tick()
        roblox.add_error("ServerScriptService.Main:3: attempt to call a nil value", clock())
        clock.advance(2)
        engine.tick()
        state["idle"] = 150
        clock.advance(150)
        [s] = [x for x in engine.tick() if x.detector == "idle_after_error"]
        self.assertIn("No input for 2 min since Roblox Studio's Output showed an error", s.reason)
        clock.advance(5)
        self.assertFalse([x for x in engine.tick() if x.detector == "idle_after_error"])    # once per error

    def test_project_folder_opened(self):
        clock = Clock()
        projects = [{"id": "p1", "name": "Obby World", "folder": "D:\\dev\\ObbyWorld", "roblox_place": ""}]
        engine, state, _ = make_engine(clock, projects_fn=lambda: projects, fg={"exe": "explorer.exe", "title": "ObbyWorld", "hwnd": 4})
        [s] = [x for x in engine.tick() if x.detector == "project_opened"]
        self.assertEqual(s.actions[0], {"label": "Open 'Obby World'", "kind": "project", "project": "p1", "name": "Obby World"})
        self.assertIn("the folder of your project 'Obby World'", s.reason)
        engine2, _, _ = make_engine(clock, projects_fn=lambda: projects, active_project_fn=lambda: {"id": "p1"},
                                    fg={"exe": "explorer.exe", "title": "ObbyWorld", "hwnd": 4})
        self.assertFalse([x for x in engine2.tick() if x.detector == "project_opened"])      # already open

    def test_studio_place_of_a_project(self):
        clock, roblox = Clock(), FakeRoblox()
        projects = [{"id": "p2", "name": "Obby", "folder": "", "roblox_place": "obby"}]
        engine, _, _ = make_engine(clock, roblox=roblox, projects_fn=lambda: projects)
        [s] = [x for x in engine.tick() if x.detector == "studio_project"]
        self.assertEqual(s.actions[0]["kind"], "project")
        self.assertIn("the place of your project 'Obby'", s.reason)

    def test_routine_at_its_usual_time(self):
        monday_0905 = time.mktime((2026, 9, 21, 9, 5, 0, 0, 0, -1))
        clock = Clock(monday_0905)
        runs = [{"at": monday_0905 - 3 * 86400 + 300}, {"at": monday_0905 - 2 * 86400 - 600}]
        routines = [{"id": "r1", "name": "Morning setup", "history": runs}]
        engine, _, _ = make_engine(clock, routines_fn=lambda: routines, fg={"exe": "explorer.exe", "title": "Desktop", "hwnd": 2})
        [s] = [x for x in engine.tick() if x.detector == "routine_time"]
        self.assertEqual(s.actions[0], {"label": "Run it", "kind": "routine", "routine": "r1", "name": "Morning setup"})
        self.assertIn("You ran 'Morning setup' around", s.reason)
        runs.append({"at": monday_0905 - 60})                   # already ran today
        engine.last_fired.clear(); engine.key_block_until.clear(); engine.suggestions.clear()
        self.assertFalse([x for x in engine.tick() if x.detector == "routine_time"])

    def test_every_suggestion_has_a_reason(self):
        clock = Clock()
        engine, _, _ = make_engine(clock, fg={"exe": "explorer.exe", "title": "Desktop", "hwnd": 2},
                                   disks_fn=lambda: [{"mount": "C:\\", "free_gb": 2, "percent_free": 1}])
        for s in engine.tick():
            self.assertTrue(s.reason, s.detector)


class AcceptTests(unittest.TestCase):
    """A suggestion never acts by itself; accepting a project/routine one only
    hands the choice to the UI, which runs it through the normal paths."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import main
        cls.client = TestClient(main.app, base_url="http://127.0.0.1:8000")

    def test_accepting_a_routine_suggestion_runs_nothing_on_the_server(self):
        import core.observer as observer
        import tools.dispatch as dispatch
        with tempfile.TemporaryDirectory() as folder:
            # A sandbox engine: tests never read or write the user's data/awareness.json.
            sandbox, _, _ = make_engine(Clock(), store_path=os.path.join(folder, "awareness.json"))
            det = awareness.DETECTORS["routine_time"]
            with sandbox.lock:
                s = sandbox._admit(det, {"key": "test-routine", "title": "Run 'X'?", "reason": "test",
                                         "actions": [{"label": "Run it", "kind": "routine", "routine": "r1", "name": "X"}]},
                                   sandbox.settings(), sandbox.clock())
            self.assertIsNotNone(s)
            with patch.object(observer, "engine", sandbox), \
                    patch.object(dispatch, "execute_tool", side_effect=AssertionError("nothing may run")):
                res = self.client.post(f"/api/suggestions/{s.id}/accept", json={"action": 0}).json()
        self.assertEqual((res["kind"], res["routine"]), ("routine", "r1"))


if __name__ == "__main__":
    unittest.main()

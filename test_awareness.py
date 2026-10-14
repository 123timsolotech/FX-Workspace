"""Offline tests for awareness.py - every probe is faked, no Windows needed."""
import os
import tempfile
import time
import unittest

import awareness


class Clock:
    def __init__(self, t=None):
        self.t = t or time.mktime((2026, 9, 21, 10, 0, 0, 0, 0, -1))   # a Monday, 10:00

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeRoblox:
    def __init__(self):
        self.connected = True
        self.errors = []
        self.seq = 0

    def is_connected(self):
        return self.connected

    def latest_context(self):
        return {"place": "Obby", "activeScript": "ServerScriptService/Main", "selection": ["Workspace/Lava"],
                "running": True, "error_seq": self.seq}

    def errors_since(self, seq, limit=20):
        return [e for e in self.errors if e["seq"] > seq][-limit:]

    def add_error(self, text, at):
        self.seq += 1
        self.errors.append({"seq": self.seq, "text": text, "at": at})


def make_engine(clock, **kw):
    fg = kw.pop("fg", {"exe": "RobloxStudioBeta.exe", "title": "Obby - Roblox Studio", "hwnd": 1})
    idle = kw.pop("idle", 0.5)
    state = {"fg": fg, "idle": idle}
    settings = kw.pop("settings", dict(awareness.DEFAULT_SETTINGS))
    engine = awareness.AwarenessEngine(
        clock=clock, foreground_fn=lambda: state["fg"], idle_fn=lambda: state["idle"],
        settings_fn=lambda: settings, **kw)
    return engine, state, settings


class AwarenessTests(unittest.TestCase):
    def test_studio_errors_become_a_suggestion_with_reasoning(self):
        clock, roblox = Clock(), FakeRoblox()
        engine, _, _ = make_engine(clock, roblox=roblox)
        engine.tick()                                   # baseline: old errors are not flagged
        roblox.add_error("ServerScriptService.Main:12: attempt to index nil with 'Humanoid'", clock())
        clock.advance(2)
        self.assertEqual(engine.tick(), [])             # burst still settling
        clock.advance(5)
        created = engine.tick()
        self.assertEqual(len(created), 1)
        s = created[0]
        self.assertEqual(s.detector, "studio_errors")
        self.assertIn("attempt to index nil", s.detail)
        self.assertTrue(any("Output logged" in r for r in s.reasoning))
        self.assertEqual(s.actions[0]["kind"], "prompt")
        # The request names where the errors are; their text is read with a tool
        # (framed as untrusted), never pasted in as the user's own words.
        self.assertIn("roblox_get_output", s.actions[0]["prompt"])
        self.assertNotIn("Humanoid", s.actions[0]["prompt"])
        self.assertEqual(s.actions[-1]["kind"], "dismiss")
        # the same errors are never raised twice
        clock.advance(200)
        self.assertEqual([x for x in engine.tick() if x.detector == "studio_errors"], [])

    def test_bridge_off_hint_after_focus_time(self):
        clock, roblox = Clock(), FakeRoblox()
        roblox.connected = False
        engine, _, _ = make_engine(clock, roblox=roblox)
        engine.tick()
        for _ in range(30):
            clock.advance(2)
            created = engine.tick()
            if created:
                break
        self.assertEqual(created[0].detector, "studio_bridge_off")
        self.assertEqual(created[0].actions[0], {"label": "Show me how", "kind": "help", "topic": "roblox-bridge"})

    def test_not_responding_offers_force_close_through_a_tool(self):
        clock = Clock()
        windows = ["Blender (Not Responding)", "Discord"]
        engine, _, _ = make_engine(clock, windows_fn=lambda: windows,
                                   fg={"exe": "explorer.exe", "title": "Desktop", "hwnd": 2})
        engine.tick()
        clock.advance(14)
        created = engine.tick()
        s = next(x for x in created if x.detector == "not_responding")
        self.assertEqual(s.priority, "high")
        self.assertEqual(s.actions[0]["tool"], "close_window")
        self.assertEqual(s.actions[0]["args"]["window_title"], "Blender")

    def test_dismiss_lowers_weight_and_forever_mutes(self):
        clock = Clock()
        windows = ["Game (Not Responding)"]
        engine, _, _ = make_engine(clock, windows_fn=lambda: windows,
                                   fg={"exe": "explorer.exe", "title": "Desktop", "hwnd": 2})
        engine.tick(); clock.advance(13)
        s = engine.tick()[0]
        engine.dismiss(s.id)
        self.assertLess(engine.weight("not_responding"), 1.0)
        # Phase 3.10: "Don't suggest this again" mutes exactly this suggestion
        # (this app freezing), not every frozen-app warning.
        engine.dismiss(s.id, forever=True)
        self.assertIn(awareness._pk("not_responding:Game"), engine.store["muted"])
        clock.advance(7 * 3600)
        engine.scratch.clear()
        engine.tick(); clock.advance(13)
        self.assertEqual(engine.tick(), [])
        windows[:] = ["Other App (Not Responding)"]
        engine.scratch.clear()
        engine.tick(); clock.advance(13)
        self.assertEqual([x.key for x in engine.tick()], ["not_responding:Other App"])

    def test_accept_returns_action_and_marks_accepted(self):
        clock = Clock()
        engine, _, _ = make_engine(clock, disks_fn=lambda: [{"mount": "C:\\", "free_gb": 4.2, "percent_free": 2.0}],
                                   fg={"exe": "explorer.exe", "title": "Desktop", "hwnd": 2})
        s = next(x for x in engine.tick() if x.detector == "low_disk")
        self.assertIn("C:", s.title)
        action = engine.accept(s.id, 0)
        self.assertEqual(action["prompt"], "Scan drive C with wiztree")
        self.assertEqual(engine.get(s.id).status, "accepted")
        self.assertIsNone(engine.accept(s.id, 0))       # one click, one action

    def test_rate_limit_per_level(self):
        clock = Clock()
        settings = dict(awareness.DEFAULT_SETTINGS, proactive_level="quiet")
        engine, _, _ = make_engine(clock, settings=settings)
        det = awareness.DETECTORS["error_dialog"]
        admitted = [engine._admit(det, {"key": f"k{i}", "title": "t", "priority": "medium"},
                                  engine.settings(), clock()) for i in range(10)]
        self.assertEqual(sum(1 for a in admitted if a), awareness.MAX_PER_HOUR["quiet"])

    def test_quiet_level_disables_balanced_detectors(self):
        clock = Clock()
        settings = dict(awareness.DEFAULT_SETTINGS, proactive_level="quiet")
        engine, _, _ = make_engine(clock, settings=settings)
        self.assertFalse(engine.detector_enabled(awareness.DETECTORS["habit"], engine.settings()))
        self.assertTrue(engine.detector_enabled(awareness.DETECTORS["studio_errors"], engine.settings()))
        settings["proactive_detectors"] = {"studio_errors": False}
        self.assertFalse(engine.detector_enabled(awareness.DETECTORS["studio_errors"], engine.settings()))

    def test_disabled_engine_suggests_nothing(self):
        clock = Clock()
        settings = dict(awareness.DEFAULT_SETTINGS, proactive_enabled=False)
        engine, _, _ = make_engine(clock, settings=settings,
                                   disks_fn=lambda: [{"mount": "C:\\", "free_gb": 1, "percent_free": 1}])
        self.assertEqual(engine.tick(), [])

    def test_welcome_back_recaps_last_app_and_unfinished_plan(self):
        clock = Clock()
        plan = {"goal": "Build the lava obby", "steps": [{"status": "done"}, {"status": "pending"}]}
        engine, state, _ = make_engine(clock, plan_fn=lambda: plan)
        for _ in range(200):                      # ~6.6 min of Studio work
            clock.advance(2)
            engine.tick()
        state["fg"] = {"exe": "explorer.exe", "title": "Desktop", "hwnd": 2}
        state["idle"] = 20 * 60
        clock.advance(20 * 60)
        engine.tick()
        state["idle"] = 0.5
        clock.advance(2)
        created = engine.tick()
        s = next(x for x in created if x.detector == "welcome_back")
        self.assertIn("Roblox Studio", s.detail)
        labels = [a["label"] for a in s.actions]
        self.assertIn("Resume the task", labels)
        self.assertIn("Reopen Roblox Studio", labels)

    def test_habit_learning_suggests_usual_app(self):
        clock = Clock()
        engine, state, _ = make_engine(clock, fg={"exe": "blender.exe", "title": "scene.blend - Blender", "hwnd": 3})
        for week in range(3):                     # three Mondays at 10:xx
            engine.sessions.clear()
            state["fg"] = {"exe": "blender.exe", "title": "scene.blend - Blender", "hwnd": 3}
            engine.tick()
            state["fg"] = {"exe": "explorer.exe", "title": "Desktop", "hwnd": 2}
            engine.tick()
            clock.advance(7 * 24 * 3600)
        self.assertEqual(engine.store["habits"]["Blender"]["0-10"], 3)
        engine.sessions.clear()
        engine.last_fired.clear()
        created = engine.tick()
        s = next(x for x in created if x.detector == "habit")
        self.assertEqual(s.actions[0]["tool"], "open_application")
        self.assertEqual(s.actions[0]["args"]["app_name"], "Blender")

    def test_downloads_detects_finished_file_only(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            engine, _, _ = make_engine(clock, downloads_dir=folder,
                                       fg={"exe": "explorer.exe", "title": "Desktop", "hwnd": 2})
            engine.tick()                          # baseline
            with open(os.path.join(folder, "model.fbx.crdownload"), "wb") as f:
                f.write(b"x" * 10)
            with open(os.path.join(folder, "model.fbx"), "wb") as f:
                f.write(b"x" * 2048)
            clock.advance(16); engine.tick()       # first sight: size recorded
            clock.advance(16)
            created = engine.tick()
            s = next(x for x in created if x.detector == "new_download")
            self.assertIn("model.fbx", s.title)
            self.assertEqual(s.actions[0]["tool"], "open_file_in_os")

    def test_store_persists_and_clears(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "data", "awareness.json")
            engine, _, _ = make_engine(clock, store_path=path)
            engine.store["weights"]["habit"] = 0.5
            engine.save(force=True)
            again, _, _ = make_engine(clock, store_path=path)
            self.assertEqual(again.weight("habit"), 0.5)
            again.clear_data()
            third, _, _ = make_engine(clock, store_path=path)
            self.assertEqual(third.weight("habit"), 1.0)

    def test_context_and_activity_line_describe_current_app(self):
        clock, roblox = Clock(), FakeRoblox()
        engine, _, _ = make_engine(clock, roblox=roblox)
        for _ in range(10):
            clock.advance(2)
            engine.tick()
        ctx = engine.context()
        self.assertEqual(ctx["app"], "Roblox Studio")
        self.assertTrue(any("Review Main" == c["label"] for c in ctx["chips"]))
        line = engine.activity_line()
        self.assertIn("Roblox Studio", line)
        self.assertIn("ServerScriptService/Main", line)

    def test_quiet_hours(self):
        t = time.mktime((2026, 9, 21, 23, 30, 0, 0, 0, -1))
        self.assertTrue(awareness.in_quiet_hours("23:00-08:00", t))
        self.assertFalse(awareness.in_quiet_hours("09:00-17:00", t))
        self.assertFalse(awareness.in_quiet_hours("", t))

    def test_clean_settings_rejects_garbage(self):
        cleaned = awareness.clean_settings({"proactive_level": "loud", "proactive_quiet_hours": "late",
                                            "proactive_detectors": {"nope": True, "habit": 0}})
        self.assertEqual(cleaned["proactive_level"], "balanced")
        self.assertEqual(cleaned["proactive_quiet_hours"], "")
        self.assertEqual(cleaned["proactive_detectors"], {"habit": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)

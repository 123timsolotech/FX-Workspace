"""1.4.2 Phase 2: the mini window is settings the desktop app applies; the assistant's mini_window tool changes them."""
import os
import unittest
from unittest.mock import patch

import app_window
import core.config as config
import core.state as shared
import mini_window


class MiniWindowTests(unittest.TestCase):
    def setUp(self):
        base = dict(shared.app_state.get("settings") or {})
        base.update(overlay_window=False, overlay_opacity=0.88, overlay_clickthrough=False, mini_place="studio")
        p = patch.dict(shared.app_state, settings=base)
        p.start()
        self.addCleanup(p.stop)
        for target in (patch.object(config, "save_settings", return_value=True),
                       patch.object(app_window, "overlay_sync"), patch.object(app_window, "studio_rect", return_value=[0, 0, 1000, 800])):
            target.start()
            self.addCleanup(target.stop)

    def test_open_set_and_close(self):
        r = mini_window.apply("open")
        self.assertTrue(r["ok"] and r["mini"]["open"])
        r = mini_window.apply("set", opacity=60, click_through=True, place="br")
        self.assertEqual((r["mini"]["opacity"], r["mini"]["click_through"], r["mini"]["place"]), (0.6, True, "br"))
        self.assertIn("open in the bottom-right corner", r["message"])
        self.assertFalse(mini_window.apply("close")["mini"]["open"])

    def test_bad_values_are_refused_or_clamped(self):
        self.assertFalse(mini_window.apply("set", place="middle")["ok"])
        self.assertFalse(mini_window.apply("set")["ok"])                       # nothing to change
        self.assertEqual(mini_window.apply("set", opacity=0.05)["mini"]["opacity"], 0.3)

    def test_shell_state_has_studio_only_when_following_it(self):
        mini_window.apply("open")
        self.assertEqual(mini_window.shell_state()["studio_rect"], [0, 0, 1000, 800])
        mini_window.apply("set", place="tl")
        self.assertIsNone(mini_window.shell_state()["studio_rect"])
        self.assertTrue(mini_window.shell_state()["close_to_tray"])

    def test_the_server_keeps_it_on_top_in_the_desktop_app(self):
        # Electron's own always-on-top doesn't stick on Windows; each /api/shell read pins it again.
        with patch.dict(os.environ, {"FX_SHELL": "electron"}), patch.object(app_window, "keep_on_top") as pin:
            mini_window.shell_state()
            pin.assert_not_called()                                    # closed: nothing to pin
            mini_window.apply("open")
            mini_window.shell_state()
        pin.assert_called_with(mini_window.TITLE)

    def test_the_tool_is_offered_with_apps_and_windows_and_runs_without_asking(self):
        import permissions
        import skills
        import tools.schemas as schemas
        self.assertIn("mini_window", schemas.TOOL_NAMES)
        self.assertEqual(skills.SKILL_OF["mini_window"], "desktop")
        self.assertIn("desktop", skills.pick("put yourself in the corner as a mini window"))
        self.assertEqual(permissions.decide("mini_window", config.DEFAULT_SETTINGS), "auto")

    def test_show_and_hide_it_are_actions(self):
        import intent_router
        for text in ("show the mini window", "hide the mini window", "please bring up the mini-window",
                     "Put your mini window in the top-left corner, 70% see-through."):
            self.assertEqual(intent_router.classify_turn(text)["mode"], "action", text)
        self.assertNotEqual(intent_router.classify_turn("show me my open windows")["mode"], "action")


class DesktopShellTests(unittest.TestCase):
    def test_no_edge_window_under_the_desktop_app(self):
        with patch.dict(os.environ, {"FX_SHELL": "electron"}), patch.object(app_window, "WINDOWS", True), \
                patch.object(app_window._overlay, "start") as start, patch.object(app_window, "find_hud", return_value=None):
            app_window.overlay_sync({"overlay_window": True})
        start.assert_not_called()

    def test_the_mini_window_never_leaks_into_the_main_one(self):
        # Both windows share localStorage: the main window once started as the mini one (all hidden) because the
        # saved boot look carried data-hud. And the window keeps its own title, or the old-HUD cleanup closes it.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "index.html"), encoding="utf-8") as f:
            page = f.read()
        restore = page[page.index("localStorage.getItem('fx-boot-look')"):][:300]
        self.assertIn("delete b.d.hud", restore)
        save = page[:page.index("localStorage.setItem('fx-boot-look'")][-200:]
        self.assertIn("delete d.hud", save)
        with open(os.path.join(root, "desktop", "main.js"), encoding="utf-8") as f:
            self.assertIn("mini.on('page-title-updated', e => e.preventDefault())", f.read())

    def test_settings_are_cleaned(self):
        out = config.clean_privacy_settings({"mini_place": "nowhere", "close_to_tray": "yes"})
        self.assertEqual((out["mini_place"], out["close_to_tray"]), ("studio", True))
        out = config.clean_privacy_settings({"mini_place": "bl", "close_to_tray": False})
        self.assertEqual((out["mini_place"], out["close_to_tray"]), ("bl", False))


if __name__ == "__main__":
    unittest.main()

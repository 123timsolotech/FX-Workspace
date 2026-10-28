"""1.4.3's polish of what 1.4.2 added: notifications held back by Windows (focus_assist.py), and the rest of part 4
of docs/plans/1.4.3.md."""
import unittest
from pathlib import Path
from unittest.mock import patch

import focus_assist
import remote_access

ROOT = Path(__file__).resolve().parent
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
SERVER = (ROOT / "api" / "server.py").read_text(encoding="utf-8")


class FocusAssistTests(unittest.TestCase):
    def test_states(self):
        cases = [((0, 5), {"quiet": False, "why": "", "known": True}),
                 ((1, 5), {"quiet": True, "why": "priority", "known": True}),
                 ((2, 5), {"quiet": True, "why": "alarms", "known": True}),
                 ((0, 3), {"quiet": True, "why": "fullscreen", "known": True}),        # a game full screen
                 ((0, 4), {"quiet": True, "why": "presentation", "known": True}),
                 ((None, None), {"quiet": False, "why": "", "known": False})]          # unreadable: say nothing
        for (profile, quns), want in cases:
            with patch.object(focus_assist, "_profile", return_value=profile), patch.object(focus_assist, "_quns", return_value=quns):
                self.assertEqual(focus_assist.state(), want, (profile, quns))

    def test_reads_this_pc(self):
        s = focus_assist.state()                                             # never raises, whatever Windows says
        self.assertEqual(set(s), {"quiet", "why", "known"})

    def test_opens_the_windows_page(self):
        with patch.object(focus_assist.os, "startfile", create=True) as start, patch.object(focus_assist.sys, "platform", "win32"):
            self.assertTrue(focus_assist.open_settings())
        start.assert_called_once_with("ms-settings:quiethours")

    def test_the_settings_hint_and_routes(self):
        self.assertIn('@app.get("/api/notify/quiet")', SERVER)
        self.assertIn('@app.post("/api/notify/quiet/open")', SERVER)
        self.assertIn('<div id="notify-quiet"></div>', HTML)
        self.assertIn("notifyQuietHint();", HTML)
        # From a phone, the PC's own Windows settings never open.
        self.assertTrue(remote_access.blocked("POST", "/api/notify/quiet/open"))
        self.assertFalse(remote_access.blocked("GET", "/api/notify/quiet"))


class StarterModelTests(unittest.TestCase):
    """The first-run welcome's one-click starter (model_finder.starter): the biggest that fits, no network."""

    def pick(self, vram, ram=16.0):
        import model_finder
        with patch.object(model_finder, "_on_disk_names", return_value=set()):
            r = model_finder.starter({"gpu": "GPU", "vram_gb": vram, "free_vram_gb": vram, "ram_gb": ram, "free_ram_gb": ram})
        return (r["pick"] or {}).get("name"), (r["pick"] or {}).get("fit")

    def test_the_biggest_that_fits_on_the_gpu(self):
        self.assertEqual(self.pick(10), ("Qwen3 8B", "gpu"))
        self.assertEqual(self.pick(6.5), ("Qwen3 8B", "gpu"))
        self.assertEqual(self.pick(5), ("Qwen3 4B", "gpu"))
        self.assertEqual(self.pick(2.6), ("Qwen3 1.7B", "gpu"))

    def test_no_gpu_room_runs_the_smallest_from_ram(self):
        self.assertEqual(self.pick(0, ram=8), ("Qwen3 1.7B", "partial"))
        self.assertEqual(self.pick(0, ram=1), (None, None))

    def test_the_starters_are_real_files(self):
        # Sizes checked against Hugging Face on 2026-10-28 (the fit is computed from them without a request).
        import model_finder
        self.assertEqual([(s["repo"], s["path"], s["size"]) for s in model_finder.STARTERS],
                         [("Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", 5027783488),
                          ("Qwen/Qwen3-4B-GGUF", "Qwen3-4B-Q4_K_M.gguf", 2497280256),
                          ("Qwen/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q8_0.gguf", 1834426016)])

    def test_the_welcome_has_three_steps(self):
        self.assertIn("const STEPS = ['A chat model', 'A look', 'What stays here'];", HTML)
        self.assertIn("api('/api/model-finder/starter')", HTML)
        self.assertIn('@router.get("/api/model-finder/starter")', (ROOT / "api" / "extras.py").read_text(encoding="utf-8"))
        self.assertIn("if (e.key === 'Escape') { e.preventDefault(); done(); }", HTML)    # skippable at every step


class WhatsNewTests(unittest.TestCase):
    def test_notes_of_a_version(self):
        import app_version
        n = app_version.notes("1.4.2")
        self.assertEqual(n["version"], "1.4.2")
        self.assertFalse(n["markdown"].startswith("# "))                    # the title line is the dialog's own
        self.assertIn("Stop keeps what was written", n["markdown"])
        self.assertEqual(app_version.notes("9.9.9")["markdown"], "")
        self.assertEqual(app_version.notes("../../VERSION")["markdown"], "")   # only a version number reads a file

    def test_shown_once_after_an_update(self):
        self.assertIn("WhatsNew.check();", HTML)
        self.assertIn("if (seen === d.version || !welcomed) return;", HTML)   # same version, or a first install: nothing
        self.assertIn('@app.get("/api/version/notes")', SERVER)


class SettingsTests(unittest.TestCase):
    def test_search_goes_to_the_setting(self):
        self.assertIn("S.setQuery = q; S.setJumped = false;", HTML)
        self.assertIn("if (S.setQuery && S.settingsTab === tab) searchHighlight(S.setQuery);", HTML)
        self.assertIn("@keyframes set-hit", HTML)

    def test_connections_and_web_search_and_network_are_one_part_each(self):
        self.assertIn("const SETTINGS_STACKS = { connections: ['connectors', 'mcp', 'channels'], webnet: ['websearch', 'network'] };", HTML)
        self.assertIn("['websearch', [['connections', 'Connections'], ['remote', 'Phone access'], ['webnet', 'Web search and network']]]", HTML)
        # Every renderer of a section draws into its own section, and checks its section is showing.
        for name in ("connectors", "mcp", "channels", "websearch", "network"):
            self.assertIn(f"setBody('{name}')", HTML)
            self.assertNotIn(f"S.settingsTab !== '{name}'", HTML)
        # The old names still work: they open the part at their section.
        self.assertIn("const section = STACK_OF[tab] ? tab : null;", HTML)

    def test_the_ai_appearances_are_out_of_beta(self):
        self.assertNotIn("AI_APPEARANCES.includes(a) ? ' (Beta)'", HTML)
        self.assertNotIn('AI-inspired <span class="beta-tag">Beta</span>', HTML)


class ShellWatchTests(unittest.TestCase):
    """launcher.pyw stops the server when the desktop app has closed - and only then. Found in 1.4.3's live checks:
    one failed process check, while the PC loaded a 9.7 GB model for the MoE test, stopped the server under a
    running app."""

    def setUp(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("fx_launcher", str(ROOT / "launcher.pyw"))
        spec = importlib.util.spec_from_loader("fx_launcher", loader)
        self.launcher = importlib.util.module_from_spec(spec)
        loader.exec_module(self.launcher)

    def test_an_unclear_check_is_not_gone(self):
        import psutil
        L = self.launcher
        with patch.object(psutil, "pid_exists", return_value=True), patch.object(psutil, "Process", side_effect=psutil.AccessDenied(1)):
            self.assertTrue(L._alive(1))
        with patch.object(psutil, "pid_exists", return_value=True), patch.object(psutil, "Process", side_effect=OSError("busy")):
            self.assertTrue(L._alive(1))
        with patch.object(psutil, "pid_exists", return_value=True), patch.object(psutil, "Process", side_effect=psutil.NoSuchProcess(1)):
            self.assertFalse(L._alive(1))
        with patch.object(psutil, "pid_exists", return_value=False):
            self.assertFalse(L._alive(1))

    def test_it_takes_three_seconds_gone_in_a_row(self):
        L, server = self.launcher, type("S", (), {"should_exit": False})()
        answers = iter([True, False, True, False, False, True, False, False, False, True])
        seen = []

        def alive(pid):
            v = next(answers)
            seen.append(v)
            return v
        with patch.dict(L.os.environ, {"FX_PARENT_PID": "1234"}), patch.object(L, "_alive", alive), patch.object(L.time, "sleep"):
            L.watch_shell(server)
        self.assertTrue(server.should_exit)
        self.assertEqual(len(seen), 9)              # the odd False answers before never stopped it


if __name__ == "__main__":
    unittest.main()

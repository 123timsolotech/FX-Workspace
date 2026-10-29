"""1.4.4: a clear Quit, and no tray icons left behind by copies that were closed by force."""
import os
import subprocess
import sys
import time
import unittest

import remote_access
import tray_sweep

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()
MAIN = open(os.path.join(HERE, "desktop", "main.js"), encoding="utf-8").read()
LAUNCHER = open(os.path.join(HERE, "launcher.pyw"), encoding="utf-8").read()


class QuitTests(unittest.TestCase):
    def test_a_quit_button_in_the_app(self):
        self.assertIn('class="sb-quit" onclick="quitApp()"', PAGE)
        self.assertIn("async function quitApp()", PAGE)
        self.assertIn("api('/api/shell/quit'", PAGE)
        self.assertIn("'Quit FX Workspace', 'Stop the server and the model", PAGE)        # the command palette
        self.assertIn("html.fx-desktop .sb-quit { display: inline-flex", PAGE)            # desktop app only

    def test_the_phone_cannot_quit_the_app(self):
        self.assertTrue(remote_access.blocked("POST", "/api/shell/quit"))

    def test_the_tray_icon_goes_with_the_app(self):
        self.assertIn("if (tray) { tray.destroy(); tray = null; }", MAIN)
        self.assertIn("tray_sweep.sweep", LAUNCHER)


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("FX_LIVE_TRAY") == "1", "set FX_LIVE_TRAY=1: makes and sweeps real tray icons")
class LiveSweepTests(unittest.TestCase):
    def test_leftover_icons_are_swept(self):
        ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
              "$n.Icon = [System.Drawing.SystemIcons]::Warning; $n.Visible = $true; Start-Sleep 60")
        p = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000)
        time.sleep(4)
        p.kill()
        self.assertGreaterEqual(tray_sweep.sweep(), 1)


class ReleaseNotesTests(unittest.TestCase):
    def test_notes_are_not_hard_wrapped(self):
        # What's new shows each line break as a break: a paragraph wrapped by hand came out cut mid-sentence (1.4.4).
        v = open(os.path.join(HERE, "VERSION"), encoding="utf-8").read().strip()
        lines = open(os.path.join(HERE, "docs", "releases", f"{v}.md"), encoding="utf-8").read().split("\n")
        for a, b in zip(lines, lines[1:]):
            if a.strip() and b.strip() and not a.startswith("#"):
                self.assertRegex(b, r"^(#|- |\d+\. |\|)", f"a wrapped line after: {a[:60]}")


class SweepTests(unittest.TestCase):
    def test_does_nothing_where_there_is_no_tray(self):
        if sys.platform != "win32":
            self.assertEqual(tray_sweep.sweep(), 0)
        else:
            self.assertIn(tray_sweep.sweep(), (0, 1, 2))      # never raises


if __name__ == "__main__":
    unittest.main()

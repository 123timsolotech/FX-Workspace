"""Ambient idle screen (D30): the setting degrades safely, and the screen can
only appear while the live state is idle (never over work, an approval, a
prep card, an error, Settings or a hidden tab)."""
import os
import re
import unittest

import core.config as config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AmbientIdleTests(unittest.TestCase):
    def test_setting_values(self):
        self.assertEqual(config.DEFAULT_SETTINGS["ambient_idle"], "10")
        for value, want in (("off", "off"), ("5", "5"), (30, "30"), ("7", "10"), (None, "10"), (True, "10")):
            self.assertEqual(config.clean_agent_settings({"ambient_idle": value})["ambient_idle"], want, value)

    def test_the_screen_only_enters_while_idle(self):
        with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
            html = f.read()
        block = html[html.index("const Idle = (() => {"):html.index("function renderLive()")]
        eligible = re.search(r"const eligible = (.+?);", block, re.S).group(1)
        for condition in ("root.dataset.state === 'idle'", "!S.busy", "document.visibilityState === 'visible'",
                          "$('settings').classList.contains('hidden')", ".approval-card[data-open=\"1\"]"):
            self.assertIn(condition, eligible)
        self.assertIn("e.preventDefault()", block)          # the waking key is not typed / pressed


if __name__ == "__main__":
    unittest.main()

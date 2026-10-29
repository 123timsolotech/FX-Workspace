"""1.4.2: appearance switches settle at once; the chat limits allow opinions and can be relaxed from one red switch."""
import os
import re
import unittest

import chat_guard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _page() -> str:
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        return f.read()


def _function(html: str, name: str) -> str:
    m = re.search(r"\nfunction %s\([^)]*\) \{\n(.*?)\n\}\n" % re.escape(name), html, re.S)
    assert m, name
    return m.group(1)


class AppearanceSwitchTests(unittest.TestCase):
    def test_the_choice_is_recorded_before_the_transition_repaints(self):
        # Set inside the transition, anything redrawn right after a switch used the previous appearance.
        for name, setting in (("changeAppearance", "Object.assign(S.settings, partial)"),
                              ("setColorMode", "S.settings.color_mode = mode")):
            body = _function(_page(), name)
            self.assertIn(setting, body, name)
            self.assertLess(body.index(setting), body.index("withTransition("), name)
            self.assertNotIn(setting, body[body.index("withTransition("):], name)

    def test_the_returned_promise_waits_for_the_repaint(self):
        body = _function(_page(), "changeAppearance")
        self.assertIn("Promise.all([shown, saveSettings(", body)


class ChatLimitTests(unittest.TestCase):
    def test_politics_and_opinions_are_explicitly_allowed(self):
        policy = chat_guard._POLICY.lower()
        for word in ("politics", "religion", "controversial", "own view"):
            self.assertIn(word, policy)

    def test_relaxed_limits_switch_is_red_findable_and_keeps_minors(self):
        html = _page()
        self.assertIn("Relaxed limits", html)
        self.assertIn(".set-group.guard-relax", html)
        self.assertRegex(html, r"privacy: '[^']*uncensored[^']*abliterated")
        body = re.search(r"async function guardRelax\(on\) \{(.*?)\n\}", html, re.S).group(1)
        self.assertIn("filter(c => c.always)", body)            # relaxed keeps only the always-on category

    def test_relaxing_through_the_store_keeps_minors_on(self):
        import tempfile
        from unittest.mock import patch
        with patch.object(chat_guard, "PATH", os.path.join(tempfile.mkdtemp(), "chat_guard.json")):
            info = chat_guard.update_limits(categories=["minors"])
            self.assertEqual(info["categories"], ["minors"])
            info = chat_guard.update_limits(categories=[])         # even an empty list can't drop it
            self.assertIn("minors", info["categories"])
            # Relaxed: a misfiled verdict on an opinion question no longer refuses it.
            self.assertEqual(chat_guard.screen("Who is to blame for the war, in your opinion?", "",
                                               lambda s, u: "ABUSE")["action"], "allow")


if __name__ == "__main__":
    unittest.main()

"""Appearance families (Classic, Sunrise, Terminal, Eclipse, Aurora) with a light/dark mode, the
looks (Studio, Focus, Operator, Command), and the move from the older single "theme" setting and
the Companion look. Nothing here writes the real settings.json."""
import json
import os
import re
import tempfile
import unittest
from unittest import mock

import core.config as config
import core.state as shared


class FamilyTests(unittest.TestCase):
    def test_defaults(self):
        d = config.DEFAULT_SETTINGS
        self.assertEqual((d["appearance"], d["color_mode"], d["terminal_phosphor"], d["theme"], d["look"]),
                         ("classic", "system", False, "system", "studio"))

    def test_each_family_has_a_light_and_a_dark_theme(self):
        self.assertEqual(set(config.FAMILY_THEMES), set(config.UI_APPEARANCES))
        for appearance, pair in config.FAMILY_THEMES.items():
            for theme in pair:
                self.assertIn(theme, config.UI_THEMES, appearance)
        pick = lambda **s: config.clean_appearance(s)["theme"]
        self.assertEqual(pick(appearance="classic", color_mode="dark"), "dark")
        self.assertEqual(pick(appearance="classic", color_mode="system"), "system")
        self.assertEqual(pick(appearance="sunrise", color_mode="light"), "dawn")
        self.assertEqual(pick(appearance="sunrise", color_mode="dark"), "dusk")
        self.assertEqual(pick(appearance="terminal", color_mode="dark"), "termdark")
        self.assertEqual(pick(appearance="terminal", color_mode="dark", terminal_phosphor=True), "terminal")
        self.assertEqual(pick(appearance="terminal", color_mode="light", terminal_phosphor=True), "termlight")
        self.assertEqual(pick(appearance="eclipse", color_mode="light"), "eclipselight")
        self.assertEqual(pick(appearance="aurora", color_mode="light"), "auroralight")
        self.assertEqual(pick(appearance="aurora", color_mode="system"), "aurora")

    def test_bad_values_degrade(self):
        out = config.clean_appearance({"appearance": "neon", "color_mode": "blue", "terminal_phosphor": "yes"})
        self.assertEqual(out, {"appearance": "classic", "color_mode": "system", "terminal_phosphor": False, "theme": "system"})

    def test_every_older_theme_keeps_its_look(self):
        for theme in config.UI_THEMES:
            out = config.clean_appearance({"theme": theme}, from_theme=True)
            self.assertEqual(out["theme"], theme, theme)
            self.assertEqual(config.THEME_FAMILY[theme][0], out["appearance"])
        self.assertEqual(config.clean_appearance({"theme": "terminal"}, from_theme=True)["terminal_phosphor"], True)
        # Themes from the UI before these map onto the classic pair, as they always did.
        self.assertEqual(config.clean_appearance({"theme": "midnight"}, from_theme=True)["theme"], "dark")
        self.assertEqual(config.clean_appearance({"theme": ""}, from_theme=True)["theme"], "system")

    def test_the_secret_theme_is_no_family(self):
        self.assertNotIn("judaism", config.UI_APPEARANCES)
        self.assertNotIn("mossad", config.UI_THEMES)

    def test_companion_became_focus(self):
        self.assertNotIn("companion", config.UI_LOOKS)
        self.assertIn("command", config.UI_LOOKS)
        self.assertEqual(config.clean_look("companion"), "focus")
        self.assertEqual(config.clean_look("command"), "command")
        self.assertEqual(config.clean_look("sideways"), "studio")


class LoadTests(unittest.TestCase):
    def load(self, saved):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            if saved is not None:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(saved, f)
            with mock.patch.object(config, "SETTINGS_PATH", path):
                s = config.load_settings()
            if saved is not None:
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f), saved)          # loading never rewrites the file
        return s

    def test_an_older_file_moves_to_families(self):
        s = self.load({"theme": "dusk", "look": "companion"})
        self.assertEqual((s["appearance"], s["color_mode"], s["theme"], s["look"]), ("sunrise", "dark", "dusk", "focus"))
        s = self.load({"theme": "terminal"})
        self.assertEqual((s["appearance"], s["color_mode"], s["terminal_phosphor"]), ("terminal", "dark", True))

    def test_a_new_file_is_read_by_family(self):
        # A stale "theme" next to the family never wins.
        s = self.load({"appearance": "eclipse", "color_mode": "light", "theme": "dark"})
        self.assertEqual((s["appearance"], s["color_mode"], s["theme"]), ("eclipse", "light", "eclipselight"))

    def test_no_file(self):
        s = self.load(None)
        self.assertEqual((s["appearance"], s["color_mode"], s["theme"]), ("classic", "system", "system"))


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def page() -> str:
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        return f.read()


def _lum(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    x, y = sorted((_lum(a), _lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = page()
        cls.css = cls.html[cls.html.index("<style>"):cls.html.index("</style>")]

    def js_list(self, name):
        return tuple(re.findall(r"'(\w+)'", re.search(r"const %s = \[(.*?)\];" % name, self.html).group(1)))

    def test_lists_match_the_server(self):
        self.assertEqual(self.js_list("LOOKS"), config.UI_LOOKS)
        self.assertEqual(self.js_list("APPEARANCES"), config.UI_APPEARANCES)
        body = re.search(r"const FAMILY_THEMES = \{(.*?)\};", self.html).group(1)
        pairs = {a: (light, dark) for a, light, dark in re.findall(r"(\w+): \['(\w+)', '(\w+)'\]", body)}
        self.assertEqual(pairs, config.FAMILY_THEMES)
        self.assertNotIn("companion:", self.html)
        self.assertNotIn("'companion'", self.html)

    def test_the_mode_button_is_always_in_the_top_bar(self):
        bar = self.html[self.html.index('<header class="topbar">'):self.html.index("</header>", self.html.index('<header class="topbar">'))]
        btn = re.search(r'<button[^>]*id="mode-btn"[^>]*>', bar).group(0)
        self.assertIn('onclick="toggleColorMode()"', btn)
        self.assertNotIn("hidden", btn)
        self.assertNotIn("chat-only", btn)
        self.assertIn("=== 'l' || e.code === 'KeyL'", self.html)            # Ctrl+Alt+L, whatever the keyboard layout
        self.assertIn("renderModeBtn();", self.html)
        for sym in ("i-sun", "i-moon", "i-reticle"):
            self.assertIn(f'<symbol id="{sym}"', self.html)

    def test_the_mode_stays_within_the_family_and_keeps_a_secret_theme(self):
        fn = self.html[self.html.index("function setColorMode("):self.html.index("function renderModeBtn(")]
        self.assertIn("color_mode: mode, appearance", fn)
        self.assertNotIn("secretTheme = null", fn)                          # only picking an appearance leaves it
        change = self.html[self.html.index("function changeAppearance("):self.html.index("const $ = id =>")]
        self.assertIn("if (partial.appearance) secretTheme = null;", change)

    def tone(self, skin, tone):
        block = re.search(r'^:root\[data-skin="%s"\]\[data-tone="%s"\] \{(.*?)\}' % (skin, tone), self.css, re.S | re.M).group(1)
        return block, dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))

    def test_the_new_light_versions_are_light_and_readable(self):
        for skin in ("eclipse", "aurora"):
            block, p = self.tone(skin, "light")
            self.assertIn("color-scheme: light", block)
            self.assertGreater(_lum(p["bg"]), 0.8, skin)
            for fg in ("text", "text-2", "muted"):
                self.assertGreaterEqual(_contrast(p[fg], p["bg"]), 4.5, f"{skin} {fg}")
            self.assertGreaterEqual(_contrast(p["accent-ink"], p["accent"]), 4.5, skin)
            for token in ("ok", "warn", "danger", "accent", "border"):
                self.assertIn(f"--{token}:", block, f"{skin} {token}")

    def test_judaism_mode_after_dark_is_mossadgpt(self):
        fn = self.html[self.html.index("function applyAppearance() {"):self.html.index("function fitTextColors()")]
        self.assertIn("if (secret && scheme === 'dark') { root.dataset.skin = 'terminal'; root.dataset.tone = 'mossad'; root.dataset.secret = 'mossad'; }", fn)
        self.assertIn("root.dataset.secret = secret;", fn)
        self.assertIn("else delete root.dataset.secret;", fn)
        block, p = self.tone("terminal", "mossad")
        self.assertIn("color-scheme: dark", block)
        self.assertLess(_lum(p["bg"]), 0.02)
        for fg in ("text", "text-2", "muted"):
            self.assertGreaterEqual(_contrast(p[fg], p["bg"]), 4.5, fg)
            self.assertGreaterEqual(_contrast(p[fg], p["surface"]), 4.5, fg)
        self.assertGreaterEqual(_contrast(p["accent"], p["bg"]), 4.5)
        self.assertGreaterEqual(_contrast(p["accent-ink"], p["accent"]), 4.5)
        # Less of the flag, more of the ops room: no stars, lattice or bands - the reticle mark only.
        self.assertIn(':root[data-skin="judaism"] .sfx { display: block;', self.css)
        self.assertNotIn('[data-tone="mossad"] .sfx', self.css)

    def test_mossad_extras_only_show_in_it_and_stop_for_reduced_motion(self):
        self.assertIn(".ops-radar { display: none; }", self.css)
        self.assertIn(".ops-clock { display: none; }", self.css)
        self.assertIn("animation: ops-sweep 4s linear infinite paused", self.css)            # turns only while working
        self.assertIn('[data-state="working"] .ops-radar i { opacity: .55; animation-play-state: running; }', self.css)
        self.assertIn("html.sfx-still .ops-radar i { animation: none !important; }", self.css)
        motion = "\n".join(re.findall(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", self.css, re.S))
        self.assertIn(".ops-radar i", motion)
        # Its status strip reads the app's real state.
        for state, word in (("working", "ACTIVE"), ("waiting", "AWAITING GO"), ("error", "FAULT"), ("offline", "NO LINK")):
            self.assertIn(f':root[data-state="{state}"] .ops-clock .oc-state::before {{ content: "{word}"; }}', self.css)

    def test_secret_effects_follow_the_half_on_screen(self):
        start = self.html.index("const SecretFx = (() => {")
        fx = self.html[start:self.html.index("/* ---- Settings > Diagnostics", start)]
        self.assertIn("swapMarks(ops ? '#i-reticle' : secret ? '#i-star6' : '#mark');", fx)
        self.assertIn("if (!secret && audio) audio.pause();", fx)          # day to night keeps the music playing
        self.assertIn("clock(ops);", fx)
        self.assertIn("|| !root.dataset.secret) return;", fx)              # Ctrl+Alt+K works in both halves
        self.assertIn("Leave the secret theme", fx)

    def test_operator_follows_light_and_dark(self):
        self.assertIn(':root[data-look="operator"][data-skin="classic"][data-theme="dark"] {', self.css)
        self.assertIn(':root[data-look="operator"][data-skin="classic"][data-theme="light"] {', self.css)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import main
        cls.client = TestClient(main.app, base_url="http://127.0.0.1:8000")

    def post(self, start, body):
        before = shared.app_state["settings"]
        try:
            shared.app_state["settings"] = {**before, **start}
            with mock.patch.object(config, "save_settings"):   # never the real settings.json
                return self.client.post("/api/settings", json=body).json()["settings"]
        finally:
            shared.app_state["settings"] = before

    def test_the_mode_flips_within_the_family(self):
        s = self.post({"appearance": "eclipse", "color_mode": "dark", "terminal_phosphor": False}, {"color_mode": "light"})
        self.assertEqual((s["appearance"], s["color_mode"], s["theme"]), ("eclipse", "light", "eclipselight"))

    def test_an_older_client_sending_a_theme_picks_the_family(self):
        s = self.post({"appearance": "classic", "color_mode": "system"}, {"theme": "termlight"})
        self.assertEqual((s["appearance"], s["color_mode"], s["theme"]), ("terminal", "light", "termlight"))

    def test_bad_values_and_companion(self):
        s = self.post({}, {"appearance": "neon", "color_mode": "sepia", "look": "companion"})
        self.assertEqual((s["appearance"], s["color_mode"], s["look"]), ("classic", "system", "focus"))


if __name__ == "__main__":
    unittest.main()

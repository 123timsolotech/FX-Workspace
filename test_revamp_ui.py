"""
test_revamp_ui.py
=================
The revamped shell (index.html) and the storage view behind it: the More menu, the
keyboard map, error cards, approval verdicts, per-action indicators, the privacy line,
Storage, and dropping a picture on Images. Page tests read the markup; the storage
test runs the endpoint against a temporary folder - never the real data\\.
"""
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
            cls.html = f.read()

    def test_static_copy_matches(self):
        with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
            self.assertEqual(f.read(), self.html)

    def test_the_top_bar_more_menu(self):
        self.assertIn('id="more-btn" onclick="openMoreMenu(this)"', self.html)
        self.assertNotIn('id="export-btn"', self.html)                     # folded into More
        for action in ("rename:", "find:", "pins:", "branch:", "md:", "html:", "pdf:"):
            self.assertIn(action, self.html[self.html.index("function openMoreMenu"):][:2500])
        self.assertIn("openExportMenu($('more-btn'))", self.html)           # Ctrl+Shift+E anchors on it

    def test_the_keyboard_map(self):
        keys = self.html[self.html.index("const SHORTCUTS = ["):self.html.index("const Shortcuts = ")]
        for k in ("Alt+1", "Alt+2", "Alt+3", "Alt+4", "Ctrl+Alt+T", "Ctrl+Alt+U", "Ctrl+/", "Ctrl+Alt+M", "Ctrl+Alt+V", "Ctrl+Alt+L"):
            self.assertIn(f"['{k}'", keys)
        self.assertIn("{ Digit1: 'home', Digit2: 'images', Digit3: 'projects', Digit4: 'routines' }[e.code]", self.html)
        self.assertIn("e.code === 'KeyT') { e.preventDefault(); newTemporaryChat()", self.html)
        # On a place the message box is hidden: the chat (or Home) comes back before its menu opens.
        self.assertIn("if (PLACES[S.view]) { closePopover(); showThread(!!S.transcript.length); }", self.html)

    def test_errors_say_what_happened_and_what_to_do(self):
        kinds = self.html[self.html.index("const ERROR_KINDS = ["):self.html.index("function errorInfo")]
        self.assertIn("exceeds the available context size", kinds)
        self.assertIn("['compress', 'new', 'retry']", kinds)
        self.assertIn("llama-server is not answering", kinds)
        self.assertIn('<details class="me-raw"><summary>Details</summary>', self.html)
        self.assertIn("addError(m.t, false, i)", self.html)                   # restored errors know their entry
        self.assertIn("filter(a => a !== 'retry' || last)", self.html)         # Try again only on the latest

    def test_approval_cards_name_the_risk_and_fold(self):
        self.assertIn("const TIER_WORD = { benign: 'Looks only', input: 'Types or clicks', destructive: 'Changes things' };", self.html)
        self.assertIn("card.dataset.risk = ['destructive', 'input', 'benign'].find(", self.html)
        self.assertIn("function settleApproval(card, how, word)", self.html)
        self.assertIn(".approval-card.answered:not(.show) .action-list", self.html)
        self.assertIn("What was asked", self.html)

    def test_every_theme_moves_for_every_action(self):
        acts = ("read", "search", "write", "tool", "draw", "wait")
        skins = ('[data-skin="classic"][data-scheme="light"]', '[data-skin="classic"][data-scheme="dark"]', '[data-skin="terminal"]',
                 '[data-skin="eclipse"],[data-skin="dawn"],[data-skin="dusk"]', '[data-skin="aurora"]')
        for skin in skins:
            for act in acts:
                pattern = re.escape(skin) + r"\)? \.think\[data-act=\"" + act + '"\\]'
                self.assertRegex(self.html, pattern, f"{skin} has no '{act}' indicator")
        # Reduced motion: the action's icon, standing still, instead of the animation.
        self.assertIn(':root:is([data-motion="reduced"], [data-motion="off"]) .act-ic { display: inline-flex; }', self.html)
        self.assertIn("function currentAct(turn)", self.html)
        self.assertIn("paintAct(turn, phase === 'live');", self.html)
        self.assertIn('.act-line[hidden] { display: none; }', self.html)

    def test_the_privacy_line_and_storage_and_drop(self):
        self.assertIn("function renderPrivacyHint()", self.html)
        self.assertIn("'Stays on this PC'", self.html)
        self.assertIn('id="storage-group"', self.html)
        self.assertIn("await api('/api/storage')", self.html)
        self.assertIn("place.addEventListener('drop'", self.html)
        self.assertIn("useRefFile(e.dataTransfer.files[0])", self.html)


class StorageTests(unittest.TestCase):
    def test_storage_reads_a_folder_and_changes_nothing(self):
        from fastapi.testclient import TestClient
        import image_gen
        import main
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "data", "chats"))
        with open(os.path.join(root, "data", "chats", "a.json"), "wb") as f:
            f.write(b"x" * 3000)
        with open(os.path.join(root, "data", "settings.json"), "wb") as f:
            f.write(b"y" * 10)
        before = sorted(os.listdir(os.path.join(root, "data")))
        client = TestClient(main.app, base_url="http://127.0.0.1:8000")
        with mock.patch.object(image_gen, "ROOT", root), \
             mock.patch.object(image_gen, "_cfg", return_value={"image_root": root}):
            r = client.get("/api/storage").json()
        self.assertTrue(r["ok"])
        self.assertEqual([(i["name"], i["bytes"], i["files"]) for i in r["items"]], [("chats", 3000, 1), ("settings.json", 10, 1)])
        self.assertEqual(r["total"], 3010)
        self.assertEqual({d["label"] for d in r["drives"]}, {"App data", "Image models"})
        self.assertEqual(sorted(os.listdir(os.path.join(root, "data"))), before)


if __name__ == "__main__":
    unittest.main()

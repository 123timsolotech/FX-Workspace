"""Patch editing (patch_edit.py + the apply_patch tool): multi-hunk, line
context, fuzzy anchors, dry-run preview, all-or-nothing apply, undoable."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import patch_edit as pe
import undo_store
from tool_executor import ToolExecutor
import tools.approvals as approvals
import tools.dispatch as dispatch
import tools.files as files
import tools.runtime as runtime

SRC = """def load(path):
    with open(path) as f:
        return f.read()


def save(path, text):
    with open(path, "w") as f:
        f.write(text)


def main():
    data = load("a.txt")
    save("b.txt", data)
"""


class EngineTests(unittest.TestCase):
    def test_multi_hunk_exact(self):
        new, matches = pe.apply_to_text(SRC, [
            {"find": "return f.read()", "replace": "return f.read().strip()"},
            {"find": 'save("b.txt", data)', "replace": 'save("c.txt", data)'},
        ])
        self.assertIn("f.read().strip()", new)
        self.assertIn('save("c.txt", data)', new)
        self.assertEqual([m["method"] for m in matches], ["exact", "exact"])

    def test_ambiguous_find_is_refused_until_disambiguated(self):
        edit = {"find": "    with open(path", "replace": "    with open(os.fspath(path)"}
        with self.assertRaises(pe.PatchError) as cm:
            pe.apply_to_text(SRC, [edit])
        self.assertIn("matches 2 places", str(cm.exception))
        by_line, m1 = pe.apply_to_text(SRC, [{**edit, "line": 7}])
        self.assertEqual(m1[0]["lines"], "7-7")
        by_ctx, m2 = pe.apply_to_text(SRC, [{**edit, "before": "def save(path, text):"}])
        self.assertEqual(by_line, by_ctx)

    def test_whitespace_and_fuzzy_anchors(self):
        # The model re-typed the lines with different indentation and a small slip.
        ws, m = pe.apply_to_text(SRC, [{"find": "def main():\ndata = load('a.txt')".replace("'", '"'),
                                        "replace": "def main():\ndata = load(\"z.txt\")"}])
        self.assertEqual(m[0]["method"], "whitespace")
        self.assertIn('    data = load("z.txt")', ws)                   # file indentation kept
        fz, m = pe.apply_to_text(SRC, [{"find": "def save(path, txt):\n    with open(path, 'w') as f:\n        f.write(text)",
                                        "replace": "def save(path, text):\n    Path(path).write_text(text)"}])
        self.assertEqual(m[0]["method"], "fuzzy")
        self.assertGreaterEqual(m[0]["score"], pe.FUZZY_MIN)
        self.assertIn("Path(path).write_text(text)", fz)
        self.assertNotIn('open(path, "w")', fz)

    def test_not_found_and_overlap_are_errors(self):
        with self.assertRaises(pe.PatchError) as cm:
            pe.apply_to_text(SRC, [{"find": "def delete_everything():", "replace": ""}])
        self.assertIn("not found", str(cm.exception))
        with self.assertRaises(pe.PatchError) as cm:
            pe.apply_to_text(SRC, [{"find": "def load(path):\n    with open(path) as f:", "replace": "x"},
                                   {"find": "    with open(path) as f:\n        return f.read()", "replace": "y"}])
        self.assertIn("overlap", str(cm.exception))

    def test_all_or_nothing(self):
        with self.assertRaises(pe.PatchError):
            pe.apply_to_text(SRC, [{"find": "return f.read()", "replace": "return 1"},
                                   {"find": "this is not in the file at all", "replace": "x"}])
        self.assertFalse(pe.dry_run(SRC, [{"find": "nope nope nope", "replace": "x"}])["ok"])

    def test_crlf_is_preserved(self):
        text = SRC.replace("\n", "\r\n")
        new, _ = pe.apply_to_text(text, [{"find": "return f.read()", "replace": "return f.read()\n        # done"}])
        self.assertIn("\r\n        # done\r\n", new)
        self.assertNotIn("\n", new.replace("\r\n", ""))

    def test_find_ending_in_newline_adds_no_blank_line(self):
        text = "a\n    b = 1\nc\n"
        # whitespace match (spacing differs), both find and replace end with "\n"
        new, m = pe.apply_to_text(text, [{"find": "b  =  1\n", "replace": "b = 2\n", "after": "c"}])
        self.assertEqual(m[0]["method"], "whitespace")
        self.assertEqual(new, "a\n    b = 2\nc\n")
        exact, m = pe.apply_to_text("a\nb\nc\n", [{"find": "b\n", "replace": "B\n", "after": "c"}])
        self.assertEqual((exact, m[0]["method"]), ("a\nB\nc\n", "exact"))

    def test_mixed_line_endings_are_left_alone_and_bad_hints_ignored(self):
        mixed = "one\r\ntwo\nthree\r\n"
        new, _ = pe.apply_to_text(mixed, [{"find": "two", "replace": "TWO", "line": "not a number"}])
        self.assertEqual(new, "one\r\nTWO\nthree\r\n")

    def test_dry_run_diff(self):
        res = pe.dry_run(SRC, [{"find": "return f.read()", "replace": "return f.read().strip()"}], "io.py")
        self.assertIn("-        return f.read()", res["diff"])
        self.assertIn("+        return f.read().strip()", res["diff"])
        self.assertEqual(SRC.count("strip"), 0)                          # the text itself is untouched


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "io.py"
        self.file.write_text(SRC, encoding="utf-8")
        self.store = undo_store.UndoStore(os.path.join(self.tmp.name, "undo"))
        self.ex = ToolExecutor(runtime.TOOL_REGISTRY, dispatch.execute_tool, undo=self.store)
        self.p = patch.object(files, "undo", self.store)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_apply_undo_and_preview(self):
        edits = [{"find": "return f.read()", "replace": "return f.read().strip()"}]
        pv = approvals._patch_preview({"filename": str(self.file), "edits": edits})
        self.assertEqual(pv["kind"], "diff")
        self.assertIn("+        return f.read().strip()", pv["text"])
        res = self.ex.execute("apply_patch", {"filename": str(self.file), "edits": edits})
        self.assertTrue(res["ok"], res["message"])
        self.assertIn("strip()", self.file.read_text(encoding="utf-8"))
        self.assertTrue(self.store.undo(res["undo_id"])["ok"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), SRC)

    def test_failing_patch_changes_nothing_and_leaves_no_trail(self):
        res = self.ex.execute("apply_patch", {"filename": str(self.file), "edits": [
            {"find": "return f.read()", "replace": "return 1"}, {"find": "missing text here", "replace": "x"}]})
        self.assertFalse(res["ok"])
        self.assertIn("nothing was changed", res["message"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), SRC)
        self.assertEqual(self.store.list(), [])

    def test_interrupted_patch_is_rolled_back(self):
        def crash(path, text):
            Path(path).write_text("half-written", encoding="utf-8")
            raise OSError("power cut")
        with patch.object(files, "_atomic_write", side_effect=crash):
            res = self.ex.execute("apply_patch", {"filename": str(self.file),
                                                  "edits": [{"find": "return f.read()", "replace": "return 2"}]})
        self.assertFalse(res["ok"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), SRC)

    def test_registered_undoable_and_shown_as_a_diff(self):
        self.assertTrue(runtime.TOOL_REGISTRY.get("apply_patch").undoable)
        view = approvals._approval_view({"tool_name": "apply_patch", "args": {"filename": str(self.file), "edits": [
            {"find": "return f.read()", "replace": "return 3"}]}})
        self.assertEqual(view["undo_text"], "Can be undone")
        self.assertEqual(view["preview"]["kind"], "diff")


if __name__ == "__main__":
    unittest.main()

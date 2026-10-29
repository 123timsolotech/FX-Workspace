"""The undo trail (undo_store.py): every file-changing tool is journaled by
the ToolExecutor, can be undone later, and is rolled back at once if it fails
or is interrupted - through the real executor and the real tool code."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import undo_store
from run_control import RunStopped
from tool_executor import ToolExecutor
import tools.dispatch as dispatch
import tools.files as files
import tools.runtime as runtime


class UndoTrailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "work"
        self.dir.mkdir()
        self.store = undo_store.UndoStore(os.path.join(self.tmp.name, "undo"))
        self.ex = ToolExecutor(runtime.TOOL_REGISTRY, dispatch.execute_tool, undo=self.store)
        self.patch = patch.object(files, "undo", self.store)     # _journal() follows the executor's store
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_tool(self, name, **args):
        return self.ex.execute(name, args)

    def p(self, *parts):
        return str(self.dir.joinpath(*parts))

    # ---- write / edit ---------------------------------------------------------
    def test_new_file_is_removed_by_undo_but_kept_in_the_trail(self):
        res = self.run_tool("write_file", filename=self.p("a.txt"), content="hello")
        self.assertTrue(res["ok"])
        self.assertTrue(res["undo_id"])
        self.assertTrue(self.store.undo(res["undo_id"])["ok"])
        self.assertFalse(os.path.exists(self.p("a.txt")))
        self.assertEqual(self.store.list()[0]["status"], "undone")

    def test_overwrite_and_append_are_restored(self):
        Path(self.p("a.txt")).write_text("original", encoding="utf-8")
        first = self.run_tool("write_file", filename=self.p("a.txt"), content="new")
        second = self.run_tool("write_file", filename=self.p("a.txt"), content="+more", append=True)
        self.assertEqual(Path(self.p("a.txt")).read_text(encoding="utf-8"), "new+more")
        self.assertTrue(self.store.undo_last()["ok"])                       # undoes the append
        self.assertEqual(Path(self.p("a.txt")).read_text(encoding="utf-8"), "new")
        self.assertTrue(self.store.undo(first["undo_id"])["ok"])
        self.assertEqual(Path(self.p("a.txt")).read_text(encoding="utf-8"), "original")
        self.assertEqual(second["ok"], True)

    def test_edit_file_undo(self):
        Path(self.p("c.py")).write_text("x = 1\ny = 2\n", encoding="utf-8")
        res = self.run_tool("edit_file", filename=self.p("c.py"), old_text="y = 2", new_text="y = 3")
        self.assertTrue(res["ok"])
        self.assertTrue(self.store.undo(res["undo_id"])["ok"])
        self.assertEqual(Path(self.p("c.py")).read_text(encoding="utf-8"), "x = 1\ny = 2\n")

    # ---- delete / move / copy ----------------------------------------------------
    def test_deletes_are_recoverable(self):
        Path(self.p("f.txt")).write_text("keep me", encoding="utf-8")
        os.makedirs(self.p("folder", "inner"))
        Path(self.p("folder", "inner", "g.txt")).write_text("deep", encoding="utf-8")
        a = self.run_tool("delete_path", path=self.p("f.txt"))
        b = self.run_tool("delete_path", path=self.p("folder"), force=True)
        self.assertFalse(os.path.exists(self.p("f.txt")))
        self.assertFalse(os.path.exists(self.p("folder")))
        self.assertIn("restored with Undo", a["message"])
        self.assertTrue(self.store.undo(b["undo_id"])["ok"])
        self.assertTrue(self.store.undo(a["undo_id"])["ok"])
        self.assertEqual(Path(self.p("f.txt")).read_text(encoding="utf-8"), "keep me")
        self.assertEqual(Path(self.p("folder", "inner", "g.txt")).read_text(encoding="utf-8"), "deep")

    def test_move_with_overwrite_restores_both_sides(self):
        Path(self.p("src.txt")).write_text("source", encoding="utf-8")
        Path(self.p("dst.txt")).write_text("old destination", encoding="utf-8")
        res = self.run_tool("move_path", source=self.p("src.txt"), destination=self.p("dst.txt"), overwrite=True)
        self.assertTrue(res["ok"])
        self.assertTrue(self.store.undo(res["undo_id"])["ok"])
        self.assertEqual(Path(self.p("src.txt")).read_text(encoding="utf-8"), "source")
        self.assertEqual(Path(self.p("dst.txt")).read_text(encoding="utf-8"), "old destination")

    def test_copied_folder_and_created_dirs_are_removed(self):
        os.makedirs(self.p("tree"))
        Path(self.p("tree", "t.txt")).write_text("t", encoding="utf-8")
        cp = self.run_tool("copy_path", source=self.p("tree"), destination=self.p("new", "sub", "tree2"))
        mk = self.run_tool("make_dir", path=self.p("made", "deeper"))
        self.assertTrue(os.path.isdir(self.p("new", "sub", "tree2")))
        self.assertTrue(self.store.undo(mk["undo_id"])["ok"])
        self.assertTrue(self.store.undo(cp["undo_id"])["ok"])
        self.assertFalse(os.path.exists(self.p("made")))
        self.assertFalse(os.path.exists(self.p("new")))
        self.assertTrue(os.path.exists(self.p("tree", "t.txt")))              # the source is untouched

    # ---- safety -----------------------------------------------------------------
    def test_undo_refuses_to_overwrite_newer_work_unless_forced(self):
        res = self.run_tool("write_file", filename=self.p("n.txt"), content="v1")
        Path(self.p("n.txt")).write_text("edited by the user later", encoding="utf-8")
        out = self.store.undo(res["undo_id"])
        self.assertFalse(out["ok"])
        self.assertTrue(out["conflict"])
        self.assertEqual(Path(self.p("n.txt")).read_text(encoding="utf-8"), "edited by the user later")
        self.assertTrue(self.store.undo(res["undo_id"], force=True)["ok"])

    def test_interrupted_edit_leaves_the_original_file(self):
        """A write that dies halfway is rolled back at once - nothing half-applied."""
        Path(self.p("doc.txt")).write_text("important original", encoding="utf-8")

        def dies_halfway(path, text):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text[:3])                                           # partial content lands on disk
            raise OSError("disk full")
        with patch.object(files, "_atomic_write", side_effect=dies_halfway):
            res = self.run_tool("write_file", filename=self.p("doc.txt"), content="replacement text")
        self.assertFalse(res["ok"])
        self.assertIn("rolled back", res["message"])
        self.assertEqual(Path(self.p("doc.txt")).read_text(encoding="utf-8"), "important original")
        self.assertEqual(self.store.list(), [])                                # nothing left to undo

    def test_stop_during_a_tool_rolls_back_and_still_stops(self):
        Path(self.p("s.txt")).write_text("before", encoding="utf-8")

        def stopped_midway(path, text):
            with open(path, "w", encoding="utf-8") as f:
                f.write("half")
            raise RunStopped("user")
        with patch.object(files, "_atomic_write", side_effect=stopped_midway):
            with self.assertRaises(RunStopped):
                self.run_tool("write_file", filename=self.p("s.txt"), content="after")
        self.assertEqual(Path(self.p("s.txt")).read_text(encoding="utf-8"), "before")
        self.assertIsNone(self.store.active())                                 # the journal lock was released
        self.assertTrue(self.run_tool("write_file", filename=self.p("s.txt"), content="ok")["ok"])

    def test_failed_tools_leave_no_trail_entry(self):
        res = self.run_tool("edit_file", filename=self.p("missing.txt"), old_text="a", new_text="b")
        self.assertFalse(res["ok"])
        self.assertEqual(self.store.list(), [])

    def test_retention_keeps_the_newest_changes(self):
        with patch.object(undo_store, "MAX_CHANGES", 3):
            ids = [self.run_tool("write_file", filename=self.p(f"r{i}.txt"), content=str(i))["undo_id"] for i in range(5)]
        kept = [c["id"] for c in self.store.list()]
        self.assertEqual(kept, list(reversed(ids[-3:])))
        self.assertFalse(os.path.exists(os.path.join(self.store.root, ids[0])))  # pruned backups are gone

    def test_the_newest_change_is_never_pruned_even_when_over_the_size_limit(self):
        """A delete bigger than the whole retention limit must stay restorable."""
        os.makedirs(self.p("big"))
        Path(self.p("big", "data.bin")).write_bytes(b"x" * 4096)
        with patch.object(undo_store, "MAX_BYTES", 1024):
            older = self.run_tool("write_file", filename=self.p("old.txt"), content="o" * 2000)
            res = self.run_tool("delete_path", path=self.p("big"), force=True)
        kept = [c["id"] for c in self.store.list()]
        self.assertIn(res["undo_id"], kept)
        self.assertNotIn(older["undo_id"], kept)                       # older ones make room instead
        self.assertTrue(self.store.undo(res["undo_id"])["ok"])
        self.assertEqual(Path(self.p("big", "data.bin")).read_bytes(), b"x" * 4096)

    def test_append_to_a_big_file_keeps_its_contents(self):
        Path(self.p("log.txt")).write_text("0123456789", encoding="utf-8")
        with patch.object(undo_store, "BIG_FILE", 5):                  # "big" = over 5 bytes
            res = self.run_tool("write_file", filename=self.p("log.txt"), content="+more", append=True)
        self.assertEqual(Path(self.p("log.txt")).read_text(encoding="utf-8"), "0123456789+more")
        self.assertTrue(self.store.undo(res["undo_id"])["ok"])
        self.assertEqual(Path(self.p("log.txt")).read_text(encoding="utf-8"), "0123456789")

    def test_half_done_folder_copy_is_rolled_back(self):
        os.makedirs(self.p("src"))
        Path(self.p("src", "a.txt")).write_text("a", encoding="utf-8")

        def copy_then_fail(src, dst, *a, **k):
            os.makedirs(dst)
            Path(dst, "a.txt").write_text("partial", encoding="utf-8")
            raise OSError("disk full")
        with patch("fs_tools.shutil.copytree", side_effect=copy_then_fail):
            res = self.run_tool("copy_path", source=self.p("src"), destination=self.p("dst"))
        self.assertFalse(res["ok"])
        self.assertFalse(os.path.exists(self.p("dst")))                # no partial copy left behind

    def test_replays_never_touch_the_real_undo_trail(self):
        import replay_harness as rh
        real = self.patch.temp_original                                # the store behind data/undo
        before = list(real.list(300)) if os.path.exists(real.manifest_path) else []
        rh.run_fixture(rh.load_fixture("fragmented_tool_calls"))       # really writes a file
        after = list(real.list(300)) if os.path.exists(real.manifest_path) else []
        self.assertEqual(before, after)

    def test_every_file_changing_tool_is_marked_undoable(self):
        for name in ("write_file", "edit_file", "copy_path", "move_path", "rename_path", "delete_path",
                     "make_dir", "zip_paths", "unzip_file", "download_file", "screenshot_to_file"):
            self.assertTrue(runtime.TOOL_REGISTRY.get(name).undoable, name)
        for name in ("execute_command", "run_python", "open_url", "kill_process"):
            self.assertFalse(runtime.TOOL_REGISTRY.get(name).undoable, name)


if __name__ == "__main__":
    unittest.main()

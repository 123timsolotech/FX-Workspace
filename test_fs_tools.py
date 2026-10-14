"""Tests for fs_tools.py. Uses a real temp directory rather than mocking
os/shutil - these functions are thin, well-tested-stdlib wrappers, so
exercising them against real files catches more than mocking would."""
import os
import shutil
import tempfile
import unittest
import zipfile

import fs_tools


class FsToolsTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fs_tools_test_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def write(self, path, content="hello"):
        full = self.p(path)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full


class CopyMoveRenameDeleteTests(FsToolsTestCase):
    def test_copy_file(self):
        self.write("a.txt", "hi")
        result = fs_tools.copy_path(self.p("a.txt"), self.p("b.txt"))
        self.assertTrue(result["ok"])
        self.assertTrue(os.path.exists(self.p("b.txt")))
        self.assertTrue(os.path.exists(self.p("a.txt")))  # source untouched

    def test_copy_missing_source_fails(self):
        result = fs_tools.copy_path(self.p("nope.txt"), self.p("b.txt"))
        self.assertFalse(result["ok"])

    def test_copy_existing_destination_requires_overwrite(self):
        self.write("a.txt")
        self.write("b.txt", "existing")
        result = fs_tools.copy_path(self.p("a.txt"), self.p("b.txt"))
        self.assertFalse(result["ok"])
        self.assertIn("overwrite=true", result["message"])
        result2 = fs_tools.copy_path(self.p("a.txt"), self.p("b.txt"), overwrite=True)
        self.assertTrue(result2["ok"])

    def test_move_file(self):
        self.write("a.txt", "hi")
        result = fs_tools.move_path(self.p("a.txt"), self.p("sub/b.txt"))
        self.assertTrue(result["ok"])
        self.assertFalse(os.path.exists(self.p("a.txt")))
        self.assertTrue(os.path.exists(self.p("sub/b.txt")))

    def test_rename_rejects_path_separators(self):
        self.write("a.txt")
        result = fs_tools.rename_path(self.p("a.txt"), "sub/b.txt")
        self.assertFalse(result["ok"])

    def test_rename_happy_path(self):
        self.write("a.txt")
        result = fs_tools.rename_path(self.p("a.txt"), "renamed.txt")
        self.assertTrue(result["ok"])
        self.assertTrue(os.path.exists(self.p("renamed.txt")))

    def test_delete_file(self):
        self.write("a.txt")
        result = fs_tools.delete_path(self.p("a.txt"))
        self.assertTrue(result["ok"])
        self.assertFalse(os.path.exists(self.p("a.txt")))

    def test_delete_non_empty_dir_requires_force(self):
        self.write("dir/a.txt")
        result = fs_tools.delete_path(self.p("dir"))
        self.assertFalse(result["ok"])
        self.assertIn("force=true", result["message"])
        result2 = fs_tools.delete_path(self.p("dir"), force=True)
        self.assertTrue(result2["ok"])
        self.assertFalse(os.path.exists(self.p("dir")))

    def test_make_dir_is_idempotent(self):
        r1 = fs_tools.make_dir(self.p("new/nested"))
        r2 = fs_tools.make_dir(self.p("new/nested"))
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])


class ZipTests(FsToolsTestCase):
    def test_zip_and_unzip_roundtrip(self):
        self.write("a.txt", "content-a")
        self.write("sub/b.txt", "content-b")
        zip_result = fs_tools.zip_paths([self.p("a.txt"), self.p("sub")], self.p("out.zip"))
        self.assertTrue(zip_result["ok"])
        self.assertTrue(os.path.isfile(self.p("out.zip")))

        unzip_result = fs_tools.unzip_file(self.p("out.zip"), self.p("extracted"))
        self.assertTrue(unzip_result["ok"])
        self.assertTrue(os.path.exists(self.p("extracted/a.txt")))

    def test_unzip_rejects_path_traversal(self):
        evil = self.p("evil.zip")
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../../escape.txt", "pwned")
        result = fs_tools.unzip_file(evil, self.p("dest"))
        self.assertFalse(result["ok"])
        self.assertIn("traversal", result["message"])
        self.assertFalse(os.path.exists(self.p("escape.txt")))

    def test_zip_missing_source_fails(self):
        result = fs_tools.zip_paths([self.p("nope.txt")], self.p("out.zip"))
        self.assertFalse(result["ok"])


class SearchAndInfoTests(FsToolsTestCase):
    def test_search_by_pattern(self):
        self.write("notes.txt")
        self.write("image.png")
        result = fs_tools.search_files(self.root, pattern="*.txt")
        self.assertTrue(result["ok"])
        self.assertIn("notes.txt", result["message"])
        self.assertNotIn("image.png", result["message"])

    def test_search_by_content(self):
        self.write("a.txt", "the quick brown fox")
        self.write("b.txt", "something else entirely")
        result = fs_tools.search_files(self.root, pattern="*.txt", content_query="quick brown")
        self.assertTrue(result["ok"])
        self.assertIn("a.txt", result["message"])
        self.assertNotIn("b.txt", result["message"])

    def test_search_no_matches(self):
        self.write("a.txt")
        result = fs_tools.search_files(self.root, pattern="*.pdf")
        self.assertFalse(result["ok"])

    def test_file_info_reports_size_and_kind(self):
        self.write("a.txt", "12345")
        result = fs_tools.file_info(self.p("a.txt"))
        self.assertTrue(result["ok"])
        self.assertIn("5 bytes", result["message"])
        self.assertIn("file", result["message"])

    def test_file_info_missing_path(self):
        result = fs_tools.file_info(self.p("nope"))
        self.assertFalse(result["ok"])


class DiskAndCompareTests(FsToolsTestCase):
    def test_disk_usage_reports_numbers(self):
        result = fs_tools.disk_usage(self.root)
        self.assertTrue(result["ok"])
        self.assertIn("GB free", result["message"])

    def test_compare_identical_files(self):
        self.write("a.txt", "same content")
        self.write("b.txt", "same content")
        result = fs_tools.compare_files(self.p("a.txt"), self.p("b.txt"))
        self.assertTrue(result["ok"])
        self.assertIn("Identical", result["message"])

    def test_compare_different_size(self):
        self.write("a.txt", "short")
        self.write("b.txt", "a much longer content string")
        result = fs_tools.compare_files(self.p("a.txt"), self.p("b.txt"))
        self.assertIn("Different", result["message"])
        self.assertIn("sizes differ", result["message"])

    def test_compare_same_size_different_content(self):
        self.write("a.txt", "AAAAA")
        self.write("b.txt", "BBBBB")
        result = fs_tools.compare_files(self.p("a.txt"), self.p("b.txt"))
        self.assertIn("Different", result["message"])

    def test_compare_missing_file(self):
        self.write("a.txt")
        result = fs_tools.compare_files(self.p("a.txt"), self.p("missing.txt"))
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

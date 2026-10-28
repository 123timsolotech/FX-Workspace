"""Settings > Data and files (1.4.4): sizes, Clear (Recycle Bin or for good), pinned chats kept, nothing outside data\\."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import data_files
import remote_access


class DataFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "data")
        p = patch.object(data_files, "DATA", self.data)
        p.start()
        self.addCleanup(p.stop)
        self.write("chats/aaaaaaaaaaaa.json", {"id": "aaaaaaaaaaaa", "pinned": True})
        self.write("chats/bbbbbbbbbbbb.json", {"id": "bbbbbbbbbbbb"})
        self.write("chats/trash/cccccccccccc-1.json", {"id": "cccccccccccc"})
        self.write("images/2026/one.png", "x" * 1000)
        self.outside = os.path.join(self.tmp.name, "keep.txt")
        with open(self.outside, "w") as f:
            f.write("not app data")

    def write(self, rel, content):
        path = os.path.join(self.data, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))

    def test_summary_counts_each_kind(self):
        s = {k["id"]: k for k in data_files.summary()["kinds"]}
        self.assertEqual(s["chats"]["files"], 2)              # the trash is its own kind
        self.assertEqual(s["chat_trash"]["files"], 1)
        self.assertEqual(s["images"]["files"], 1)
        self.assertGreaterEqual(s["images"]["bytes"], 1000)
        self.assertFalse(s["memory"]["clearable"])

    def test_clear_chats_keeping_pinned_for_good(self):
        r = data_files.clear("chats", permanent=True, keep_pinned=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["files"], 1)
        left = os.listdir(os.path.join(self.data, "chats"))
        self.assertIn("aaaaaaaaaaaa.json", left)               # pinned stays
        self.assertNotIn("bbbbbbbbbbbb.json", left)
        self.assertTrue(os.path.exists(os.path.join(self.data, "chats", "trash", "cccccccccccc-1.json")))
        self.assertTrue(os.path.exists(self.outside))

    def test_recycle_bin_by_default_and_nothing_lost_if_it_fails(self):
        with patch.object(data_files, "_recycle", return_value=True) as rec:
            self.assertTrue(data_files.clear("images")["ok"])
        self.assertEqual([os.path.basename(p) for p in rec.call_args[0][0]], ["one.png"])
        with patch.object(data_files, "_recycle", return_value=False):
            r = data_files.clear("chats")
        self.assertFalse(r["ok"])
        self.assertIn("Nothing was deleted", r["message"])
        self.assertEqual(len(os.listdir(os.path.join(self.data, "chats"))), 3)

    def test_open_only_kinds_and_unknown_kinds_refuse(self):
        self.assertFalse(data_files.clear("memory")["ok"])
        self.assertFalse(data_files.clear("../../Windows")["ok"])

    def test_never_outside_the_data_folder(self):
        self.assertFalse(data_files._inside(self.outside))
        self.assertFalse(data_files._inside(self.data))
        self.assertTrue(data_files._inside(os.path.join(self.data, "images", "2026", "one.png")))

    def test_the_phone_cannot_use_it(self):
        self.assertTrue(remote_access.blocked("POST", "/api/data-files/clear"))
        self.assertTrue(remote_access.blocked("GET", "/api/data-files"))


if __name__ == "__main__":
    unittest.main()

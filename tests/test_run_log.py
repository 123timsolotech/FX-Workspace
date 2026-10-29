"""Past runs (run_log.py): saved from real events, slimmed, searchable,
pruned by count and age, and never broken by a bad record."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import run_log


def _run(i, request="make the lava kill players", tool="roblox_edit_script", ok=True, target="ServerScriptService/Lava"):
    return {"id": f"run{i:06d}", "request": request, "started": 1000 + i, "ended": 1010 + i, "status": "success",
            "effort": "high", "model": "m", "usage": {"steps": 3, "step_limit": 30},
            "events": [{"id": 1, "kind": "activity", "phase": "run_started", "label": "Working · Thorough", "effort": "high"},
                       {"id": 2, "kind": "tool", "tool": tool, "label": f"Edit {target}", "ok": ok,
                        "detail": f"Updated {target}", "undo_id": "u1", "secret_blob": "x" * 5000},
                       {"id": 3, "kind": "approval", "label": "Approved", "ok": True}]}


class RunLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = run_log.RunLog(os.path.join(self.tmp.name, "runs"))

    def test_save_list_get_and_search(self):
        self.log.save(_run(1))
        self.log.save(_run(2, request="write my grocery list", tool="write_file", ok=False, target="C:\\groceries.txt"))
        rows = self.log.list()
        self.assertEqual([r["id"] for r in rows], ["run000002", "run000001"])
        self.assertEqual((rows[0]["tools"], rows[0]["failed"], rows[1]["approvals"]), (1, 1, 1))
        self.assertEqual([r["id"] for r in self.log.list("lava")], ["run000001"])
        self.assertEqual([r["id"] for r in self.log.list("roblox_edit_script lava")], ["run000001"])
        self.assertEqual(self.log.list("nothing matches"), [])
        full = self.log.get("run000001")
        self.assertEqual(full["events"][1]["undo_id"], "u1")
        self.assertNotIn("secret_blob", full["events"][1])             # only the fields the feed shows

    def test_pruned_by_count_and_age(self):
        with patch.object(run_log, "MAX_RUNS", 3):
            for i in range(5):
                self.log.save(_run(i))
                now = time.time() - 100 + i
                os.utime(os.path.join(self.log.root, f"run{i:06d}.json"), (now, now))
        self.assertEqual(len(self.log.list()), 3)
        old = os.path.join(self.log.root, "run000004.json")
        os.utime(old, (time.time() - run_log.MAX_AGE_S - 10,) * 2)
        self.log.save(_run(9))
        self.assertNotIn("run000004", [r["id"] for r in self.log.list()])

    def test_bad_input_never_raises(self):
        self.assertIsNone(self.log.save({"id": "../../etc/passwd", "events": []}))
        self.assertIsNone(self.log.get("../x"))
        os.makedirs(self.log.root, exist_ok=True)
        with open(os.path.join(self.log.root, "broken1.json"), "w") as f:
            f.write("{not json")
        self.log.save(_run(1))
        self.assertEqual([r["id"] for r in self.log.list()], ["run000001"])


if __name__ == "__main__":
    unittest.main()

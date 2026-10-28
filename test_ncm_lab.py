"""1.4.2 Phase 7: the NCM story writer's setup and level-by-level reading (ncm_lab.py) - no download in tests."""
import ast
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import labs
import ncm_lab


class NcmLabTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        p = patch.object(ncm_lab, "ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        ncm_lab.state.update(setup=None, progress="", error=None, writing=False, levels=[], result=None, compare=None)

    def test_not_ready_until_code_and_checkpoint_are_there(self):
        with patch.object(labs, "engine", return_value={"installed": True}):
            self.assertFalse(ncm_lab.status()["ready"])
            self.assertFalse(ncm_lab.write()["ok"])
            os.makedirs(os.path.join(ncm_lab.repo_dir(), "models"))
            open(os.path.join(ncm_lab.repo_dir(), "models", "next_concept.py"), "w").close()
            os.makedirs(ncm_lab.ckpt_dir())
            open(os.path.join(ncm_lab.ckpt_dir(), ".complete"), "w").close()
            self.assertTrue(ncm_lab.status()["ready"])

    def test_setup_refuses_without_the_labs_engine(self):
        with patch.object(labs, "engine", return_value={"installed": False}):
            ncm_lab._setup()
        self.assertIn("Labs engine", ncm_lab.state["error"])

    def test_the_worker_reads_levels_line_by_line(self):
        lines = [json.dumps({"loaded": 3.2, "levels": 12}), "a warning from torch",
                 json.dumps({"level": 1, "of": 2, "text": "Once"}), json.dumps({"level": 2, "of": 2, "text": "Once upon a time."}),
                 json.dumps({"ok": True, "seconds": 0.21})]

        class FakeProc:
            stdout = iter(l + "\n" for l in lines)
            returncode = 0
            stderr = None

            def wait(self, timeout=None):
                return 0
        for d in (ncm_lab.repo_dir() + "/models", ncm_lab.ckpt_dir()):
            os.makedirs(d, exist_ok=True)
        open(os.path.join(ncm_lab.repo_dir(), "models", "next_concept.py"), "w").close()
        open(os.path.join(ncm_lab.ckpt_dir(), ".complete"), "w").close()
        with patch.object(labs, "engine", return_value={"installed": True}), patch.object(labs, "gpu_busy", return_value=False), \
                patch.object(labs, "_free_gpu"), patch.object(labs, "_worker_env", return_value={}), \
                patch("subprocess.Popen", return_value=FakeProc()):
            self.assertTrue(ncm_lab.write(7)["ok"])
            for _ in range(100):
                if not ncm_lab.state["writing"]:
                    break
                import time
                time.sleep(0.02)
        self.assertEqual([l["text"] for l in ncm_lab.state["levels"]], ["Once", "Once upon a time."])
        self.assertEqual(ncm_lab.state["result"], {"seconds": 0.21, "seed": 7, "words": 4})

    def test_the_worker_is_valid_python_and_uses_the_repos_entry_points(self):
        with open(ncm_lab.WORKER, encoding="utf-8") as f:
            src = f.read()
        ast.parse(src)
        for name in ("VQVAETokenizer.from_config", "NextConceptTransformer(", "idx_to_zhats", "tinystories.yaml"):
            self.assertIn(name, src)


if __name__ == "__main__":
    unittest.main()

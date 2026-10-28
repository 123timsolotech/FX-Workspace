"""1.4.2 Phase 7: the MoE cache is kept only when it measured faster (moe_test.py)."""
import unittest
from unittest.mock import patch

import llama_local
import moe_test


class MoeDecisionTests(unittest.TestCase):
    base = {"mib": None, "prompt_tps": 500.0, "gen_tps": 20.0}

    def test_kept_only_when_writing_is_5_percent_faster_and_prompt_holds(self):
        tried = [{"mib": 512, "prompt_tps": 498.0, "gen_tps": 20.5},      # +2.5%: not enough
                 {"mib": 1024, "prompt_tps": 495.0, "gen_tps": 21.4},     # +7%, prompt -1%: ok
                 {"mib": 1536, "prompt_tps": 420.0, "gen_tps": 23.0}]     # faster writing, but prompt dropped 16%
        keep, why = moe_test.decide(self.base, tried)
        self.assertEqual(keep, 1024)
        self.assertIn("Kept 1024 MiB", why)

    def test_slower_or_failed_changes_nothing(self):
        keep, why = moe_test.decide(self.base, [{"mib": 512, "prompt_tps": 500.0, "gen_tps": 17.0},
                                                {"mib": 1024, "error": "oom", "prompt_tps": None, "gen_tps": None}])
        self.assertIsNone(keep)
        self.assertIn("stays off", why)

    def test_sizes_fit_the_free_memory(self):
        self.assertEqual(moe_test.sizes(4000), [768, 1536, 2304])
        self.assertEqual(moe_test.sizes(900), [])                        # too little room: nothing to try
        self.assertTrue(all(s % 256 == 0 for s in moe_test.sizes(3333)))

    def test_the_option_may_be_written(self):
        self.assertIn("moe-cache-mib", llama_local.OPTION_KEYS)

    def test_a_failed_run_puts_the_setting_back(self):
        writes = []
        with patch.object(moe_test, "candidates", return_value=[{"name": "M"}]), \
                patch.object(llama_local, "model_options", return_value={"moe-cache-mib": "256"}), \
                patch.object(llama_local, "set_model_options", side_effect=lambda c, backup=True: writes.append(c)), \
                patch.object(moe_test, "_with_cache", side_effect=RuntimeError("router down")), \
                patch.object(moe_test, "_save"), patch.object(moe_test, "_load", return_value={}):
            r = moe_test.run("M")
        self.assertFalse(r["ok"])
        self.assertEqual(writes[-1], {"M": {"moe-cache-mib": "256"}})


if __name__ == "__main__":
    unittest.main()

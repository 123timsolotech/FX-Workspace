"""Tune speed (1.4.5): what it tries, what it keeps, and that a failure puts models.ini back. No GPU needed."""
import unittest
from unittest.mock import patch

import speed_tune


class DecideTests(unittest.TestCase):
    def test_prefill_keeps_only_a_clear_win(self):
        base = {"batch": 1024, "ubatch": 512, "prompt_tps": 400, "gen_tps": 20}
        tried = [{"batch": 2048, "ubatch": 512, "prompt_tps": 420, "gen_tps": 20},      # +5%: not enough
                 {"batch": 2048, "ubatch": 2048, "prompt_tps": 900, "gen_tps": 19.8}]   # +125%, writing -1%: kept
        keep, why = speed_tune.decide_prefill(base, tried)
        self.assertEqual((keep["batch"], keep["ubatch"]), (2048, 2048))
        self.assertIn("+125%", why)
        keep, _ = speed_tune.decide_prefill(base, [{"batch": 2048, "ubatch": 2048, "prompt_tps": 900, "gen_tps": 15}])
        self.assertIsNone(keep)                                                         # writing got slower

    def test_split_never_keeps_a_spill(self):
        base = {"n_cpu_moe": 34, "prompt_tps": 300, "gen_tps": 18}
        tried = [{"n_cpu_moe": 30, "prompt_tps": 310, "gen_tps": 21},
                 {"n_cpu_moe": 26, "prompt_tps": 320, "gen_tps": 40, "spilled": True}]
        keep, why = speed_tune.decide_split(base, tried)
        self.assertEqual(keep["n_cpu_moe"], 30)
        self.assertIn("was 34", why)
        self.assertIsNone(speed_tune.decide_split(base, [{"n_cpu_moe": 30, "prompt_tps": 300, "gen_tps": 18.5}])[0])

    def test_split_values(self):
        self.assertEqual(speed_tune.split_values(34, 40), [30, 26, 22, 38])
        self.assertEqual(speed_tune.split_values(6, 8), [2])                             # never below 0 or above the layers

    def test_the_prompt_is_long(self):
        self.assertGreater(len(speed_tune.PROMPT), 6000)
        self.assertNotIn("{n}", speed_tune.PROMPT)


class RunTests(unittest.TestCase):
    def test_a_failure_puts_the_models_own_settings_back(self):
        writes = []
        with patch.object(speed_tune, "candidates", return_value=[{"name": "M", "moe": False}]), \
                patch("llama_local.model_own_options", return_value={"batch-size": "512"}), \
                patch("llama_local.model_options", return_value={"batch-size": "512", "ubatch-size": "512"}), \
                patch("llama_local.set_model_options", side_effect=lambda ch, backup=True: writes.append(ch)), \
                patch("llama_local.unload_all"), \
                patch.object(speed_tune, "_with", side_effect=RuntimeError("router gone")), \
                patch.object(speed_tune, "_save"), patch.object(speed_tune, "_load", return_value={}):
            r = speed_tune.run("M")
        self.assertFalse(r["ok"])
        self.assertEqual(writes[-1], {"M": {"batch-size": "512", "ubatch-size": None, "n-cpu-moe": None}})


if __name__ == "__main__":
    unittest.main()

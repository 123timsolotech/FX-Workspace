"""A reply's writing speed (tok/s) is measured over the time spent writing, not the whole reply."""
import unittest

from agent import sessions


class ReplySpeedTests(unittest.TestCase):
    def test_speed_excludes_the_wait_for_the_first_token(self):
        events = [
            {"kind": "model", "status": "done", "tokens_out": 100, "duration_ms": 12000, "first_token_s": 10.0, "model": "m", "t": 1.0},
            {"kind": "model", "status": "done", "tokens_out": 50, "duration_ms": 1500, "first_token_s": 0.5, "model": "m", "t": 20.0},
        ]
        m = sessions.metrics_of(events, started=None)
        self.assertEqual(m["out"], 150)
        self.assertEqual(m["gen_s"], 3.0)          # (12 - 10) + (1.5 - 0.5)
        self.assertEqual(m["tps"], 50.0)
        self.assertEqual(m["ttft"], 10.0)
        self.assertEqual(m["calls"], 2)

    def test_no_timing_no_speed(self):
        m = sessions.metrics_of([{"kind": "model", "status": "done", "tokens_out": 10, "model": "m", "t": 1.0}], started=None)
        self.assertNotIn("tps", m)
        self.assertNotIn("ttft", m)


if __name__ == "__main__":
    unittest.main()

"""Replay tests: the real agent pipeline driven by recorded (or faithfully
reconstructed) model responses from fixtures/replay/. No live model runs.

Each fixture carries its own expectations; this module runs every fixture
and adds the cross-cutting checks that must hold for all of them."""
import unittest

import replay_harness as rh
from run_control import run_control


class FixtureFormatTests(unittest.TestCase):
    def test_every_fixture_loads_and_declares_provenance(self):
        names = rh.list_fixtures()
        self.assertGreaterEqual(len(names), 4)
        for name in names:
            fx = rh.load_fixture(name)
            self.assertIn(fx["meta"]["provenance"], ("recorded", "reconstructed"), name)
            self.assertTrue(fx["meta"].get("evidence"), f"{name}: evidence is required")


class ReplayTests(unittest.TestCase):
    def _run(self, name):
        fx = rh.load_fixture(name)
        run = rh.run_fixture(fx)
        problems = rh.check_expectations(fx, run)
        self.assertEqual(problems, [], f"{name}: {problems}\nmessage: {run.message[:300]}")
        # Holds for every replay: nothing reaches approval without arguments,
        # and every tool call the model made got exactly one result.
        asked = [c["id"] for m in run.conversation if m.get("role") == "assistant" for c in m.get("tool_calls") or []]
        answered = [m["tool_call_id"] for m in run.conversation if m.get("role") == "tool"]
        self.assertEqual(sorted(asked), sorted(answered), f"{name}: dangling or duplicate tool results")
        return run

    def test_snake_game_cutoff(self):
        self._run("snake_game_cutoff")

    def test_truncated_call_repeats(self):
        self._run("truncated_call_repeats")

    def test_loop_repetition(self):
        self._run("loop_repetition")

    def test_fragmented_tool_calls(self):
        run = self._run("fragmented_tool_calls")
        name, args = run.executed[0]
        self.assertEqual(args["content"], "line 1\nline 2 é")               # escapes split across fragments
        self.assertTrue(all(r.get("streamed") for r in run.requests if r.get("tools")))

    def test_runaway_tool_call(self):
        run = self._run("runaway_tool_call")
        self.assertEqual(run.executed[0][1]["content"], "body { margin: 0; }\n")   # only the corrected call ran

    def test_bashisms_rejected_before_approval(self):
        run = self._run("bashisms_rejected_before_approval")
        card = run.approvals[0][0]
        self.assertIn("Remove-Item", card["args"]["command"])            # only the corrected command was shown

    def test_tools_refused_schema_fallback(self):
        run = self._run("tools_refused_schema_fallback")
        sent = [r for r in run.requests if "response_format" in r or "response_format" in (r.get("extra_body") or {})]
        self.assertTrue(sent, "the fallback request must carry a JSON-schema constraint")
        self.assertFalse(sent[0].get("tools"), "no native tools once the server refused them")

    def test_a_recorded_run_replays_the_same_way(self):
        """recorder.py writes real runs in the fixture format; replaying one
        reproduces the same tool calls and approvals."""
        import copy, json, tempfile
        from unittest.mock import patch
        import recorder
        base = rh.load_fixture("snake_game_cutoff")
        with tempfile.TemporaryDirectory() as tmp, patch.object(recorder, "REC_DIR", tmp):
            fx = copy.deepcopy(base)
            fx["setup"]["settings"] = {**fx["setup"]["settings"], "record_model_io": True}
            first = rh.run_fixture(fx)
            files = [f for f in __import__("os").listdir(tmp) if f.endswith(".json")]
            self.assertEqual(len(files), 1)
            rec = json.load(open(__import__("os").path.join(tmp, files[0]), encoding="utf-8"))
        self.assertEqual(rec["meta"]["provenance"], "recorded")
        self.assertEqual(len(rec["responses"]), len(base["responses"]))        # every raw reply, incl. the cut-off one
        self.assertEqual(rec["responses"][2]["finish_reason"], "length")
        rec["tools"] = base["tools"]
        second = rh.run_fixture(rec)
        self.assertEqual([n for n, _ in second.executed], [n for n, _ in first.executed])
        self.assertEqual([[a["tool_name"] for a in c] for c in second.approvals],
                         [[a["tool_name"] for a in c] for c in first.approvals])

    def test_cancelled_run(self):
        run = self._run("cancelled_run")
        self.assertEqual(run_control.state(), "idle")      # a stop never leaks into the next run


if __name__ == "__main__":
    unittest.main()

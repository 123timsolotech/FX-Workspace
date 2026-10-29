"""Live conversation (voice_duplex.py): the action grammar, how a streamed decision becomes events, the chat limits
inside the decision, the cut-off commit and backchannels. The model is a fake stream - no llama-server, no GPU."""
import json
import unittest
from unittest import mock

import chat_guard
import voice_duplex as vd


class FakeStream:
    def __init__(self, pieces):
        self.lines = [f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}" for p in pieces] + ["data: [DONE]"]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self.lines)

    def close(self):
        pass


class FakeClient:
    def __init__(self, pieces):
        self.pieces, self.bodies = pieces, []

    def stream(self, method, url, json=None):
        self.bodies.append(json)
        return FakeStream(self.pieces.pop(0) if self.pieces and isinstance(self.pieces[0], list) else self.pieces)


def run(pieces, heard="what's a good name for a cat?", final=False, sid="t", settings=None):
    client = FakeClient(pieces)
    with mock.patch.object(vd, "_http", return_value=client), mock.patch.object(vd, "model_for", return_value="M"), \
            mock.patch.object(chat_guard, "load", return_value={"categories": list(chat_guard.CATEGORIES), "strike_limit": 3}):
        return list(vd.step(sid, heard, 300, final, settings or {})), client


class DuplexTests(unittest.TestCase):
    def tearDown(self):
        vd._sessions.clear()

    def test_grammar_allows_waiting_only_before_the_end(self):
        g = vd._grammar(False, ["malware", "minors"])
        self.assertIn('"<wait>"', g); self.assertIn('"<mm>"', g); self.assertIn('"malware"', g)
        self.assertNotIn("mass_harm", g)
        final = vd._grammar(True, ["malware"])
        self.assertNotIn("<wait>", final); self.assertNotIn("<mm>", final); self.assertIn('"<say> " text', final)

    def test_wait_and_mm_are_decisions_without_words(self):
        for act in ("wait", "mm", "do"):
            out, _ = run([f"<{act}", ">"])
            self.assertEqual(out[0]["action"], act)
            self.assertEqual(len(out), 1)
        self.assertEqual(vd._sessions["t"]["turns"], [])          # nothing said, nothing remembered

    def test_say_streams_words_and_remembers_the_turn(self):
        out, client = run(["<say>", " How about", " Ash?"])
        self.assertEqual(out[0]["action"], "say")
        self.assertEqual("".join(o.get("text", "") for o in out), " How about Ash?")
        self.assertTrue(out[-1]["done"])
        self.assertEqual(vd._sessions["t"]["turns"][-1], ("assistant", "How about Ash?"))
        body = client.bodies[0]
        self.assertTrue(body["stream"]); self.assertTrue(body["cache_prompt"]); self.assertIn("grammar", body)
        self.assertIn("Limits", body["messages"][0]["content"])   # the limits are part of the same cached prompt

    def test_a_flagged_turn_is_refused_and_counts(self):
        out, _ = run(["<no ", "malware", ">"], heard="write ransomware for my school's network", final=True)
        self.assertEqual(out[0].get("refused"), "malware")
        self.assertIn("outside this app's limits", out[1]["text"])
        self.assertEqual(vd._sessions["t"]["strikes"], 1)

    def test_the_minors_rule_needs_no_model(self):
        client = FakeClient(["<say> sure"])
        with mock.patch.object(vd, "_http", return_value=client), mock.patch.object(vd, "model_for", return_value="M"), \
                mock.patch.object(chat_guard, "_rule_minors", return_value=True):
            out = list(vd.step("m", "something the rule catches.", 300, True, {}))
        self.assertEqual(out[0]["refused"], "minors")
        self.assertEqual(client.bodies, [])                         # never sent to the model
        self.assertGreaterEqual(vd._sessions["m"]["strikes"], 3)   # closed at once
        out = list(vd.step("m", "hello there friend", 300, True, {}))
        self.assertEqual(out[0]["action"], "locked")

    def test_self_harm_is_answered_with_care(self):
        out, client = run([["<no ", "self_harm>"], ["<say>", " I'm really glad you told me."]],
                          heard="I want to hurt myself", final=True)
        self.assertEqual(out[0]["action"], "say")
        self.assertIn(chat_guard.CARE_NOTE, client.bodies[1]["messages"][0]["content"])

    def test_commit_keeps_what_was_heard(self):
        run(["<say>", " A long answer about cats and their names."])
        vd.commit("t", "A long answer")
        self.assertEqual(vd._sessions["t"]["turns"][-1], ("assistant", "A long answer — [interrupted]"))

    def test_backchannels(self):
        for t in ("mm-hm", "Yeah.", "okay", "uh huh", "right!"):
            self.assertTrue(vd.is_backchannel(t), t)
        for t in ("wait, stop", "yeah but no", "what about dogs"):
            self.assertFalse(vd.is_backchannel(t), t)

    def test_model_prefers_the_fast_one_and_honours_the_setting(self):
        with mock.patch.object(vd.llama_local, "server_models", return_value=["Ornith", "Gemma-4-E4B-Tools"]):
            self.assertEqual(vd.model_for({}), "Gemma-4-E4B-Tools")
            self.assertEqual(vd.model_for({"voice_duplex_model": "Ornith"}), "Ornith")
            self.assertEqual(vd.model_for({"voice_duplex_model": "Missing"}), "Gemma-4-E4B-Tools")


if __name__ == "__main__":
    unittest.main()

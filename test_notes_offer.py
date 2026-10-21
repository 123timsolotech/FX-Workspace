"""The assistant can add notes, tasks and docs itself: the tools come with any request about them (questions and
small local models too), new ones run without asking, replacing a note's text asks."""
import unittest
from unittest import mock

import core.state as shared
import permissions
import skills
from tools import schemas


class NotesOfferTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: shared.app_state.get(k) for k in ("current_user_request", "turn_policy", "turn_tools", "raw_mode", "design_mode")}
        shared.app_state["raw_mode"] = False
        shared.app_state["design_mode"] = False
        shared.app_state["turn_tools"] = []

    def tearDown(self):
        shared.app_state.update(self._saved)

    def _offered(self, request, mode, local=True, window=8192):
        shared.app_state["current_user_request"] = request
        shared.app_state["turn_policy"] = {"mode": mode}
        with mock.patch.object(schemas, "_local_context_window", return_value=window):
            return {t["function"]["name"] for t in schemas.tools_for_mode(local, mode)}

    def test_words_pick_notes(self):
        for msg in ("add a task to call Dana on Friday", "write this down", "jot that", "put the plan in a doc",
                    "add milk to my to-do list"):
            self.assertIn("notes", skills.pick(msg), msg)
        self.assertNotIn("notes", skills.pick("what time is it"))

    def test_offered_on_every_kind_of_turn(self):
        for mode in ("action", "read", "none"):
            got = self._offered("write my plan down as a doc", mode)
            self.assertTrue({"doc_write", "note_save", "task_add"} <= got, mode)

    def test_not_offered_when_unrelated(self):
        self.assertFalse({"doc_write", "note_save", "task_add"} & self._offered("what is the capital of France", "none"))

    def test_allowed_on_a_question_turn(self):
        shared.app_state["current_user_request"] = "add a task to call Dana"
        shared.app_state["turn_policy"] = {"mode": "none"}
        self.assertTrue(schemas.tool_allowed_current_turn("task_add"))
        shared.app_state["current_user_request"] = "hello there"
        self.assertFalse(schemas.tool_allowed_current_turn("task_add"))

    def test_new_ones_run_replacing_a_note_asks(self):
        s = {}
        for tool in ("task_add", "task_done", "doc_write"):
            self.assertEqual(permissions.decide(tool, s, args={}), "auto", tool)
        self.assertEqual(permissions.decide("note_save", s, args={"body": "x"}), "auto")
        self.assertEqual(permissions.decide("note_save", s, args={"body": "x", "note_id": "n1"}), "ask")
        self.assertEqual(permissions.decide("doc_write", s, tainted=True, args={}), "ask")


if __name__ == "__main__":
    unittest.main()

"""
test_messages.py
================
Message actions (agent/sessions.edit_messages, POST /api/chats/{id}/messages):
pin, delete, truncate (Edit & resend, Retry) and branch - on both what the chat
shows (the transcript) and what the model remembers (the conversation).
"""
from __future__ import annotations

import os
import tempfile
import unittest

import main  # noqa: F401  (wires the app together)
import chat_store

import agent.sessions as sessions
import core.state as shared

U = lambda t: {"role": "user", "content": t}
A = lambda t: {"role": "assistant", "content": t}


def _chat():
    """Three turns; the second used a tool and got a host nudge (not a turn)."""
    transcript = [{"r": "u", "t": "one"}, {"r": "a", "t": "reply one"},
                  {"r": "u", "t": "two"}, {"r": "a", "t": "reply two"},
                  {"r": "u", "t": "three"}, {"r": "e", "t": "It failed."}]
    conversation = [U("one"), A("reply one"),
                    U("two"), {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
                    {"role": "tool", "tool_call_id": "c1", "content": "ok"}, U("[SYSTEM] Make the call now."), A("reply two"),
                    U("three")]
    return transcript, conversation


class EditMessagesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = (shared.chat_store, shared.app_state.get("active_chat"), list(shared.app_state["conversation"]))
        shared.chat_store = chat_store.ChatStore(os.path.join(self.dir.name, "chats"))
        transcript, conversation = _chat()
        self.chat = shared.chat_store.save({"id": shared.chat_store.new_id(), "title": "Test", "project": None,
                                            "transcript": transcript, "conversation": conversation})
        shared.app_state["active_chat"] = {k: self.chat[k] for k in ("id", "title", "project", "created", "updated", "transcript")}
        shared.app_state["conversation"] = [{"role": "system", "content": "sys"}] + conversation
        self.session_path = sessions.SESSION_PATH
        sessions.SESSION_PATH = os.path.join(self.dir.name, "session.json")

    def tearDown(self):
        shared.chat_store, shared.app_state["active_chat"], shared.app_state["conversation"][:] = (
            self.saved[0], self.saved[1], self.saved[2])
        sessions.SESSION_PATH = self.session_path
        self.dir.cleanup()

    def edit(self, op, index, **kw):
        return sessions.edit_messages(self.chat["id"], op, index, **kw)

    def model_sees(self):
        return [m.get("content") for m in shared.app_state["conversation"][1:]]

    def test_the_turn_map_skips_host_notes(self):
        transcript, conversation = _chat()
        self.assertEqual([sessions.conv_index(conversation, transcript, i) for i in (0, 2, 3, 4)], [0, 2, 2, 7])
        self.assertEqual(sessions.conv_index(conversation, transcript, 2, after=True), 7)

    def test_pin(self):
        saved = self.edit("pin", 3, pinned=True)
        self.assertTrue(saved["transcript"][3]["pin"])
        self.assertTrue(shared.chat_store.load(self.chat["id"])["transcript"][3]["pin"])
        self.assertNotIn("pin", self.edit("pin", 3, pinned=False)["transcript"][3])

    def test_truncate_for_edit_and_retry(self):
        saved = self.edit("truncate", 2)
        self.assertEqual([e["t"] for e in saved["transcript"]], ["one", "reply one"])
        self.assertEqual(self.model_sees(), ["one", "reply one"])
        self.assertEqual(shared.app_state["conversation"][0]["content"], "sys")          # the system prompt stays
        with self.assertRaises(ValueError):
            self.edit("truncate", 1)                                                    # only a message of yours

    def test_deleting_your_message_takes_its_reply(self):
        saved = self.edit("delete", 2)
        self.assertEqual([e["t"] for e in saved["transcript"]], ["one", "reply one", "three", "It failed."])
        self.assertEqual(self.model_sees(), ["one", "reply one", "three"])

    def test_deleting_a_reply_keeps_the_conversation_well_formed(self):
        saved = self.edit("delete", 1)
        self.assertEqual([e["t"] for e in saved["transcript"]], ["one", "two", "reply two", "three", "It failed."])
        self.assertEqual(self.model_sees()[:2], ["one", sessions.DELETED_REPLY])

    def test_branch_opens_a_new_chat_with_the_exchange(self):
        called = []
        new = self.edit("branch", 2, before_open=lambda: called.append(1))
        self.assertNotEqual(new["id"], self.chat["id"])
        self.assertEqual([e["t"] for e in new["transcript"]], ["one", "reply one", "two", "reply two"])
        self.assertEqual(new["conversation"][-1]["content"], "reply two")
        self.assertEqual(called, [1])
        self.assertEqual(shared.app_state["active_chat"]["id"], new["id"])
        self.assertEqual(len(shared.chat_store.load(self.chat["id"])["transcript"]), 6)   # the original is unchanged

    def test_a_closed_chat_is_edited_on_disk_only(self):
        shared.app_state["active_chat"] = None
        before = list(shared.app_state["conversation"])
        self.edit("delete", 4)
        self.assertEqual(shared.app_state["conversation"], before)
        self.assertEqual([m["content"] for m in shared.chat_store.load(self.chat["id"])["conversation"]][-1], "reply two")

    def test_bad_index(self):
        with self.assertRaises(ValueError):
            self.edit("pin", 99)
        with self.assertRaises(KeyError):
            sessions.edit_messages("0123456789ab", "pin", 0)


if __name__ == "__main__":
    unittest.main()

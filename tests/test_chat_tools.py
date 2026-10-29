"""1.4.2 Phase 1: the context meter's breakdown, and tool groups switched off per chat (hidden AND refused)."""
import os
import tempfile
import unittest
from unittest.mock import patch

import core.state as shared
import tools.schemas as schemas


class ToolGroupTests(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(schemas.tool_group("write_file"), "files")
        self.assertEqual(schemas.tool_group("mcp__roblox_studio__run_code"), "mcp:roblox_studio")
        self.assertEqual(schemas.tool_group("ask_user"), "core")

    def test_a_switched_off_group_is_neither_offered_nor_allowed(self):
        state = {"active_chat": {"id": "c1", "tools_off": ["files"]}, "turn_policy": {"mode": "action"},
                 "current_user_request": "save this to a file in my documents"}
        with patch.dict(shared.app_state, state):
            names = {t["function"]["name"] for t in schemas.tools_for_mode(False, "action")}
            self.assertFalse(names & {"write_file", "read_file", "list_dir"})
            self.assertFalse(schemas.tool_allowed_current_turn("write_file"))     # a remembered name won't do
            self.assertTrue(schemas.tool_allowed_current_turn("ask_user"))

    def test_core_cannot_be_switched_off(self):
        with patch.dict(shared.app_state, {"active_chat": {"id": "c1", "tools_off": ["core"]}, "turn_policy": {"mode": "action"}}):
            self.assertNotIn("core", schemas.chat_tools_off())
            self.assertTrue(schemas.tool_allowed_current_turn("ask_user"))


class BreakdownTests(unittest.TestCase):
    def test_parts_and_tool_groups_are_listed_with_switched_off_ones(self):
        import agent.context_window as cw
        with patch.dict(shared.app_state, {"active_chat": {"id": "c1", "tools_off": ["web"]}, "turn_policy": {"mode": "action"},
                                           "current_user_request": "open notepad", "context_summary": ""}):
            b = cw.breakdown(None, False)
        labels = [p["label"] for p in b["parts"]]
        self.assertIn("Instructions (system message)", labels)
        groups = {t["group"]: t for t in b["tools"]}
        self.assertIn("core", groups)
        self.assertFalse(groups["web"]["on"])                   # off and listed, so it can be switched back on
        self.assertFalse(groups["core"]["switchable"])
        self.assertEqual(b["tools_tokens"], sum(t["tokens"] for t in b["tools"] if t["on"]))


class SaveTests(unittest.TestCase):
    def test_switches_are_saved_with_the_chat_and_survive_a_normal_save(self):
        import chat_store
        store = chat_store.ChatStore(tempfile.mkdtemp())
        chat = store.save({"id": store.new_id(), "title": "t", "transcript": [], "conversation": []})
        store.set_tools_off(chat["id"], ["web", "files", "web"])
        store.save({"id": chat["id"], "title": "t", "transcript": [{"r": "u", "t": "hi"}], "conversation": []})
        self.assertEqual(store.load(chat["id"])["tools_off"], ["files", "web"])

    def test_the_endpoint_refuses_core_and_needs_a_chat(self):
        import api.server as server
        self.assertFalse(server.set_chat_tools(server.ChatToolsBody(group="core", on=False))["ok"])
        with patch.dict(shared.app_state, {"active_chat": None}):
            self.assertFalse(server.set_chat_tools(server.ChatToolsBody(group="web", on=False))["ok"])


if __name__ == "__main__":
    unittest.main()

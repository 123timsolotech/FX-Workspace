"""skills.py: an action turn is shown the core plus the skills its request needs; the model loads more with
load_skill; a real tool of an unloaded skill still runs and loads its skill; the setting turns it all off."""
import unittest
from unittest.mock import patch

import core.state as shared
import llama_local
import roblox_bridge
import skills
import tools.schemas as schemas


class SkillTests(unittest.TestCase):
    def setUp(self):
        keys = ("turn_policy", "current_user_request", "chat_skills", "turn_skills", "turn_tools")
        saved = {k: shared.app_state.get(k) for k in keys}
        self.addCleanup(lambda: shared.app_state.update(saved))
        settings = patch.dict(shared.app_state["settings"], {"skills_on": True})
        settings.start()
        self.addCleanup(settings.stop)
        for p in (patch.object(roblox_bridge, "is_connected", return_value=False),
                  patch.object(llama_local, "model_context_limit", return_value=65536)):
            p.start()
            self.addCleanup(p.stop)
        skills.reset_chat()
        shared.app_state.update(turn_policy={"mode": "action"}, turn_tools=set())

    def shown(self, request, local=True):
        shared.app_state["current_user_request"] = request
        return {t["function"]["name"] for t in schemas.tools_for_current_turn(local=local)}

    def test_requests_pick_their_skills_in_three_languages(self):
        self.assertEqual(skills.pick("open notepad and type hello"), ["desktop"])
        self.assertIn("files", skills.pick("zip the reports folder"))
        self.assertIn("shell", skills.pick("run this in powershell"))
        self.assertIn("roblox", skills.pick("stack 10 parts in Studio"))
        self.assertNotIn("roblox", skills.pick("I'm planning a party"))          # "part" only as a word
        self.assertIn("desktop", skills.pick("תפתח את הדפדפן"))
        self.assertIn("files", skills.pick("удали этот файл"))
        self.assertIn("reminders", skills.pick("remind me in 10 minutes"))

    def test_an_action_turn_is_shown_a_fraction_of_the_catalogue(self):
        names = self.shown("open notepad and type hello")
        for tool in ("open_app_and_list", "type_text", "ask_user", "set_plan", "load_skill"):
            self.assertIn(tool, names)
        for tool in ("zip_paths", "roblox_batch", "schedule_task", "service_control"):
            self.assertNotIn(tool, names)
        full = {t["function"]["name"] for t in schemas.TOOLS}
        self.assertLess(len(names), len(full) / 2)

    def test_nothing_matched_gets_desktop_and_files(self):
        names = self.shown("do the thing we talked about")
        self.assertIn("open_application", names)
        self.assertIn("write_file", names)

    def test_load_skill_adds_tools_from_the_next_step_and_they_stay_for_the_chat(self):
        self.assertNotIn("schedule_task", self.shown("open notepad"))
        res = skills.load_skill_tool({"skills": ["reminders"]})
        self.assertTrue(res["ok"])
        self.assertIn("schedule_task", res["message"])
        self.assertIn("schedule_task", self.shown("open notepad"))
        self.assertIn("schedule_task", self.shown("now close it"))               # a later turn of the same chat
        skills.reset_chat()                                                       # another chat
        self.assertNotIn("schedule_task", self.shown("now close it"))
        self.assertFalse(skills.load_skill_tool({"skills": ["nope"]})["ok"])

    def test_a_real_tool_of_an_unloaded_skill_still_runs_and_loads_it(self):
        self.shown("open notepad")
        self.assertTrue(schemas.tool_allowed_current_turn("zip_paths"))          # authority is the mode's
        self.assertEqual(skills.note_used("zip_paths"), "files")
        self.assertIn("zip_paths", self.shown("open notepad"))

    def test_the_index_names_only_what_is_not_loaded(self):
        line = skills.index(self.shown("open notepad"))
        self.assertIn("files -", line)
        self.assertIn("roblox -", line)
        self.assertNotIn("desktop -", line)
        self.assertEqual(skills.index({"open_application"}), "")                 # no load_skill offered: no index

    def test_off_means_every_tool_and_no_load_skill(self):
        with patch.dict(shared.app_state["settings"], {"skills_on": False}):
            names = self.shown("open notepad")
        self.assertIn("zip_paths", names)
        self.assertNotIn("load_skill", names)

    def test_questions_never_see_load_skill(self):
        shared.app_state["turn_policy"] = {"mode": "read"}
        self.assertNotIn("load_skill", self.shown("what's in my downloads folder?"))
        shared.app_state["turn_policy"] = {"mode": "none"}
        self.assertNotIn("load_skill", self.shown("hi"))

    # D50: the start context - skills fade, Studio's tools are a default not an extra, MCP tools come with a skill.
    def test_skills_fade_when_nothing_wants_them(self):
        self.assertIn("web_search", self.shown("search the web for llama.cpp news"))
        for i in range(skills.FADE_AFTER - 1):
            self.assertIn("web_search", self.shown(f"open notepad {i}"))         # still this chat's
        self.assertNotIn("web_search", self.shown("open notepad again"))

    def test_a_tool_in_use_keeps_its_skill(self):
        self.shown("search the web for llama.cpp news")
        for i in range(skills.FADE_AFTER + 2):
            self.shown(f"open notepad {i}")
            skills.note_used("web_search")
        self.assertIn("web_search", self.shown("open notepad once more"))

    def test_studio_connected_is_the_default_not_an_extra(self):
        with patch.object(roblox_bridge, "is_connected", return_value=True):
            self.assertIn("roblox_batch", self.shown("make the door open when touched"))
            skills.reset_chat()
            self.assertIn("roblox_batch", self.shown("do the thing we talked about"))
            skills.reset_chat()
            self.assertNotIn("roblox_batch", self.shown("open notepad and type hello"))

    def test_mcp_tools_come_with_their_skill(self):
        schema = lambda n: {"type": "function", "function": {"name": n, "description": "d", "parameters": {}}}
        class Hub:
            def tool_schemas(self):
                return [schema("mcp__roblox_studio__execute_luau"), schema("mcp__notion__search_pages")]
        hub = Hub()
        with patch.object(shared, "mcp_hub", hub), patch.object(schemas, "mcp_schemas", hub.tool_schemas):
            names = self.shown("open notepad")
            self.assertFalse({"mcp__roblox_studio__execute_luau", "mcp__notion__search_pages"} & names)
            self.assertIn("mcp - the tools of your connected MCP servers (notion)", skills.index(names))
            self.assertIn("studio - Roblox Studio's own MCP tools", skills.index(names))
            skills.reset_chat()
            self.assertIn("mcp__roblox_studio__execute_luau", self.shown("stack 10 parts in Studio"))
            skills.reset_chat()
            self.assertIn("mcp__notion__search_pages", self.shown("find my meeting notes in notion"))
            skills.reset_chat()
            self.shown("open notepad")
            self.assertEqual(skills.note_used("mcp__notion__search_pages"), "mcp")      # called by name: loaded
            self.assertIn("mcp__notion__search_pages", self.shown("open notepad"))

    def test_studio_open_brings_the_bridge_but_its_mcp_only_for_studio_words(self):
        schema = lambda n: {"type": "function", "function": {"name": n, "description": "d", "parameters": {}}}
        mcp = [schema("mcp__roblox_studio__execute_luau")]
        with patch.object(schemas, "mcp_schemas", lambda: mcp), patch.object(roblox_bridge, "is_connected", return_value=True):
            names = self.shown("what time is it in Tokyo")
            self.assertIn("roblox_status", names)                                   # nothing matched: Studio work
            self.assertNotIn("mcp__roblox_studio__execute_luau", names)
            skills.reset_chat()
            self.assertIn("mcp__roblox_studio__execute_luau", self.shown("make the door open when touched"))

    def test_the_meter_does_not_measure_against_a_model_that_is_not_loaded(self):
        from agent import context_window
        st = context_window.status(None, True, known=False)
        self.assertTrue(st["unknown_model"])
        self.assertIsNone(st["window"])
        self.assertLess(st["percent"], 60)

    def test_every_skill_tool_is_a_real_tool(self):
        real = {t["function"]["name"] for t in schemas.TOOLS}
        self.assertEqual(set(skills.SKILL_OF) - real, set())
        self.assertIn("load_skill", schemas.READ_ONLY_TOOLS)                    # loading never needs an approval


if __name__ == "__main__":
    unittest.main()

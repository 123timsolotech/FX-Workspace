"""The current-turn authority boundary (intent_router.classify_turn), checked
against the phrasings that left the model without tools in real chats
(2026-09-24: "in Test1 make a simple inventory GUI", "New experience",
"Up to you"), and the router's "open <place> in studio"."""
import json
import unittest

import intent_router as ir
import replay_harness as rh


class ActionPhrasingTests(unittest.TestCase):
    def test_requests_that_were_refused_now_get_tools(self):
        for text in ("in Test1 make a simple inventory GUI", "in place Test1 make a simple inventory UI",
                     "New experience", "for my obby, add a kill brick", "on the desktop create a folder called x",
                     "let's add a leaderboard", "I'd like to fix the door script", "ok, make it red",
                     "go ahead and publish it", "in Test1 can you make a door?", "use roblox tools to add a part",
                     "Create a new experience", "Hey, open Notepad", "Can you open Proton VPN?"):
            self.assertEqual(ir.classify_turn(text)["mode"], "action", text)

    def test_questions_and_remarks_still_get_no_action(self):
        for text in ("How do I open Proton VPN?", "in which file does it run?", "what is bonsai 2 27B?",
                     "I made a script yesterday", "is it done?", "tell me a joke", "hi"):
            self.assertEqual(ir.classify_turn(text)["mode"], "none", text)
        for text in ("What windows are open?", "why does my script error?", "latest news in israel",
                     "the game crashes when I open the menu"):
            self.assertEqual(ir.classify_turn(text)["mode"], "read", text)

    def test_a_short_answer_continues_the_request_the_assistant_asked_about(self):
        prev = {"request": "in Test1 make a simple inventory GUI",
                "assistant": "Should the inventory be a grid or a list?"}
        for reply in ("Up to you", "yes", "go ahead", "you decide", "whatever you think is best", "Sure!"):
            p = ir.classify_turn(reply, previous=prev)
            self.assertEqual((p["mode"], p["follows"]), ("action", prev["request"]), reply)
        # No question or offer from the assistant -> a bare "ok" grants nothing.
        done = {"request": "delete x.txt", "assistant": "Done - deleted x.txt."}
        self.assertEqual(ir.classify_turn("ok", previous=done)["mode"], "none")
        # A new instruction is classified on its own.
        self.assertEqual(ir.classify_turn("what time is it?", previous=prev)["mode"], "none")


class OpenInStudioTests(unittest.TestCase):
    def test_open_place_in_studio_targets_the_place(self):
        for text in ("Open Test1 in studio", "open Test1 in roblox studio", "open Test1 with Roblox Studio"):
            routed = ir.route(text)
            self.assertIsInstance(routed, ir.DesktopIntent, text)
            self.assertEqual(routed.app_name, "Test1", text)
            self.assertTrue(routed.arguments.get("place_only"), text)
        self.assertEqual(ir.route("open roblox studio").app_name, "roblox studio")

    def test_a_missing_place_is_explained_not_confused_with_apps(self):
        import desktop_router
        from unittest.mock import patch
        with patch.object(desktop_router.di, "IS_WINDOWS", True), \
             patch.object(desktop_router.roblox_places, "resolve_place", return_value=(None, [])), \
             patch.object(desktop_router.app_registry, "resolve_app") as apps:
            out = desktop_router._open_place("Test1")
        apps.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertIn("no saved place file named 'Test1'", out["message"])
        self.assertIn("start page", out["message"])


class ReplayTests(unittest.TestCase):
    def test_in_place_request_reaches_tools_and_approval(self):
        fx = {"format": "agentic-replay/1",
              "meta": {"name": "scope-first request", "provenance": "reconstructed", "model": "unrecorded",
                       "evidence": "2026-09-24 chat: 'in Test1 make a simple inventory GUI' got no tools"},
              "setup": {"mode": "local", "entry": "agent", "settings": {"effort": "low", "prompt_prep": "off"}},
              "request": "in Test1 make a simple inventory GUI",
              "responses": [
                  {"finish_reason": "tool_calls", "tool_calls": [{"id": "c1", "name": "write_file", "arguments": json.dumps(
                      {"filename": "C:/tmp/inventory.lua", "content": "-- inventory", "justification": "Inventory script"})}]},
                  {"finish_reason": "stop", "content": "Stopped."}],
              "approvals": [False], "tools": {"mode": "stub"}}
        run = rh.run_fixture(fx)
        offered = {t if isinstance(t, str) else t["function"]["name"] for r in run.requests for t in (r.get("tools") or [])}
        self.assertIn("write_file", offered)                      # tools were offered at all
        self.assertEqual([a[0]["tool_name"] for a in run.approvals], ["write_file"])   # and the call reached the card


if __name__ == "__main__":
    unittest.main()

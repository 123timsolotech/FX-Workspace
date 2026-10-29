"""Slash commands and saved thinking.

/plan: the model writes a plan and nothing runs until the user approves it
(an edited plan is what runs; Reject runs nothing), for any request.
/design: a visual-design turn - the design system prompt, no computer tools,
no live machine state, room for a whole HTML document, and no prep, planning
or review. Thinking: a reply's reasoning and how long the model thought are
saved with it (transcript k / kt) and returned with the turn."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import orchestrator
from run_control import RunControl
import agent.models as llm
import agent.prompts as prompts
import agent.sessions as sessions
import agent.tasks as tasks
import chat_store
import core.config as config
import core.state as shared
from test_orchestration import FakeModels


class SlashCommandTests(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(tasks.slash_command("/plan Build a lobby"), ("plan", "Build a lobby"))
        self.assertEqual(tasks.slash_command("  /DESIGN   a login card"), ("design", "a login card"))
        self.assertEqual(tasks.slash_command("/plan\nstep one please"), ("plan", "step one please"))
        self.assertEqual(tasks.slash_command("/plan"), ("plan", ""))
        for text in ("/planet facts", "/plan-b now", "make a /plan", "plan something", "/designer tips", ""):
            self.assertEqual(tasks.slash_command(text), (None, text), text)


class _Gate:
    def __init__(self, answer):
        self.answer, self.cards = answer, []

    def request(self, kind, payload, cancelled=None):
        self.cards.append((kind, payload))
        return self.answer


class OrchestratorCommandTests(unittest.TestCase):
    def _agent(self, gate, executed, models, prep="off"):
        return orchestrator.AgentOrchestrator(
            execute=lambda req: (executed.append(req.message), {"status": "success", "message": "done"})[1],
            revise=lambda s, f: None, settings=lambda: {"effort": "high", "prompt_prep": prep},
            run_control=RunControl(), emit=lambda *a, **k: None,
            analyze_request=lambda text, v: orchestrator.analyze(text, "none", 1),
            preparer=orchestrator.PromptPreparer(models), planner=orchestrator.Planner(models),
            reviewer=orchestrator.Reviewer(models, None), tool_scope=lambda *a: None, tool_history=lambda: [],
            set_context=lambda state, profile: None, stopped_response=lambda reason: {"status": "stopped", "message": "Stopped."},
            prep_gate=gate)

    def test_plan_stops_at_a_model_plan_even_for_a_short_request_with_prep_off(self):
        executed, models, gate = [], FakeModels(), _Gate({"action": "cancel", "text": ""})
        res = self._agent(gate, executed, models).run(SimpleNamespace(message="tidy my desktop", with_vision=False, command="plan"))
        self.assertEqual([k for k, _p in gate.cards], ["plan"])
        payload = gate.cards[0][1]
        self.assertEqual(payload["command"], "plan")
        self.assertEqual(payload["text"], "Open Notepad\nType text")         # the model wrote it
        self.assertIn("plan", models.calls)
        self.assertEqual(executed, [])                                       # Reject: nothing ran
        self.assertEqual(res["message"], "Plan rejected - nothing was run.")
        self.assertTrue(res["prep_cancelled"])

    def test_the_approved_edited_plan_is_what_runs(self):
        executed, captured = [], {}
        gate = _Gate({"action": "send", "text": "1. Open Notepad\n2. Type hello\n3. Save it"})
        agent = self._agent(gate, executed, FakeModels())
        agent.set_context = lambda state, profile: captured.setdefault("state", state) if state else None
        res = agent.run(SimpleNamespace(message="tidy my desktop", with_vision=False, command="plan"))
        self.assertEqual(executed, ["tidy my desktop"])
        st = captured["state"]
        self.assertTrue(st.plan_accepted)
        self.assertEqual(st.plan["steps"], ["Open Notepad", "Type hello", "Save it"])
        self.assertIn("ACCEPTED PLAN", st.brief())
        self.assertEqual(res["status"], "success")

    def test_design_is_not_prepared_planned_or_reviewed(self):
        executed, models, gate = [], FakeModels(), _Gate({"action": "send", "text": "x"})
        res = self._agent(gate, executed, models, prep="plan").run(
            SimpleNamespace(message="design a settings page with sections, toggles and a save bar for my app", with_vision=False,
                            command="design"))
        self.assertEqual(gate.cards, [])
        self.assertEqual(models.calls, [])                                   # no prepare / plan / review call
        self.assertEqual(executed, ["design a settings page with sections, toggles and a save bar for my app"])
        self.assertEqual(res["status"], "success")


class DesignTurnTests(unittest.TestCase):
    def setUp(self):
        keys = ("conversation", "mode", "turn_policy", "current_user_request", "task_context", "context_summary", "design_mode",
                "effort_thinking", "run_budget")
        saved = {k: shared.app_state.get(k) for k in keys}
        self.addCleanup(lambda: [shared.app_state.pop(k, None) if v is None else shared.app_state.__setitem__(k, v)
                                 for k, v in saved.items()])
        shared.app_state.update(conversation=[{"role": "system", "content": "x"}, {"role": "user", "content": "a login card"}],
                                current_user_request="a login card", task_context="", context_summary="",
                                turn_policy={"mode": "none", "allow_mutation": False, "reason": "/design"})
        shared.app_state.pop("effort_thinking", None)
        shared.app_state.pop("run_budget", None)

    def test_design_prompt_replaces_the_computer_prompt_and_drops_machine_state(self):
        shared.app_state["design_mode"] = True
        for mode in (config.MODE_LOCAL, config.MODE_CLOUD):
            shared.app_state["mode"] = mode
            messages = llm.build_messages()
            self.assertTrue(messages[0]["content"].startswith(prompts.DESIGN_PROMPT), mode)
            text = "\n".join(str(m["content"]) for m in messages)
            self.assertNotIn("LIVE MACHINE STATE", text)
            self.assertIn("CURRENT REQUEST: a login card", text)
            self.assertIn("```html", text)
        shared.app_state["design_mode"] = False
        shared.app_state["mode"] = config.MODE_LOCAL
        self.assertFalse(llm.build_messages()[0]["content"].startswith(prompts.DESIGN_PROMPT))

    def test_room_for_a_whole_document(self):
        shared.app_state.pop("design_mode", None)
        plain = llm._local_reply_tokens()
        shared.app_state["design_mode"] = True
        self.assertGreaterEqual(llm._local_reply_tokens(), llm.DESIGN_REPLY_TOKENS)
        self.assertGreater(llm._local_reply_tokens(), plain)

    def test_design_turn_offers_no_tools_and_clears_the_flag(self):
        seen = {}

        def loop():
            seen["policy"] = dict(shared.app_state["turn_policy"])
            seen["design"] = shared.app_state.get("design_mode")
            return {"status": "success", "message": "```html\n<p>hi</p>\n```"}
        with patch.object(tasks.agent_loop, "run_agent_loop", side_effect=loop):
            res = tasks._design_turn(SimpleNamespace(message="a login card"))
        self.assertEqual(seen["policy"]["mode"], "none")
        self.assertTrue(seen["design"])
        self.assertNotIn("design_mode", shared.app_state)
        self.assertEqual(res["status"], "success")
        self.assertEqual(shared.app_state["conversation"][-1], {"role": "user", "content": "a login card"})


class ThinkingSavedTests(unittest.TestCase):
    EVENTS = [{"kind": "thought", "reasoning": "short", "reasoning_full": "First I check the brick.", "thinking_s": 2.4},
              {"kind": "tool", "label": "Read script"},
              {"kind": "thought", "note": "Reading it", "reasoning": "", "thinking_s": 9},       # a note, not a thought
              {"kind": "thought", "reasoning": "Then I add a debounce.", "thinking_s": 1.2}]

    def test_thinking_of_joins_the_reasoning_and_its_time(self):
        self.assertEqual(sessions.thinking_of(self.EVENTS),
                         {"text": "First I check the brick.\n\nThen I add a debounce.", "seconds": 3.6})
        self.assertIsNone(sessions.thinking_of([{"kind": "thought", "note": "hi"}, {"kind": "tool"}]))
        long = sessions.thinking_of([{"kind": "thought", "reasoning": "x" * (sessions.THINKING_KEEP + 50), "thinking_s": 1}])
        self.assertTrue(long["text"].startswith("…"))
        self.assertLessEqual(len(long["text"]), sessions.THINKING_KEEP + 1)

    def test_record_turn_saves_thinking_and_the_command(self):
        saved = {k: shared.app_state.get(k) for k in ("active_chat", "active_project", "conversation")}
        self.addCleanup(shared.app_state.update, saved)
        with tempfile.TemporaryDirectory() as tmp, patch.object(shared, "chat_store", chat_store.ChatStore(tmp)), \
             patch.object(sessions, "_save_session"), patch.object(sessions.event_log, "events_since", return_value=self.EVENTS):
            shared.app_state.update(active_chat=None, active_project=None)
            got = sessions.record_turn(SimpleNamespace(message="Fix the brick", with_vision=False, command="plan"),
                                       {"status": "success", "message": "Fixed."}, start_seq=0)
            user, reply = shared.app_state["active_chat"]["transcript"]
            reopened = shared.chat_store.load(shared.app_state["active_chat"]["id"])["transcript"][1]
        self.assertEqual(got["seconds"], 3.6)
        self.assertEqual(user["c"], "plan")
        self.assertEqual((reply["k"], reply["kt"]), (got["text"], 3.6))
        self.assertEqual(reopened["k"], got["text"])                          # survives a reload

    def test_a_reply_without_reasoning_saves_none(self):
        saved = {k: shared.app_state.get(k) for k in ("active_chat", "active_project", "conversation")}
        self.addCleanup(shared.app_state.update, saved)
        with tempfile.TemporaryDirectory() as tmp, patch.object(shared, "chat_store", chat_store.ChatStore(tmp)), \
             patch.object(sessions, "_save_session"), patch.object(sessions.event_log, "events_since", return_value=[]):
            shared.app_state.update(active_chat=None, active_project=None)
            self.assertIsNone(sessions.record_turn(SimpleNamespace(message="hi", with_vision=False), {"status": "success", "message": "Hi!"}, 0))
            user, reply = shared.app_state["active_chat"]["transcript"]
        self.assertNotIn("c", user)
        self.assertNotIn("k", reply)


class ThinkingTimeTests(unittest.TestCase):
    """_local_complete: thinking ends at the first answer text or tool call;
    reasoning printed inline (<think>...</think>) is still thinking."""

    def _first(self, chunks):
        timing = {"start": 0.0, "first": None, "content": "", "closed": None}
        for i, text in enumerate(chunks):
            timing["content"] += text
            llm._mark_answer_start(timing)
            if timing["first"] is not None:
                return i
        return None

    def test_answer_start(self):
        self.assertEqual(self._first(["Hello", " there"]), 0)
        self.assertEqual(self._first(["<think>", "hmm, let me see", "</think>", "\n", "Answer"]), 4)
        self.assertEqual(self._first(["<thi", "nk>plan", "</think>Yes"]), 2)
        self.assertEqual(self._first(["  ", "\n", "Ok"]), 2)
        self.assertIsNone(self._first(["<think>still thinking"]))


class ChatApiCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import main
        cls.client = TestClient(main.app, base_url="http://127.0.0.1:8000")

    def test_the_command_is_split_from_the_message(self):
        with patch.object(tasks.turns, "start", return_value={"id": "t1", "status": "running"}) as start:
            r = self.client.post("/api/chat", json={"message": "/plan set up my Roblox session"}).json()
        self.assertEqual(r["status"], "accepted")
        req, meta = start.call_args.args
        self.assertEqual((req.message, req.command), ("set up my Roblox session", "plan"))
        self.assertEqual(meta["command"], "plan")

    def test_a_command_needs_a_request_and_is_never_taken_from_the_body(self):
        with patch.object(tasks.turns, "start") as start:
            r = self.client.post("/api/chat", json={"message": "/design"}).json()
            self.assertEqual(r["status"], "error")
            self.assertIn("after /design", r["message"])
            start.return_value = {"id": "t2", "status": "running"}
            self.client.post("/api/chat", json={"message": "hello", "command": "design"})
        self.assertIsNone(start.call_args.args[0].command)


if __name__ == "__main__":
    unittest.main()

"""Prompt Prep (prompt_prep.py): modes, persistence and migration, the card
gate, and the guarantees - Plan first runs nothing before acceptance, the
prepared text is sent exactly as shown, Clarify asks at most one question,
and accepting a card never approves an action."""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import orchestrator
import prompt_prep
import replay_harness as rh
from run_control import RunControl
import core.config as config


class ModeTests(unittest.TestCase):
    def test_choice_order_is_session_project_model_setting(self):
        s = {"prompt_prep": "clarify", "prompt_prep_by_model": {"m1": "expand"}}
        self.assertEqual(prompt_prep.choose_mode(s, "m1", {"prompt_prep": "plan"}, "off"), ("off", "session"))
        self.assertEqual(prompt_prep.choose_mode(s, "m1", {"prompt_prep": "plan"}), ("plan", "project"))
        self.assertEqual(prompt_prep.choose_mode(s, "m1"), ("expand", "model"))
        self.assertEqual(prompt_prep.choose_mode(s, "m2"), ("clarify", "setting"))
        self.assertEqual(prompt_prep.choose_mode({}, None, None, "nonsense"), ("clarify", "setting"))

    def test_existing_settings_migrate(self):
        for old, new in (("off", "off"), ("minimal", "clarify"), ("standard", "clarify"), ("detailed", "clarify")):
            cleaned = config.clean_agent_settings({"prompt_enhancement": old})
            self.assertEqual(cleaned["prompt_prep"], new, old)
            self.assertEqual(cleaned["prompt_enhancement"], old)          # kept as it was
        self.assertEqual(config.clean_agent_settings({"prompt_prep": "plan", "prompt_enhancement": "off"})["prompt_prep"], "plan")
        self.assertEqual(config.clean_agent_settings({"prompt_prep_by_model": {"m": "bad", "n": "expand"}})["prompt_prep_by_model"],
                         {"n": "expand"})

    def test_spec_and_steps_text(self):
        text = prompt_prep.spec_text({"intent": "Make a kill brick", "constraints": ["only in Workspace"],
                                      "success_criteria": ["touching it resets the player"], "ambiguities": []})
        self.assertEqual(text, "Goal: Make a kill brick\nConstraints:\n- only in Workspace\nDone when:\n- touching it resets the player")
        self.assertEqual(prompt_prep.steps_from_text("1. Open Studio\n- Add a part\n\n  3) Test it  \n• Save"),
                         ["Open Studio", "Add a part", "Test it", "Save"])


class GateTests(unittest.TestCase):
    def test_request_waits_for_the_answer(self):
        gate = prompt_prep.PrepGate()
        got = {}
        t = threading.Thread(target=lambda: got.update(r=gate.request("spec", {"text": "Goal: x"})))
        t.start()
        for _ in range(100):
            if gate.pending():
                break
            time.sleep(0.01)
        card = gate.pending()
        self.assertEqual((card["kind"], card["text"]), ("spec", "Goal: x"))
        self.assertNotIn("event", card)
        self.assertFalse(gate.answer("wrong-id", "send", "y"))
        self.assertFalse(gate.answer(card["id"], "approve-everything", "y"))
        self.assertTrue(gate.answer(card["id"], "send", "Goal: y"))
        t.join(2)
        self.assertEqual(got["r"], {"action": "send", "text": "Goal: y"})
        self.assertIsNone(gate.pending())

    def test_stop_ends_the_wait(self):
        gate = prompt_prep.PrepGate()
        stop = threading.Event()
        threading.Timer(0.05, stop.set).start()
        self.assertIsNone(gate.request("plan", {"text": "1. a"}, cancelled=stop.is_set, poll=0.01))
        self.assertIsNone(gate.pending())


class _Gate:
    def __init__(self, answer=None, on_request=None):
        self.answer, self.on_request, self.cards = answer, on_request, []

    def request(self, kind, payload, cancelled=None):
        self.cards.append((kind, payload))
        if self.on_request:
            self.on_request()
        return self.answer


class OrchestratorPrepTests(unittest.TestCase):
    def _agent(self, mode, gate, executed, rc=None):
        from test_orchestration import FakeModels
        models = FakeModels()
        return orchestrator.AgentOrchestrator(
            execute=lambda req: (executed.append(req.message), {"status": "success", "message": "done"})[1],
            revise=lambda s, f: None, settings=lambda: {"effort": "normal", "prompt_prep": mode},
            run_control=rc or RunControl(), emit=lambda *a, **k: None,
            analyze_request=lambda text, v: orchestrator.analyze(text, "action", 1),
            preparer=orchestrator.PromptPreparer(models), planner=orchestrator.Planner(models),
            reviewer=orchestrator.Reviewer(models, None), tool_scope=lambda *a: None, tool_history=lambda: [],
            set_context=lambda state, profile: None, stopped_response=lambda reason: {"status": "stopped", "message": "Stopped."},
            prep_gate=gate)

    def test_plan_first_cancel_runs_nothing(self):
        executed = []
        res = self._agent("plan", _Gate({"action": "cancel", "text": ""}), executed).run(
            SimpleNamespace(message="open notepad and type hello", with_vision=False))
        self.assertEqual(executed, [])                         # the executor never started
        self.assertTrue(res.get("prep_cancelled"))

    def test_stop_while_the_card_is_open_cancels_the_run(self):
        executed, rc = [], RunControl()
        agent = self._agent("expand", _Gate(None, on_request=rc.stop), executed, rc)
        res = agent.run(SimpleNamespace(message="open notepad and type hello", with_vision=False))
        self.assertEqual(executed, [])
        self.assertEqual(res["status"], "stopped")

    def test_the_wait_does_not_use_the_time_limit(self):
        rc = RunControl()
        with patch.object(rc, "set_deadline", wraps=rc.set_deadline) as sd:
            self._agent("expand", _Gate({"action": "send", "text": "Goal: x"}), [], rc).run(
                SimpleNamespace(message="open notepad and type hello", with_vision=False))
        limits = [c.args[0] for c in sd.call_args_list]
        self.assertEqual(limits[0], 24 * 3600)                 # suspended while the card is open
        self.assertLess(limits[1], 24 * 3600)                  # the level's limit, from the answer on


def _fixture(prep, request, responses, prep_answers=(), approvals=(), app_state=None, entry="agent"):
    return {"format": "agentic-replay/1",
            "meta": {"name": "prep", "provenance": "reconstructed", "model": "unrecorded", "evidence": "test"},
            "setup": {"mode": "local", "entry": entry, "turn_policy": "action",
                      "settings": {"effort": "normal", "prompt_prep": prep}, "app_state": app_state or {}},
            "request": request, "responses": responses, "prep_answers": list(prep_answers),
            "approvals": list(approvals), "tools": {"mode": "stub"}}


def _call(cid, name, args):
    return {"finish_reason": "tool_calls", "tool_calls": [{"id": cid, "name": name, "arguments": json.dumps(args)}]}


class PrepReplayTests(unittest.TestCase):
    def test_plan_first_cancel_means_no_model_step_and_no_tool(self):
        run = rh.run_fixture(_fixture("plan", "Create notes.txt on my desktop with my todo list",
                                      [_call("w1", "write_file", {"filename": "notes.txt", "content": "x"})],
                                      prep_answers=[{"action": "cancel"}]))
        self.assertEqual([c["kind"] for c in run.prep_cards], ["plan"])
        self.assertEqual(run.executed, [])
        self.assertFalse([r for r in run.requests if r.get("tools")])     # the executor never asked the model
        self.assertIn("nothing was run", run.message.lower())

    def test_accepted_plan_is_sent_as_edited_and_actions_still_ask(self):
        run = rh.run_fixture(_fixture("plan", "Create notes.txt on my desktop with my todo list",
                                      [_call("w1", "write_file", {"filename": "notes.txt", "content": "x", "justification": "j"}),
                                       {"finish_reason": "stop", "content": "Done."}],
                                      prep_answers=[{"action": "send", "text": "1. Write notes.txt\n2. Tell me where it is"}],
                                      approvals=[False]))
        brief = next(e for e in run.events if e.get("phase") == "brief_sent")["text"]
        self.assertIn("ACCEPTED PLAN", brief)
        self.assertIn("1. Write notes.txt | 2. Tell me where it is", brief)
        sent = json.dumps([r.get("messages") for r in run.requests if r.get("tools")])
        self.assertIn(json.dumps(brief)[1:-1], sent)                       # exactly what Activity shows
        self.assertEqual(len(run.approvals), 1)                            # accepting the plan approved nothing
        self.assertEqual(run.executed, [])

    def test_expand_sends_the_edited_spec_or_nothing_when_run_as_typed(self):
        edited = "Goal: a kill brick in Workspace\nDone when:\n- touching it resets the player"
        run = rh.run_fixture(_fixture("expand", "Make a kill brick", [{"finish_reason": "stop", "content": "Ok."}],
                                      prep_answers=[{"action": "send", "text": edited}]))
        self.assertEqual([c["kind"] for c in run.prep_cards], ["spec"])
        brief = next(e for e in run.events if e.get("phase") == "brief_sent")["text"]
        self.assertIn("PREPARED SPEC (the user reviewed and sent this before the run):\n" + edited, brief)
        raw = rh.run_fixture(_fixture("expand", "Make a kill brick", [{"finish_reason": "stop", "content": "Ok."}],
                                      prep_answers=[{"action": "raw"}]))
        brief = next(e for e in raw.events if e.get("phase") == "brief_sent")["text"]
        self.assertNotIn("PREPARED SPEC", brief)
        self.assertNotIn("Interpreted intent", brief)

    def test_off_sends_only_the_request(self):
        run = rh.run_fixture(_fixture("off", "Make a kill brick", [{"finish_reason": "stop", "content": "Ok."}]))
        self.assertEqual(run.prep_cards, [])
        brief = next(e for e in run.events if e.get("phase") == "brief_sent")["text"]
        self.assertNotIn("Interpreted intent", brief)
        self.assertNotIn("Constraints", brief)

    def test_clarify_allows_one_question(self):
        answered = {"question": "Which file should I edit?", "answer": "notes.txt"}
        run = rh.run_fixture(_fixture(
            "clarify", "Fix the typo", entry="loop",
            app_state={"resolved_clarification": answered, "prep_mode": "clarify"},
            responses=[_call("q2", "ask_user", {"question": "Which line has the typo?", "options": ["1", "2"]}),
                       {"finish_reason": "stop", "content": "I fixed the typo I could find in notes.txt."}]))
        self.assertEqual(run.status, "success")
        self.assertNotEqual(run.result.get("status"), "clarification")
        self.assertTrue(any("allows one question" in str(m.get("content")) for m in run.conversation))


class PreparedViewTests(unittest.TestCase):
    """What the chat shows under a message ("what Prompt Prep changed") is the
    same additions the model received - never a separate description."""

    def test_brief_is_built_from_the_additions(self):
        st = orchestrator.TaskState("Make a kill brick, only in Workspace")
        st.prep_mode = "clarify"
        st.enhanced = {"intent": "Make a kill brick", "constraints": ["only in Workspace"], "ambiguities": []}
        st.success_criteria = ["Done: Make a kill brick"]
        st.plan, st.plan_accepted = {"steps": ["Add a part", "Add a touch script"], "risks": ["Editing your place"]}, True
        adds = st.additions()
        self.assertEqual([a["key"] for a in adds], ["intent", "constraints", "clarify", "plan", "risks", "done"])
        lines = st.brief().split("\n")
        self.assertEqual(lines[0], "ORIGINAL REQUEST (verbatim, authoritative): Make a kill brick, only in Workspace")
        self.assertEqual(lines[1:-1], [a["line"] for a in adds])
        self.assertEqual(adds[3]["items"], ["Add a part", "Add a touch script"])

    def test_off_adds_nothing(self):
        st = orchestrator.TaskState("hi there")
        self.assertEqual(st.additions(), [])
        self.assertEqual(len(st.brief().split("\n")), 2)

    def test_the_run_reports_it_and_the_chat_keeps_it(self):
        import tempfile
        import chat_store
        import agent.sessions as sessions
        import core.state as shared
        edited = "Goal: a kill brick in Workspace"
        run = rh.run_fixture(_fixture("expand", "Make a kill brick", [{"finish_reason": "stop", "content": "Ok."}],
                                      prep_answers=[{"action": "send", "text": edited}]))
        ev = next(e for e in run.events if e.get("phase") == "brief_sent")
        prepared = ev["prepared"]
        self.assertEqual((prepared["mode"], prepared["original"]), ("expand", "Make a kill brick"))
        self.assertEqual(prepared["additions"][0], {"key": "spec", "label": "Spec you sent", "text": edited})
        self.assertNotIn("line", prepared["additions"][0])
        saved = {k: shared.app_state.get(k) for k in ("active_chat", "active_project", "conversation")}
        self.addCleanup(shared.app_state.update, saved)
        with tempfile.TemporaryDirectory() as tmp, patch.object(shared, "chat_store", chat_store.ChatStore(tmp)), \
             patch.object(sessions, "_save_session"), patch.object(sessions.event_log, "events_since", return_value=[ev]):
            shared.app_state.update(active_chat=None, active_project=None)
            sessions.record_turn(SimpleNamespace(message="Make a kill brick", with_vision=False),
                                 {"status": "success", "message": "Ok."}, start_seq=0)
            user = shared.app_state["active_chat"]["transcript"][0]
        self.assertEqual(user["r"], "u")
        self.assertEqual(user["p"]["additions"][0]["text"], edited)


if __name__ == "__main__":
    unittest.main()

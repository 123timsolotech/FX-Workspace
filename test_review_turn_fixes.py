"""Turn routing and history-edit regressions, with model/network boundaries faked.

The real turn dispatcher, orchestrator, revision loop, research planner/report
and chat stores run. All persistent stores are rooted in a temporary folder.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main  # noqa: F401  (wires the app together)
import chat_store
import context
import model_caps
import routines
import run_log
import stream_hub
import usage
import agent.models as llm
import agent.research as research
import agent.sessions as sessions
import agent.tasks as tasks
import core.config as config
import core.state as shared


class TurnFixesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        # Runtime objects can own locks after other suites have exercised a turn.
        # Replaced mutable state below is isolated; patch.dict restores the originals.
        state = dict(shared.app_state)
        state.update(conversation=[{"role": "system", "content": "system"}], events=[], audit_log=[], event_seq=0,
                     active_chat=None, active_project=None, pending_clarification=None, task_queue=None,
                     context_summary="", mode=config.MODE_CLOUD, local_model="old-local", local_engaged="old-vision",
                     last_working_model="cloud-test", effort_session=None, prep_session=None,
                     settings={**config.DEFAULT_SETTINGS, "effort": "high", "prompt_prep": "expand",
                               "memory_enabled": False, "record_model_io": False, "auto_model_routing": False})
        self._patch(patch.dict(shared.app_state, state, clear=True))
        for name, store in {
            "chat_store": chat_store.ChatStore(os.path.join(root, "chats")),
            "usage_store": usage.UsageStore(os.path.join(root, "usage.json")),
            "routine_store": routines.RoutineStore(os.path.join(root, "routines.json")),
            "run_log": run_log.RunLog(os.path.join(root, "runs")),
            "task_board": context.TaskBoard(),
        }.items():
            self._patch(patch.object(shared, name, store))
        self._patch(patch.object(sessions, "SESSION_PATH", os.path.join(root, "session.json")))
        self._patch(patch.object(research, "LEDGER_DIR", os.path.join(root, "research")))
        self._patch(patch.object(stream_hub.hub, "publish"))
        self._patch(patch.object(model_caps, "get", return_value={}))
        self._patch(patch.object(llm.llama_local, "server_models", return_value=["new-local", "old-local"]))
        self._patch(patch.object(llm, "candidate_pairs", return_value=[("cloud-test", "fake-key")]))
        self._patch(patch.object(llm, "_watch_load", return_value=None))
        self.calls = []
        self.revise_request = None
        self.revised = False
        self._patch(patch.object(llm.llm_compat, "completion", side_effect=self.complete))
        self._patch(patch.object(llm, "_local_complete", side_effect=lambda kwargs, **kw: self.complete(**kwargs)))
        self._patch(patch.object(llm, "call_model", side_effect=self.answer))
        # The chat limits' check (chat_guard.py, test_chat_guard.py) is its own model call; these tests count
        # the run's stages, so it answers "allow" here without one.
        self._patch(patch.object(tasks, "_guard_model", return_value="ALLOW"))
        self._patch(patch.object(tasks.agent.prep_gate, "request",
                                 side_effect=lambda kind, payload, **kw: {"action": "send", "text": payload["text"]}))

    def _patch(self, p):
        obj = p.start()
        self.addCleanup(p.stop)
        return obj

    @staticmethod
    def response(content):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content=content, tool_calls=None, role="assistant"))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20))

    def complete(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        model = kwargs["model"]
        if system.startswith("You prepare"):
            kind, result = "prepare", {"intent": "Write the requested report", "success_criteria": ["Report answered"]}
        elif system.startswith("You plan web research") or system.startswith("You plan investment research"):
            kind, result = "research_plan", {"title": "Report", "questions": [{"q": "What is the evidence?"}]}
        elif system.startswith("You plan"):
            kind, result = "plan", {"steps": ["Write the requested report"]}
        elif system.startswith("You verify"):
            kind, result = "review", {"verdict": "complete", "findings": []}
            request = json.loads(kwargs["messages"][1]["content"])["original_request"]
            if request == self.revise_request and not self.revised:
                self.revised = True
                result = {"verdict": "revision_required", "findings": [{"type": "incorrect_result", "severity": "high",
                           "explanation": "Correct the second report", "action": "Write a corrected reply"}]}
        elif system.startswith(research.REPORT_SYSTEM):
            kind, result = "research_report", "# Report\nThe supplied evidence supports this answer."
        else:
            kind, result = "research_plan", {"title": "Report", "questions": [{"q": "What is the evidence?"}]}
        self.calls.append((kind, model, bool(kwargs.get("api_base"))))
        return self.response(result if isinstance(result, str) else json.dumps(result))

    def answer(self):
        self.calls.append(("execute", llm._current_model(), shared.app_state["mode"] == config.MODE_LOCAL))
        request = shared.app_state["current_user_request"]
        corrected = self.revised and request == self.revise_request
        return self.response(("Corrected reply: " if corrected else "Reply: ") + request)

    def run_turn(self, message="Write a report about the supplied private notes", **kwargs):
        return tasks._run_turn(shared.ChatRequest(message=message, mode="local", local_model="new-local", **kwargs))

    def test_cloud_to_local_is_applied_before_preparation_planning_and_review(self):
        result = self.run_turn()
        self.assertEqual(result["status"], "success", result)
        self.assertEqual([c[0] for c in self.calls], ["prepare", "plan", "execute", "review"])
        self.assertTrue(all(local for _stage, _model, local in self.calls), self.calls)
        self.assertTrue(all(model in ("new-local", "openai/new-local") for _stage, model, _local in self.calls), self.calls)

    def test_new_local_pin_controls_its_own_effort_and_prompt_prep(self):
        shared.app_state["mode"] = config.MODE_LOCAL
        shared.app_state["settings"].update(effort="low", prompt_prep="off", effort_by_model={"new-local": "high"},
                                             prompt_prep_by_model={"new-local": "expand"})
        result = self.run_turn()
        self.assertEqual(result["verification"]["effort"], "high")
        self.assertEqual([c[0] for c in self.calls], ["prepare", "plan", "execute", "review"])
        self.assertTrue(all("new-local" in model for _stage, model, _local in self.calls), self.calls)

    def test_research_uses_requested_backend_for_both_plan_and_report(self):
        def gather(r, questions):
            r.notes = [{"q": questions[0]["q"], "text": "Evidence collected by the fake web boundary."}]
            r.pages_ok = 1
        with patch.object(research.Research, "gather", gather):
            result = self.run_turn(command="research")
        self.assertEqual(result["status"], "success", result)
        self.assertEqual([c[0] for c in self.calls], ["research_plan", "research_report"])
        self.assertTrue(all(model == "openai/new-local" and local for _stage, model, local in self.calls), self.calls)

    def test_prompt_routine_trigger_preserves_current_backend_and_pin(self):
        shared.app_state.update(mode=config.MODE_LOCAL, local_model="new-local", local_engaged="new-local")
        routine = shared.routine_store.create_prompt("Report", "Write a report about the supplied private notes")
        result = tasks._run_turn(SimpleNamespace(message="Run routine: Report", with_vision=False,
                                                routine_id=routine["id"], trigger="schedule"))
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(self.calls)
        self.assertTrue(all(model in ("new-local", "openai/new-local") and local for _stage, model, local in self.calls))

    def test_investment_backbone_respects_market_data_permission(self):
        shared.app_state['settings']['permission_overrides'] = {'market_data': 'deny'}
        def gather(r, questions):
            r.notes = [{'q': 'Evidence', 'text': 'Synthetic web evidence.'}]
        with patch.object(research.Research, 'gather', gather), patch.object(research, 'backbone') as market:
            result = self.run_turn(command='invest')
        self.assertEqual(result['status'], 'success', result)
        market.assert_not_called()

    def test_direct_executor_keeps_legacy_model_field_and_invalid_mode_fallback(self):
        result = tasks.chat_with_agent(shared.ChatRequest(message="Write a short report", mode="invalid",
                                                          local_text_model="new-local"))
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(shared.app_state["mode"], config.MODE_LOCAL)
        self.assertEqual(shared.app_state["local_model"], "new-local")
        self.assertEqual(self.calls, [("execute", "new-local", True)])

    def revised_chat(self, legacy=False):
        messages = ["Write the first report", "Write the second report", "Write the third report"]
        self.revise_request = messages[1]
        for message in messages:
            result = self.run_turn(message)
            self.assertEqual(result["status"], "success", result)
        self.assertTrue(self.revised)
        convo = shared.app_state["conversation"]
        feedback = next(m for m in convo if str(m.get("content", "")).startswith("[REVIEWER FEEDBACK]"))
        self.assertEqual(feedback.get("name"), "agent_host")
        if legacy:
            feedback.pop("name")
        chat = shared.app_state["active_chat"]
        saved = shared.chat_store.load(chat["id"])
        saved["conversation"] = convo[1:]
        shared.chat_store.save(saved)
        return chat["id"], messages

    def test_delete_revised_turn_removes_request_draft_and_revision_even_after_reload(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                shared.app_state.update(active_chat=None, conversation=[{"role": "system", "content": "system"}])
                self.revised = False
                chat_id, requests = self.revised_chat(legacy=legacy)
                sessions.open_chat(chat_id)
                saved = sessions.edit_messages(chat_id, "delete", 2)
                self.assertEqual([m["content"] for m in saved["conversation"]],
                                 [requests[0], "Reply: " + requests[0], requests[2], "Reply: " + requests[2]])
                self.assertEqual([e["t"] for e in saved["transcript"] if e["r"] == "u"], [requests[0], requests[2]])

    def test_retry_truncation_cuts_before_revised_request(self):
        chat_id, requests = self.revised_chat(legacy=True)
        saved = sessions.edit_messages(chat_id, "truncate", 2)
        self.assertEqual([m["content"] for m in saved["conversation"]], [requests[0], "Reply: " + requests[0]])
        self.assertEqual(len(saved["transcript"]), 2)

    def test_branch_before_revised_turn_does_not_copy_later_request_or_draft(self):
        chat_id, requests = self.revised_chat(legacy=True)
        saved = sessions.edit_messages(chat_id, "branch", 1)
        self.assertEqual([m["content"] for m in saved["conversation"]], [requests[0], "Reply: " + requests[0]])
        self.assertEqual(len(saved["transcript"]), 2)

    def test_legacy_task_queue_notes_are_not_turns_but_clarification_answers_are(self):
        prefixes = ("[REVIEWER FEEDBACK]", "[REMAINING TASK]", "[CURRENT TASK]", "[CURRENT TASK 1/2]", "[ANSWER]")
        convo = [{"role": "user", "content": "original request"}]
        convo += [{"role": "user", "content": prefix + " host note"} for prefix in prefixes]
        convo += [{"role": "user", "name": "agent_host", "content": "Future host wording without a prefix"}]
        convo += [{"role": "user", "content": "[CLARIFICATION ANSWER] my selected option"},
                  {"role": "user", "content": "[ordinary bracketed user text]"}]
        self.assertEqual(sessions._turn_starts(convo), [0, len(convo) - 2, len(convo) - 1])


if __name__ == "__main__":
    unittest.main()

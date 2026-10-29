"""Tests for the grounding fix in main.py/context.py: an action that ran
without error (ok=True) but could not confirm its effect (verified=False)
must never be silently accepted by update_plan as 'done', and the ledger
must render that distinction instead of collapsing it into a single 'OK'.

This directly covers the bug where the agent said 'Game started.' after an
Invoke-only click that never actually confirmed anything."""
import unittest
from unittest.mock import patch

import context
import core.state as shared
import tools.dispatch as dispatch
import tools.runtime as runtime


class LedgerVerifiedFieldTests(unittest.TestCase):
    def setUp(self):
        self.ledger = context.ActionLedger()

    def test_ok_and_verified_renders_distinctly(self):
        self.ledger.record("act_element", {"element_id": "e1"}, True, "clicked", verified=True)
        self.assertIn("OK/VERIFIED", self.ledger.render())

    def test_ok_but_unverified_renders_distinctly(self):
        self.ledger.record("act_element", {"element_id": "e1"}, True, "no state field changed", verified=False)
        rendered = self.ledger.render()
        self.assertIn("OK/UNVERIFIED", rendered)
        self.assertNotIn("OK/VERIFIED", rendered)

    def test_failed_call_is_still_failed_regardless_of_verified(self):
        self.ledger.record("act_element", {"element_id": "e1"}, False, "element not found", verified=False)
        self.assertIn("FAILED", self.ledger.render())

    def test_tool_without_verified_concept_renders_plain_ok(self):
        self.ledger.record("open_application", {"app_name": "Notepad"}, True, "opened", verified=None)
        rendered = self.ledger.render()
        self.assertIn(" -> OK:", rendered)
        self.assertNotIn("OK/VERIFIED", rendered)
        self.assertNotIn("OK/UNVERIFIED", rendered)


class UpdatePlanGroundingTests(unittest.TestCase):
    """These call dispatch.execute_tool directly (not through the HTTP layer) to
    isolate the update_plan branch itself."""

    def setUp(self):
        shared.task_board.clear()
        shared.ledger.clear()
        shared.app_state["last_verifiable_result"] = None
        shared.task_board.set_plan("Start a game", ["Open Steam", "Start Bloons TD6"])
        shared.task_board.update(1, "done")   # step 1 finished normally, step 2 auto-activated

    def test_done_is_accepted_when_last_action_was_verified(self):
        shared.app_state["last_verifiable_result"] = {
            "tool": "act_element", "verified": True, "message": "toggle Off -> On",
        }
        result = dispatch.execute_tool("update_plan", {"step": 2, "status": "done"})
        self.assertTrue(result["ok"])
        step2 = next(s for s in shared.task_board.steps if s["n"] == 2)
        self.assertEqual(step2["status"], "done")

    def test_done_is_refused_and_downgraded_when_last_action_was_unverified(self):
        """This is the exact bug from the screenshot: act_element ran fine
        (ok=True) but could not confirm the game actually started
        (verified=False) - update_plan must not accept 'done' anyway."""
        shared.app_state["last_verifiable_result"] = {
            "tool": "act_element", "verified": False,
            "message": "invoke on Button 'Play': no state field changed (fine for buttons that just navigate)",
        }
        result = dispatch.execute_tool("update_plan", {"step": 2, "status": "done"})
        self.assertTrue(result["ok"])   # the update_plan CALL succeeded...
        self.assertIn("NOT marked done", result["message"])
        step2 = next(s for s in shared.task_board.steps if s["n"] == 2)
        self.assertEqual(step2["status"], "failed")   # ...but the STEP was not accepted as done
        self.assertIn("AUTO-DOWNGRADED", step2["note"])

    def test_evidence_is_consumed_once_so_it_cannot_block_a_later_legitimate_done(self):
        shared.app_state["last_verifiable_result"] = {
            "tool": "act_element", "verified": False, "message": "no state field changed",
        }
        first = dispatch.execute_tool("update_plan", {"step": 2, "status": "done"})
        self.assertIn("NOT marked done", first["message"])
        # A later, genuinely verified action for the (re-activated) step...
        shared.task_board.update(2, "active")
        shared.app_state["last_verifiable_result"] = {
            "tool": "act_element", "verified": True, "message": "toggle Off -> On",
        }
        second = dispatch.execute_tool("update_plan", {"step": 2, "status": "done"})
        self.assertNotIn("NOT marked done", second["message"])
        step2 = next(s for s in shared.task_board.steps if s["n"] == 2)
        self.assertEqual(step2["status"], "done")

    def test_no_prior_evidence_at_all_does_not_block_done(self):
        """A step with no mutating action behind it at all (e.g. a plan step
        that's just bookkeeping) must not be blocked - only an EXPLICIT
        verified=False blocks it."""
        shared.app_state["last_verifiable_result"] = None
        result = dispatch.execute_tool("update_plan", {"step": 2, "status": "done"})
        self.assertNotIn("NOT marked done", result["message"])
        step2 = next(s for s in shared.task_board.steps if s["n"] == 2)
        self.assertEqual(step2["status"], "done")

    def test_non_done_status_is_never_blocked(self):
        shared.app_state["last_verifiable_result"] = {"tool": "act_element", "verified": False, "message": "x"}
        result = dispatch.execute_tool("update_plan", {"step": 2, "status": "failed", "note": "gave up"})
        self.assertTrue(result["ok"])
        step2 = next(s for s in shared.task_board.steps if s["n"] == 2)
        self.assertEqual(step2["status"], "failed")
        self.assertEqual(step2["note"], "gave up")


class ExecuteToolGuardedWiringTests(unittest.TestCase):
    """Confirms execute_tool_guarded actually populates last_verifiable_result
    from real tool results, not just that update_plan behaves correctly in
    isolation."""

    def setUp(self):
        shared.app_state["last_verifiable_result"] = None
        shared.ledger.clear()
        shared.app_state["turn_policy"] = {"mode": "action", "allow_mutation": True, "reason": "test"}
        shared.app_state["intent_guard"] = None

    def test_a_tool_reporting_verified_updates_last_verifiable_result(self):
        with patch.object(dispatch, "execute_tool", return_value={"ok": True, "verified": False, "message": "no state field changed"}):
            runtime.execute_tool_guarded("act_element", {"element_id": "e1", "action": "click"})
        evidence = shared.app_state["last_verifiable_result"]
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["tool"], "act_element")
        self.assertFalse(evidence["verified"])

    def test_a_tool_without_a_verified_key_does_not_touch_last_verifiable_result(self):
        shared.app_state["last_verifiable_result"] = {"tool": "act_element", "verified": True, "message": "prior"}
        with patch.object(dispatch, "execute_tool", return_value={"ok": True, "message": "just a plain read"}):
            runtime.execute_tool_guarded("get_active_window", {})
        # unchanged - a tool with no verification concept must not clobber
        # the most recent REAL evidence
        self.assertEqual(shared.app_state["last_verifiable_result"]["tool"], "act_element")

    def test_ledger_records_the_verified_field(self):
        with patch.object(dispatch, "execute_tool", return_value={"ok": True, "verified": True, "message": "toggled"}):
            runtime.execute_tool_guarded("act_element", {"element_id": "e1", "action": "toggle"})
        self.assertTrue(shared.ledger.entries)
        self.assertTrue(shared.ledger.entries[-1]["verified"])


class FinalReturnGroundingCaveatTests(unittest.TestCase):
    """The safety net at the end of run_agent_loop: if the task board has
    unfinished/failed steps when the model stops calling tools, the message
    returned to the user must say so regardless of what the model's own
    prose claimed."""

    def setUp(self):
        shared.task_board.clear()

    def test_caveat_appended_when_a_step_is_still_failed(self):
        shared.task_board.set_plan("Start a game", ["Open Steam", "Start Bloons TD6"])
        shared.task_board.update(1, "done")
        shared.task_board.update(2, "failed", "AUTO-DOWNGRADED from 'done': could not confirm.")

        prose = "Game started."
        message = prose or "Done."
        if shared.task_board.steps:
            unfinished = [s for s in shared.task_board.steps if s["status"] not in ("done", "skipped")]
            if unfinished:
                summary = "; ".join(
                    f"step {s['n']} [{s['status']}]: {s['text']}" + (f" - {s['note']}" if s.get("note") else "")
                    for s in unfinished)
                message += f"\n\n(Task board shows this was not fully completed: {summary})"

        self.assertIn("Game started.", message)
        self.assertIn("not fully completed", message)
        self.assertIn("Start Bloons TD6", message)

    def test_no_caveat_when_everything_is_actually_done(self):
        shared.task_board.set_plan("Start a game", ["Open Steam", "Start Bloons TD6"])
        shared.task_board.update(1, "done")
        shared.task_board.update(2, "done")

        message = "Game started."
        if shared.task_board.steps:
            unfinished = [s for s in shared.task_board.steps if s["status"] not in ("done", "skipped")]
            if unfinished:
                message += "\n\n(should not appear)"

        self.assertEqual(message, "Game started.")


if __name__ == "__main__":
    unittest.main(verbosity=2)

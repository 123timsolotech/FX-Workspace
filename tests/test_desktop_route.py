"""The deterministic desktop route through the REAL runtime.execute_internal
and ToolExecutor (Phase 2 review): the caller sees exactly what the route
returned, a route that raises still fails the turn, and internal calls use
neither the model's tool budget nor count as model changes for the reviewer."""
import unittest
from unittest.mock import patch

import desktop_router
import intent_router
import orchestrator
import agent.tasks as tasks
import core.state as shared
import events as event_log
import tools.approvals as approvals
import tools.runtime as runtime


class DesktopRouteTests(unittest.TestCase):
    def setUp(self):
        runtime.tool_exec.begin_task("t-route", None)
        self.addCleanup(runtime.tool_exec.begin_task, None, None)
        self.addCleanup(shared.task_board.clear)
        saved = {k: shared.app_state.get(k) for k in ("turn_engine", "last_desktop_intent", "turn_policy")}
        self.addCleanup(shared.app_state.update, saved)
        shared.app_state["turn_policy"] = {"mode": "action", "allow_mutation": True, "reason": "test"}
        p = patch.object(approvals, "gate_batch", return_value=("auto", True))
        p.start()
        self.addCleanup(p.stop)

    def run_intent(self, route_result=None, raises=None, actions=("open",)):
        intent = intent_router.DesktopIntent(app_name="Notepad", actions=list(actions), original="open notepad")
        side = raises if raises else (lambda _i: route_result)
        with patch.object(desktop_router, "execute", side_effect=side):
            return tasks.run_desktop_intent(intent)

    def test_the_route_message_reaches_the_user_unchanged(self):
        out = self.run_intent({"ok": True, "verified": True, "title": "Untitled - Notepad", "message": "Opened Notepad."})
        self.assertEqual(out["message"], "Opened Notepad.")
        self.assertNotIn("[status:", out["message"])

    def test_no_route_message_falls_back_as_before(self):
        out = self.run_intent({"ok": True, "verified": True, "title": "Untitled - Notepad"})
        self.assertEqual(out["message"], "Opened Notepad.")
        closed = self.run_intent({"ok": False}, actions=("close",))
        self.assertEqual(closed["message"], "Could not close the app.")

    def test_a_route_that_raises_fails_the_turn(self):
        with self.assertRaises(FileNotFoundError):
            self.run_intent(raises=FileNotFoundError(2, "The system cannot find the file specified"))
        ev = [e for e in event_log.events_since(0) if e.get("tool") == "desktop_route"][-1]
        self.assertFalse(ev["ok"])                       # Activity shows it failed

    def test_internal_calls_do_not_use_the_budget_or_count_as_model_changes(self):
        runtime.tool_exec.begin_task("t-budget", 1)
        self.run_intent({"ok": True, "verified": True, "title": "Untitled - Notepad", "message": "Opened Notepad."})
        self.run_intent({"ok": True, "verified": True, "title": "Untitled - Notepad", "message": "Opened Notepad."})
        self.assertEqual(runtime.tool_exec.calls, 0)
        hist = runtime.tool_exec.history()
        self.assertTrue(hist and all(h["internal"] for h in hist))
        mutations = sum(1 for r in hist if r.get("ok") and r.get("mutating") and not r.get("internal"))
        self.assertEqual(mutations, 0)
        profile = __import__("effort").profile_for("normal")
        analysis = {"trivial": False, "mode": "action", "complex": False}
        self.assertEqual(orchestrator.reviewer_mode(profile, analysis, mutations), profile.reviewer)

    def test_the_activity_row_names_the_app(self):
        self.run_intent({"ok": True, "verified": True, "title": "Untitled - Notepad", "message": "Opened Notepad."})
        ev = [e for e in event_log.events_since(0) if e.get("tool") == "desktop_route"][-1]
        self.assertEqual(ev["label"], "Open Notepad")


if __name__ == "__main__":
    unittest.main()

"""Routines (routines.py, agent/routine_runner.py): which steps of a run are
kept, the store, schedule validation, scheduling through schedule_task, and
that a re-run passes every approval and permission like a model's call."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import permissions
import productivity_tools
import replay_harness as rh
import routines


def _h(tool, args, ok=True, mutating=True, internal=False):
    return {"tool": tool, "args": args, "ok": ok, "mutating": mutating, "internal": internal}


class StepsTests(unittest.TestCase):
    def test_only_successful_actions_are_kept_in_order(self):
        history = [
            _h("set_plan", {"steps": ["a"]}),
            _h("read_file", {"filename": "a.txt"}, mutating=False),
            _h("desktop_route", {"app": "Discord", "actions": ["open"]}, internal=True),
            _h("write_file", {"filename": "C:\\x.txt", "content": "hi", "justification": "asked"}),
            _h("write_file", {"filename": "C:\\x.txt", "content": "hi"}),         # retried identical call
            _h("run_powershell", {"command": "Get-Date"}, ok=False),              # failed: not kept
            _h("click_at", {"x": 1, "y": 2}),                                       # coordinates: not kept
            _h("save_memory", {"text": "x"}),
            _h("open_url", {"url": "https://roblox.com"}),
        ]
        steps = routines.steps_from_history(history, {"read_file"}, lambda t, a: f"{t}:{a.get('filename') or a.get('app') or a.get('url')}")
        self.assertEqual([s["tool"] for s in steps], ["desktop_route", "write_file", "open_url"])
        self.assertNotIn("justification", steps[1]["args"])
        self.assertEqual(steps[1]["label"], "write_file:C:\\x.txt")

    def test_browser_clicks_and_fills_are_not_replayed(self):
        """Their element numbers belong to one reading of one page."""
        history = [{"tool": "browser_open", "args": {"url": "https://x"}, "ok": True, "mutating": True},
                   {"tool": "browser_click", "args": {"element": 7}, "ok": True, "mutating": True},
                   {"tool": "browser_fill", "args": {"element": 3, "value": "a"}, "ok": True, "mutating": True}]
        self.assertEqual([s["tool"] for s in routines.steps_from_history(history, set())], ["browser_open"])

    def test_nothing_replayable(self):
        self.assertEqual(routines.steps_from_history([_h("read_file", {}, mutating=False)], {"read_file"}), [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = routines.RoutineStore(os.path.join(self.tmp.name, "routines.json"))
        self.steps = [{"tool": "open_url", "args": {"url": "https://a"}, "label": "Open a"},
                      {"tool": "open_url", "args": {"url": "https://b"}, "label": "Open b"}]

    def test_create_rename_trim_and_delete(self):
        r = self.store.create("  Morning   setup ", self.steps, "open my sites")
        self.assertEqual(r["name"], "Morning setup")
        with self.assertRaises(ValueError):
            self.store.create("morning SETUP", self.steps)                      # names are unique
        with self.assertRaises(ValueError):
            self.store.create("Empty", [])
        r = self.store.update(r["id"], keep=[1])
        self.assertEqual([s["label"] for s in r["steps"]], ["Open b"])
        with self.assertRaises(ValueError):
            self.store.update(r["id"], keep=[])
        r = self.store.update(r["id"], last_run={"at": 1, "status": "done", "message": "", "trigger": "click"})
        self.assertEqual(r["runs"], 1)
        self.assertTrue(self.store.delete(r["id"]))
        self.assertEqual(self.store.list(), [])
        with open(self.store.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], 1)

    def test_schedule_validation(self):
        self.assertEqual(routines.clean_schedule({"repeat": "daily", "time": "08:30"}),
                         {"repeat": "daily", "time": "08:30", "date": ""})
        for bad in ({"repeat": "hourly", "time": "08:00"}, {"repeat": "daily", "time": "8:00"},
                    {"repeat": "daily", "time": "25:00"}, {"repeat": "weekly", "time": "08:00"},
                    {"repeat": "once", "time": "08:00", "date": "2020-01-01"},
                    {"repeat": "once", "time": "08:00", "date": "2031-02-30"}):
            with self.assertRaises(ValueError, msg=bad):
                routines.clean_schedule(bad)

    def test_the_trigger_fits_task_scheduler(self):
        cmd = routines.trigger_command({"id": "a" * 12}, 8000)
        self.assertLessEqual(len(cmd), productivity_tools.TR_MAX)
        self.assertIn("/api/routines/aaaaaaaaaaaa/run?trigger=schedule", cmd)


class ScheduleTaskTests(unittest.TestCase):
    """schedule_task with Task Scheduler mocked (the real schtasks is Windows-only)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        ok = SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")
        for p in (patch.object(productivity_tools, "IS_WINDOWS", True),
                  patch.object(productivity_tools, "SCRIPT_DIR", self.tmp.name),
                  patch.object(productivity_tools, "_run", side_effect=lambda cmd, **k: self.calls.append(cmd) or ok)):
            p.start()
            self.addCleanup(p.stop)

    def _args(self):
        cmd = self.calls[-1]
        return cmd[cmd.index("/tr") + 1], cmd[cmd.index("/sc"):]

    def test_repeats(self):
        self.assertTrue(productivity_tools.schedule_task("r", run_at="2031-06-02 08:15", command="notepad", repeat="daily")["ok"])
        self.assertEqual(self._args()[1], ["/sc", "daily", "/st", "08:15", "/f"])
        productivity_tools.schedule_task("r", run_at="2031-06-02 08:15", command="notepad", repeat="weekdays")
        self.assertEqual(self._args()[1], ["/sc", "weekly", "/d", "MON,TUE,WED,THU,FRI", "/st", "08:15", "/f"])
        productivity_tools.schedule_task("r", run_at="2031-06-02 08:15", command="notepad", repeat="weekly")
        self.assertEqual(self._args()[1][:4], ["/sc", "weekly", "/d", "MON"])       # 2031-06-02 is a Monday
        productivity_tools.schedule_task("r", run_at="2031-06-02 08:15", command="notepad")
        self.assertEqual(self._args()[1], ["/sc", "once", "/st", "08:15", "/sd", "06/02/2031", "/f"])
        self.assertFalse(productivity_tools.schedule_task("r", run_at="2031-06-02 08:15", command="x", repeat="hourly")["ok"])

    def test_a_reminder_runs_from_a_script_file(self):
        """The one-liner used to exceed Task Scheduler's 261-character /TR and
        broke on a double quote in the message."""
        res = productivity_tools.schedule_task("Stretch", delay_seconds=60, message='Say "hi" and it\'s time')
        self.assertTrue(res["ok"], res)
        tr, _ = self._args()
        self.assertLessEqual(len(tr), productivity_tools.TR_MAX)
        self.assertIn("-File", tr)
        with open(os.path.join(self.tmp.name, "Stretch.ps1"), encoding="utf-8-sig") as f:
            script = f.read()
        self.assertIn('Say "hi" and it\'\'s time', script)

    def test_an_overlong_command_is_refused_before_scheduling(self):
        res = productivity_tools.schedule_task("x", delay_seconds=60, command="echo " + "a" * 300)
        self.assertFalse(res["ok"])
        self.assertIn("261", res["message"])
        self.assertEqual(self.calls, [])


class RunnerTests(unittest.TestCase):
    """A routine re-run goes through permissions + the approval card + the
    guarded executor, with no model."""

    def setUp(self):
        import approval_broker  # noqa: F401  (imported for the patch target's module)
        import core.config as config
        import core.state as shared
        import tools.approvals as approvals
        import tools.dispatch as dispatch
        import tools.files as files
        import tools.runtime as runtime
        import undo_store
        import agent.routine_runner as runner
        self.shared, self.runner = shared, runner
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = routines.RoutineStore(os.path.join(self.tmp.name, "routines.json"))
        self.asked, self.answers, self.executed = [], [], []
        sandbox_undo = undo_store.UndoStore(os.path.join(self.tmp.name, ".undo"))
        keys = ("settings", "conversation", "turn_policy", "untrusted_seen", "intent_guard", "current_user_request")
        saved = {k: shared.app_state.get(k) for k in keys}
        self.addCleanup(lambda: shared.app_state.update(saved))
        shared.app_state["settings"] = dict(config.DEFAULT_SETTINGS)
        shared.app_state["conversation"] = [{"role": "system", "content": "s"}]

        def ask(actions, context_line="", **kw):
            self.asked.append((context_line, [a["tool_name"] for a in actions]))
            return self.answers.pop(0) if self.answers else False

        def run_tool(name, args):
            self.executed.append(name)
            return {"ok": True, "message": f"{name} ok"}
        for p in (patch.object(shared, "routine_store", self.store),
                  patch.object(approvals.broker, "request", side_effect=ask),
                  patch.object(dispatch, "execute_tool", side_effect=run_tool),
                  patch.object(files, "undo", sandbox_undo), patch.object(runtime.tool_exec, "undo", sandbox_undo)):
            p.start()
            self.addCleanup(p.stop)
        self.target = os.path.join(self.tmp.name, "notes.txt")
        self.routine = self.store.create("Morning setup", [
            {"tool": "write_file", "args": {"filename": self.target, "content": "hi"}, "label": "Write notes.txt"},
            {"tool": "get_url_status", "args": {"url": "https://example.com"}, "label": "Check example.com"},
            {"tool": "run_powershell", "args": {"command": "Get-Date"}, "label": "Run PowerShell Get-Date"}])

    def test_every_step_that_asks_still_asks(self):
        self.answers = [True, True]
        res = self.runner.run(self.routine["id"])
        self.assertEqual(res["status"], "success", res)
        self.assertEqual([a[1] for a in self.asked], [["write_file"], ["run_powershell"]])   # benign check auto-ran
        self.assertIn("Routine 'Morning setup' · step 1 of 3", self.asked[0][0])
        self.assertEqual(self.executed, ["write_file", "get_url_status", "run_powershell"])
        saved = self.store.get(self.routine["id"])
        self.assertEqual((saved["runs"], saved["last_run"]["status"]), (1, "done"))

    def test_declining_stops_the_rest(self):
        self.answers = [False]
        res = self.runner.run(self.routine["id"])
        self.assertTrue(res["rejected"])
        self.assertEqual(self.executed, [])
        self.assertIn("Not run: 2 later steps", res["message"])
        self.assertEqual(self.store.get(self.routine["id"])["last_run"]["status"], "declined")

    def test_deny_is_honoured_without_asking(self):
        self.shared.app_state["settings"]["permission_overrides"] = {"write_file": "deny"}
        res = self.runner.run(self.routine["id"])
        self.assertEqual(res["status"], "error")
        self.assertEqual((self.asked, self.executed), ([], []))

    def test_a_routine_cannot_run_twice_at_once(self):
        self.shared.state_lock.acquire()                 # something else holds the turn lock
        try:
            res = self.runner.run(self.routine["id"])
        finally:
            self.shared.state_lock.release()
        self.assertIn("Busy", res["message"])
        self.assertEqual(self.store.get(self.routine["id"])["last_run"]["status"], "skipped")

    def test_a_routine_saved_from_a_tainted_run_keeps_asking(self):
        """Phase 3 review: the caution of the run it came from is not lost -
        even the benign check that normally runs by itself asks."""
        tainted = self.store.create("From a web page", self.routine["steps"], tainted=True)
        self.answers = [True, True, True]
        res = self.runner.run(tainted["id"])
        self.assertEqual(res["status"], "success", res)
        self.assertEqual([a[1] for a in self.asked], [["write_file"], ["get_url_status"], ["run_powershell"]])

    def test_the_end_of_a_routine_is_not_called_verified(self):
        import events as event_log
        self.answers = [True, True]
        start = self.shared.app_state.get("event_seq", 0)
        self.runner.run(self.routine["id"])
        phases = [e.get("phase") for e in event_log.events_since(start) if e.get("kind") == "activity"]
        self.assertIn("routine_finished", phases)
        self.assertNotIn("verification_complete", phases)

    def test_rerunning_is_not_blocked_as_a_duplicate(self):
        self.answers = [True, True, True, True]
        self.runner.run(self.routine["id"])
        self.runner.run(self.routine["id"])
        self.assertEqual(self.executed.count("write_file"), 2)


def _call(cid, name, args):
    return {"finish_reason": "tool_calls", "tool_calls": [{"id": cid, "name": name, "arguments": json.dumps(args)}]}


class CandidateTests(unittest.TestCase):
    def test_a_successful_turn_offers_its_actions_as_a_routine(self):
        with tempfile.TemporaryDirectory() as sandbox:
            target = os.path.join(sandbox, "todo.txt")
            fx = {"format": "agentic-replay/1",
                  "meta": {"name": "routine", "provenance": "reconstructed", "model": "unrecorded", "evidence": "test"},
                  "setup": {"mode": "local", "entry": "turn", "settings": {"effort": "low", "prompt_enhancement": "off"}},
                  "request": "Create todo.txt with my list",
                  "responses": [_call("w1", "write_file", {"filename": target, "content": "milk", "justification": "asked"}),
                                {"finish_reason": "stop", "content": "Created it."}],
                  "approvals": [True], "tools": {"mode": "real"}}
            run = rh.run_fixture(fx, sandbox=sandbox)
            self.assertEqual(run.status, "success", run.message)
            cand = run.result["routine_candidate"]
            self.assertEqual(len(cand["steps"]), 1)
            self.assertIn("todo.txt", cand["steps"][0])
            self.assertTrue(run.result["chat"]["id"])                    # the turn was saved to a chat too

    def test_a_reply_without_actions_offers_nothing(self):
        fx = {"format": "agentic-replay/1",
              "meta": {"name": "routine", "provenance": "reconstructed", "model": "unrecorded", "evidence": "test"},
              "setup": {"mode": "local", "entry": "turn", "settings": {"effort": "low", "prompt_enhancement": "off"}},
              "request": "What is 2+2?", "responses": [{"finish_reason": "stop", "content": "4"}]}
        run = rh.run_fixture(fx)
        self.assertNotIn("routine_candidate", run.result)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import main
        cls.client = TestClient(main.app, base_url="http://127.0.0.1:8000")

    def setUp(self):
        import core.state as shared
        import agent.routine_runner as runner
        self.shared, self.runner = shared, runner
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(shared, "routine_store", routines.RoutineStore(os.path.join(self.tmp.name, "r.json")))
        p.start()
        self.addCleanup(p.stop)
        runner._candidates["t" * 12] = {"request": "open my sites", "project": None,
                                        "steps": [{"tool": "open_url", "args": {"url": "https://a"}, "label": "Open a"}]}
        self.addCleanup(lambda: runner._candidates.pop("t" * 12, None))

    def test_save_rename_schedule_guard_and_delete(self):
        c = self.client
        self.assertFalse(c.post("/api/routines", json={"name": "x", "task_id": "nope"}).json()["ok"])
        r = c.post("/api/routines", json={"name": "Sites", "task_id": "t" * 12}).json()
        self.assertTrue(r["ok"], r)
        rid = r["routine"]["id"]
        self.assertEqual(c.post(f"/api/routines/{rid}", json={"name": "My sites"}).json()["routine"]["name"], "My sites")
        self.assertFalse(c.post(f"/api/routines/{rid}/schedule", json={"repeat": "daily", "time": "99:00"}).json()["ok"])
        # Scheduling goes through the schedule_task approval path; declined -> not scheduled.
        import api.server as server
        with patch.object(server, "_run_tool_for_user", return_value={"ok": False, "message": "Cancelled - nothing was changed."}) as gate:
            res = c.post(f"/api/routines/{rid}/schedule", json={"repeat": "weekdays", "time": "08:30"}).json()
        self.assertFalse(res["ok"])
        tool, args, _why = gate.call_args[0]
        self.assertEqual((tool, args["repeat"], args["run_at"][-5:]), ("schedule_task", "weekdays", "08:30"))
        self.assertIn(f"/api/routines/{rid}/run?trigger=schedule", args["command"])
        with patch.object(server, "_run_tool_for_user", return_value={"ok": True, "message": "Scheduled"}):
            self.assertEqual(c.post(f"/api/routines/{rid}/schedule", json={"repeat": "daily", "time": "07:00"}).json()["routine"]["schedule"]["repeat"], "daily")
        self.assertFalse(c.delete(f"/api/routines/{rid}").json()["ok"])              # unschedule first
        with patch.object(server, "_run_tool_for_user", return_value={"ok": True, "message": "Cancelled"}):
            self.assertIsNone(c.delete(f"/api/routines/{rid}/schedule").json()["routine"]["schedule"])
        self.assertTrue(c.delete(f"/api/routines/{rid}").json()["ok"])

    def test_run_starts_a_turn(self):
        import agent.tasks as tasks
        rid = self.shared.routine_store.create("R", [{"tool": "open_url", "args": {"url": "https://a"}, "label": "Open a"}])["id"]
        with patch.object(tasks.turns, "start", return_value={"id": "turn1"}) as start:
            res = self.client.post(f"/api/routines/{rid}/run?trigger=schedule").json()
        self.assertEqual(res["status"], "accepted")
        req = start.call_args[0][0]
        self.assertEqual((req.routine_id, req.trigger), (rid, "schedule"))


class NoAuthorityTests(unittest.TestCase):
    def test_permissions_never_read_routines(self):
        """No permission decision takes a routine into account: a tool's
        decision is the same whatever routines exist."""
        import inspect
        self.assertNotIn("routine", inspect.getsource(permissions).lower())


if __name__ == "__main__":
    unittest.main()

"""Readable approvals (approval_view.py, approval.py, approval_broker.py):
full untruncated arguments, file-change previews, an honest undoable /
not-undoable marker, and one wording on both approval surfaces."""
import os
import queue
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import approval
import approval_broker
import approval_view
import sys_tools
import web_tools
import core.state as shared
import tools.approvals as approvals
import tools.dispatch as dispatch
import app_registry
import context
import desktop_input
import roblox_places
import subprocess


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_arguments_are_never_truncated(self):
        cmd = "Write-Output " + "x" * 5000
        view = approval_view.describe("run_powershell", {"command": cmd, "force": True, "justification": "why"}, "why")
        self.assertEqual(view["args"]["command"], cmd)                  # all 5,000+ characters
        self.assertEqual(view["args"]["force"], "true")                 # force is shown, not hidden
        self.assertEqual(view["justification"], "why")
        self.assertNotIn("justification", view["args"])
        self.assertEqual(approval._format_args({"command": cmd, "force": True}), f"command: {cmd}\nforce: true")

    def test_write_file_preview_is_a_real_diff(self):
        f = self.dir / "a.txt"
        f.write_text("line one\nline two\n", encoding="utf-8")
        view = approval_view.describe("write_file", {"filename": str(f), "content": "line one\nline 2\n"}, undoable=True)
        diff = view["preview"]["text"]
        self.assertEqual(view["preview"]["kind"], "diff")
        self.assertIn("-line two", diff)
        self.assertIn("+line 2", diff)
        new = approval_view.describe("write_file", {"filename": str(self.dir / "new.txt"), "content": "hi"}, undoable=True)
        self.assertEqual(new["preview"]["note"], "Creates a new file.")
        app = approval_view.describe("write_file", {"filename": str(f), "content": "more\n", "append": True}, undoable=True)
        self.assertIn("+more", app["preview"]["text"])
        self.assertNotIn("-line one", app["preview"]["text"])

    def test_edit_and_delete_previews(self):
        f = self.dir / "c.py"
        f.write_text("x = 1\n", encoding="utf-8")
        self.assertIn("+x = 2", approval_view.preview("edit_file", {"filename": str(f), "old_text": "x = 1", "new_text": "x = 2"})["text"])
        self.assertIn("will fail", approval_view.preview("edit_file", {"filename": str(f), "old_text": "nope", "new_text": "y"})["text"])
        (self.dir / "d").mkdir()
        (self.dir / "d" / "k.txt").write_text("k", encoding="utf-8")
        self.assertIn("1 file(s)", approval_view.preview("delete_path", {"path": str(self.dir / "d")})["text"])
        self.assertIn("Replaces", approval_view.preview("move_path", {"source": "x", "destination": str(f), "overwrite": True})["text"])

    def test_undo_marker_is_honest(self):
        self.assertEqual(approval_view.describe("write_file", {}, undoable=True)["undo_text"], "Can be undone")
        self.assertEqual(approval_view.describe("run_powershell", {}, undoable=False)["undo_text"], "Not undoable")
        self.assertEqual(approval_view.describe("list_dir", {}, undoable=False, mutating=False)["undo_text"], "")

    def test_one_wording_everywhere(self):
        self.assertEqual(approval_view.title(1), "Allow this action?")
        self.assertEqual(approval_view.title(3), "Allow these 3 actions?")
        popup = Path(approval.__file__).read_text(encoding="utf-8")
        page = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
        for text in (popup, page):
            self.assertNotIn("Approve all", text)
            self.assertNotRegex(text, r"Enter approves|Esc rejects")
        self.assertIn('words["allow"]', popup)
        self.assertIn("p.wording", page)


class BrokerTests(unittest.TestCase):
    def test_web_card_gets_the_full_view(self):
        long_text = "y" * 3000
        b = approval_broker.ApprovalBroker(native_ask=lambda a, c: True, native_available=lambda: False)
        b.heartbeat(True)
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("v", b.request(
            [{"tool_name": "write_file", "args": {"filename": "x", "content": long_text}}], "ctx", timeout=5)))
        t.start()
        for _ in range(40):
            p = b.pending()
            if p:
                break
            time.sleep(0.05)
        self.assertEqual(p["actions"][0]["args"]["content"], long_text)
        self.assertEqual(p["title"], "Allow this action?")
        self.assertEqual(p["wording"]["allow"], "Allow")
        b.answer(p["id"], False)
        t.join(5)
        self.assertFalse(result["v"])

    def test_stop_closes_the_desktop_popup_request(self):
        stopped = threading.Event()
        with patch.object(approval, "_gui_queue", queue.Queue()) as q, patch.object(approval, "_gui_thread", None):
            approval._gui_failed.clear()
            threading.Timer(0.3, stopped.set).start()
            started = time.monotonic()
            answer = approval.ask([{"tool_name": "delete_path", "args": {"path": "x"}}], "", timeout=30,
                                  cancelled=stopped.is_set)
            self.assertFalse(answer)
            self.assertLess(time.monotonic() - started, 3)
            kinds = [q.get_nowait()[0] for _ in range(q.qsize())]
        self.assertIn("close", kinds)                                   # the popup is told to close

    def test_broker_passes_stop_to_native_popups_that_accept_it(self):
        seen = {}

        def native(actions, context, cancelled=None):
            seen["cancelled"] = cancelled
            return False
        b = approval_broker.ApprovalBroker(native_ask=native)
        b.request([{"tool_name": "x", "args": {}}], "", cancelled=lambda: True)
        self.assertTrue(callable(seen["cancelled"]))


class AnswerRecordTests(unittest.TestCase):
    """D49: what a card asked for outlives the card - the answer event names it, and a saved chat keeps it (and
    Undo) for the work lines under the reply."""

    def test_the_answer_event_names_what_was_asked(self):
        import events as event_log
        start = shared.app_state.get("event_seq", 0)
        actions = [{"tool_name": "delete_path", "args": {"path": "C:/logs/old.log"}}]
        with patch.object(approvals.broker, "request", return_value=False):
            self.assertFalse(approvals.ask_batch_approval_os(actions, "ctx"))
        answer = [e for e in event_log.events_since(start) if e["kind"] == "approval"][-1]
        self.assertEqual(answer["label"], "Declined")
        self.assertIn("old.log", actions[0]["view"]["brief"])
        self.assertEqual(answer["asked"], [actions[0]["view"]["brief"]])

    def test_saved_steps_keep_undo_and_what_was_asked(self):
        from agent import sessions
        self.assertEqual(sessions._slim({"kind": "tool", "label": "Write notes.txt", "ok": True, "undo_id": "u1"})["undo_id"], "u1")
        self.assertEqual(sessions._slim({"kind": "approval", "label": "Declined", "ok": False,
                                         "asked": ["Delete old.log"]})["asked"], ["Delete old.log"])

    def test_the_page_folds_answers_into_work_lines(self):
        html = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
        self.assertIn("function approvalAnswers(steps)", html)
        self.assertIn("done.className = `work-line ap-done", html)
        self.assertIn("allowed.has(e) ? 'you allowed it' : ''", html)
        self.assertIn("...(Array.isArray(e.asked) && e.asked.length ? { asked: e.asked.slice(0, 8) } : {})", html)


class PreviewLimitTests(unittest.TestCase):
    def test_binary_and_huge_files_are_described_not_diffed(self):
        with tempfile.TemporaryDirectory() as d:
            binf = Path(d) / "img.png"
            binf.write_bytes(b"\x89PNG\x00\x00\x00" + b"\x01" * 100)
            pv = approval_view.preview("write_file", {"filename": str(binf), "content": "x"})
            self.assertEqual(pv["kind"], "text")
            self.assertIn("binary", pv["text"])
            big = Path(d) / "big.txt"
            big.write_text("a" * 10, encoding="utf-8")
            with patch.object(approval_view, "PREVIEW_MAX_FILE", 5):
                self.assertIn("too large", approval_view.preview("edit_file", {"filename": str(big), "old_text": "a", "new_text": "b"})["text"])

    def test_long_previews_are_shortened_and_say_so(self):
        with tempfile.TemporaryDirectory() as d, patch.object(approval_view, "PREVIEW_MAX_CHARS", 300):
            view = approval_view.describe("write_file", {"filename": str(Path(d) / "n.txt"), "content": "line\n" * 500})
            self.assertLessEqual(len(view["preview"]["text"]), 300)
            self.assertIn("shortened", view["preview"]["note"])
            self.assertEqual(view["args"]["content"], "line\n" * 500)      # the arguments stay complete

    def test_crlf_edit_preview_matches_like_the_edit(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.bat"
            f.write_bytes(b"@echo off\r\necho one\r\necho two\r\n")
            pv = approval_view.preview("edit_file", {"filename": str(f), "old_text": "echo one\necho two", "new_text": "echo 1\necho 2"})
            self.assertEqual(pv["kind"], "diff")


class SafetyTests(unittest.TestCase):
    def test_read_file_refuses_secret_files(self):
        for name in ("keys.local.bat", ".env", "prod.env", "id_rsa", "server.pem", "aws_credentials.txt"):
            res = dispatch.execute_tool("read_file", {"filename": os.path.join(tempfile.gettempdir(), name)})
            self.assertFalse(res["ok"], name)
            self.assertIn("holds secrets", res["message"])

    def test_run_sequence_cannot_bypass_a_per_tool_deny(self):
        saved = shared.app_state["settings"]
        try:
            shared.app_state["settings"] = {**saved, "permission_overrides": {"type_text": "deny"}}
            with patch.object(desktop_input, "send_key_combo") as keys, patch.object(app_registry, "set_clipboard") as clip:
                res = dispatch.execute_tool("run_sequence", {"steps": [{"kind": "key", "combo": "ctrl+a"},
                                                                    {"kind": "type", "text": "hello"}]})
            self.assertFalse(res["ok"])
            self.assertIn("type_text", res["message"])
            keys.assert_not_called()                                   # not even the allowed first step ran
            clip.assert_not_called()
        finally:
            shared.app_state["settings"] = saved

    def test_open_application_never_runs_text_through_a_shell(self):
        with patch.object(app_registry, "resolve_app", return_value=(None, [])), \
             patch.object(roblox_places, "resolve_place", return_value=(None, [])), \
             patch.object(context, "window_is_open", return_value=None), \
             patch.object(subprocess, "Popen") as popen:
            res = dispatch.execute_tool("open_application", {"app_name": "calc & del /q C:\\important"})
        self.assertFalse(res["ok"])
        popen.assert_not_called()

    def test_file_changed_after_approval_is_not_written(self):
        """The user approved a diff; the file changed before it ran -> not run."""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "notes.txt"
            f.write_text("v1", encoding="utf-8")
            action = {"tool_name": "write_file", "args": {"filename": str(f), "content": "v2"}}
            view = approvals._approval_view(action)
            self.assertIsNone(approvals._changed_since_approval(view))
            f.write_text("edited meanwhile", encoding="utf-8")
            self.assertIn("changed after the user approved", approvals._changed_since_approval(view))


    def test_open_url_refuses_local_programs(self):
        for url in ("file:///C:/Windows/System32/calc.exe", "ms-settings:privacy", "C:\\evil.exe"):
            with patch("os.startfile", create=True) as start:
                res = web_tools.open_url(url)
            self.assertFalse(res["ok"], url)
            start.assert_not_called()

    def test_env_secrets_are_hidden(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "sk-very-secret", "MY_COLOR": "blue"}):
            self.assertNotIn("sk-very-secret", sys_tools.get_env_var("GEMINI_API_KEY")["message"])
            self.assertEqual(sys_tools.get_env_var("MY_COLOR")["message"], "MY_COLOR=blue")


if __name__ == "__main__":
    unittest.main()

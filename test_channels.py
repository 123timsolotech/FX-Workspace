"""1.4.2 Channels (channels.py): only you, never blocking, approvals and stop from the channel, the webhook."""
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import channels
import tools.approvals as approvals


class FakeBroker:
    def __init__(self):
        self.p = None
        self.answers = []

    def pending(self):
        return self.p

    def answer(self, approval_id, yes, via="", scope=""):
        if self.p and self.p["id"] == approval_id:
            self.answers.append((approval_id, yes))
            self.p = None
            return True
        return False


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for target in (patch.object(channels, "DATA", os.path.join(self.dir, "channels.json")),
                       patch.object(channels, "_seal", lambda v: "x:" + v), patch.object(channels, "_unseal", lambda v: v[2:] if v else ""),
                       patch.object(channels, "restart", lambda kind: None)):
            target.start()
            self.addCleanup(target.stop)
        self.broker = FakeBroker()
        p = patch.object(approvals, "broker", self.broker)
        p.start()
        self.addCleanup(p.stop)
        channels.configure_channel("discord", enabled=True, token="t", allow="111, 222")
        self.sent = []
        self.release = threading.Event()

        def runner(chat_id, message, origin, on_approval):
            self.release.wait(3)
            return "chat-1", {"status": "success", "message": f"answer to {message}"}
        channels.configure(runner)
        self.addCleanup(channels.configure, None)

    def wait_for(self, n):
        for _ in range(60):
            if len(self.sent) >= n:
                return
            time.sleep(0.05)

    def test_strangers_are_ignored(self):
        self.assertEqual(channels.handle("discord", "999", "hi", self.sent.append), "ignored")
        self.assertEqual(self.sent, [])

    def test_a_turn_runs_in_the_background_and_answers(self):
        self.assertEqual(channels.handle("discord", "111", "hello", self.sent.append), "started")
        # Listening never stops: a second message while it works is answered at once, not queued behind it.
        self.assertEqual(channels.handle("discord", "111", "again", self.sent.append), "busy")
        self.release.set()
        self.wait_for(2)
        self.assertIn("answer to hello", self.sent[-1])
        self.assertEqual(channels.status()["discord"]["chat_id"], "chat-1")

    def test_yes_answers_a_waiting_approval_and_stop_stops(self):
        self.broker.p = {"id": "ab12", "actions": [], "title": "Allow this action?"}
        self.assertEqual(channels.handle("discord", "222", "yes", self.sent.append), "answered")
        self.assertEqual(self.broker.answers, [("ab12", True)])
        from run_control import run_control
        with patch.object(run_control, "stop") as stop:
            self.assertEqual(channels.handle("discord", "111", "stop", self.sent.append), "stopped")
        stop.assert_called_once()

    def test_discord_parsing(self):
        dm = {"t": "MESSAGE_CREATE", "d": {"author": {"id": "111"}, "channel_id": "c1", "content": "hi"}}
        self.assertEqual(channels.discord_message(dm), ("111", "c1", "hi"))
        in_server = {"t": "MESSAGE_CREATE", "d": {"guild_id": "g", "author": {"id": "111"}, "channel_id": "c1",
                                                  "content": "<@42> open notes", "mentions": [{"id": "42"}]}}
        self.assertEqual(channels.discord_message(in_server, "42"), ("111", "c1", "open notes"))
        self.assertIsNone(channels.discord_message({**in_server, "d": {**in_server["d"], "mentions": []}}, "42"))
        self.assertIsNone(channels.discord_message({"t": "MESSAGE_CREATE", "d": {"author": {"id": "1", "bot": True}}}))
        click = {"t": "INTERACTION_CREATE", "d": {"id": "i", "token": "tk", "data": {"custom_id": "fx-no-ab12"},
                                                 "member": {"user": {"id": "111"}}}}
        self.assertEqual(channels.discord_button(click), ("111", "ab12", False, "i", "tk"))

    def test_slack_parsing(self):
        env = {"type": "events_api", "payload": {"event": {"type": "message", "channel_type": "im", "user": "U1",
                                                          "channel": "D1", "text": "hi"}}}
        self.assertEqual(channels.slack_message(env), ("U1", "D1", "hi"))
        self.assertIsNone(channels.slack_message({"type": "events_api", "payload": {"event": {"type": "message", "bot_id": "B"}}}))
        click = {"type": "interactive", "payload": {"type": "block_actions", "user": {"id": "U1"}, "response_url": "u",
                                                   "actions": [{"action_id": "fx-yes", "value": "ab12"}]}}
        self.assertEqual(channels.slack_button(click), ("U1", "ab12", True, "u"))

    def test_webhook_secret_and_one_time_codes(self):
        channels.configure_channel("webhook", enabled=True)
        secret = channels.webhook_secret()
        self.assertTrue(secret)
        self.assertIsNotNone(channels.hook_check("wrong", "1.1.1.1"))
        self.assertIsNone(channels.hook_check(secret, "1.1.1.1"))

        def runner(chat_id, message, origin, on_approval):
            self.broker.p = {"id": "zz9", "actions": [], "title": "Allow this action?"}
            on_approval(self.broker.p)
            for _ in range(60):
                if self.broker.p is None:
                    break
                time.sleep(0.05)
            return "c", {"message": "done it"}
        channels.configure(runner)
        r = channels.hook_message("delete the logs", wait=3)
        self.assertEqual(r["status"], "needs_approval")
        self.assertTrue(channels.hook_answer(r["allow"])["ok"])
        self.assertFalse(channels.hook_answer(r["refuse"])["ok"])          # the pair is spent
        for _ in range(60):
            if channels.hook_result(r["id"]).get("status") == "done":
                break
            time.sleep(0.05)
        self.assertEqual(channels.hook_result(r["id"])["message"], "done it")
        self.assertEqual(self.broker.answers, [("zz9", True)])


if __name__ == "__main__":
    unittest.main()

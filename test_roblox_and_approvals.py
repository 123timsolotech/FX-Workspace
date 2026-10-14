"""Offline tests for roblox_places, roblox_bridge context, and approval_broker."""
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import approval_broker
import roblox_bridge
import roblox_places


class RobloxPlacesTests(unittest.TestCase):
    def setUp(self):
        roblox_places._cache.update(at=0.0, dirs=(), places=[])

    def test_exact_and_fuzzy_resolution(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "games"))
            for name in ("Test1.rbxl", os.path.join("games", "Lava Obby.rbxlx"), "notes.txt"):
                open(os.path.join(root, name), "w").close()
            with patch.dict(os.environ, {"ROBLOX_PLACES_DIRS": root}), \
                 patch.object(os.path, "expanduser", side_effect=lambda p: p.replace("~", root + "_nohome")):
                match, _ = roblox_places.resolve_place("test1")
                self.assertEqual(match["display"], "Test1")
                match, _ = roblox_places.resolve_place("the lava obby place")
                self.assertEqual(match["display"], "Lava Obby")
                match, candidates = roblox_places.resolve_place("lava ob")
                self.assertIsNone(match)
                self.assertEqual([c["display"] for c in candidates], ["Lava Obby"])
                self.assertEqual(roblox_places.resolve_place("zzz"), (None, []))


class RobloxBridgeContextTests(unittest.TestCase):
    def setUp(self):
        roblox_bridge._reset_for_tests()

    def test_context_and_errors_are_sequenced(self):
        roblox_bridge.mark_seen()
        roblox_bridge.record_context({"place": "Obby", "selection": ["Workspace/Part"],
                                      "errors": [{"text": "boom"}, "bang"]})
        ctx = roblox_bridge.latest_context()
        self.assertEqual(ctx["place"], "Obby")
        self.assertEqual(ctx["error_seq"], 2)
        self.assertEqual([e["text"] for e in roblox_bridge.errors_since(1)], ["bang"])
        # play-mode copies of the plugin only add errors, never overwrite context
        roblox_bridge.record_context({"role": "play-server", "place": "Other", "errors": ["late"]})
        ctx = roblox_bridge.latest_context()
        self.assertEqual(ctx["place"], "Obby")
        self.assertTrue(ctx["running"])
        self.assertEqual(roblox_bridge.errors_since(2)[0]["text"], "[play-server] late")

    def test_disconnected_bridge_reports_empty_context(self):
        self.assertEqual(roblox_bridge.latest_context(), {})
        self.assertFalse(roblox_bridge.status_dict()["connected"])
        res = roblox_bridge.enqueue("ping", {})
        self.assertFalse(res["ok"])

    def test_unknown_command_is_rejected(self):
        roblox_bridge.mark_seen()
        self.assertFalse(roblox_bridge.enqueue("run_lua", {"code": "print(1)"})["ok"])

    def test_round_trip(self):
        roblox_bridge.mark_seen()

        def plugin():
            for _ in range(20):
                cmd = roblox_bridge.poll(wait=1)
                if cmd:
                    roblox_bridge.post_result(roblox_bridge.ResultIn(id=cmd["id"], ok=True, message="pong",
                                                                     data={"version": 2}))
                    return

        t = threading.Thread(target=plugin)
        t.start()
        res = roblox_bridge.enqueue("ping", {}, timeout=5)
        t.join()
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"], {"version": 2})


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ApprovalBrokerTests(unittest.TestCase):
    ACTIONS = [{"tool_name": "delete_path", "args": {"path": "C:/x", "justification": "cleanup"}}]

    def test_unfocused_page_uses_native_popup(self):
        calls = []
        broker = approval_broker.ApprovalBroker(native_ask=lambda a, c: calls.append(a) or True)
        broker.heartbeat(False)
        self.assertTrue(broker.request(self.ACTIONS, "ctx"))
        self.assertEqual(len(calls), 1)

    def test_focused_page_gets_inline_card(self):
        native = []
        broker = approval_broker.ApprovalBroker(native_ask=lambda a, c: native.append(a) or True)
        broker.heartbeat(True)
        result = {}

        def run():
            result["value"] = broker.request(self.ACTIONS, "Step 1 of 1")

        t = threading.Thread(target=run)
        t.start()
        pending = None
        for _ in range(40):
            broker.heartbeat(True)
            pending = broker.pending()
            if pending:
                break
            time.sleep(0.05)
        self.assertEqual(pending["actions"][0]["tool"], "delete_path")
        self.assertEqual(pending["actions"][0]["tier"], "destructive")
        self.assertEqual(pending["actions"][0]["justification"], "cleanup")
        self.assertNotIn("justification", pending["actions"][0]["args"])
        self.assertFalse(broker.answer("wrong-id", True))
        self.assertTrue(broker.answer(pending["id"], False))
        t.join(3)
        self.assertFalse(result["value"])
        self.assertEqual(native, [])
        self.assertIsNone(broker.pending())

    def test_closed_tab_escalates_to_native(self):
        clock = FakeClock()
        native = []
        broker = approval_broker.ApprovalBroker(native_ask=lambda a, c: native.append(a) or True, clock=clock)
        broker.heartbeat(True)
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("v", broker.request(self.ACTIONS)))
        t.start()
        time.sleep(0.1)
        clock.t += approval_broker.HEARTBEAT_STALE + 1     # no more polls: tab closed
        t.join(3)
        self.assertTrue(result["v"])
        self.assertEqual(len(native), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

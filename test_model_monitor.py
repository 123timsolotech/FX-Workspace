"""1.4.2 Phase 1: model use outside replies is listed, and the automatic kind can be switched off."""
import threading
import unittest
from unittest.mock import patch

import model_monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        model_monitor._log.clear()

    def test_sources_follow_the_thread(self):
        self.assertEqual(model_monitor.where("turn-abc"), ("Your reply", True))
        self.assertEqual(model_monitor.where("tool-web_read"), ("A tool in your reply", True))
        self.assertEqual(model_monitor.where("model-warm"), ("Loading ahead while you type", False))
        self.assertEqual(model_monitor.where("compare"), ("Compare", False))
        self.assertFalse(model_monitor.where("something-else")[1])

    def test_replies_are_not_listed_outside_ones_are(self):
        def in_thread(name, fn):
            t = threading.Thread(target=fn, name=name)
            t.start()
            t.join()
        in_thread("turn-1", lambda: model_monitor.record("m", 100, 20, 1.0))
        in_thread("compare", lambda: model_monitor.record("m", 300, 50, 2.0, label="Compare"))
        rows = model_monitor.recent()
        self.assertEqual([r["source"] for r in rows], ["Compare"])
        self.assertEqual(model_monitor.totals(), {"Compare": {"calls": 1, "tokens": 350}})
        self.assertEqual(len(model_monitor.recent(include_replies=True)), 2)

    def test_only_automatic_sources_can_be_blocked(self):
        on = {"model_replies_only": True}
        self.assertTrue(model_monitor.blocked("Loading ahead while you type", on))
        self.assertTrue(model_monitor.blocked("Proactive help", on))
        self.assertFalse(model_monitor.blocked("Compare", on))         # started by you: always runs
        self.assertFalse(model_monitor.blocked("Your reply", on))
        self.assertFalse(model_monitor.blocked("Proactive help", {}))

    def test_a_blocked_call_never_reaches_the_model(self):
        import agent.models as llm
        import core.state as shared
        hit = []
        with patch.dict(shared.app_state, settings={**(shared.app_state.get("settings") or {}), "model_replies_only": True}), \
                patch.object(llm.llm_stream, "complete", side_effect=lambda *a, **k: hit.append(1)), \
                patch.object(model_monitor, "where", return_value=("Proactive help", False)):
            with self.assertRaises(RuntimeError):
                llm._local_complete({"model": "openai/m", "messages": []})
        self.assertEqual(hit, [])

    def test_warm_up_respects_the_switch(self):
        import api.server as server
        import core.state as shared
        with patch.dict(shared.app_state, settings={**(shared.app_state.get("settings") or {}), "model_replies_only": True}):
            r = server.warm_local_model(server.WarmRequest(mode="local", model="m"))
        self.assertFalse(r["started"])
        self.assertIn("off", r["reason"])


if __name__ == "__main__":
    unittest.main()

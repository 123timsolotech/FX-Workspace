"""1.4.2: hidden in the tray and unused for the set minutes, the model leaves the GPU (idle_unload.py)."""
import unittest
from unittest.mock import patch

import core.config as config
import core.state as shared
import idle_unload
import llama_local
import local_runtime
import model_monitor


class IdleUnloadTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict(shared.app_state, settings={**(shared.app_state.get("settings") or {}), "idle_unload": "15"})
        p.start()
        self.addCleanup(p.stop)
        idle_unload._state.update(hidden_at=None, unloaded_at=None)
        self.addCleanup(idle_unload._state.update, hidden_at=None, unloaded_at=None)
        for target in (patch.object(local_runtime, "busy", return_value=False),
                       patch.object(model_monitor, "recent", return_value=[{"at": 1000.0}])):
            target.start()
            self.addCleanup(target.stop)

    def test_only_when_hidden_and_unused_long_enough(self):
        with patch.object(llama_local, "unload_all", return_value=["Qwen"]) as unload:
            self.assertEqual(idle_unload.check(2000.0), "window shown")
            with patch("time.time", return_value=2000.0):
                idle_unload.window(False)                          # hidden at 2000
            self.assertEqual(idle_unload.check(2000.0 + 14 * 60), "not idle long enough")
            self.assertEqual(idle_unload.check(2000.0 + 15 * 60), "unloaded")
            self.assertEqual(idle_unload.check(2000.0 + 20 * 60), "already unloaded")   # once, until the model is used
        unload.assert_called_once()
        idle_unload.window(True)
        self.assertEqual(idle_unload.check(9999.0), "window shown")

    def test_never_while_busy_or_switched_off(self):
        idle_unload._state["hidden_at"] = 0.0
        with patch.object(llama_local, "unload_all") as unload:
            with patch.object(local_runtime, "busy", return_value=True):
                self.assertEqual(idle_unload.check(10 ** 6), "model busy")
            shared.app_state["settings"]["idle_unload"] = "off"
            self.assertEqual(idle_unload.check(10 ** 6), "off")
        unload.assert_not_called()

    def test_settings(self):
        out = config.clean_privacy_settings({"idle_unload": "7", "start_with_windows": "yes"})
        self.assertEqual((out["idle_unload"], out["start_with_windows"], out["start_hidden"]), ("15", False, True))
        self.assertEqual(config.clean_privacy_settings({"idle_unload": "off"})["idle_unload"], "off")


if __name__ == "__main__":
    unittest.main()

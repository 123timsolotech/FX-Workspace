"""Tests for sys_tools.py. subprocess/psutil/socket are mocked - these are
exactly the OS boundaries this module shells out to (sc.exe, ping.exe,
setx) rather than reimplementing."""
import unittest
from unittest.mock import MagicMock, patch

import netlayer
import sys_tools


class DirectRoute(unittest.TestCase):
    """The direct route, whatever settings.json sets: through a proxy or Tor, ping and
    port checks are refused on purpose (they would go around the route - test_netlayer)."""

    def setUp(self):
        p = patch.object(netlayer, "_settings_fn", dict)
        p.start()
        self.addCleanup(p.stop)


def fake_run(returncode=0, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class ListServicesTests(unittest.TestCase):
    def test_parses_service_name_and_state(self):
        stdout = (
            "SERVICE_NAME: Spooler\n        TYPE: 110  WIN32\n        STATE: 4  RUNNING\n\n"
            "SERVICE_NAME: wuauserv\n        TYPE: 20\n        STATE: 1  STOPPED\n"
        )
        with patch.object(sys_tools, "IS_WINDOWS", True), \
             patch.object(sys_tools, "_run", return_value=fake_run(stdout=stdout)):
            result = sys_tools.list_services()
        self.assertTrue(result["ok"])
        self.assertIn("Spooler: RUNNING", result["message"])
        self.assertIn("wuauserv: STOPPED", result["message"])

    def test_filters_by_query(self):
        stdout = "SERVICE_NAME: Spooler\n        STATE: 4  RUNNING\n\nSERVICE_NAME: wuauserv\n        STATE: 1  STOPPED\n"
        with patch.object(sys_tools, "IS_WINDOWS", True), \
             patch.object(sys_tools, "_run", return_value=fake_run(stdout=stdout)):
            result = sys_tools.list_services("spool")
        self.assertIn("Spooler", result["message"])
        self.assertNotIn("wuauserv", result["message"])

    def test_non_windows_rejected(self):
        with patch.object(sys_tools, "IS_WINDOWS", False):
            result = sys_tools.list_services()
        self.assertFalse(result["ok"])


class ServiceControlTests(unittest.TestCase):
    def test_invalid_action_rejected(self):
        with patch.object(sys_tools, "IS_WINDOWS", True):
            result = sys_tools.service_control("Spooler", "explode")
        self.assertFalse(result["ok"])
        self.assertIn("start, stop, or restart", result["message"])

    def test_start_success(self):
        with patch.object(sys_tools, "IS_WINDOWS", True), \
             patch.object(sys_tools, "_run", return_value=fake_run(0, "SUCCESS")) as run:
            result = sys_tools.service_control("Spooler", "start")
        self.assertTrue(result["ok"])
        run.assert_called_once_with(["sc", "start", "Spooler"])

    def test_stop_already_stopped_counts_as_ok(self):
        with patch.object(sys_tools, "IS_WINDOWS", True), \
             patch.object(sys_tools, "_run", return_value=fake_run(1, "FAILED 1062:")):
            result = sys_tools.service_control("Spooler", "stop")
        self.assertTrue(result["ok"])

    def test_restart_calls_stop_then_start(self):
        with patch.object(sys_tools, "IS_WINDOWS", True), \
             patch.object(sys_tools, "_run", return_value=fake_run(0, "SUCCESS")) as run:
            result = sys_tools.service_control("Spooler", "restart")
        self.assertTrue(result["ok"])
        run.assert_any_call(["sc", "stop", "Spooler"])
        run.assert_any_call(["sc", "start", "Spooler"])


class KillProcessTests(unittest.TestCase):
    def test_no_psutil_reports_unavailable(self):
        with patch.object(sys_tools, "psutil", None):
            result = sys_tools.kill_process("notepad.exe")
        self.assertFalse(result["ok"])

    def test_kills_by_pid(self):
        fake_proc = MagicMock()
        fake_proc.name.return_value = "notepad.exe"
        fake_proc.pid = 1234
        fake_psutil = MagicMock()
        fake_psutil.pid_exists.return_value = True
        fake_psutil.Process.return_value = fake_proc
        with patch.object(sys_tools, "psutil", fake_psutil):
            result = sys_tools.kill_process("1234", force=True)
        self.assertTrue(result["ok"])
        fake_proc.kill.assert_called_once()
        fake_proc.terminate.assert_not_called()

    def test_terminates_by_name_without_force(self):
        fake_proc = MagicMock()
        fake_proc.name.return_value = "notepad.exe"
        fake_proc.pid = 1234
        fake_proc.info = {"name": "notepad.exe"}
        fake_psutil = MagicMock()
        fake_psutil.process_iter.return_value = [fake_proc]
        with patch.object(sys_tools, "psutil", fake_psutil):
            result = sys_tools.kill_process("notepad")
        self.assertTrue(result["ok"])
        fake_proc.terminate.assert_called_once()
        fake_proc.kill.assert_not_called()

    def test_no_match_is_reported(self):
        fake_psutil = MagicMock()
        fake_psutil.process_iter.return_value = []
        with patch.object(sys_tools, "psutil", fake_psutil):
            result = sys_tools.kill_process("nonexistent_process")
        self.assertFalse(result["ok"])


class PingHostTests(DirectRoute):
    def test_no_host_rejected(self):
        result = sys_tools.ping_host("")
        self.assertFalse(result["ok"])

    def test_successful_ping(self):
        with patch.object(sys_tools, "_run", return_value=fake_run(0, "4 packets transmitted, 4 received")) as run:
            result = sys_tools.ping_host("8.8.8.8", count=4)
        self.assertTrue(result["ok"])
        run.assert_called_once()

    def test_count_is_clamped(self):
        with patch.object(sys_tools, "_run", return_value=fake_run(0, "")) as run:
            sys_tools.ping_host("8.8.8.8", count=999)
        called_cmd = run.call_args[0][0]
        self.assertIn("10", called_cmd)   # clamped to the max of 10


class EnvVarTests(unittest.TestCase):
    def test_get_existing_var(self):
        with patch.dict(sys_tools.os.environ, {"MY_VAR": "hello"}):
            result = sys_tools.get_env_var("MY_VAR")
        self.assertTrue(result["ok"])
        self.assertIn("hello", result["message"])

    def test_get_missing_var(self):
        result = sys_tools.get_env_var("DEFINITELY_NOT_SET_XYZ")
        self.assertFalse(result["ok"])

    def test_set_var_process_only(self):
        result = sys_tools.set_env_var("MY_TEST_VAR", "value1", persist=False)
        self.assertTrue(result["ok"])
        self.assertEqual(sys_tools.os.environ.get("MY_TEST_VAR"), "value1")
        self.assertIn("not persisted", result["message"])

    def test_set_var_persist_calls_setx(self):
        with patch.object(sys_tools, "IS_WINDOWS", True), \
             patch.object(sys_tools, "_run", return_value=fake_run(0, "SUCCESS")) as run:
            result = sys_tools.set_env_var("MY_TEST_VAR2", "value2", persist=True)
        self.assertTrue(result["ok"])
        run.assert_called_once_with(["setx", "MY_TEST_VAR2", "value2"])


class PortCheckTests(DirectRoute):
    def test_open_port(self):
        with patch.object(sys_tools.socket, "create_connection") as create:
            create.return_value.__enter__.return_value = MagicMock()
            result = sys_tools.port_check("example.com", 443)
        self.assertTrue(result["ok"])

    def test_closed_port(self):
        with patch.object(sys_tools.socket, "create_connection", side_effect=OSError("refused")):
            result = sys_tools.port_check("example.com", 9999)
        self.assertFalse(result["ok"])

    def test_missing_args_rejected(self):
        result = sys_tools.port_check("", 80)
        self.assertFalse(result["ok"])


class SystemInfoTests(unittest.TestCase):
    def test_reports_cpu_and_ram(self):
        fake_psutil = MagicMock()
        fake_psutil.cpu_percent.return_value = 42.0
        fake_mem = MagicMock(percent=55.0, used=4 * 1024**3, total=8 * 1024**3)
        fake_psutil.virtual_memory.return_value = fake_mem
        fake_psutil.sensors_battery.return_value = None
        with patch.object(sys_tools, "psutil", fake_psutil):
            result = sys_tools.get_system_info()
        self.assertTrue(result["ok"])
        self.assertIn("CPU: 42%", result["message"])
        self.assertIn("RAM: 55%", result["message"])

    def test_no_psutil_reports_unavailable(self):
        with patch.object(sys_tools, "psutil", None):
            result = sys_tools.get_system_info()
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""1.4.2 Phase 6: a sandboxed chat - files in its scratch folder only, commands at low integrity in a job object."""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import core.state as shared
import sandbox
import tools.dispatch as dispatch


class SandboxTests(unittest.TestCase):
    def setUp(self):
        # Under the app's own data\sandbox, like the real one (some temp folders refuse integrity labels).
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sandbox")
        os.makedirs(base, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="test-", dir=base)
        self.addCleanup(shutil.rmtree, self.root, True)
        for target in (patch.object(sandbox, "ROOT", self.root),
                       patch.dict(shared.app_state, active_chat={"id": "abc123def456", "sandboxed": True})):
            target.start()
            self.addCleanup(target.stop)
        self.box = sandbox.folder()

    def test_relative_names_land_in_the_scratch_folder_and_outside_is_refused(self):
        args, why = sandbox.confine("write_file", {"filename": "notes.txt", "content": "x"})
        self.assertIsNone(why)
        self.assertEqual(args["filename"], os.path.join(self.box, "notes.txt"))
        _args, why = sandbox.confine("read_file", {"filename": os.path.expanduser("~/Desktop/secret.txt")})
        self.assertIn("sandboxed", why)
        _args, why = sandbox.confine("copy_path", {"source": "a.txt", "destination": os.path.join(self.box, "..", "x")})
        self.assertIn("outside", why)
        _args, why = sandbox.confine("click_at", {"x": 1, "y": 1})
        self.assertIn("screen", why)
        self.assertEqual(sandbox.confine("web_search", {"query": "q"}), ({"query": "q"}, None))

    def test_off_changes_nothing(self):
        shared.app_state["active_chat"]["sandboxed"] = False
        args = {"filename": "C:/anything.txt"}
        self.assertEqual(sandbox.confine("write_file", args), (args, None))
        self.assertIsNone(sandbox.boxed_popen(["cmd", "/c", "echo"], False, None))

    def test_the_tool_gate(self):
        res = dispatch.execute_tool("read_file", {"filename": os.path.join(tempfile.gettempdir(), "elsewhere.txt")})
        self.assertFalse(res["ok"])
        self.assertIn("sandboxed", res["message"])

    @unittest.skipUnless(os.name == "nt", "Windows integrity levels")
    def test_a_command_runs_low_and_can_only_write_the_scratch_folder(self):
        outside = os.path.join(tempfile.mkdtemp(prefix="fxoutside-"), "nope.txt")
        self.addCleanup(shutil.rmtree, os.path.dirname(outside), True)
        script = ("whoami /groups | Select-String 'Mandatory Label'; "
                  "Set-Content -Path inside.txt -Value ok; "
                  f"try {{ Set-Content -Path '{outside}' -Value bad -ErrorAction Stop; 'WROTE OUTSIDE' }} catch {{ 'DENIED OUTSIDE' }}")
        res = dispatch._run_powershell(script, 60)
        self.assertIn("Low Mandatory Level", res["message"])
        self.assertIn("DENIED OUTSIDE", res["message"])
        self.assertTrue(os.path.exists(os.path.join(self.box, "inside.txt")))
        self.assertFalse(os.path.exists(outside))

    def test_copy_out(self):
        with open(os.path.join(self.box, "result.txt"), "w") as f:
            f.write("r")
        dest = tempfile.mkdtemp(prefix="fxdest-")
        self.addCleanup(shutil.rmtree, dest, True)
        self.assertTrue(sandbox.copy_out("result.txt", dest)["ok"])
        self.assertTrue(os.path.exists(os.path.join(dest, "result.txt")))
        self.assertFalse(sandbox.copy_out("result.txt", dest)["ok"])                # never overwrites
        self.assertFalse(sandbox.copy_out("C:/Windows/win.ini", dest)["ok"])         # only from the scratch folder

    def test_both_always_ask_and_screen_tools_are_not_offered(self):
        import permissions
        import tools.schemas as schemas
        for tool in ("sandbox_copy_out", "windows_sandbox_run"):
            self.assertEqual(permissions.decide(tool, {"permission_mode": "god"}), "ask")
        self.assertTrue({"desktop", "clipboard"} <= schemas.session_off())

    def test_windows_sandbox_explains_when_off(self):
        f = os.path.join(self.box, "tool.ps1")
        open(f, "w").close()
        with patch.object(sandbox, "windows_sandbox_ready", return_value=False):
            r = sandbox.windows_sandbox_run(f)
        self.assertFalse(r["ok"])
        self.assertIn("Windows Sandbox", r["message"])


if __name__ == "__main__":
    unittest.main()

"""POSIX-only constructs are caught before approval (shell_lint.py), and the
PowerShell tool runs real Windows PowerShell with UTF-8 output."""
import os
import unittest

import shell_lint as sl
import tools.dispatch as dispatch
import tools.runtime as runtime


class LintTests(unittest.TestCase):
    def constructs(self, command, shell=sl.POWERSHELL):
        return [f["construct"] for f in sl.lint(command, shell)]

    def test_bashisms_are_caught_in_powershell(self):
        cases = {
            "rm -rf build && mkdir build": ["&& / ||", "rm -rf"],
            "printf '%s\\n' '<html>' > a.html": ["printf"],
            "export API_URL=http://x": ["export"],
            "cat <<EOF > a.txt\nhello\nEOF": ["heredoc (<<EOF)"],
            "touch notes.txt": ["touch"],
            "ls -la C:\\Users": ["ls -la"],
            "rm -f old.log": ["rm -f"],
            "Get-Process | grep chrome": ["grep"],
            "which python": ["which"],
            "sudo net stop spooler": ["sudo"],
            "npm test || echo failed": ["&& / ||"],
            "python run.py > /dev/null": ["/dev/null"],
            "cp a.txt /tmp/a.txt": ["POSIX path (/tmp, /usr, ...)"],
            "NODE_ENV=production node app.js": ["inline VAR=value command"],
            "#!/bin/bash\necho hi": ["shebang (#!/bin/...)"],
        }
        for command, expected in cases.items():
            got = self.constructs(command)
            for construct in expected:
                self.assertIn(construct, got, command)

    def test_valid_powershell_is_not_flagged(self):
        for command in (
            "Remove-Item -Recurse -Force 'C:\\build'",
            "rm C:\\temp\\old.txt",                                  # PowerShell's rm alias with a path is fine
            "Get-ChildItem -Force | Select-String -Pattern 'grep'",  # 'grep' only inside quotes
            "Write-Output \"use && and rm -rf in bash\"",            # quoted text is not a command
            "npm test; if ($?) { npm run build }",
            "$env:NODE_ENV = 'production'; node app.js",
            "New-Item -ItemType Directory -Force C:\\a\\b",
            "ls C:\\Users",
            "Get-Content log.txt -Tail 20",
            # Verified on Windows PowerShell 5.1: these work there.
            "mkdir -p C:\\a\\b",
            "rm -r C:\\old",
            # Only quoted text looks POSIX-like:
            "python -c \"print(1 << 3)\"",
            "Write-Output 'logs are in /var/log/app'",
            "$s = @'\ncout << x;\n'@",
        ):
            self.assertEqual(self.constructs(command), [], command)

    def test_cmd_rules(self):
        self.assertEqual(self.constructs("cd C:\\x && dir", sl.CMD), [])          # && is valid in cmd
        self.assertIn("rm", self.constructs("rm file.txt", sl.CMD))
        self.assertIn("cat", self.constructs("cat file.txt", sl.CMD))
        self.assertIn("$(...) substitution", self.constructs("echo $(date)", sl.CMD))
        self.assertIn("mkdir -p", self.constructs("mkdir -p a\\b", sl.CMD))     # cmd makes a folder named "-p"

    def test_hint_says_nothing_ran(self):
        msg = sl.hint_message("run_powershell", sl.lint("rm -rf x && echo y"), sl.POWERSHELL)
        self.assertTrue(msg.startswith("NOT RUN"))
        self.assertIn("Windows PowerShell 5.1", msg)
        self.assertIn("delete_path", msg)


@unittest.skipUnless(os.name == "nt", "Windows PowerShell")
class PowerShellToolTests(unittest.TestCase):
    def test_real_powershell_run_with_utf8_and_exit_code(self):
        res = dispatch._run_powershell("Write-Output 'héllo wörld'; $x = 2 + 3; Write-Output \"sum=$x\"")
        self.assertTrue(res["ok"], res["message"])
        self.assertIn("héllo wörld", res["message"])
        self.assertIn("sum=5", res["message"])
        bad = dispatch._run_powershell("exit 3")
        self.assertFalse(bad["ok"])
        self.assertIn("Exit Code: 3", bad["message"])

    def test_quotes_and_newlines_survive(self):
        res = dispatch._run_powershell("$s = @'\nline \"one\" 'two'\n'@\nWrite-Output $s")
        self.assertIn("line \"one\" 'two'", res["message"])

    def test_is_registered_as_not_undoable_and_high_risk(self):
        import permissions
        spec = runtime.TOOL_REGISTRY.get("run_powershell")
        self.assertFalse(spec.undoable)
        self.assertEqual(permissions.classify("run_powershell"), "destructive")


if __name__ == "__main__":
    unittest.main()

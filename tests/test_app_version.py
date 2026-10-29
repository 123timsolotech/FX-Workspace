"""app_version: the VERSION number and Settings > About > Check for updates (GitHub is faked)."""
import contextlib
import unittest
from unittest import mock

import app_version


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _client(resp):
    c = mock.MagicMock()
    c.get.return_value = resp

    @contextlib.contextmanager
    def make(*a, **k):
        yield c
    return make


class VersionTests(unittest.TestCase):
    def test_current_reads_the_version_file(self):
        self.assertRegex(app_version.current(), r"^\d+\.\d+\.\d+$")

    def test_newer_compares_numbers_not_text(self):
        self.assertTrue(app_version.newer("1.10.0", "1.9.3"))
        self.assertTrue(app_version.newer("v2.0", "1.99.99"))
        self.assertFalse(app_version.newer("1.3.0", "1.3.0"))
        self.assertFalse(app_version.newer("1.2.9-beta", "1.3.0"))

    def test_a_newer_release_links_its_installer(self):
        rel = {"tag_name": "v99.0.0", "html_url": "https://github.com/x/y/releases/tag/v99.0.0", "body": "notes",
               "assets": [{"name": "source.zip", "browser_download_url": "z"},
                          {"name": "FX-Workspace-Setup-99.0.0.exe", "browser_download_url": "https://example/setup.exe"}]}
        with mock.patch("netlayer.client", _client(_Resp(200, rel))):
            d = app_version.check(force=True)
        self.assertTrue(d["newer"])
        self.assertEqual(d["latest"], "99.0.0")
        self.assertEqual(d["installer"], "https://example/setup.exe")

    def test_no_release_yet_and_offline_are_said_plainly(self):
        with mock.patch("netlayer.client", _client(_Resp(404, {}))):
            self.assertIn("No release", app_version.check(force=True)["error"])
        def boom(*a, **k):
            raise OSError("offline")
        with mock.patch("netlayer.client", boom):
            self.assertIn("Couldn't reach GitHub", app_version.check(force=True)["error"])


class _Stream:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self, n):
        for i in range(0, len(self.data), n):
            yield self.data[i:i + n]


def _stream_client(data, status=200):
    c = mock.MagicMock()
    c.stream.return_value = _Stream(data, status)

    @contextlib.contextmanager
    def make(*a, **k):
        yield c
    return make


class UpdateNowTests(unittest.TestCase):
    """Settings > About > Update now (1.4.2): only this repo's installer, only with a matching checksum."""
    URL = app_version.DOWNLOADS + "v99.0.0/FX-Workspace-Setup-99.0.0.exe"

    def setUp(self):
        import hashlib
        import os
        import tempfile
        self.tmp = tempfile.mkdtemp(dir=os.path.join(os.path.dirname(app_version.__file__), "data"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.data = b"MZ fake installer" * 1000
        self.sha = hashlib.sha256(self.data).hexdigest()
        p = mock.patch.object(app_version, "UPDATES_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        app_version._job.clear()
        app_version._job.update(state="idle")

    def _info(self, **over):
        return {"latest": "99.0.0", "newer": True, "installer": self.URL, "sha256": self.sha,
                "size": len(self.data), "can_install": True, **over}

    def test_only_this_repos_release_downloads_with_a_checksum_can_install(self):
        rel = {"tag_name": "v99.0.0", "assets": [{"name": "FX-Workspace-Setup-99.0.0.exe", "browser_download_url": self.URL,
                                                  "digest": "sha256:" + self.sha, "size": 5}]}
        with mock.patch("netlayer.client", _client(_Resp(200, rel))), mock.patch.object(app_version.os, "name", "nt"):
            d = app_version.check(force=True)
        self.assertTrue(d["can_install"])
        self.assertEqual(d["sha256"], self.sha)
        rel["assets"][0].pop("digest")                                  # no published checksum: link only
        with mock.patch("netlayer.client", _client(_Resp(200, rel))), mock.patch.object(app_version.os, "name", "nt"):
            self.assertFalse(app_version.check(force=True)["can_install"])
        for url in ("https://evil.example/FX-Workspace-Setup.exe",
                    "https://github.com/someone/else/releases/download/v1/FX-Workspace-Setup.exe",
                    app_version.DOWNLOADS + "../../x/setup.exe"):
            self.assertFalse(app_version._ours(url), url)

    def test_a_matching_download_starts_the_installer_silently_and_detached(self):
        with mock.patch("netlayer.client", _stream_client(self.data)), \
                mock.patch.object(app_version.subprocess, "Popen") as popen:
            app_version._run(self._info())
        self.assertEqual(app_version.progress()["state"], "started")
        args = popen.call_args.args[0]
        self.assertEqual(args[:4], ["cmd.exe", "/c", "start", ""])     # not this app's child
        self.assertIn("/SILENT", args)
        self.assertIn("/FXRESTART=1", args)
        self.assertTrue(args[4].endswith("FX-Workspace-Setup-99.0.0.exe"))

    def test_a_download_that_doesnt_match_is_deleted_and_never_run(self):
        import os
        with mock.patch("netlayer.client", _stream_client(self.data + b"tampered")), \
                mock.patch.object(app_version.subprocess, "Popen") as popen:
            app_version._run(self._info())
        p = app_version.progress()
        self.assertEqual(p["state"], "error")
        self.assertIn("checksum", p["error"])
        popen.assert_not_called()
        self.assertEqual(os.listdir(self.tmp), [])

    def test_up_to_date_or_unsafe_says_why(self):
        with mock.patch.object(app_version, "check", return_value={"newer": False, "can_install": False}):
            self.assertIn("up to date", app_version.install()["error"])
        app_version._job.update(state="idle")
        with mock.patch.object(app_version, "check", return_value={"newer": True, "can_install": False}):
            self.assertIn("checksum", app_version.install()["error"])

    def test_spent_installers_are_cleaned_at_start(self):
        import os
        for name in ("FX-Workspace-Setup-1.0.0.exe", "FX-Workspace-Setup-1.0.0.exe.part", "keep.txt"):
            open(os.path.join(self.tmp, name), "wb").close()
        self.assertEqual(app_version.clean_downloads(), 2)
        self.assertEqual(os.listdir(self.tmp), ["keep.txt"])

    def test_a_phone_cannot_install_an_update_or_quit_the_app(self):
        import remote_access
        self.assertTrue(remote_access.blocked("POST", "/api/version/install"))
        self.assertTrue(remote_access.blocked("POST", "/api/shell/quit"))
        self.assertFalse(remote_access.blocked("GET", "/api/version/install"))


class SetupFilesTests(unittest.TestCase):
    """The setup wizard and the scripts beside it (1.4.2): no console windows, no browser, tidy updates."""

    @staticmethod
    def read(*parts):
        import os
        with open(os.path.join(os.path.dirname(app_version.__file__), *parts), encoding="utf-8") as f:
            return f.read()

    def test_the_wizard_runs_setup_hidden_and_reopens_after_update_now(self):
        iss = self.read("installer", "FXWorkspace.iss")
        self.assertIn("ExecAndLogOutput", iss)
        self.assertIn("SW_HIDE", iss)
        self.assertNotIn("Read-Host", iss)                              # no console waiting for Enter
        self.assertIn("-Update", iss)
        self.assertNotIn("*>&1", iss)                                   # PS 5.1 would stop at pip's first stderr line
        # The app reopens from code after setup: a [Run] entry without postinstall runs BEFORE ssPostInstall.
        self.assertIn("if ReopenAfterUpdate then", iss.split("procedure CurStepChanged", 1)[1])
        self.assertNotIn("Check: ReopenAfterUpdate", iss)
        self.assertIn("fx-requirements.txt", iss)                       # remembers 1.4.1's requirements

    def test_install_keeps_no_pip_cache_and_tidies_up(self):
        ps = self.read("install.ps1")
        self.assertIn("PIP_NO_CACHE_DIR", ps)
        self.assertIn("update_cleanup.py packages", ps)
        self.assertIn("update_cleanup.py tidy", ps)
        self.assertTrue(ps.rstrip().splitlines()[-1].startswith("exit 0"))
        self.assertIn("--no-cache-dir", self.read("run.bat"))

    def test_run_bat_opens_the_desktop_app_before_any_browser_window(self):
        bat = self.read("run.bat")
        self.assertLess(bat.index("desktop-app\\FX Workspace.exe"), bat.index("launcher.pyw"))
        self.assertLess(bat.index("desktop-app\\FX Workspace.exe"), bat.index("app_window.py"))

    def test_the_installer_asks_the_app_to_quit_and_leaves_tests_out(self):
        self.assertIn("/api/shell/quit", self.read("installer", "close-app.ps1"))
        self.assertIn("|tests/|", self.read("installer", "build.ps1").split("$skip =", 1)[1].splitlines()[0])


if __name__ == "__main__":
    unittest.main()

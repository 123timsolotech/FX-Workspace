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


if __name__ == "__main__":
    unittest.main()

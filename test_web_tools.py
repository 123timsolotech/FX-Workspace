"""Tests for web_tools.py. os.startfile/requests/desktop_input are mocked -
these are the OS/network boundaries this module deliberately does not
reinvent (it hands off to the registered browser handler and to
desktop_input's real keyboard primitives, same as generic_actions.py does
for elements.py)."""
import os
import unittest
from unittest.mock import MagicMock, patch

import httpx

import web_tools


class NormalizeUrlTests(unittest.TestCase):
    def test_adds_https_scheme_when_missing(self):
        self.assertEqual(web_tools._normalize_url("example.com"), "https://example.com")

    def test_leaves_existing_scheme_alone(self):
        self.assertEqual(web_tools._normalize_url("http://example.com"), "http://example.com")

    def test_empty_input_stays_empty(self):
        self.assertEqual(web_tools._normalize_url(""), "")

    def test_strips_whitespace(self):
        self.assertEqual(web_tools._normalize_url("  example.com  "), "https://example.com")


class OpenUrlTests(unittest.TestCase):
    def test_no_url_is_rejected(self):
        result = web_tools.open_url("")
        self.assertFalse(result["ok"])

    def test_calls_os_startfile_with_normalized_url(self):
        with patch.object(web_tools, "IS_WINDOWS", True), \
             patch.object(web_tools.os, "startfile", create=True) as startfile:
            result = web_tools.open_url("chess.com")
        self.assertTrue(result["ok"])
        startfile.assert_called_once_with("https://chess.com")

    def test_startfile_failure_is_reported(self):
        with patch.object(web_tools, "IS_WINDOWS", True), \
             patch.object(web_tools.os, "startfile", create=True, side_effect=OSError("no handler")):
            result = web_tools.open_url("chess.com")
        self.assertFalse(result["ok"])
        self.assertIn("no handler", result["message"])

    def test_non_windows_is_rejected(self):
        with patch.object(web_tools, "IS_WINDOWS", False):
            result = web_tools.open_url("chess.com")
        self.assertFalse(result["ok"])


class NewBrowserTabTests(unittest.TestCase):
    def test_no_matching_window_is_reported(self):
        with patch.object(web_tools, "IS_WINDOWS", True), \
             patch.object(web_tools.di, "find_window", return_value=None):
            result = web_tools.new_browser_tab("Chrome", "chess.com")
        self.assertFalse(result["ok"])
        self.assertIn("No open window", result["message"])

    def test_sends_ctrl_t_then_types_url_and_enter(self):
        with patch.object(web_tools, "IS_WINDOWS", True), \
             patch.object(web_tools.di, "find_window", return_value=(123, "Chess.com - Google Chrome")), \
             patch.object(web_tools.di, "focus_window", return_value=True), \
             patch.object(web_tools.di, "send_key_combo") as send_key, \
             patch.object(web_tools.di, "send_unicode_text") as send_text, \
             patch.object(web_tools.time, "sleep"):
            result = web_tools.new_browser_tab("Chrome", "chess.com")
        self.assertTrue(result["ok"])
        send_key.assert_any_call("ctrl+t")
        send_text.assert_called_once_with("https://chess.com")
        send_key.assert_any_call("enter")

    def test_blank_tab_when_no_url_given(self):
        with patch.object(web_tools, "IS_WINDOWS", True), \
             patch.object(web_tools.di, "find_window", return_value=(123, "Chrome")), \
             patch.object(web_tools.di, "focus_window", return_value=True), \
             patch.object(web_tools.di, "send_key_combo") as send_key, \
             patch.object(web_tools.di, "send_unicode_text") as send_text, \
             patch.object(web_tools.time, "sleep"):
            result = web_tools.new_browser_tab("Chrome")
        self.assertTrue(result["ok"])
        send_text.assert_not_called()
        self.assertEqual(send_key.call_count, 1)   # only Ctrl+T, no Enter

    def test_focus_failure_is_reported(self):
        with patch.object(web_tools, "IS_WINDOWS", True), \
             patch.object(web_tools.di, "find_window", return_value=(123, "Chrome")), \
             patch.object(web_tools.di, "focus_window", return_value=False):
            result = web_tools.new_browser_tab("Chrome")
        self.assertFalse(result["ok"])
        self.assertIn("could not focus", result["message"])


def _mock_client(handler):
    """Stands in for the network layer's client: (timeout) -> an httpx client on a mock transport."""
    return lambda timeout: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class DownloadFileTests(unittest.TestCase):
    def test_downloads_and_writes_content(self):
        with patch.object(web_tools, "_other_client", _mock_client(lambda r: httpx.Response(200, content=b"hello world"))), \
             patch("builtins.open", unittest.mock.mock_open()) as m_open, \
             patch.object(web_tools.os, "makedirs"):
            result = web_tools.download_file("example.com/file.bin", "/tmp/out.bin")
        self.assertTrue(result["ok"])
        self.assertIn("11 bytes", result["message"])
        m_open().write.assert_any_call(b"hello world")

    def test_no_url_is_rejected(self):
        result = web_tools.download_file("", "/tmp/out.bin")
        self.assertFalse(result["ok"])

    def test_request_exception_is_reported(self):
        def boom(request):
            raise httpx.ConnectTimeout("timeout", request=request)
        with patch.object(web_tools, "_other_client", _mock_client(boom)), patch.object(web_tools.os, "makedirs"):
            result = web_tools.download_file("example.com/file.bin", "/tmp/out.bin")
        self.assertFalse(result["ok"])
        self.assertIn("timeout", result["message"])

    def test_exceeding_max_bytes_aborts_and_removes_partial_file(self):
        with patch.object(web_tools, "_other_client", _mock_client(lambda r: httpx.Response(200, content=b"x" * 10))), \
             patch("builtins.open", unittest.mock.mock_open()), \
             patch.object(web_tools.os, "makedirs"), \
             patch.object(web_tools.os, "remove") as remove:
            result = web_tools.download_file("example.com/file.bin", "/tmp/out.bin", max_bytes=5)
        self.assertFalse(result["ok"])
        self.assertIn("limit", result["message"])
        remove.assert_called_once()


class GetUrlStatusTests(unittest.TestCase):
    def test_reachable_url_reports_status(self):
        with patch.object(web_tools, "_other_client", _mock_client(lambda r: httpx.Response(200))):
            result = web_tools.get_url_status("example.com")
        self.assertTrue(result["ok"])
        self.assertIn("200", result["message"])

    def test_4xx_is_not_ok_but_no_exception(self):
        with patch.object(web_tools, "_other_client", _mock_client(lambda r: httpx.Response(404))):
            result = web_tools.get_url_status("example.com/missing")
        self.assertFalse(result["ok"])
        self.assertIn("404", result["message"])

    def test_unreachable_host_is_reported(self):
        def boom(request):
            raise httpx.ConnectError("DNS failure", request=request)
        with patch.object(web_tools, "_other_client", _mock_client(boom)):
            result = web_tools.get_url_status("nonexistent.invalid")
        self.assertFalse(result["ok"])
        self.assertIn("DNS failure", result["message"])

    def test_head_rejected_falls_back_to_get(self):
        methods = []

        def handler(request):
            methods.append(request.method)
            return httpx.Response(405 if request.method == "HEAD" else 200)
        with patch.object(web_tools, "_other_client", _mock_client(handler)):
            result = web_tools.get_url_status("example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(methods, ["HEAD", "GET"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

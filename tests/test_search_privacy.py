"""Web Search privacy modes (search_engine.MODES): what each sends, the query
sanitising between the model and the network, Private requests without cookies,
Referer or tracking parameters, Maximum Privacy never falling back without
asking, Local Only making no network call at all (asserted at the socket and
HTTP-transport level), Disabled removing the tools, the query preview, opt-in
history and SearXNG's JSON API."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

import main  # noqa: F401  (wires the app together)
import netlayer
import privacy
import search_engine as se
import search_history
import sources
import web_tools

import core.state as shared
import tools.schemas as schemas


def _r(url, title="T", snippet="", date=""):
    return {"title": title, "url": url, "snippet": snippet, "date": date}


class ModeTest(unittest.TestCase):
    def setUp(self):
        se.reset()
        web_tools.reset_search_state()
        sources.start_turn()
        self.saved = {k: shared.app_state.get(k) for k in ("settings", "turn_search_mode", "active_project", "turn_policy",
                                                           "turn_tools")}
        self.addCleanup(lambda: shared.app_state.update(self.saved))
        # The user's own route, VPN rule and kill switch must never decide a test (settings.json may say Tor).
        self.settings = {**shared.app_state["settings"], **netlayer.DEFAULT_SETTINGS, "search_mode": "standard",
                         "search_preview": False, "search_history": False, "user_name": "Alexey", "searxng_url": ""}
        netlayer.set_session_direct(False)
        netlayer.clear_blocked()
        shared.app_state["settings"] = self.settings
        shared.app_state["turn_search_mode"] = None
        self.calls = []

    def fake(self, answers):
        def run(provider, query, n, recency, site, region, settings, keys, browser_fallback, should_cancel):
            self.calls.append((provider, query))
            got = answers.get(provider, ([], f"{provider} down"))
            if isinstance(got, Exception):
                raise got
            return got
        return patch.object(se, "_run_provider", side_effect=run)


class ChainAndSanitizeTests(ModeTest):
    def test_each_mode_uses_only_its_backends(self):
        keys = se.KeyStore(os.devnull + ".missing")
        with patch.object(keys, "get", side_effect=lambda p: "k" if p in ("brave", "tavily") else ""):
            s = {"searxng_url": "http://127.0.0.1:8888", "web_search_provider": "tavily"}
            self.assertEqual(se.chain(s, keys, "standard")[0], "tavily")
            self.assertEqual(se.chain(s, keys, "private"), ["searxng", "brave", "duckduckgo", "ddg_instant"])   # no Tavily
            self.assertEqual(se.chain(s, keys, "max"), ["searxng"])                                          # nothing else
            self.assertEqual(se.chain({}, keys, "max"), [])
            self.assertEqual(se.chain(s, keys, "local"), [])

    def test_personal_details_never_reach_the_query(self):
        q = (r"error in C:\Users\Alexey\Projects\Obby World\main.lua for alexey@example.com chat a1b2c3d4e5f6 "
             "call +44 7700 900123 see https://x.org/page?token=abc Obby World")
        clean, removed = se.sanitize_query(q, ["Alexey", "Obby World"])
        for leak in ("Users", "alexey@", "a1b2c3d4e5f6", "7700", "token=abc", "Obby", "Alexey"):
            self.assertNotIn(leak.lower(), clean.lower(), leak)
        self.assertIn("error in", clean)
        self.assertEqual(set(removed), {"a file path", "an e-mail address", "an id", "a long number", "link parameters",
                                        "a personal name or project name"})

    def test_private_search_sends_the_cleaned_query(self):
        self.settings["search_mode"] = "private"
        shared.app_state["active_project"] = {"id": "p1", "name": "Obby World", "folder": r"C:\Games\ObbyRepo"}
        with self.fake({"duckduckgo": ([_r("https://a.org/")], "")}):
            res = se.web_search_tool({"query": r"Alexey Obby World lava kill script C:\Games\ObbyRepo\x.lua"}, self.settings)
        self.assertTrue(res["ok"])
        sent = self.calls[0][1]
        self.assertEqual(sent, "lava kill script")
        web = res["event"]["web"]
        self.assertEqual(web["mode"], "Private")
        self.assertIn("a personal name or project name", web["removed"])
        self.assertTrue(web["route"])


class PrivateRequestTests(ModeTest):
    def setUp(self):
        super().setUp()
        p = patch.object(web_tools, "_public_addresses", return_value=["93.184.216.34"])   # site.example: a public host
        p.start()
        self.addCleanup(p.stop)

    def test_no_cookies_referer_or_tracking(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, headers={"set-cookie": "sid=abc; Path=/"}, text="<title>x</title>hello")
        with netlayer.private_requests(), \
                netlayer.client("search", direct=httpx.MockTransport(handler), headers=web_tools.BROWSER_HEADERS) as c:
            with patch.object(web_tools, "HTTP_CLIENT", c):
                web_tools.fetch("https://site.example/a?utm_source=x&id=7&fbclid=y",
                                headers={**web_tools.BROWSER_HEADERS, "Referer": "https://chat.example/"})
                web_tools.fetch("https://site.example/b")
        first, second = seen
        self.assertEqual(str(first.url), "https://site.example/a?id=7")          # tracking parameters removed
        for req in seen:
            self.assertNotIn("referer", req.headers)
            self.assertNotIn("cookie", req.headers)                              # the first answer's cookie is not kept
            self.assertEqual(req.headers["user-agent"], netlayer.GENERIC_UA)

    def test_standard_requests_are_unchanged(self):
        seen = []
        c = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, text="ok")))
        with patch.object(web_tools, "HTTP_CLIENT", c):
            web_tools.fetch("https://site.example/a?utm_source=x")
        self.assertIn("utm_source", str(seen[0].url))
        self.assertEqual(seen[0].headers["user-agent"], web_tools.USER_AGENT)


class MaxPrivacyTests(ModeTest):
    def setUp(self):
        super().setUp()
        self.settings.update(search_mode="max", searxng_url="http://127.0.0.1:8888")
        p = patch.object(se, "max_privacy_route_problem", return_value="")        # a route (Tor/proxy) is on
        p.start()
        self.addCleanup(p.stop)

    def test_needs_a_route(self):
        with patch.object(se, "max_privacy_route_problem", return_value="needs Tor"):
            res = se.web_search_tool({"query": "x"}, self.settings)
        self.assertEqual((res["ok"], res["message"]), (False, "needs Tor"))

    def test_never_falls_back_without_asking(self):
        answers = {"searxng": ([], "SearXNG failed (connection refused)"), "duckduckgo": ([_r("https://a.org/")], "")}
        with self.fake(answers), patch("prompt_prep.gate.request", return_value={"action": "cancel", "text": ""}) as ask:
            res = se.web_search_tool({"query": "lava"}, self.settings)
        self.assertEqual([c[0] for c in self.calls], ["searxng"])                 # nothing less private was tried
        self.assertFalse(res["ok"])
        self.assertEqual(ask.call_args[0][0], "search_fallback")
        self.calls.clear()
        with self.fake(answers), patch("prompt_prep.gate.request", return_value={"action": "send", "text": ""}):
            res = se.web_search_tool({"query": "lava"}, self.settings)
        self.assertEqual([c[0] for c in self.calls], ["searxng", "duckduckgo"])   # only after the yes, this search only
        self.assertTrue(res["ok"])
        self.assertIn("allowed this one search", res["message"])

    def test_a_dead_route_asks_before_going_direct(self):
        blocked = netlayer.RouteBlocked("Tor isn't reachable (kill switch)", kind="kill", component="search")
        answers = {"searxng": blocked}
        with self.fake(answers), patch("prompt_prep.gate.request", return_value={"action": "cancel"}) as ask:
            res = se.web_search_tool({"query": "lava"}, self.settings)
        self.assertFalse(res["ok"])
        self.assertIn("DIRECT", ask.call_args[0][1]["text"])
        self.assertEqual(res["event"]["web"]["blocked"]["kind"], "kill")

    def test_no_cache_and_no_history(self):
        self.settings["search_history"] = True
        path = os.path.join(tempfile.mkdtemp(), "h.json")
        with patch.object(search_history, "PATH", path), self.fake({"searxng": ([_r("https://a.org/")], "")}):
            se.web_search_tool({"query": "lava"}, self.settings)
            se.web_search_tool({"query": "lava"}, self.settings)
        self.assertEqual(len(self.calls), 2)                                      # not served from a cache
        self.assertFalse(os.path.exists(path))


class LocalOnlyTests(ModeTest):
    def test_zero_network_calls_at_the_socket_and_transport_level(self):
        self.settings["search_mode"] = "local"
        web_tools._page_cache["https://docs.example/luau"] = (__import__("time").monotonic(), {
            "page": {"title": "Luau task library", "text": "task.wait yields the thread. task.spawn runs now.", "links": []},
            "final": "https://docs.example/luau", "note": ""})
        was = netlayer.installed()
        netlayer.install()
        if not was:
            self.addCleanup(netlayer.uninstall)
        sockets, transports = [], []
        real_connect = netlayer._orig["connect"]
        with patch.dict(netlayer._orig, connect=lambda s, a: sockets.append(a) or real_connect(s, a)), \
                patch.object(httpx.HTTPTransport, "handle_request", side_effect=lambda *a: transports.append(a)):
            res = se.web_search_tool({"query": "luau task wait"}, self.settings)
            page = se.fetch_page_tool({"urls": ["https://docs.example/luau", "https://never-read.example/"]})
            with se.scope("local"):
                read = web_tools.web_read("https://never-read.example/")
        self.assertEqual([a for a in sockets if not netlayer.is_local(str(a[0]))], [])
        self.assertEqual(transports, [])
        self.assertTrue(res["ok"])
        self.assertIn("Luau task library", res["message"])
        self.assertIn("not the live web", res["message"])
        self.assertIn("task.wait yields", page["message"])
        self.assertIn("doesn't go online", page["message"])
        self.assertFalse(read["ok"])

    def test_the_scope_refuses_even_a_direct_attempt(self):
        with se.scope("local"), netlayer.client("search", direct=httpx.MockTransport(lambda r: self.fail("sent"))) as c:
            with self.assertRaises(netlayer.RouteBlocked):
                c.get("https://example.com/")


class DisabledTests(ModeTest):
    def test_the_tools_are_removed(self):
        shared.app_state["turn_policy"] = {"mode": "action"}
        saved = {k: shared.app_state.get(k) for k in ("current_user_request", "chat_skills", "turn_skills")}
        self.addCleanup(lambda: shared.app_state.update(saved))
        # A request that wants the web (the web skill, skills.py), in a fresh chat.
        shared.app_state.update(current_user_request="search the web for llama.cpp releases", chat_skills=[], turn_skills=None)
        names = lambda: {t["function"]["name"] for t in schemas.tools_for_current_turn(local=False)}   # noqa: E731
        self.assertIn("web_search", names())
        self.settings["search_mode"] = "disabled"
        self.assertTrue(names().isdisjoint({"web_search", "fetch_page", "web_read", "browser_open", "browser_read"}))
        self.assertFalse(schemas.tool_allowed_current_turn("web_search"))
        self.assertFalse(se.web_search_tool({"query": "x"}, self.settings)["ok"])
        self.settings["search_mode"] = "local"                                   # Local Only: no browser, local search
        self.assertIn("web_search", names())
        self.assertNotIn("browser_open", names())

    def test_a_per_message_override_wins(self):
        self.settings["search_mode"] = "standard"
        shared.app_state["turn_search_mode"] = "disabled"
        self.assertEqual(se.turn_mode(self.settings), "disabled")


class PreviewAndHistoryTests(ModeTest):
    def test_the_query_can_be_edited_or_cancelled_before_it_is_sent(self):
        self.settings["search_preview"] = True
        with self.fake({"duckduckgo": ([_r("https://a.org/")], "")}), \
                patch("prompt_prep.gate.request", return_value={"action": "send", "text": "edited words"}) as ask:
            se.web_search_tool({"query": "original words"}, self.settings)
        self.assertEqual(ask.call_args[0][0], "search_preview")
        self.assertEqual(self.calls[0][1], "edited words")
        self.calls.clear()
        with self.fake({"duckduckgo": ([_r("https://a.org/")], "")}), \
                patch("prompt_prep.gate.request", return_value={"action": "cancel", "text": ""}):
            res = se.web_search_tool({"query": "x"}, self.settings)
        self.assertEqual(self.calls, [])
        self.assertTrue(res["cancelled"])

    def test_history_is_opt_in_and_never_from_a_temporary_chat(self):
        path = os.path.join(tempfile.mkdtemp(), "h.json")
        with patch.object(search_history, "PATH", path), self.fake({"duckduckgo": ([_r("https://a.org/", "A")], "")}):
            se.web_search_tool({"query": "one"}, self.settings)
            self.assertFalse(os.path.exists(path))                                # off by default
            self.settings["search_history"] = True
            se.reset()
            se.web_search_tool({"query": "two"}, self.settings)
            with privacy.scope("tmp-abcdefabcdef"):
                se.web_search_tool({"query": "temporary canary"}, self.settings)
            saved = search_history.load()
            self.assertEqual([s["q"] for s in saved["searches"]], ["two"])
            self.assertEqual(search_history.clear()["searches"], 1)
            self.assertFalse(os.path.exists(path))


class SearxngTests(unittest.TestCase):
    def test_json_api_and_json_turned_off(self):
        ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [
            {"title": "Luau", "url": "https://luau.org/", "content": "A language"}]})))
        got = se.searxng_request("http://127.0.0.1:8888", "luau", client=ok)
        self.assertEqual(got[0]["url"], "https://luau.org/")
        seen = []
        forbidden = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(403)))
        with self.assertRaises(se.ProviderError) as cm:
            se.searxng_request("http://127.0.0.1:8888", "luau", client=forbidden)
        self.assertIn("formats: [html, json]", str(cm.exception))
        self.assertEqual(seen[0].url.params["format"], "json")                   # its JSON API, not the page
        html = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>results</html>")))
        with self.assertRaises(se.ProviderError) as cm:
            se.searxng_request("http://127.0.0.1:8888", "luau", client=html)
        self.assertIn("JSON output is probably turned off", str(cm.exception))


class SavedRowTests(unittest.TestCase):
    def test_the_saved_search_row_keeps_mode_backend_and_route(self):
        import agent.sessions as sessions
        web = {"op": "search", "queries": ["luau"], "providers": ["This PC"], "mode": "Local Only", "route": "This PC (no network)"}
        w = sessions.web_of([{"kind": "tool", "web": web}])
        self.assertEqual(w["via"], {"mode": "Local Only", "providers": ["This PC"], "route": "This PC (no network)"})
        self.assertNotIn("via", sessions.web_of([{"kind": "tool", "web": {"op": "read", "pages": [{"url": "https://a.example"}]}}]))


if __name__ == "__main__":
    unittest.main()

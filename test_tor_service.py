"""The built-in Tor (1.4.4): tor_service and netlayer's route through it. Nothing here starts the real Tor - its
live check is LiveTorTests, which runs only with FX_LIVE_TOR=1."""
import os
import unittest
from unittest.mock import patch

import netlayer
import tor_service

FILES = {"base": "C:/x", "exe": "C:/x/tor/tor.exe", "geoip": "C:\\x\\data\\geoip", "geoip6": "C:\\x\\data\\geoip6",
         "lyrebird": "C:\\x\\tor\\pluggable_transports\\lyrebird.exe", "pt_config": "C:/nope/pt_config.json", "version": "15.0.26"}
OBFS4 = "obfs4 37.218.245.14:38224 D9A82D2F9C2F65A18407B1D2B764F130847F8B5D cert=bjRaMrr1BRiAW8IE9U5z27fQaYgOhX1UCmOpg2pFpoMvo6ZgQMzLsaTzzQNTlm7hNcb+Sg iat-mode=0"


class TorrcTests(unittest.TestCase):
    def test_own_ports_isolation_and_owner(self):
        rc = tor_service.torrc(FILES, 51001, 51002)
        self.assertIn("SocksPort 127.0.0.1:51001 IsolateSOCKSAuth", rc)
        self.assertIn("ControlPort 127.0.0.1:51002", rc)
        self.assertIn("CookieAuthentication 1", rc)
        self.assertIn(f"__OwningControllerProcess {os.getpid()}", rc)            # Tor exits with the app
        self.assertIn("ClientOnly 1", rc)
        self.assertNotIn("UseBridges", rc)
        self.assertNotIn("9050", rc + "") and self.assertNotIn("9150", rc)

    def test_bridges(self):
        rc = tor_service.torrc(FILES, 1, 2, "obfs4", f"Bridge {OBFS4}\nnot a bridge\n")
        self.assertIn("UseBridges 1", rc)
        self.assertIn(f"Bridge {OBFS4}", rc)
        self.assertIn("ClientTransportPlugin obfs4 exec", rc)
        self.assertNotIn("not a bridge", rc)
        self.assertEqual(tor_service.clean_bridge_lines("obfs4 nonsense\nhello"), [])

    def test_the_shipped_bundle_is_found(self):
        files = tor_service.find()
        if files is None:
            self.skipTest("no Tor bundle in installer/cache (run installer/build.ps1 once)")
        self.assertTrue(os.path.isfile(files["exe"]))
        self.assertTrue(os.path.isfile(files["geoip"]))
        self.assertEqual(files["version"], open("installer/tor.version").read().strip())


class BuildTests(unittest.TestCase):
    def test_the_build_verifies_before_staging(self):
        ps = open("installer/build.ps1", encoding="utf-8").read()
        self.assertLess(ps.index("verify_tor.py"), ps.index("tar -xzf $tgz"))
        self.assertIn("throw \"The Tor bundle's signature did not verify", ps)
        self.assertIn("EF6E286DDA85EA2A4BA7DE684E2C6E8793298290", open("installer/verify_tor.py").read())
        self.assertIn("installer/cache/", open(".gitignore").read())


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"net_route": "direct"}
        saved = netlayer._settings_fn
        netlayer.configure(lambda: self.settings)
        self.addCleanup(netlayer.configure, saved)
        netlayer.set_session_direct(False)
        netlayer.clear_blocked()
        self.addCleanup(netlayer.clear_blocked)
        for name, value in (("available", True), ("ensure", "socks5h://127.0.0.1:51001")):
            p = patch.object(tor_service, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_search_through_the_builtin_tor_with_a_circuit_per_chat(self):
        self.settings = {"net_route": "direct", "net_components": {"search": "tor"}}
        with patch.object(netlayer, "_isolation_fn", lambda: "chat 42!"):
            r = netlayer.route_for("search")
        self.assertEqual((r.kind, r.label), ("tor", "Tor (built in)"))
        self.assertEqual(r.proxy, "socks5h://fx-chat42:x@127.0.0.1:51001")
        self.assertEqual(r.endpoint(), ("127.0.0.1", 51001))
        self.assertEqual(netlayer.route_for("models").kind, "direct")             # only search goes through Tor
        self.assertTrue(netlayer.uses_builtin_tor())
        self.settings = {"net_route": "direct"}
        self.assertFalse(netlayer.uses_builtin_tor())                              # the watchdog then stops it

    def test_external_tor_still_works(self):
        self.settings = {"net_route": "tor", "net_tor_source": "external"}
        with patch.object(netlayer, "probe", return_value=False):
            self.assertNotEqual(netlayer.route_for("search").label, "Tor (built in)")

    def test_never_direct_while_tor_connects(self):
        self.settings = {"net_route": "tor"}
        route = netlayer.route_for("search")
        with patch.object(tor_service, "wait_ready", return_value=False), \
                patch.object(tor_service, "status", return_value={"state": "connecting", "progress": 63, "summary": "Loading relay descriptors", "error": ""}):
            with self.assertRaises(netlayer.RouteBlocked) as e:
                netlayer.check(route, "search", "duckduckgo.com")
        self.assertIn("63%", str(e.exception))
        self.assertIn("instead of going direct", str(e.exception))
        with patch.object(tor_service, "wait_ready", return_value=True):
            netlayer.check(route, "search", "duckduckgo.com")                     # connected: goes out

    def test_settings_are_cleaned(self):
        c = netlayer.clean_settings({"net_tor_source": "weird", "net_tor_bridges": "meek", "net_tor_bridge_lines": f"{OBFS4}\njunk"})
        self.assertEqual((c["net_tor_source"], c["net_tor_bridges"], c["net_tor_bridge_lines"]), ("builtin", "off", OBFS4))


class SearchingThroughTorTests(RouteTests):
    def test_onion_addresses_only_through_tor(self):
        import web_tools
        self.settings = {"net_route": "direct"}
        with self.assertRaises(web_tools.BlockedAddress):
            web_tools._check_public(f"https://{web_tools.DDG_ONION}/html/")
        with self.assertRaises(netlayer.RouteBlocked):
            netlayer.check(netlayer.route_for("search"), "search", "example.onion")
        self.settings = {"net_route": "direct", "net_components": {"search": "tor"}}
        self.assertTrue(web_tools._check_public(f"https://{web_tools.DDG_ONION}/html/"))

    def test_duckduckgo_through_its_onion_service(self):
        import web_tools
        self.settings = {"net_route": "direct", "net_components": {"search": "tor"}}
        web_tools.reset_search_state()
        seen = {}

        def fake_fetch(url, **kw):
            seen["url"] = url
            class R:
                status_code, headers, url = 200, {}, "x"
            return R(), b'<a class="result__a" href="https://example.com/">Example</a>', url
        with patch.object(web_tools, "fetch", fake_fetch):
            web_tools._duckduckgo("llama.cpp")
        self.assertEqual(seen["url"], web_tools.DDG_ONION_URL)
        self.assertEqual(web_tools._unwrap_ddg(f"https://{web_tools.DDG_ONION}/l/?uddg=https%3A%2F%2Fa.org%2F"), "https://a.org/")

    def test_a_refusing_exit_gets_one_new_circuit(self):
        import httpx
        self.settings = {"net_route": "tor"}
        used = []

        class Inner(httpx.BaseTransport):
            def __init__(self, proxy):
                self.proxy = proxy

            def handle_request(self, request):
                used.append(self.proxy)
                return httpx.Response(403 if len(used) == 1 else 200, content=b"ok")
        t = netlayer.GuardedTransport("search")
        with patch.object(tor_service, "wait_ready", return_value=True), \
                patch.object(netlayer.GuardedTransport, "_inner", lambda self, route: Inner(route.proxy)):
            resp = t.handle_request(httpx.Request("GET", "https://example.com/"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(used), 2)
        self.assertNotEqual(used[0], used[1])                   # another SOCKS username: another circuit


@unittest.skipUnless(os.environ.get("FX_LIVE_TOR") == "1", "set FX_LIVE_TOR=1 to start the real Tor")
class LiveTorTests(unittest.TestCase):
    def test_a_search_goes_through_tor(self):
        settings = {"net_route": "direct", "net_components": {"search": "tor"}}
        saved = netlayer._settings_fn
        netlayer.configure(lambda: settings)
        try:
            with netlayer.client("search", timeout=60) as c:
                body = c.get("https://check.torproject.org/api/ip").json()
            self.assertTrue(body["IsTor"], body)
        finally:
            netlayer.configure(saved)
            tor_service.stop()


if __name__ == "__main__":
    unittest.main()

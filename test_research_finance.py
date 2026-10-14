"""Research depth tiers and the scalable ledger (agent/research.py), and the finance data and
evidence classes (finance.py). No network and no model: both are replaced by fakes."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import main  # noqa: F401  (wires the app together)
import agent.research as research
import core.config as config
import core.state as shared
import finance
import privacy
import search_engine
import sources
import tools.schemas as schemas


class EvidenceTests(unittest.TestCase):
    def test_classes(self):
        cases = {"https://www.sec.gov/Archives/x": "FACT", "https://home.treasury.gov/a": "FACT", "https://ir.nvidia.com/": "FACT",
                 "https://www.reuters.com/markets/x": "REPORTED", "https://seekingalpha.com/article/1": "OPINION",
                 "https://www.reddit.com/r/stocks/x": "SPECULATION", "https://www.prnewswire.com/news/x": "PROMOTIONAL",
                 "https://www.tipranks.com/stocks/nvda/forecast": "ESTIMATE", "https://www.morningstar.com/news/x": "REPORTED",
                 "https://www.morningstar.com/stocks/x": "MARKET", "https://some-blog.example/post": "OPINION",
                 "https://data.census.gov/x": "FACT"}
        for url, kind in cases.items():
            self.assertEqual(finance.classify(url), kind, url)

    def test_headlines_that_sell_a_view_are_not_news(self):
        self.assertEqual(finance.classify("https://finance.yahoo.com/news/a", "3 Stocks to Buy Before They Soar"), "OPINION")
        self.assertEqual(finance.classify("https://www.reuters.com/a", "Analysts raise price target to $300"), "ESTIMATE")
        self.assertEqual(finance.classify("https://www.reuters.com/a", "Nvidia reports record revenue"), "REPORTED")

    def test_weights_rank_facts_first(self):
        order = sorted(finance.EVIDENCE, key=lambda k: -finance.WEIGHT[k])
        self.assertEqual(order[0], "FACT")
        self.assertEqual(order[-1], "SPECULATION")


class NumbersTests(unittest.TestCase):
    def test_price_stats(self):
        close = [100.0] * 100 + [50.0] * 50 + [120.0] * 150        # a 50% drop, then a new high
        px = {"close": close, "dates": [f"d{i}" for i in range(300)], "interval": "1d"}
        st = finance.price_stats(px)
        self.assertAlmostEqual(st["max drawdown"], -0.5)
        self.assertEqual(st["last close"], 120.0)
        self.assertAlmostEqual(st["return 1 year"], 120 / 100 - 1)                # 252 trading days back: day 47
        self.assertEqual(st["52-week high"], 120.0)
        self.assertIn("200-day average", st)

    def test_annual_uses_full_years_and_the_newest_tag(self):
        facts = {"facts": {"us-gaap": {
            "OldCapexTag": {"units": {"USD": [{"form": "10-K", "fp": "FY", "start": "2011-01-01", "end": "2011-12-31", "val": 1, "filed": "2012-02-01"}]}},
            "NewCapexTag": {"units": {"USD": [
                {"form": "10-K", "fp": "FY", "start": "2024-01-01", "end": "2024-12-31", "val": 10, "filed": "2025-02-01"},
                {"form": "10-K", "fp": "FY", "start": "2024-10-01", "end": "2024-12-31", "val": 3, "filed": "2025-02-01"},   # a quarter inside the 10-K
                {"form": "10-Q", "fp": "Q1", "start": "2025-01-01", "end": "2025-03-31", "val": 4, "filed": "2025-05-01"}]}}}}}
        rows = finance._annual(facts, ("OldCapexTag", "NewCapexTag"), "USD")
        self.assertEqual(rows, [(2024, 10, "2025-02-01", "10-K")])

    def test_market_data_needs_the_network_mode(self):
        with patch.object(search_engine, "turn_mode", return_value="local"):
            res = finance.market_data_tool({"action": "company", "symbol": "NVDA"})
        self.assertFalse(res["ok"])
        self.assertIn("Local Only", res["message"])

    def test_the_tool_is_offered_only_where_search_is(self):
        with patch.object(search_engine, "turn_mode", return_value="disabled"):
            self.assertIn("market_data", schemas.search_mode_hidden())
        with patch.object(search_engine, "turn_mode", return_value="local"):
            self.assertIn("market_data", schemas.search_mode_hidden())
        self.assertIn("market_data", schemas.READ_ONLY_TOOLS)


class SettingsTests(unittest.TestCase):
    def test_depth_and_sec_contact(self):
        out = config.clean_privacy_settings({"research_depth": "ultra", "sec_contact": "Jane Doe jane@example.com"})
        self.assertEqual((out["research_depth"], out["sec_contact"]), ("ultra", "Jane Doe jane@example.com"))
        out = config.clean_privacy_settings({"research_depth": "infinite", "sec_contact": "not an email\nX-Evil: 1"})
        self.assertEqual((out["research_depth"], out["sec_contact"]), ("auto", ""))

    def test_budgets(self):
        self.assertEqual(research.budget("normal", "ultra")["target"], 350)
        self.assertEqual(research.budget("normal", "quick")["target"], 8)
        self.assertEqual(research.budget("max")["target"], 40)                 # auto follows the effort
        self.assertTrue(research.wants_resume("resume"))
        self.assertTrue(research.wants_resume("Continue the research"))
        self.assertFalse(research.wants_resume("resume writing my essay about X"))


class RegistryLimitTests(unittest.TestCase):
    def test_research_raises_the_limit_but_never_past_three_digits(self):
        reg = sources.Registry()
        self.assertEqual(sum(1 for i in range(100) if reg.add(f"https://a.example/{i}")), sources.MAX_SOURCES)
        reg = sources.Registry()
        reg.limit = 5000
        self.assertEqual(sum(1 for i in range(1200) if reg.add(f"https://a.example/{i}")), sources.CITE_MAX)

    def test_research_searches_do_not_use_numbers(self):
        import inspect
        self.assertIn('"unnumbered": True', inspect.getsource(research.Research.search))
        self.assertIn('args.get("unnumbered")', inspect.getsource(search_engine.web_search_tool))


class NoveltyTests(unittest.TestCase):
    def test_repeats_are_not_new(self):
        seen = [research.signature("- [FACT] Revenue rose 65% to $216B in fiscal 2026 [3]")]
        self.assertFalse(research.is_new(research.signature("- Revenue rose 65% to $216B in fiscal 2026 [7, 9]"), seen))
        self.assertTrue(research.is_new(research.signature("- Data-center sales were 88% of revenue [4]"), seen))


def _fake_search(results_by_call):
    it = iter(results_by_call)

    def run(args, settings, **kw):
        rows = next(it, [])
        return {"ok": True, "data": {"results": rows}}
    return run


class GatherTests(unittest.TestCase):
    """The ledger: diversity per site, saturation, consolidation - with fake search, reads and model."""

    def setUp(self):
        sources.start_turn()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(research, "LEDGER_DIR", self.tmp.name),
                  patch.object(research.event_log, "emit_event", lambda *a, **k: {"research": {}}),
                  patch.object(research.event_log, "update_event", lambda *a, **k: None),
                  patch.object(research.event_log, "finish_event", lambda *a, **k: None),
                  patch.object(research.event_log, "emit_activity", lambda *a, **k: None),
                  patch.object(research.run_control, "checkpoint", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def _read(self, calls):
        def fetch(args, **kw):
            calls.append(list(args["urls"]))
            pages = [{"n": sources.current().add(u, title=u, read=True), "url": u, "ok": True} for u in args["urls"]]
            return {"ok": True, "message": "\n".join(f"[{p['n']}] text of {p['url']}" for p in pages), "event": {"web": {"pages": pages}}}
        return fetch

    def test_per_site_cap_and_saturation(self):
        r = research.Research("q", "normal", {}, depth="deep")
        r.b.update(target=40, rounds=6, per_round=3, per_domain=2)
        many = [{"url": f"https://same.example/{i}", "title": "t"} for i in range(10)] + \
               [{"url": f"https://other{i}.example/", "title": "t"} for i in range(10)]
        calls, notes = [], iter(["- [FACT] a new fact one about widgets\n- another fact about gadgets\nFOLLOW-UP: more"]
                                + ["- a new fact one about widgets [9]\nFOLLOW-UP: more"] * 10)
        with patch.object(search_engine, "web_search_tool", _fake_search([many] * 10)), \
                patch.object(search_engine, "fetch_page_tool", self._read(calls)), \
                patch.object(search_engine, "low_quality", lambda r: False), \
                patch.object(research.llm, "model_text", lambda *a, **k: {"text": next(notes)}):
            r.gather([{"q": "sub-question one", "search": ["x"]}])
        read = [u for c in calls for u in c]
        self.assertLessEqual(sum(1 for u in read if "same.example" in u), 2)       # at most 2 pages from one site
        self.assertEqual(len(calls), 3)            # round 1 new; rounds 2 and 3 repeat what's known: saturated, stops
        self.assertEqual(len(research.bullets(r.notes[0]["text"])), 2)
        with open(os.path.join(self.tmp.name, f"{r.task_id}.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["done"], ["sub-question one"])

    def test_consolidation_keeps_citations(self):
        r = research.Research("q", "normal", {}, depth="ultra")
        long_notes = "\n".join(f"- fact number {i} about topic {i * 7} [{i}]" for i in range(1, 400))
        with patch.object(research.llm, "model_text", lambda *a, **k: {"text": "- merged but lost the sources"}):
            self.assertEqual(r.consolidate("q", long_notes), long_notes)            # a lossy merge is refused
        kept = "\n".join(f"- merged facts [{i}]" for i in range(1, 400))
        with patch.object(research.llm, "model_text", lambda *a, **k: {"text": kept}):
            self.assertEqual(r.consolidate("q", long_notes), kept)

    def test_resume_restores_the_same_source_numbers(self):
        r = research.Research("big question", "normal", {}, depth="ultra")
        r.title, r.questions = "T", [{"q": "a", "search": []}, {"q": "b", "search": []}]
        sources.current().add("https://one.example/", title="one")
        n2 = sources.current().add("https://two.example/", title="two")
        r.notes = [{"q": "a", "text": f"- fact [{n2}]"}]
        r.done = {"a"}
        r.save("stopped")
        sources.start_turn()                       # a new turn: a fresh registry
        back = research.Research.load_unfinished({})
        self.assertIsNotNone(back)
        self.assertEqual((back.task_id, back.done, back.depth), (r.task_id, {"a"}, "ultra"))
        self.assertEqual(sources.current().find("https://two.example/"), n2)       # [n] still points at the same page

    def test_a_temporary_chat_saves_nothing(self):
        r = research.Research("q", "normal", {}, depth="quick")
        with privacy.scope(privacy.new_temp_id()):
            r.save()
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_large_reports_are_written_by_section(self):
        r = research.Research("q", "normal", {}, depth="ultra")
        r.notes = [{"q": f"topic {i}", "text": "- x [1]\n" * 1200} for i in range(3)]
        calls = []

        def fake(system, user, **kw):
            calls.append(kw.get("label"))
            if system.startswith("You finish"):
                return {"text": "# Title\nSummary.\n## Key findings\n- k [1]\n===SECTIONS===\n## Open questions\n- none"}
            return {"text": "## A section\nbody [1]"}
        with patch.object(research.llm, "model_text", fake):
            report = r.write("Title")
        self.assertEqual(calls, ["Writing section 1 of 3", "Writing section 2 of 3", "Writing section 3 of 3", "Writing the summary"])
        self.assertTrue(report.startswith("# Title"))
        self.assertLess(report.index("## A section"), report.index("## Open questions"))


class FinanceModeTests(unittest.TestCase):
    def test_finance_research_is_both_sided_and_labelled(self):
        r = research.Research("Is NVDA overvalued?", "normal", {}, finance_mode=True)
        self.assertTrue(r.bias)
        self.assertIn("Bull case", research.FINANCE_REPORT)
        self.assertIn("not financial advice", research.FINANCE_REPORT)
        self.assertIn("[ESTIMATE]", research.EVIDENCE_NOTES)

    def test_invest_and_market_are_composer_tools(self):
        import composer_tools
        self.assertIn("invest", composer_tools.IDS)
        self.assertIn("market_data", composer_tools.OFFERS["market"])
        from agent.tasks import SLASH_COMMANDS
        self.assertEqual(SLASH_COMMANDS["invest"]["tools"], ["invest"])


if __name__ == "__main__":
    unittest.main()

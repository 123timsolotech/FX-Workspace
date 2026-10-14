"""How well a claim is confirmed (corroboration.py): sources count as independent ORIGINS - wire copies,
one newsroom's several domains and copied text count once - and research reports and the Claim
timelines tool say so. No network, no model."""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import corroboration as co

HERE = os.path.dirname(os.path.abspath(__file__))

STORY = ("Officials said the new bridge will open to traffic in March after two years of repairs to the main span "
         "and the approach roads on both banks of the river")


def src(n, domain, title="", snippet="", excerpt="", date=""):
    return {"n": n, "domain": domain, "title": title, "snippet": snippet, "excerpt": excerpt, "date": date}


ITEMS = [
    src(1, "www.reuters.com", "Bridge to reopen in March", excerpt=STORY, date="2026-03-01"),
    src(2, "finance.yahoo.com", "Bridge to reopen", snippet="LONDON (Reuters) - " + STORY, date="2026-03-02"),
    src(3, "www.bbc.co.uk", "Bridge repairs end", excerpt="The council confirmed the date at a meeting on Tuesday evening.", date="2026-03-03"),
    src(4, "www.bbc.com", "Bridge date set", excerpt="A different BBC write-up of the council meeting and its decision.", date="2026-03-04"),
    src(5, "localnews.example.org", "Bridge opening", excerpt="Residents welcomed the council decision on the bridge opening date.", date="Mar 05, 2026"),
    src(6, "rumours.example.net", "Bridge to get toll", excerpt="A toll is planned, one blog claims without naming anyone."),
    src(7, "copycat.example.com", "Bridge news", excerpt=STORY + " said on Monday"),
]


class OriginTests(unittest.TestCase):
    def test_site(self):
        self.assertEqual(co.site("edition.cnn.com"), "cnn.com")
        self.assertEqual(co.site("www.bbc.co.uk"), "bbc.co.uk")
        self.assertEqual(co.site("news.ynet.co.il"), "ynet.co.il")
        self.assertEqual(co.site(""), "")

    def test_wires_families_and_copies_count_once(self):
        g = co.origins(ITEMS)
        self.assertEqual(g[1], "Reuters")
        self.assertEqual(g[2], "Reuters")                 # a Yahoo page carrying the Reuters story
        self.assertEqual(g[3], g[4])                      # bbc.co.uk and bbc.com: one newsroom
        self.assertEqual(g[3], "BBC")
        self.assertEqual(g[7], "Reuters")                 # copied text joins the story it copies
        self.assertNotEqual(g[5], g[3])
        self.assertEqual(g[6], "example.net")

    def test_cited(self):
        self.assertEqual(co.cited("As [3] and [2, 5] say, and [4-6] too [7 · news site · trust: high]."), [3, 2, 5, 4, 6, 7])
        self.assertEqual(co.cited("no citations"), [])

    def test_assess(self):
        self.assertEqual(co.assess([6], ITEMS)["label"], "single source (example.net)")
        one = co.assess([1, 2, 7], ITEMS)
        self.assertEqual((one["level"], one["independent"], one["sources"]), ("single", 1, 3))
        self.assertEqual(one["label"], "one origin (Reuters, carried by 3 sites)")
        self.assertEqual(co.assess([1, 3, 4], ITEMS)["label"], "2 independent sources")
        self.assertEqual(co.assess([1, 3, 5], ITEMS)["label"], "confirmed by 3 independent sources")
        self.assertEqual(co.assess([99], ITEMS)["level"], "none")

    def test_annotate_tags_cited_bullets_only(self):
        out = co.annotate("Intro [1]\n- Opens in March [1, 2]\n- Toll planned [6]\n- Council set the date [3, 5, 1]", ITEMS)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Intro [1]")                       # not a bullet: untouched
        self.assertTrue(lines[1].endswith("{single source}"))
        self.assertTrue(lines[2].endswith("{single source}"))
        self.assertTrue(lines[3].endswith("{confirmed: 3 independent}"))

    def test_origin_labels_only_where_the_origin_is_not_the_site(self):
        labels = co.origin_labels(ITEMS)
        self.assertEqual(labels.get(2), "Reuters")
        self.assertEqual(labels.get(7), "Reuters")
        for n in (1, 3, 4, 5, 6):
            self.assertNotIn(n, labels)


REPORT = """# Bridge

The bridge opens in March after repairs [1, 2, 7].

## Key findings
- The council set the opening date on Tuesday [3, 5].
- A toll is planned for next year [6].
- Repairs covered the main span [1, 3, 5].
"""


class ReportTests(unittest.TestCase):
    def test_report_section(self):
        out = co.report_section(REPORT, ITEMS)
        self.assertTrue(out.startswith("## How well confirmed"))
        self.assertIn("**Confirmed by 3 or more independent sources:** 1 claim", out)
        self.assertIn("**Two independent sources:** 1 claim", out)
        self.assertIn("**One source only** - treat as possible, not established: 2 claims", out)
        self.assertIn("A toll is planned for next year - only example.net", out)
        self.assertIn("only Reuters", out)                         # three links, one origin
        self.assertEqual(co.report_section("No citations here.", ITEMS), "")

    def test_timelines(self):
        out = co.timelines(REPORT, ITEMS)
        self.assertTrue(out.startswith("## Claim timelines"))
        block = out.split("**Repairs covered the main span**")[1].split("\n\n")[0]
        rows = [r for r in block.splitlines() if r.startswith("- ")]
        self.assertEqual(rows[0], "- 2026-03-01 · reuters.com [1] - first reported")
        self.assertEqual(rows[1], "- 2026-03-03 · bbc.co.uk [3] - confirmed independently")
        self.assertEqual(rows[2], "- 2026-03-05 · example.org [5] - confirmed independently")   # "Mar 05, 2026" read
        copy = out.split("**The bridge opens in March after repairs**")[1].split("\n\n")[0]
        self.assertIn("[2] - repeats Reuters - not independent", copy)
        self.assertIn("no date · example.com [7] - repeats Reuters - not independent", copy)
        self.assertEqual(co.timelines("Nothing cited.", ITEMS), "")

    def test_research_adds_the_section_before_the_closing_line(self):
        from agent import research
        fake = SimpleNamespace(items=ITEMS)
        with mock.patch.object(research.sources, "current", return_value=fake):
            plain = research.with_confirmation(REPORT)
            invest = research.with_confirmation(REPORT + "\n*Research, not financial advice.*")
            none = research.with_confirmation("# Title\nNo citations.")
        self.assertTrue(plain.rstrip().split("## How well confirmed")[0].startswith("# Bridge"))
        self.assertIn("## How well confirmed", plain)
        self.assertTrue(invest.rstrip().endswith("*Research, not financial advice.*"))
        self.assertLess(invest.index("## How well confirmed"), invest.index("*Research, not financial advice"))
        self.assertEqual(none, "# Title\nNo citations.")
        self.assertIn("{single source}", research.REPORT_SYSTEM)
        self.assertIn("never as settled fact", research.SECTION_SYSTEM)


class TimelineToolTests(unittest.TestCase):
    def test_only_when_the_tool_is_on(self):
        import agent.tasks as tasks
        import core.state as shared
        saved = {k: shared.app_state.get(k) for k in ("turn_tools", "conversation")}
        self.addCleanup(lambda: shared.app_state.update(saved))
        fake = SimpleNamespace(items=ITEMS)
        result = {"status": "success", "message": REPORT}
        with mock.patch.object(tasks.sources, "current", return_value=fake):
            shared.app_state.update(turn_tools=[], conversation=[])
            self.assertIs(tasks._with_timelines(result), result)
            shared.app_state.update(turn_tools=["timeline"], conversation=[{"role": "assistant", "content": REPORT}])
            out = tasks._with_timelines(result)
            self.assertIn("## Claim timelines", out["message"])
            self.assertEqual(shared.app_state["conversation"][-1]["content"], out["message"])   # the model sees it too
            self.assertIs(tasks._with_timelines({"status": "error", "message": "x"})["message"], "x")

    def test_saved_sources_carry_their_origin(self):
        from agent import sessions
        fake = SimpleNamespace(for_reply=lambda message: [dict(it) for it in ITEMS])
        with mock.patch.object(sessions.sources, "current", return_value=fake):
            saved = {s["n"]: s for s in sessions.sources_for("reply")}
        self.assertEqual(saved[2]["origin"], "Reuters")               # shown as "via Reuters"
        self.assertNotIn("origin", saved[1])
        self.assertEqual({saved[n]["group"] for n in (1, 2, 7)}, {"Reuters"})   # the reading rail counts these once
        self.assertEqual(saved[3]["group"], saved[4]["group"])
        self.assertEqual(len({s["group"] for s in saved.values()}), 4)

    def test_the_composer_offers_it(self):
        import composer_tools
        self.assertIn("timeline", composer_tools.IDS)
        self.assertIn("timeline", composer_tools.HEADERS)
        with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("label: 'Claim timelines'", html)
        self.assertIn('class="src-origin"', html)


if __name__ == "__main__":
    unittest.main()

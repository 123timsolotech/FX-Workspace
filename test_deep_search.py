"""Deep search, part B2-B4 (1.4.4): forum and Q&A sources, the router, query fan-out, rank fusion, the model re-rank.
No network: every source answers from a recorded response."""
import json
import time
import unittest
from unittest.mock import patch

import httpx

import deep_sources
import search_engine


class FakeResp:
    def __init__(self, data=None, text="", status=200):
        self._data, self.text, self.status_code = data, text or json.dumps(data or {}), status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=None, response=None)


def answering(table):
    """_get_json replaced: the first URL fragment that matches gives the response."""
    def get(source, url, params=None):
        for frag, resp in table.items():
            if frag in url:
                return resp(params) if callable(resp) else resp
        raise AssertionError(url)
    return get


class AdapterTests(unittest.TestCase):
    def setUp(self):
        deep_sources._cache.clear()
        deep_sources._state["se_quota"] = None

    def test_hacker_news_stories_and_comments(self):
        data = {"hits": [{"objectID": "1", "title": "Vulkan on AMD", "points": 120, "num_comments": 40, "created_at": "2026-01-02T00:00:00Z"},
                         {"objectID": "9", "story_id": "1", "story_title": "Vulkan on AMD", "comment_text": "<p>Set <b>GGML_VK</b>=1</p>"}]}
        with patch.object(deep_sources, "_get_json", answering({"algolia": FakeResp(data)})):
            got = deep_sources.hackernews("vulkan amd")
        self.assertEqual(got[0]["url"], "https://news.ycombinator.com/item?id=1")
        self.assertEqual((got[0]["kind"], got[0]["points"], got[0]["date"]), ("forum", 120, "2026-01-02"))
        self.assertEqual(got[1]["title"], "Comment on: Vulkan on AMD")
        self.assertEqual(got[1]["snippet"], "Set GGML_VK =1")

    def test_stack_exchange_similar_first_then_full_text_and_quota(self):
        calls = []

        def similar(params):
            calls.append("similar")
            return FakeResp({"items": [], "quota_remaining": 250})

        def advanced(params):
            calls.append("advanced")
            return FakeResp({"items": [{"title": "Activate venv", "link": "https://stackoverflow.com/q/1", "score": 12,
                                        "accepted_answer_id": 5, "answer_count": 3, "body": "<p>Set-ExecutionPolicy</p>"}],
                             "quota_remaining": 249})
        with patch.object(deep_sources, "_get_json", answering({"/similar": similar, "/search/advanced": advanced})):
            got = deep_sources.stackexchange("activate venv")
        self.assertEqual(calls, ["similar", "advanced"])
        self.assertTrue(got[0]["answered"])
        self.assertEqual(deep_sources.quota()["stackexchange_remaining"], 249)
        deep_sources._state["se_quota"] = 2
        self.assertEqual(deep_sources.run("stackexchange", "anything")[0], [])      # out of quota: skipped, said why

    def test_discourse_thread_address(self):
        data = {"posts": [{"topic_id": 7, "post_number": 3, "blurb": "use py -m venv", "created_at": "2025-05-01"}],
                "topics": [{"id": 7, "title": "venv on Windows", "slug": "venv-on-windows", "posts_count": 9, "has_accepted_answer": True}]}
        with patch.object(deep_sources, "_get_json", answering({"discuss.python.org": FakeResp(data)})):
            got = deep_sources.discourse("venv", forum="discuss.python.org")
        self.assertEqual(got[0]["url"], "https://discuss.python.org/t/venv-on-windows/7/3")
        self.assertTrue(got[0]["answered"])

    def test_a_refusal_is_a_reason_not_a_crash(self):
        with patch.object(deep_sources, "_get_json", side_effect=deep_sources.Refused("Lemmy refused the request (403)")):
            self.assertEqual(deep_sources.run("lemmy", "x"), ([], "Lemmy refused the request (403)"))

    def test_requests_go_through_the_search_route_honestly(self):
        seen = {}

        class C:
            def __init__(self, comp, **kw):
                seen.update(comp=comp, ua=kw["headers"]["User-Agent"])

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                return FakeResp({"hits": []})
        with patch.object(deep_sources.netlayer, "client", C):
            deep_sources.hackernews("x")
        self.assertEqual(seen["comp"], "search")
        self.assertIn("FX-Workspace", seen["ua"])                         # never pretends to be a browser


class RouterTests(unittest.TestCase):
    def names(self, q):
        return [s for s, _ in deep_sources.route(q)]

    def test_errors_go_to_q_and_a_issues_and_forums(self):
        r = deep_sources.route("llama.cpp vulkan backend crashes with out of memory")
        self.assertEqual([s for s, _ in r][:3], ["stackexchange", "github", "hackernews"])

    def test_experience_and_hardware(self):
        r = dict(deep_sources.route("is the RTX 3090 worth it for local LLMs"))
        self.assertIn("lemmy", r)
        self.assertEqual(r["stackexchange"], {"site": "superuser"})

    def test_science_and_plain_questions(self):
        self.assertEqual(self.names("peer reviewed study on intermittent fasting"), ["arxiv", "wikipedia"])
        self.assertEqual(self.names("capital of australia"), ["wikipedia", "hackernews"])     # Marginalia was too slow

    def test_forums_get_keywords_and_a_slow_source_is_left_out(self):
        self.assertEqual(deep_sources._words("how does reciprocal rank fusion work?"), "reciprocal rank fusion work")
        seen = []

        def slow(source, q, n=8, **o):
            seen.append((source, q))
            if source == "wikipedia":
                time.sleep(1.5)
            return [{"title": "t", "url": f"https://{source}.org/x", "source": source}], ""
        with patch.object(deep_sources, "run", slow), patch.object(deep_sources, "WAIT_S", .5):
            got = deep_sources.search("capital of australia")
        self.assertEqual(got["used"], ["hackernews"])
        self.assertIn("Wikipedia took too long", got["why"][0])
        self.assertIn(("hackernews", "capital australia"), seen)

    def test_a_named_forum_is_searched(self):
        r = deep_sources.route("home assistant zigbee devices unavailable after restart")
        self.assertIn(("discourse", {"forum": "community.home-assistant.io"}), r)


class FanoutFuseRerankTests(unittest.TestCase):
    def test_fanout_kinds(self):
        qs = search_engine.fanout("Why does pip fail with ModuleNotFoundError: No module named 'venv' on Ubuntu?")
        self.assertEqual(qs[0][:7], "Why doe")
        self.assertTrue(any(q.startswith('"') for q in qs))              # the exact phrase
        self.assertTrue(any(q.endswith(" forum") for q in qs))
        self.assertTrue(any(q.endswith(" solved") for q in qs))
        self.assertLessEqual(len(qs), 6)
        self.assertEqual(len({q.lower() for q in qs}), len(qs))

    def test_plain_question_stays_short(self):
        self.assertEqual(search_engine.fanout("capital of australia"), ["capital of australia"])

    def test_rrf_rewards_agreement(self):
        a = [{"url": "https://a.com/1", "title": "A"}, {"url": "https://b.com/2", "title": "B"}]
        b = [{"url": "https://c.com/3", "title": "C"}, {"url": "https://b.com/2", "title": "B", "kind": "q&a", "answered": True}]
        fused = search_engine.fuse([a, b])
        self.assertEqual(fused[0]["url"], "https://b.com/2")              # found by both lists
        self.assertEqual((fused[0]["hits"], fused[0]["kind"], fused[0]["answered"]), (2, "q&a", True))

    def test_rank_keeps_the_thread_fields(self):
        ranked = search_engine.rank([{"url": "https://stackoverflow.com/q/1", "title": "T", "kind": "q&a", "points": 9, "hits": 2}], ["t"], False)
        self.assertEqual((ranked[0]["kind"], ranked[0]["points"], ranked[0]["hits"]), ("q&a", 9, 2))
        self.assertIn("accepted answer", search_engine._thread_note({"kind": "q&a", "answered": True, "points": 9}))

    def test_rerank_by_the_model_and_without_it(self):
        rs = [{"url": f"https://x.com/{i}", "title": str(i), "snippet": ""} for i in range(5)]
        out = search_engine.rerank("q", rs, ask=lambda s, u: {"order": [3, 1, 99, 3]}, batch=3)
        self.assertEqual([r["title"] for r in out], ["2", "0", "1", "3", "4"])
        self.assertEqual(search_engine.rerank("q", rs, ask=lambda s, u: None), rs)


class ToolTests(unittest.TestCase):
    def test_sources_join_the_web_results_in_standard_mode(self):
        web = [{"title": "W", "url": "https://w.com/", "snippet": "", "date": ""}]
        src = {"lists": [[{"title": "Fixed it", "url": "https://stackoverflow.com/q/9", "kind": "q&a", "answered": True,
                           "source": "Stack Exchange", "snippet": "", "date": ""}]], "used": ["Stack Exchange"], "why": []}
        with patch.object(search_engine, "_run_provider", return_value=(web, "")), \
                patch.object(deep_sources, "search", return_value=src) as ds:
            out = search_engine.web_search_tool({"query": "pip fails"}, {"search_sources": True, "search_cache_minutes": 0})
            self.assertTrue(ds.called)
            self.assertIn("stackoverflow.com", out["message"])
            self.assertIn("Q&A · accepted answer", out["message"])
            self.assertIn("Stack Exchange", out["data"]["providers"])
            ds.reset_mock()
            search_engine.web_search_tool({"query": "pip fails"}, {"search_sources": False, "search_cache_minutes": 0})
            self.assertFalse(ds.called)                                    # the switch off: only the web


if __name__ == "__main__":
    unittest.main()

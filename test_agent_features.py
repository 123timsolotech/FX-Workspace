"""Charts and statistics (stats_tools.py), tools it turns on by itself (auto_tools.py) and Careful and honest
(careful.py): the maths against known answers, the chart checks, which messages turn which tools on, the
model's use_tools, the after-answer checks, and how they reach the prompt, the tool list and the saved chat.
No model, no network: attached tables are temporary CSV files."""
import os
import shutil
import statistics
import tempfile
import unittest
from unittest import mock

import auto_tools
import careful
import composer_tools
import core.config as config
import core.state as shared
import sources
import stats_tools


class StatsTests(unittest.TestCase):
    def test_describe_matches_known_answers(self):
        d = stats_tools.describe([2, 4, 4, 4, 5, 5, 7, 9])
        self.assertEqual((d["count"], d["sum"], d["mean"], d["median"], d["mode"]), (8, 40, 5, 4.5, [4]))
        self.assertEqual(d["stdev_population"], 2)                       # the textbook example
        self.assertAlmostEqual(d["stdev_sample"], statistics.stdev([2, 4, 4, 4, 5, 5, 7, 9]), places=5)
        self.assertEqual((d["q1"], d["q3"], d["iqr"]), (4, 5.5, 1.5))     # Excel PERCENTILE.INC
        self.assertEqual(d["outliers"], [9])                              # beyond Q3 + 1.5 IQR = 7.75

    def test_numbers_from_text(self):
        self.assertEqual(stats_tools.numbers(["1,234", "$5", "12%", "(3)", "", None, "−2"]), [1234, 5, 12, -3, -2])
        with self.assertRaises(stats_tools.StatsError):
            stats_tools.numbers(["a", "b", 1])
        d = stats_tools.describe([1.123456789, 2])
        self.assertEqual(d["mean"], 1.56173)                             # 6 significant digits

    def test_relation(self):
        r = stats_tools.relation([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
        self.assertEqual((r["pearson_r"], r["slope"], r["intercept"], r["r_squared"]), (1, 2, 0, 1))
        self.assertIn("not that one causes the other", r["note"])
        with self.assertRaises(stats_tools.StatsError):
            stats_tools.relation([1, 2, 3], [1, 2])
        with self.assertRaises(stats_tools.StatsError):
            stats_tools.relation([1, 1, 1], [1, 2, 3])                   # x doesn't vary

    def test_growth_and_moving_average(self):
        g = stats_tools.growth([100, 110, 121])
        self.assertEqual((g["change"], g["pct_change"], g["pct_change_each_step"], g["cagr_pct"]), (21, 21, [10, 10], 10))
        self.assertEqual(stats_tools.growth([50, 100], periods=5)["cagr_pct"], 14.87)          # doubling in 5 years
        self.assertEqual(stats_tools.moving_average([1, 2, 3, 4], 2)["values"], [1.5, 2.5, 3.5])
        with self.assertRaises(stats_tools.StatsError):
            stats_tools.moving_average([1, 2], 5)

    def test_compute_stats_tool(self):
        out = stats_tools.compute_stats({"series": {"A": [1, 2, 3], "B": [10, 20]}, "growth": True})
        self.assertTrue(out["ok"])
        self.assertIn("COMPUTED", out["label"])
        self.assertEqual(out["describe"]["B"]["mean"], 15)
        self.assertEqual(out["growth"]["A"]["pct_change"], 200)
        self.assertEqual(stats_tools.compute_stats({"x": [1, 2, 3], "y": [3, 2, 1]})["relation"]["pearson_r"], -1)
        bad = stats_tools.compute_stats({})
        self.assertEqual((bad["ok"], bad["status"]), (False, "permanent_failure"))


class ChartTests(unittest.TestCase):
    def test_a_good_chart(self):
        out = stats_tools.make_chart({"type": "bar", "title": "Sales", "labels": ["Q1", "Q2"],
                                      "series": [{"name": "2025", "values": ["1,200", 900.5]}], "unit": "USD", "source": "[2]"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["chart"], {"type": "bar", "title": "Sales", "labels": ["Q1", "Q2"],
                                        "series": [{"name": "2025", "values": [1200, 900.5]}], "unit": "USD", "source": "[2]"})
        block = stats_tools.chart_block(out["chart"])
        self.assertTrue(block.startswith("```chart\n{") and block.endswith("}\n```"))

    def test_bad_charts_are_refused_never_padded(self):
        cases = [({"type": "radar", "series": [{"values": [1]}]}, "type must be"),
                 ({"type": "bar", "labels": ["a", "b", "c"], "series": [{"values": [1, 2]}]}, "3 labels for 2 values"),
                 ({"type": "bar", "series": [{"values": [1, "n/a"]}]}, "every value must be a number"),
                 ({"type": "line", "series": [{"values": [1, 2]}, {"values": [1]}]}, "same number of values"),
                 ({"type": "pie", "labels": ["a", "b"], "series": [{"values": [1, -1]}]}, "negative"),
                 ({"type": "pie", "labels": ["a"], "series": [{"values": [1]}, {"values": [2]}]}, "exactly one series"),
                 ({"type": "scatter", "series": [{"points": [[1, 2]]}]}, "at least 2"),
                 ({"type": "bar", "series": []}, "series must be a list")]
        for args, why in cases:
            out = stats_tools.make_chart(args)
            self.assertFalse(out["ok"], args)
            self.assertIn(why, out["message"], args)

    def test_scatter(self):
        out = stats_tools.make_chart({"type": "scatter", "series": [{"name": "s", "points": [[1, 2], ["3", "4.5"], ["x", 1]]}]})
        self.assertEqual(out["chart"]["series"][0]["points"], [[1, 2], [3, 4.5]])


class TableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        path = os.path.join(self.tmp, "sales.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("region,month,revenue,units\nNorth,Jan,100,5\nSouth,Jan,200,8\nNorth,Feb,150,6\nSouth,Feb,\"1,000\",9\n")
        self.files = [{"id": "abc", "name": "sales.csv", "kind": "table", "path": path}]

    def test_stats_from_columns(self):
        out = stats_tools.compute_stats({"file": "sales.csv", "columns": ["Revenue"]}, self.files)
        self.assertEqual(out["describe"]["Revenue"]["sum"], 1450)                 # "1,000" read as 1000
        auto = stats_tools.compute_stats({"file": "abc"}, self.files)            # every numeric column
        self.assertEqual(sorted(auto["describe"]), ["revenue", "units"])
        rel = stats_tools.compute_stats({"file": "sales.csv", "x": "units", "y": "revenue"}, self.files)
        self.assertGreater(rel["relation"]["pearson_r"], 0.7)
        missing = stats_tools.compute_stats({"file": "nope.csv"}, self.files)
        self.assertIn("attached tables: sales.csv", missing["message"])
        col = stats_tools.compute_stats({"file": "sales.csv", "columns": ["profit"]}, self.files)
        self.assertIn("Columns: region, month, revenue, units", col["message"])

    def test_chart_from_columns_with_grouping(self):
        out = stats_tools.make_chart({"type": "bar", "file": "sales.csv", "x": "region", "y": ["revenue"], "agg": "sum"}, self.files)
        self.assertEqual((out["chart"]["labels"], out["chart"]["series"]), (["North", "South"], [{"name": "revenue (sum)", "values": [250, 1200]}]))
        count = stats_tools.make_chart({"type": "pie", "file": "sales.csv", "x": "region", "y": ["units"], "agg": "count"}, self.files)
        self.assertEqual(count["chart"]["series"][0]["values"], [2, 2])
        rows = stats_tools.make_chart({"type": "line", "file": "sales.csv", "x": "month", "y": ["units"]}, self.files)
        self.assertEqual(rows["chart"]["series"][0]["values"], [5, 8, 6, 9])


class CarefulTests(unittest.TestCase):
    def test_setting_and_prompt(self):
        self.assertTrue(config.DEFAULT_SETTINGS["careful_mode"])
        self.assertTrue(careful.on({}))
        self.assertEqual(careful.block({"careful_mode": False}), "")
        rules = careful.block({})
        for must in ("I don't know", "Never invent facts, numbers, names, dates, quotes, sources or links",
                     "wrong premise", "Never claim to have done"):
            self.assertIn(must, rules)
        self.assertLess(len(rules) / 4, 200)                                    # small enough for a 16k local model
        self.assertFalse(config.clean_agent_settings({"careful_mode": "no"}) ["careful_mode"] is not True)

    def test_which_questions_are_checkable(self):
        for q in ("Who won the 2022 World Cup?", "When was the Eiffel Tower built?", "How many people live in Haifa?",
                  "Is it true that bats are blind?", "what's the price of bitcoin today", "מי ניצח בבחירות", "кто выиграл"):
            self.assertTrue(careful.checkable(q), q)
        for q in ("write a poem about the sea", "how do I reverse a list in python", "solve 2x + 3 = 7",
                  "what do you think of my plan", "hello"):
            self.assertFalse(careful.checkable(q), q)

    def test_review(self):
        reg = sources.Registry()
        reg.add("https://example.org/a", title="A")
        text, v = careful.review("Paris is big [1]. Also see [7] and https://made.up/page. Code: ```x = a[3]```",
                                 "Tell me about Paris", reg, tools_used=True, web_available=True)
        self.assertEqual(text, "Paris is big [1]. Also see and https://made.up/page. Code: ```x = a[3]```")
        self.assertEqual((v["removed"], v["links"], v["from_memory"], v["checked"]), ([7], ["https://made.up/page"], False, 1))
        self.assertTrue(careful.worth_showing(v))
        _t, v = careful.review("It was finished in 1889 and is 330 m tall.", "When was the Eiffel Tower built?",
                               sources.Registry(), tools_used=False, web_available=True)
        self.assertTrue(v["from_memory"])
        _t, v = careful.review("Here is the poem.", "write a poem", sources.Registry(), tools_used=False, web_available=True)
        self.assertFalse(careful.worth_showing(v))
        _t, v = careful.review("See https://given.example/x", "summarize https://given.example/x", sources.Registry(),
                               tools_used=False, web_available=True)
        self.assertEqual(v["links"], [])                                         # the user's own link is fine


class AutoToolsTests(unittest.TestCase):
    def pick(self, message, chosen=(), settings=None, web_ok=True, table=False):
        return [a["id"] for a in auto_tools.pick(message, set(chosen), settings if settings is not None else {}, web_ok, table)]

    def test_messages_turn_on_the_right_tools(self):
        self.assertEqual(self.pick("what's the latest news about the Mars rover?"), ["grounded"])
        self.assertEqual(self.pick("plot my monthly spending as a bar chart"), ["charts"])
        self.assertEqual(self.pick("what's the median and standard deviation of 3, 5, 9?"), ["charts"])
        self.assertEqual(self.pick("who reported it first that the merger was off?"), ["timeline"])
        self.assertEqual(self.pick("is it true that coffee dehydrates you?"), ["biascheck", "grounded"])
        self.assertEqual(self.pick("https://example.com/article"), ["summarize"])
        self.assertEqual(self.pick("what's NVDA's market cap and P/E?"), ["market"])
        self.assertEqual(self.pick("build me a dashboard for my habits"), ["canvas"])
        self.assertEqual(self.pick("research whether 4-day weeks work, in-depth report"), ["research"])
        self.assertEqual(self.pick("draw a graph of these: 1, 4, 9"), ["charts"])
        # D50: an explanation or a comparison may get an interactive chart, explainer or choices.
        self.assertEqual(self.pick("explain how compound interest works"), ["visuals"])
        self.assertEqual(self.pick("plot y = x^2 + 3x"), ["charts", "visuals"])
        self.assertEqual(self.pick("hello there"), [])
        self.assertEqual(self.pick("סכם לי חדשות היום"), ["grounded"])
        self.assertEqual(self.pick("покажи график продаж"), ["charts"])

    def test_web_needs_both_settings(self):
        with mock.patch("search_engine.turn_mode", return_value="standard"):
            self.assertTrue(auto_tools.web_ok({}))
            self.assertFalse(auto_tools.web_ok({"web_auto_search": False}))      # "no web for questions" holds
        with mock.patch("search_engine.turn_mode", return_value="disabled"):
            self.assertFalse(auto_tools.web_ok({}))

    def test_use_tools_is_for_questions_only(self):
        import tools.schemas as schemas
        saved = {k: shared.app_state.get(k) for k in ("turn_policy", "settings", "design_mode")}
        self.addCleanup(lambda: shared.app_state.update(saved))
        shared.app_state.update(settings={**config.DEFAULT_SETTINGS}, design_mode=None, turn_policy={"mode": "action"})
        self.assertIn("use_tools", schemas.world_hidden())                     # Studio keeps its room on 16k models
        shared.app_state["turn_policy"] = {"mode": "none"}
        self.assertNotIn("use_tools", schemas.world_hidden())

    def test_limits(self):
        self.assertEqual(self.pick("latest news on AI", web_ok=False), [])           # Web Search off: no web tools
        self.assertEqual(self.pick("plot this", web_ok=False), ["charts"])           # charts need no web
        self.assertEqual(self.pick("latest news", chosen={"grounded"}), [])          # already on
        self.assertEqual(self.pick("latest news", chosen={"research"}), [])          # Research does its own
        self.assertEqual(self.pick("latest news", settings={"auto_tools": False}), [])
        self.assertEqual(self.pick("anything", table=True), ["charts"])
        self.assertEqual(self.pick("When was the Eiffel Tower built?"), ["grounded"])   # Careful and honest
        self.assertEqual(self.pick("When was the Eiffel Tower built?", settings={"careful_mode": False}), [])
        for a in auto_tools.pick("latest news", set(), {}, True):
            self.assertTrue(a["why"])                                            # always says why

    def test_use_tools(self):
        saved = {k: shared.app_state.get(k) for k in ("turn_tools", "turn_auto", "turn_search_mode")}
        self.addCleanup(lambda: shared.app_state.update(saved))
        shared.app_state.update(turn_tools=set(), turn_auto=[], turn_search_mode=None)
        with mock.patch("search_engine.turn_mode", return_value="standard"):
            out = auto_tools.use_tools_tool({"tools": ["grounded", "charts"], "why": "needs current numbers"})
            self.assertTrue(out["ok"])
            self.assertEqual(shared.app_state["turn_tools"], {"grounded", "charts"})
            self.assertEqual(auto_tools.for_reply()[0], {"id": "grounded", "label": "Grounded answers",
                                                         "why": "needs current numbers", "by": "model"})
            refused = auto_tools.use_tools_tool({"tools": ["code"], "why": "x"})        # runs code: never by itself
            self.assertFalse(refused["ok"])
            self.assertIn("Tools you can turn on", refused["message"])
            self.assertNotIn("research", auto_tools.SAFE)
        with mock.patch("search_engine.turn_mode", return_value="disabled"):
            self.assertFalse(auto_tools.use_tools_tool({"tools": ["timeline"], "why": "x"})["ok"])


class WiringTests(unittest.TestCase):
    def setUp(self):
        keys = ("turn_tools", "turn_auto", "turn_charts", "turn_files", "turn_attachments", "turn_persona", "turn_images",
                "settings", "conversation", "design_mode", "turn_policy", "turn_search_mode", "turn_sources")
        saved = {k: shared.app_state.get(k) for k in keys}
        self.addCleanup(lambda: shared.app_state.update(saved))

    def test_the_turn_records_what_it_turned_on(self):
        import agent.tasks as tasks
        import image_gen
        shared.app_state["settings"] = {**config.DEFAULT_SETTINGS}
        with mock.patch.object(image_gen, "warm"):
            tasks._begin_composer_tools(shared.ChatRequest(message="what's the latest news? draw me a fox too"))
        self.assertEqual(shared.app_state["turn_tools"], {"grounded", "image"})
        self.assertEqual([a["id"] for a in shared.app_state["turn_auto"]], ["image", "grounded"])
        req = shared.ChatRequest(message="latest news")
        req.command = "translate"
        tasks._begin_composer_tools(req)                                         # a /command says what it's for
        self.assertEqual(shared.app_state["turn_auto"], [])

    def test_tools_are_offered_only_when_on(self):
        import tools.schemas as schemas
        shared.app_state.update(settings={**config.DEFAULT_SETTINGS}, turn_tools=set(), design_mode=None,
                                turn_policy={"mode": "none"})
        offered = {t["function"]["name"] for t in schemas.tools_for_current_turn(local=False)}
        self.assertIn("use_tools", offered)
        self.assertNotIn("make_chart", offered)
        shared.app_state["turn_tools"] = {"charts"}
        offered = {t["function"]["name"] for t in schemas.tools_for_current_turn(local=False)}
        self.assertTrue({"compute_stats", "make_chart"} <= offered)
        self.assertTrue(schemas.tool_allowed_current_turn("make_chart"))
        shared.app_state["settings"] = {**config.DEFAULT_SETTINGS, "auto_tools": False}
        self.assertNotIn("use_tools", {t["function"]["name"] for t in schemas.tools_for_current_turn(local=False)})
        self.assertFalse(schemas.tool_allowed_current_turn("use_tools"))
        self.assertTrue({"compute_stats", "make_chart", "use_tools"} <= schemas.LOCAL_TOOL_NAMES)
        self.assertEqual(composer_tools.OFFERS["charts"], {"compute_stats", "make_chart"})

    def test_charts_and_care_after_the_answer(self):
        import agent.tasks as tasks
        shared.app_state.update(settings={**config.DEFAULT_SETTINGS}, turn_auto=[{"id": "charts", "why": "numbers", "by": "app"}],
                                turn_charts=[{"type": "bar", "title": "T", "labels": ["a"], "series": [{"name": "s", "values": [1]}]}],
                                conversation=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "Here [9]."}])
        sources.start_turn()
        result = tasks._with_charts({"status": "success", "message": "Here [9]."})
        self.assertIn("```chart\n{\"type\":\"bar\"", result["message"])
        with mock.patch("search_engine.turn_mode", return_value="standard"):
            result = tasks._with_care(shared.ChatRequest(message="q"), result)
        self.assertTrue(result["message"].startswith("Here."))                   # [9] pointed at no source
        self.assertEqual(result["careful"]["removed"], [9])
        self.assertEqual(result["auto_tools"][0]["label"], "Charts and statistics")
        self.assertEqual(shared.app_state["conversation"][-1]["content"], result["message"])
        shared.app_state["settings"] = {**config.DEFAULT_SETTINGS, "careful_mode": False}
        plain = tasks._with_care(shared.ChatRequest(message="q"), {"status": "success", "message": "x [5]"})
        self.assertEqual(plain["message"], "x [5]")                              # off: untouched

    def test_the_rules_reach_the_prompt_and_the_saved_chat(self):
        import inspect
        import agent.models as models
        import agent.sessions as sessions
        import agent.prompts as prompts
        # Since D47 the honesty rules are part of Enhanced mode's core, which opens both prompts - before any
        # persona (test_response_modes checks the order in the prompt as sent).
        self.assertIn(careful.PROMPT, prompts.SYSTEM_PROMPT)
        self.assertIn(prompts.CAREFUL_COMPACT, prompts.SYSTEM_PROMPT_COMPACT)
        self.assertIn("system_prompt(local, base_prompt)", inspect.getsource(models.build_messages))
        rec = inspect.getsource(sessions.record_turn)
        self.assertIn('("au", result.get("auto_tools"))', rec)
        self.assertIn('("ck", result.get("careful"))', rec)


class ContextFitTests(unittest.TestCase):
    """A 16k model was sent 16,937 tokens: the fit measured the bare base prompt, not the persona and careful
    rules on top of it (2026-10-01). It measures the prompt as sent now, and an overflow is trimmed and retried."""

    def setUp(self):
        keys = ("settings", "conversation", "turn_persona", "design_mode", "context_summary", "run_budget", "turn_tools",
                "turn_policy", "_local_fit_budget", "local_planned", "current_user_request", "turn_attachments", "mode")
        saved = {k: shared.app_state.get(k) for k in keys}
        self.addCleanup(lambda: shared.app_state.update(saved))
        shared.app_state.update(settings={**config.DEFAULT_SETTINGS}, turn_persona=None, design_mode=None, context_summary="",
                                run_budget=None, turn_tools=set(), turn_policy={"mode": "none"}, turn_attachments=None,
                                current_user_request="q", mode=config.MODE_LOCAL)

    def test_the_overflow_is_read_from_both_shapes(self):
        import agent.models as models
        raw = ('Error code: 400 - {"error":{"code":400,"message":"request (16937 tokens) exceeds the available context size '
               '(16384 tokens), try increasing it","type":"exceed_context_size_error","n_prompt_tokens":16937,"n_ctx":16384}}')
        self.assertEqual(models.context_overflow(RuntimeError(raw)), (16937, 16384))
        self.assertEqual(models.context_overflow("{'type': 'exceed_context_size_error', 'n_prompt_tokens': 9000, 'n_ctx': 8192}"), (9000, 8192))
        self.assertEqual(models.context_overflow("exceed_context_size_error"), (0, 0))
        self.assertIsNone(models.context_overflow("Error code: 500 - model crashed"))

    def test_the_fit_counts_the_system_prompt(self):
        """Enhanced sends its core (honesty, ground rules) and the tool rules; Raw sends no system message (D47). The
        fit measures what is sent, so Raw leaves the history more room."""
        import agent.models as models
        import llama_local
        shared.app_state["conversation"] = [{"role": "system", "content": ""}]
        saved = shared.app_state.get("raw_mode")
        self.addCleanup(lambda: shared.app_state.__setitem__("raw_mode", saved))
        budgets = {}
        for raw in (True, False):
            shared.app_state["raw_mode"] = raw
            self.assertEqual("GROUND RULES" in models.system_prompt(True), not raw)
            with mock.patch.object(models, "resolve_local_model", return_value="m"), \
                 mock.patch.object(llama_local, "model_context_limit", return_value=16384):
                models._fit_for_local()
            budgets[raw] = shared.app_state["_local_fit_budget"]
        self.assertGreater(budgets[True] - budgets[False], 1000)                 # the prompt's tokens come off the history

    def test_an_overflow_is_trimmed_and_retried_once(self):
        import agent.models as models
        long = "x" * 3600                                                        # ~1000 tokens each
        convo = [{"role": "system", "content": ""}]
        for i in range(12):
            convo += [{"role": "user", "content": f"q{i} {long}"}, {"role": "assistant", "content": f"a{i} {long}"}]
        shared.app_state["conversation"] = convo
        sent = []
        over = RuntimeError("This chat is too long for m: the request (16937 tokens) exceeds the available context size (16384 tokens)")

        def fake_call(messages, vision, model_name=None):
            sent.append(len(messages))
            if len(sent) == 1:
                raise over
            return "ok"
        with mock.patch.object(models, "_fit_for_local", side_effect=lambda: shared.app_state.update(_local_fit_budget=20000)), \
             mock.patch.object(models, "call_local", side_effect=fake_call), \
             mock.patch.object(models, "build_messages", side_effect=lambda *a: list(shared.app_state["conversation"])), \
             mock.patch("image_gen.yield_gpu"), mock.patch("agent.context_window.maybe_compress"):
            self.assertEqual(models.call_model(), "ok")
        self.assertEqual(len(sent), 2)
        self.assertLess(sent[1], sent[0])                                        # older turns were dropped for the retry
        with mock.patch.object(models, "_fit_for_local"), mock.patch.object(models, "call_local", side_effect=RuntimeError("boom")), \
             mock.patch.object(models, "build_messages", return_value=[]), mock.patch("image_gen.yield_gpu"), \
             mock.patch("agent.context_window.maybe_compress"):
            with self.assertRaisesRegex(RuntimeError, "boom"):                   # any other error is not retried
                models.call_model()


if __name__ == "__main__":
    unittest.main()

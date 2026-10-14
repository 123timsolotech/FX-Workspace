"""Tests for permissions.py: the auto/ask/deny decision engine behind
Settings > Permissions (general category defaults, per-tool advanced
overrides, and the Lock down / God mode global switches)."""
import unittest

import permissions


def settings(**overrides):
    base = {
        "permission_mode": "normal",
        "permission_categories": {"benign": "auto", "input": "ask", "destructive": "ask"},
        "permission_overrides": {},
    }
    base.update(overrides)
    return base


class ClassifyTests(unittest.TestCase):
    def test_known_destructive_tools(self):
        for name in ("delete_path", "service_control", "kill_process", "schedule_task",
                     "download_file", "write_file", "execute_command"):
            with self.subTest(name=name):
                self.assertEqual(permissions.classify(name), "destructive")

    def test_known_input_tools(self):
        for name in ("click_by_name", "type_text", "new_browser_tab", "act_element"):
            with self.subTest(name=name):
                self.assertEqual(permissions.classify(name), "input")

    def test_unknown_and_read_tools_default_to_benign(self):
        for name in ("observe", "list_windows", "search_files", "open_url",
                     "some_totally_new_tool_nobody_registered"):
            with self.subTest(name=name):
                self.assertEqual(permissions.classify(name), "benign")


class NormalModeTests(unittest.TestCase):
    def test_uses_category_default_with_no_override(self):
        self.assertEqual(permissions.decide("observe", settings()), "auto")     # benign
        self.assertEqual(permissions.decide("click_by_name", settings()), "ask")  # input
        self.assertEqual(permissions.decide("delete_path", settings()), "ask")  # destructive

    def test_category_default_can_be_relaxed(self):
        s = settings(permission_categories={"benign": "auto", "input": "auto", "destructive": "ask"})
        self.assertEqual(permissions.decide("click_by_name", s), "auto")

    def test_per_tool_override_wins_over_category_default(self):
        s = settings(permission_overrides={"delete_path": "auto"})
        self.assertEqual(permissions.decide("delete_path", s), "auto")   # destructive, but overridden
        s2 = settings(permission_overrides={"observe": "ask"})
        self.assertEqual(permissions.decide("observe", s2), "ask")       # benign, but overridden

    def test_deny_override_blocks_regardless_of_category(self):
        s = settings(permission_overrides={"observe": "deny"})
        self.assertEqual(permissions.decide("observe", s), "deny")


class GodModeTests(unittest.TestCase):
    def test_auto_approves_everything_with_no_override(self):
        s = settings(permission_mode="god")
        for name in ("observe", "click_by_name", "delete_path", "execute_command", "kill_process"):
            with self.subTest(name=name):
                self.assertEqual(permissions.decide(name, s), "auto")

    def test_explicit_ask_override_still_holds_in_god_mode(self):
        s = settings(permission_mode="god", permission_overrides={"delete_path": "ask"})
        self.assertEqual(permissions.decide("delete_path", s), "ask")

    def test_explicit_deny_override_still_holds_in_god_mode(self):
        """This is what keeps god mode 'almost all' rather than unconditional."""
        s = settings(permission_mode="god", permission_overrides={"execute_command": "deny"})
        self.assertEqual(permissions.decide("execute_command", s), "deny")


class LockdownModeTests(unittest.TestCase):
    def test_asks_for_everything_including_normally_benign_tools(self):
        s = settings(permission_mode="lockdown")
        for name in ("observe", "list_windows", "ask_user", "get_active_window"):
            with self.subTest(name=name):
                self.assertEqual(permissions.decide(name, s), "ask")

    def test_overrides_do_not_relax_lockdown(self):
        """Lock down is an absolute panic button: even a tool explicitly set
        to 'auto' must still ask while lockdown is on."""
        s = settings(permission_mode="lockdown", permission_overrides={"observe": "auto"})
        self.assertEqual(permissions.decide("observe", s), "ask")

    def test_deny_override_also_does_not_relax_lockdown_to_a_silent_block(self):
        """A 'deny' override under lockdown still resolves to 'ask', not
        'deny' - lockdown means 'always ask', full stop, no exceptions in
        either direction."""
        s = settings(permission_mode="lockdown", permission_overrides={"delete_path": "deny"})
        self.assertEqual(permissions.decide("delete_path", s), "ask")


class CleanSettingsTests(unittest.TestCase):
    def test_missing_settings_fall_back_to_defaults(self):
        clean = permissions.clean_settings({})
        self.assertEqual(clean["permission_mode"], "normal")
        self.assertEqual(clean["permission_categories"], {"benign": "auto", "input": "ask", "destructive": "ask"})
        self.assertEqual(clean["permission_overrides"], {})

    def test_invalid_mode_falls_back_to_normal(self):
        clean = permissions.clean_settings({"permission_mode": "nonsense"})
        self.assertEqual(clean["permission_mode"], "normal")

    def test_invalid_category_value_is_dropped_to_default(self):
        clean = permissions.clean_settings({"permission_categories": {"benign": "nonsense", "input": "auto"}})
        self.assertEqual(clean["permission_categories"]["benign"], "auto")   # kept default
        self.assertEqual(clean["permission_categories"]["input"], "auto")    # valid value accepted

    def test_invalid_override_value_is_dropped(self):
        clean = permissions.clean_settings({"permission_overrides": {"delete_path": "yolo", "observe": "deny"}})
        self.assertNotIn("delete_path", clean["permission_overrides"])
        self.assertEqual(clean["permission_overrides"]["observe"], "deny")

    def test_non_string_override_keys_are_ignored(self):
        clean = permissions.clean_settings({"permission_overrides": {42: "auto"}})
        self.assertEqual(clean["permission_overrides"], {})


class PartitionTests(unittest.TestCase):
    def test_splits_plain_tool_name_list_into_three_buckets(self):
        s = settings(permission_overrides={"delete_path": "deny"})
        auto, ask, denied = permissions.partition(["observe", "click_by_name", "delete_path"], s)
        self.assertEqual(auto, ["observe"])
        self.assertEqual(ask, ["click_by_name"])
        self.assertEqual(denied, ["delete_path"])

    def test_splits_tuples_by_first_element(self):
        s = settings()
        calls = [("observe", {}), ("click_by_name", {"name": "OK"})]
        auto, ask, denied = permissions.partition(calls, s)
        self.assertEqual(auto, [("observe", {})])
        self.assertEqual(ask, [("click_by_name", {"name": "OK"})])
        self.assertEqual(denied, [])


class BatchDecisionTests(unittest.TestCase):
    """Used by the deterministic desktop-router fast path (main.py's
    gate_batch), where a fixed batch of actions is one indivisible step."""

    def test_all_auto_is_auto(self):
        s = settings(permission_mode="god")
        self.assertEqual(permissions.batch_decision(["open_application", "click_by_name"], s), "auto")

    def test_any_ask_makes_the_whole_batch_ask(self):
        s = settings()  # open_application benign->auto, click_by_name input->ask
        self.assertEqual(permissions.batch_decision(["open_application", "click_by_name"], s), "ask")

    def test_any_deny_makes_the_whole_batch_deny_even_with_others_auto(self):
        s = settings(permission_mode="god", permission_overrides={"click_by_name": "deny"})
        self.assertEqual(permissions.batch_decision(["open_application", "click_by_name"], s), "deny")


class AllKnownToolsTests(unittest.TestCase):
    def test_returns_sorted_entries_with_tiers(self):
        result = permissions.all_known_tools(["delete_path", "observe", "click_by_name"])
        self.assertEqual([r["name"] for r in result], ["click_by_name", "delete_path", "observe"])
        tiers = {r["name"]: r["tier"] for r in result}
        self.assertEqual(tiers["delete_path"], "destructive")
        self.assertEqual(tiers["click_by_name"], "input")
        self.assertEqual(tiers["observe"], "benign")


if __name__ == "__main__":
    unittest.main(verbosity=2)

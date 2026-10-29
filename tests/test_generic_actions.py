"""Offline checks for generic_actions.py: the app-agnostic deterministic
semantic-action layer (connect/disconnect/start/stop/toggle) that replaced
the old per-app Proton VPN / TIDAL handlers in desktop_router.py.

Every test here uses a MADE-UP app name ("Streamly", "Aquarius", "Nimbus",
...) on purpose, to prove the module makes no app-specific assumption -
anything that only worked because of a Proton- or TIDAL-shaped fixture
would fail these tests.
"""
import unittest
from unittest.mock import patch

import generic_actions


class ChooseAndVerifyTests(unittest.TestCase):
    """Unit-level checks of the scoring/selection helper, independent of
    list_elements/act_element wiring."""

    def test_exact_label_beats_prefix_label(self):
        entries = [
            {"id": "e1", "name": "Connect Automatically", "enabled": True},
            {"id": "e2", "name": "Connect", "enabled": True},
        ]
        chosen = generic_actions._choose(entries, generic_actions.ACTION_SPECS["connect"])
        self.assertEqual(chosen["id"], "e2")

    def test_excluded_words_are_never_chosen(self):
        entries = [{"id": "e1", "name": "Connect Automatically Setting", "enabled": True}]
        chosen = generic_actions._choose(entries, generic_actions.ACTION_SPECS["connect"])
        self.assertIsNone(chosen)

    def test_disabled_controls_are_never_candidates(self):
        entries = [{"id": "e1", "name": "Connect", "enabled": False}]
        chosen = generic_actions._choose(entries, generic_actions.ACTION_SPECS["connect"])
        self.assertIsNone(chosen)

    def test_play_does_not_match_a_playlist_row(self):
        entries = [{"id": "e1", "name": "Play playlist Favorites", "enabled": True}]
        chosen = generic_actions._choose(entries, generic_actions.ACTION_SPECS["start"])
        self.assertIsNone(chosen)

    def test_space_shortcut_suffix_is_normalized_away(self):
        entries = [{"id": "e1", "name": "Play (Space)", "enabled": True}]
        chosen = generic_actions._choose(entries, generic_actions.ACTION_SPECS["start"])
        self.assertEqual(chosen["id"], "e1")

    def test_unsupported_action_word_is_rejected(self):
        result = generic_actions.run("Nimbus", "levitate")
        self.assertFalse(result["ok"])
        self.assertIn("Unsupported", result["message"])


class RunConnectDisconnectTests(unittest.TestCase):
    """These use 'Streamly', a made-up app, to prove nothing here is
    Proton-specific - only the generic 'connect'/'disconnect' vocabulary."""

    def test_already_in_target_state_is_not_clicked(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Disconnect", "enabled": True}]}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element") as act:
            result = generic_actions.run("Streamly", "connect")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        act.assert_not_called()

    def test_no_candidate_control_reports_visible_controls(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Settings", "enabled": True}]}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element") as act:
            result = generic_actions.run("Streamly", "connect")
        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_agent"])
        self.assertIn("Settings", result["message"])
        act.assert_not_called()

    def test_disabled_target_is_reported_without_clicking(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Connect", "enabled": False}]}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element") as act:
            result = generic_actions.run("Streamly", "connect")
        self.assertFalse(result["ok"])
        self.assertIn("DISABLED", result["message"])
        act.assert_not_called()

    def test_concrete_state_diff_is_trusted_immediately_without_polling(self):
        """When act_element's own before/after diff shows a real field
        change (not just 'no state field changed' / 'no longer exists'),
        that is trusted directly - no re-listing/polling needed at all."""
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Connect", "enabled": True}]}
        acted = {"ok": True, "verified": True,
                 "message": "toggle on Button 'Connect': value 'Off' -> 'On'"}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing) as list_el, \
             patch.object(generic_actions.elements, "act_element", return_value=acted) as act, \
             patch.object(generic_actions.time, "sleep") as sleep:
            result = generic_actions.run("Streamly", "connect")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        act.assert_called_once_with("e1", "click")
        list_el.assert_called_once()  # no poll re-listing was needed
        sleep.assert_not_called()

    def test_invoke_only_button_falls_back_to_polling_the_opposite_control(self):
        """A pure Invoke button with no native state (act_element reports
        'no state field changed') is inconclusive on its own - the module
        must poll for the opposite control to appear, exactly like the old
        Proton-specific logic did, but for ANY app."""
        before = {"ok": True, "entries": [{"id": "e1", "name": "Connect", "enabled": True}]}
        after = {"ok": True, "entries": [{"id": "e2", "name": "Disconnect", "enabled": True}]}
        acted = {"ok": True, "verified": True,
                 "message": "invoke on Button 'Connect': no state field changed (fine for buttons...)"}
        with patch.object(generic_actions.elements, "list_elements", side_effect=[before, after]), \
             patch.object(generic_actions.elements, "act_element", return_value=acted) as act, \
             patch.object(generic_actions.time, "sleep"):
            result = generic_actions.run("Streamly", "connect")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        act.assert_called_once_with("e1", "click")

    def test_disconnect_uses_the_disconnect_keyword_set(self):
        before = {"ok": True, "entries": [{"id": "d1", "name": "Disconnect", "enabled": True}]}
        after = {"ok": True, "entries": [{"id": "c1", "name": "Connect", "enabled": True}]}
        acted = {"ok": True, "verified": True, "message": "invoke on Button 'Disconnect': no state field changed"}
        with patch.object(generic_actions.elements, "list_elements", side_effect=[before, after]), \
             patch.object(generic_actions.elements, "act_element", return_value=acted) as act, \
             patch.object(generic_actions.time, "sleep"):
            result = generic_actions.run("Streamly", "disconnect")
        self.assertTrue(result["verified"])
        act.assert_called_once_with("d1", "click")

    def test_never_verified_after_polling_reports_needs_agent(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Connect", "enabled": True}]}
        acted = {"ok": True, "verified": True, "message": "invoke on Button 'Connect': no state field changed"}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element", return_value=acted), \
             patch.object(generic_actions.di, "find_window", return_value=(1, "Streamly")), \
             patch.object(generic_actions.time, "sleep"):
            result = generic_actions.run("Streamly", "connect", poll_attempts=3, poll_interval=0.01)
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["needs_agent"])

    def test_window_closing_twice_in_a_row_is_reported_as_interrupted(self):
        before = {"ok": True, "entries": [{"id": "e1", "name": "Connect", "enabled": True}]}
        gone = {"ok": False, "entries": [], "message": "Window not found"}
        acted = {"ok": True, "verified": True, "message": "invoke on Button 'Connect': no state field changed"}
        with patch.object(generic_actions.elements, "list_elements", side_effect=[before, gone, gone]), \
             patch.object(generic_actions.elements, "act_element", return_value=acted) as act, \
             patch.object(generic_actions.di, "find_window", return_value=None), \
             patch.object(generic_actions.time, "sleep"):
            result = generic_actions.run("Streamly", "connect")
        self.assertFalse(result["ok"])
        self.assertTrue(result["interrupted"])
        self.assertIn("closed", result["message"].casefold())
        act.assert_called_once_with("e1", "click")

    def test_act_element_failure_is_propagated(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Connect", "enabled": True}]}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element",
                          return_value={"ok": False, "message": "Action 'click' failed: element busy"}):
            result = generic_actions.run("Streamly", "connect")
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])

    def test_no_accessible_controls_is_reported_without_needs_agent_false_positive(self):
        listing = {"ok": True, "entries": [], "message": "No actionable elements were exposed."}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element") as act:
            result = generic_actions.run("Streamly", "connect")
        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_agent"])
        act.assert_not_called()


class RunStartStopTests(unittest.TestCase):
    """A completely different app/action pair ('Aquarius', start/stop)
    proves the vocabulary generalizes beyond connect/disconnect."""

    def test_play_alias_resolves_to_start(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Play", "enabled": True}]}
        acted = {"ok": True, "verified": True, "message": "invoke on Button 'Play': no state field changed"}
        after = {"ok": True, "entries": [{"id": "e2", "name": "Pause", "enabled": True}]}
        with patch.object(generic_actions.elements, "list_elements", side_effect=[listing, after]), \
             patch.object(generic_actions.elements, "act_element", return_value=acted) as act, \
             patch.object(generic_actions.time, "sleep"):
            result = generic_actions.run("Aquarius", "play")
        self.assertTrue(result["verified"])
        act.assert_called_once_with("e1", "click")

    def test_pause_alias_resolves_to_stop(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Pause", "enabled": True}]}
        acted = {"ok": True, "verified": True, "message": "invoke on Button 'Pause': no state field changed"}
        after = {"ok": True, "entries": [{"id": "e2", "name": "Play", "enabled": True}]}
        with patch.object(generic_actions.elements, "list_elements", side_effect=[listing, after]), \
             patch.object(generic_actions.elements, "act_element", return_value=acted) as act, \
             patch.object(generic_actions.time, "sleep"):
            result = generic_actions.run("Aquarius", "pause")
        self.assertTrue(result["verified"])
        act.assert_called_once_with("e1", "click")

    def test_already_playing_is_not_clicked_again(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Pause", "enabled": True}]}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element") as act:
            result = generic_actions.run("Aquarius", "play")
        self.assertTrue(result["ok"])
        act.assert_not_called()


class ToggleTests(unittest.TestCase):
    def test_single_toggle_control_is_flipped_and_verified_via_toggle_state(self):
        listing = {"ok": True, "entries": [
            {"id": "e1", "name": "Dark Mode", "enabled": True, "toggle_state": 0},
            {"id": "e2", "name": "Save", "enabled": True, "toggle_state": None},
        ]}
        acted = {"ok": True, "verified": True, "message": "toggle on CheckBox 'Dark Mode': toggle 0 -> 1"}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element", return_value=acted) as act:
            result = generic_actions.toggle("Nimbus")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        act.assert_called_once_with("e1", "toggle")

    def test_multiple_toggle_controls_without_a_hint_are_ambiguous(self):
        listing = {"ok": True, "entries": [
            {"id": "e1", "name": "Dark Mode", "enabled": True, "toggle_state": 0},
            {"id": "e2", "name": "Auto-save", "enabled": True, "toggle_state": 1},
        ]}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element") as act:
            result = generic_actions.toggle("Nimbus")
        self.assertFalse(result["ok"])
        self.assertIn("2 possible toggles", result["message"])
        act.assert_not_called()

    def test_name_hint_disambiguates_multiple_toggles(self):
        listing = {"ok": True, "entries": [
            {"id": "e1", "name": "Dark Mode", "enabled": True, "toggle_state": 0},
            {"id": "e2", "name": "Auto-save", "enabled": True, "toggle_state": 1},
        ]}
        acted = {"ok": True, "verified": True, "message": "toggle on CheckBox 'Auto-save': toggle 1 -> 0"}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element", return_value=acted) as act:
            result = generic_actions.toggle("Nimbus", name_hint="auto-save")
        self.assertTrue(result["ok"])
        act.assert_called_once_with("e2", "toggle")

    def test_no_toggle_pattern_control_is_reported_not_guessed(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Save", "enabled": True, "toggle_state": None}]}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element") as act:
            result = generic_actions.toggle("Nimbus")
        self.assertFalse(result["ok"])
        act.assert_not_called()

    def test_toggle_verification_does_not_depend_on_the_word_toggle_in_the_message(self):
        """elements.act_element can fall back to click_input() internally
        when wrapper.toggle() raises, so its message may say 'click_input',
        not 'toggle' - verification must still work from the real state
        diff, not a substring of act_element's own log text."""
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Dark Mode", "enabled": True, "toggle_state": 0}]}
        acted = {"ok": True, "verified": True,
                 "message": "click_input on CheckBox 'Dark Mode': toggle 0 -> 1"}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element", return_value=acted):
            result = generic_actions.toggle("Nimbus")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])

    def test_toggle_with_no_state_change_is_not_verified(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Dark Mode", "enabled": True, "toggle_state": 0}]}
        acted = {"ok": True, "verified": True,
                 "message": "click_input on CheckBox 'Dark Mode': no state field changed (fine for buttons...)"}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing), \
             patch.object(generic_actions.elements, "act_element", return_value=acted):
            result = generic_actions.toggle("Nimbus")
        self.assertFalse(result["verified"])


class StatusTests(unittest.TestCase):
    def test_opposite_control_present_means_connected(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Disconnect", "enabled": True}]}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing):
            result = generic_actions.status("Streamly", "connect")
        self.assertTrue(result["verified"])
        self.assertIn("connected", result["message"])

    def test_own_control_present_means_not_yet_connected(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Connect", "enabled": True}]}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing):
            result = generic_actions.status("Streamly", "connect")
        self.assertTrue(result["verified"])
        self.assertIn("disconnected", result["message"])

    def test_ambiguous_controls_are_not_guessed(self):
        listing = {"ok": True, "entries": [{"id": "e1", "name": "Settings", "enabled": True}]}
        with patch.object(generic_actions.elements, "list_elements", return_value=listing):
            result = generic_actions.status("Streamly", "connect")
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

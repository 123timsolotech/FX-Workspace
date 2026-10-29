"""Exercise media-session behavior without Windows or an actual player."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import desktop_router
import intent_router
import media_session


class Session:
    def __init__(self, source="TIDAL.exe", state="PLAYING", stop=True, pause=True,
                 previous=True, next_=True, accept=True, change=True):
        self.source_app_user_model_id = source
        self.state, self.accept, self.change = state, accept, change
        self.controls = SimpleNamespace(
            is_stop_enabled=stop, is_pause_enabled=pause, is_play_enabled=True,
            is_previous_enabled=previous, is_next_enabled=next_)
        self.commands = []

    def get_playback_info(self):
        return SimpleNamespace(playback_status=SimpleNamespace(name=self.state), controls=self.controls)

    async def try_stop_async(self):
        self.commands.append("stop")
        if self.accept and self.change:
            self.state = "STOPPED"
        return self.accept

    async def try_pause_async(self):
        self.commands.append("pause")
        if self.accept and self.change:
            self.state = "PAUSED"
        return self.accept

    async def try_play_async(self):
        self.commands.append("play")
        if self.accept and self.change:
            self.state = "PLAYING"
        return self.accept

    async def try_skip_previous_async(self):
        self.commands.append("previous")
        return self.accept

    async def try_skip_next_async(self):
        self.commands.append("next")
        return self.accept


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def control(self, sessions, action="stop", app="tidal"):
        manager = SimpleNamespace(get_sessions=lambda: sessions)
        return await media_session._control(manager, app, action)

    async def test_targets_tidal_not_the_other_player(self):
        tidal, other = Session(), Session("Spotify.exe")
        result = await self.control([other, tidal])
        self.assertTrue(result["verified"])
        self.assertEqual(tidal.commands, ["stop"])
        self.assertEqual(other.commands, [])

    async def test_paused_is_left_untouched(self):
        session = Session(state="PAUSED")
        result = await self.control([session])
        self.assertTrue(result["verified"])
        self.assertEqual(session.commands, [])

    async def test_pause_only_player_uses_explicit_pause(self):
        session = Session(stop=False)
        result = await self.control([session])
        self.assertTrue(result["verified"])
        self.assertIn("paused", result["message"])
        self.assertEqual(session.commands, ["pause"])

    async def test_missing_session_never_controls_another_app(self):
        other = Session("Spotify.exe")
        result = await self.control([other])
        self.assertFalse(result["ok"])
        self.assertEqual(other.commands, [])

    async def test_ambiguous_sessions_send_nothing(self):
        sessions = [Session(), Session("TIDALMusicAS.TIDAL_abc!App")]
        result = await self.control(sessions)
        self.assertFalse(result["ok"])
        self.assertEqual([s.commands for s in sessions], [[], []])

    async def test_rejected_command_is_not_success(self):
        result = await self.control([Session(accept=False)])
        self.assertFalse(result["verified"])

    async def test_accepted_but_unchanged_is_not_success_or_retried(self):
        session = Session(change=False)
        with patch.object(media_session.asyncio, "sleep", new=AsyncMock()):
            result = await self.control([session])
        self.assertFalse(result["verified"])
        self.assertEqual(session.commands, ["stop"])

    async def test_unknown_state_is_not_toggled(self):
        session = Session(state="CHANGING")
        result = await self.control([session])
        self.assertFalse(result["ok"])
        self.assertEqual(session.commands, [])

    async def test_play_resumes_paused_target_without_focusing(self):
        session = Session(state="PAUSED")
        result = await self.control([session], "play")
        self.assertTrue(result["verified"])
        self.assertEqual(session.commands, ["play"])

    async def test_playing_target_is_left_untouched(self):
        session = Session(state="PLAYING")
        result = await self.control([session], "play")
        self.assertTrue(result["verified"])
        self.assertEqual(session.commands, [])

    async def test_previous_targets_the_only_playing_session(self):
        tidal = Session()
        paused_other = Session("Spotify.exe", state="PAUSED")
        result = await self.control([paused_other, tidal], "previous", "active media")
        self.assertTrue(result["verified"])
        self.assertEqual(tidal.commands, ["previous"])
        self.assertEqual(paused_other.commands, [])

    async def test_previous_with_multiple_playing_sessions_sends_nothing(self):
        sessions = [Session(), Session("Spotify.exe")]
        result = await self.control(sessions, "previous", "active media")
        self.assertFalse(result["ok"])
        self.assertEqual([session.commands for session in sessions], [[], []])

    async def test_named_next_never_controls_another_player(self):
        tidal, other = Session(), Session("Spotify.exe")
        result = await self.control([other, tidal], "next", "tidal")
        self.assertTrue(result["verified"])
        self.assertEqual(tidal.commands, ["next"])
        self.assertEqual(other.commands, [])

    async def test_stop_never_opens_or_focuses_window(self):
        with patch.object(desktop_router.media_session, "control", return_value={"ok": True, "verified": True}) as control, \
             patch.object(desktop_router, "_open_app") as open_app:
            result = desktop_router.execute(intent_router.route("stop music in tidal"))
        self.assertTrue(result["ok"])
        control.assert_called_once_with("tidal", "stop")
        open_app.assert_not_called()

    async def test_play_with_existing_session_never_opens_or_focuses_window(self):
        with patch.object(desktop_router.media_session, "control", return_value={"ok": True, "verified": True}) as control, \
             patch.object(desktop_router, "_open_app") as open_app:
            result = desktop_router.execute(intent_router.route("play music on tidal"))
        self.assertTrue(result["ok"])
        control.assert_called_once_with("tidal", "play")
        open_app.assert_not_called()

    async def test_previous_never_opens_or_focuses_window(self):
        with patch.object(desktop_router.media_session, "control", return_value={"ok": True, "verified": True}) as control, \
             patch.object(desktop_router, "_open_app") as open_app:
            result = desktop_router.execute(intent_router.route("Prev song"))
        self.assertTrue(result["ok"])
        control.assert_called_once_with("active media", "previous")
        open_app.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

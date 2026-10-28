"""1.4.4 Stage 1 fixes."""
import unittest
from unittest.mock import patch

import history

import core.state as shared
from agent import models

IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def convo(image_turn_is_last: bool):
    old = [{"role": "system", "content": "sys"},
           {"role": "user", "content": [{"type": "text", "text": "what is this?"}, IMG]},
           {"role": "assistant", "content": "A cat."}]
    if image_turn_is_last:
        return old
    return old + [{"role": "user", "content": "write me a poem"}]


class EarlierImagesTests(unittest.TestCase):
    """An image from an earlier turn no longer swaps a text model for a vision one on every message."""

    def test_this_turn(self):
        self.assertTrue(history.images_this_turn(convo(True)))
        self.assertFalse(history.images_this_turn(convo(False)))

    def test_stub_keeps_text_and_drops_only_earlier_images(self):
        out = history.stub_earlier_images(convo(False))
        self.assertFalse(history.has_images(out))
        self.assertIn("what is this?", out[1]["content"])
        self.assertIn("not sent", out[1]["content"])
        self.assertTrue(history.has_images(history.stub_earlier_images(convo(True))))

    def test_text_model_stays_for_an_old_image(self):
        with patch.dict(shared.app_state, {"local_engaged": "Qwen-3.5-4B-Q4_K_M"}), \
                patch.object(models.llama_local, "is_vision_model", side_effect=lambda n: "Vision" in n):
            msgs, vision = models._local_payload(convo(False))
            self.assertFalse(vision)
            self.assertFalse(history.has_images(msgs))
            self.assertTrue(models._local_payload(convo(True))[1])         # a new image still escalates

    def test_loaded_vision_model_keeps_old_images(self):
        with patch.dict(shared.app_state, {"local_engaged": "Gemma-4-Vision-Q8"}), \
                patch.object(models.llama_local, "is_vision_model", side_effect=lambda n: "Vision" in n):
            msgs, vision = models._local_payload(convo(False))
            self.assertTrue(vision)
            self.assertTrue(history.has_images(msgs))


class ResearchRetryTests(unittest.TestCase):
    def test_retry_words_pick_up_the_last_research(self):
        from agent import research
        for text in ("retry", "Retry.", "try again", "resume", "continue the research", "redo it", "keep going"):
            self.assertTrue(research.wants_resume(text), text)
        for text in ("retry logic in python", "continue reading about Rome", "again and again by Bowie"):
            self.assertFalse(research.wants_resume(text), text)


class LoadingNoticeTests(unittest.TestCase):
    def test_a_reply_queued_behind_a_load_says_loading(self):
        import local_runtime
        ev = {"status": "running", "phase": None}
        with patch.dict(shared.app_state, {"running_model_event": ev}), \
                patch.dict(local_runtime.loading, {"model": "Qwen-3.5-4B-Q4_K_M", "since": 100.0}), \
                patch.object(models.run_control, "checkpoint"), \
                patch.object(models.event_log, "update_event", side_effect=lambda e, **k: e.update(k)):
            models._queued_wait()
        self.assertEqual((ev["phase"], ev["loading"], ev["loading_since"]), ("loading", "Qwen-3.5-4B-Q4_K_M", 100.0))


class CalmMotionTests(unittest.TestCase):
    page = open("index.html", encoding="utf-8").read()

    def test_phrases_and_marks_change_only_for_a_reason(self):
        self.assertNotIn("claySay(el) || pick(el._phrase)", self.page)          # no new phrase every 2.5 s
        self.assertIn("el._want = act; clearTimeout(el._wantT);", self.page)      # a change must last 0.6 s
        self.assertNotIn("(i + t / 1.2) % 3", self.page)                          # no colour cycling

    def test_icon_less_sites_get_a_coloured_tile(self):
        self.assertIn('style="--fh:${hue}"', self.page)
        self.assertIn("background: hsl(var(--fh, 220) 38% 44%)", self.page)


if __name__ == "__main__":
    unittest.main()

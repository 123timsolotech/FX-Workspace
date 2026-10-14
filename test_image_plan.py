"""Think first and sequences (image_plan.py, image_gen.generate_planned / generate_sequence) with the models and the
engine mocked. Nothing is written into data/."""
import os
import tempfile
import unittest
from unittest import mock

import image_gen
import image_plan


class TagRules(unittest.TestCase):
    def test_counts_become_danbooru_tags_and_drop_solo(self):
        self.assertEqual(image_plan.fix_counts("two girls, solo, park"), "2girls, multiple girls, park")
        self.assertEqual(image_plan.fix_counts("1girl, solo, smile"), "1girl, solo, smile")
        self.assertEqual(image_plan.fix_counts("1boy, 1girl, solo"), "1boy, 1girl")
        self.assertEqual(image_plan.fix_counts("three women"), "3girls, multiple girls")

    def test_negative_never_fights_the_request(self):
        req = "a girl holding an ice cream cone, smiling"
        self.assertEqual(image_plan.safe_negative("no ice cream cone, smiling, extra arms", req, "1girl, ice cream cone"),
                         "extra arms")

    def test_score_tags_are_never_planned(self):
        self.assertEqual(image_plan._clean_tags("score_9, source_anime, rating_safe, 1girl"), "1girl")

    def test_json_inside_prose(self):
        self.assertEqual(image_plan._json('Sure! ```json\n{"tags": "1girl",}\n```')["tags"], "1girl")


class Flows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(image_gen, "IMAGES_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

        def fake_generate(prompt, quality, size=None, seed=None, negative="", **kw):
            n = len(self.calls)
            self.calls.append({"prompt": prompt, "negative": negative, "seed": seed, **kw})
            iid = f"20261014-000000-{n:06x}"
            path = os.path.join(self.tmp.name, iid + ".png")
            with open(path, "wb") as f:
                f.write(b"png")
            meta = {"id": iid, "prompt": prompt, **(kw.get("extra_meta") or {})}
            with open(os.path.join(self.tmp.name, iid + ".json"), "w", encoding="utf-8") as f:
                import json
                json.dump(meta, f)
            return {**meta, "path": path, "url": f"/api/images/{iid}.png", "images": []}
        for p in (mock.patch.object(image_gen, "generate", side_effect=fake_generate),
                  mock.patch.object(image_gen, "stop")):
            p.start()
            self.addCleanup(p.stop)

    def test_planned_picture_uses_the_plan_and_fixes_once(self):
        plan = {"thinking": "t", "tags": "2girls, multiple girls, bench", "negative": "", "checks": ["two girls"], "model": "m"}
        check = {"ok": False, "problems": ["only one girl"], "add_tags": "2girls", "add_negative": "solo", "model": "v"}
        with mock.patch.object(image_plan, "plan", return_value=plan), \
             mock.patch.object(image_plan, "check", return_value=check), \
             mock.patch("PIL.Image.open"):
            r = image_gen.generate_planned("two girls on a bench", "pony6", check_result=True)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[0]["prompt"].startswith("score_9"))        # base Pony gets its score tags here
        self.assertIn("solo", self.calls[1]["negative"])
        self.assertEqual(r["plan"]["tags"], plan["tags"])
        self.assertEqual(self.calls[1]["extra_meta"]["plan"]["fix"], ["only one girl"])
        self.assertEqual(image_gen._planning, {})                           # status is clear afterwards

    def test_presets_keep_their_own_prefix(self):
        plan = {"thinking": "", "tags": "1girl, solo", "negative": "", "checks": [], "model": "m"}
        with mock.patch.object(image_plan, "plan", return_value=plan):
            image_gen.generate_planned("a girl", "pony6_anime", check_result=False)
        self.assertEqual(self.calls[0]["prompt"], "1girl, solo")               # the preset's prefix is added later

    def test_frames_all_start_from_frame_one(self):
        seq = {"thinking": "", "identity": "1girl, red dress", "negative": "", "model": "m",
               "pictures": [{"label": "a", "tags": "arm down"}, {"label": "b", "tags": "arm up"},
                            {"label": "c", "tags": "waving"}]}
        with mock.patch.object(image_plan, "plan_sequence", return_value=seq):
            r = image_gen.generate_sequence("a girl waving", "pony6", "frames", 3, seed=7)
        self.assertEqual(len(r["images"]), 3)
        self.assertIsNone(self.calls[0]["init_image"])
        self.assertEqual(self.calls[1]["init_image"], b"png")
        self.assertTrue(all(c["seed"] == 7 for c in self.calls))
        self.assertEqual([m["seq"]["index"] for m in image_gen.sequence(r["sequence"])], [0, 1, 2])

    def test_angles_without_thinking_use_the_camera_list(self):
        r = image_gen.generate_sequence("a knight", "pony6", "angles", 3, think=False)
        self.assertTrue(all(c.get("init_image") is None for c in self.calls))
        self.assertIn("from front", self.calls[0]["prompt"])
        self.assertIn("from side", self.calls[2]["prompt"])
        self.assertEqual(r["plan"]["identity"], "a knight")

    def test_only_pony(self):
        with self.assertRaises(image_gen.ImageError):
            image_gen.generate_planned("x", "fast")


if __name__ == "__main__":
    unittest.main()

"""Expand a picture (outpainting, 1.4.5): the canvas plan, the mask and the original put back exactly. No GPU."""
import io
import unittest
from unittest.mock import patch

from PIL import Image

import image_gen


def png(w, h, color=(200, 40, 40)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


class PlanTests(unittest.TestCase):
    def test_left_and_right_by_half(self):
        p = image_gen.expand_plan(1024, 1024, ["left", "right"], 0.5)
        self.assertEqual((p["w"] % 64, p["h"] % 64), (0, 0))
        self.assertLessEqual(max(p["w"], p["h"]), image_gen.EXPAND_MAX_SIDE)
        self.assertLessEqual(p["w"] * p["h"], image_gen.EXPAND_MAX_PIXELS)
        self.assertGreater(p["w"], p["h"])                           # wider, not taller
        self.assertAlmostEqual(p["left"], (p["w"] - p["pw"]) / 2, delta=2)
        self.assertEqual(p["top"], 0)

    def test_one_side_only_and_defaults(self):
        p = image_gen.expand_plan(832, 1216, ["bottom"], 0.25)
        self.assertEqual((p["left"], p["top"]), (0, 0))
        self.assertGreater(p["h"] / p["w"], 1216 / 832)
        self.assertEqual(image_gen.expand_plan(512, 512, None, 0.5)["sides"], list(image_gen.EXPAND_SIDES))
        self.assertEqual(image_gen.expand_plan(512, 512, ["nonsense"], 9)["sides"], list(image_gen.EXPAND_SIDES))


class ImagesTests(unittest.TestCase):
    def test_mask_covers_the_new_area_and_the_original_comes_back(self):
        plan = image_gen.expand_plan(512, 512, ["right"], 0.5)
        start, mask, placed, alpha = image_gen._expand_images(png(512, 512), plan)
        m = Image.open(io.BytesIO(mask))
        self.assertEqual(m.size, (plan["w"], plan["h"]))
        self.assertGreater(m.getpixel((plan["w"] - 5, plan["h"] // 2)), 240)      # the new area: draw
        self.assertLess(m.getpixel((5, plan["h"] // 2)), 15)                       # deep in the original: keep
        self.assertGreater(alpha.getpixel((10, 10)), 240)                          # the original's pixels go back
        self.assertEqual(alpha.getpixel((plan["w"] - 5, 10)), 0)

    def test_expand_draws_only_the_mask_and_keeps_the_picture(self):
        sent = {}

        def fake_generate(prompt, quality, size, seed, negative, **kw):
            sent.update(prompt=prompt, quality=quality, size=size, **kw)
            w, h = map(int, size.split("x"))
            out = kw["after"](png(w, h, (0, 0, 255)))                              # the "engine" paints all blue
            return {"ok": True, "png": out}
        meta = {"width": 512, "height": 512, "prompt": "a red wall", "quality": "balanced", "negative": ""}
        with patch.object(image_gen, "find", return_value=(meta, "x")), \
                patch.object(image_gen, "image_bytes", return_value=png(512, 512)), \
                patch.object(image_gen, "generate", side_effect=fake_generate):
            r = image_gen.expand("20261029-120000-abcdef", ["right"], 0.5)
        self.assertEqual(sent["quality"], "balanced")
        self.assertIn("a red wall", sent["prompt"])
        self.assertEqual(sent["strength"], image_gen.EXPAND_STRENGTH)
        self.assertIsNotNone(sent["mask_image"])
        self.assertEqual(sent["extra_meta"]["expanded_from"], "20261029-120000-abcdef")
        out = Image.open(io.BytesIO(r["png"])).convert("RGB")
        self.assertEqual(out.getpixel((20, 20)), (200, 40, 40))                    # the original, exact
        # The new area, drawn - and colour-matched to the original next to it (pure blue becomes the wall's red):
        # the slight relighting a model does left visible seams on a live test.
        r2, g2, b2 = out.getpixel((out.width - 5, 20))
        self.assertTrue(abs(r2 - 200) < 6 and b2 < 50, (r2, g2, b2))


class PageTests(unittest.TestCase):
    def test_the_expand_control(self):
        page = open("index.html", encoding="utf-8").read()
        self.assertIn("ImagesView.expandOpen(", page)
        self.assertIn("/expand`, { sides:", page)


if __name__ == "__main__":
    unittest.main()

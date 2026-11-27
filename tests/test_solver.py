import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solver import Box, Rect, plan_piece, plan_state  # noqa: E402
from samples import PINWHEEL  # noqa: E402


def boxes_from_sample(sample):
    return [
        Box(i + 1, f["x"], f["y"], f["x"] + f["w"], f["y"] + f["h"], f["name"])
        for i, f in enumerate(sample["frames"])
    ]


class SolverTests(unittest.TestCase):
    def test_single_interior_frame(self):
        boxes = [Box(1, 10, 10, 50, 50, "F")]
        p = plan_piece(1, Rect(0, 0, 100, 100), boxes)
        self.assertEqual(p["score"], 1)
        self.assertEqual(len(p["products"]), 1)
        # Four edge trims are needed.
        self.assertEqual(p["cut_count"], 4)
        for st in p["steps"]:
            x0, y0, x1, y1 = st["piece_rect"]
            if st["orient"] == "V":
                self.assertTrue(x0 < st["position"] < x1)
            else:
                self.assertTrue(y0 < st["position"] < y1)

    def test_exact_full_sheet_is_product_no_cuts(self):
        boxes = [Box(7, 0, 0, 40, 30, "full")]
        p = plan_piece(9, Rect(0, 0, 40, 30), boxes)
        self.assertEqual(p["score"], 1)
        self.assertEqual(p["cut_count"], 0)
        self.assertEqual(p["steps"], [])

    def test_two_disjoint_frames_both_saved(self):
        boxes = [Box(1, 5, 5, 45, 95, "L"), Box(2, 55, 5, 95, 95, "R")]
        p = plan_piece(1, Rect(0, 0, 100, 100), boxes)
        self.assertEqual(p["score"], 2)

    def test_pinwheel_max_four_and_sacrifices_small(self):
        boxes = boxes_from_sample(PINWHEEL)
        p = plan_piece(1, Rect(0, 0, 100, 100), boxes)
        self.assertEqual(p["score"], 4)
        saved = {q["name"] for q in p["products"]}
        self.assertNotIn("小画 D", saved)
        self.assertEqual(
            saved, {"A 大画", "B 大画", "C 大画", "E 中画"}
        )
        # First recommended cut is the horizontal y=72 that abandons D.
        first = p["steps"][0]
        self.assertEqual(first["orient"], "H")
        self.assertEqual(first["position"], 72)

    def test_plan_state_across_multiple_roots(self):
        boxes = [Box(1, 2, 2, 8, 8, "F")]
        state = plan_state(
            [(1, Rect(0, 0, 10, 10)), (2, Rect(20, 20, 30, 30))], boxes
        )
        # Frame belongs only to root #1.
        self.assertEqual(state["max_more"], 1)
        root_ids = {s["piece_id"] for s in state["steps"]}
        self.assertIn(1, root_ids)

    def test_every_single_frame_is_always_extractable(self):
        # Any one frame inside a piece must always be achievable.
        import random

        rnd = random.Random(123)
        for _ in range(200):
            w = rnd.randint(20, 200)
            h = rnd.randint(20, 200)
            fw = rnd.randint(1, w)
            fh = rnd.randint(1, h)
            x = rnd.randint(0, w - fw)
            y = rnd.randint(0, h - fh)
            b = Box(1, x, y, x + fw, y + fh, "F")
            p = plan_piece(1, Rect(0, 0, w, h), [b])
            self.assertEqual(p["score"], 1)


if __name__ == "__main__":
    unittest.main()

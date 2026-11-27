"""Guillotine-cut optimizer for fixed rectangular print frames.

Coordinates are integer millimetres, origin at the upper-left corner
(y grows downward).  A cut is either vertical at coordinate ``x`` or
horizontal at coordinate ``y`` and runs through the whole current piece.
The saw kerf is zero.  A frame is a finished product only when a leaf piece
has exactly the same four edges.

Optimisation objective, lexicographic:

1. maximise number of complete frames;
2. among equal-count plans, maximise total saved frame area
   (equivalently abandon the smallest possible amount of print);
3. among ties, use the fewest cuts.

The planner is a memoised recursion over rectangles.  In an optimal plan
every useful cut can be placed on an edge coordinate of one of the frames
inside the current rectangle: moving a cut within an open interval between
frame edges to an endpoint never worsens the left/right partition and may
only align an additional edge.  Therefore only frame-edge positions are
tried.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Box:
    """A rectangle in absolute sheet coordinates."""

    id: int
    x0: int
    y0: int
    x1: int
    y1: int
    name: str = ""

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class Rect:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)


def rect_contains(r: "Rect | Box", other: "Rect | Box") -> bool:
    return (
        r.x0 <= other.x0
        and other.x1 <= r.x1
        and r.y0 <= other.y0
        and other.y1 <= r.y1
    )


# A chosen action: (orient, pos, child_a_rect, child_b_rect)
Action = Tuple[str, int, Rect, Rect]
# Objective vector: (frame_count, saved_area, cut_count)
Obj = Tuple[int, int, int]
# memo value: (count, area, cuts, action_or_none)
MemoValue = Tuple[int, int, int, Optional[Action]]


def _exact_frame(r: Rect, boxes: Sequence[Box]) -> Optional[Box]:
    for b in boxes:
        if (b.x0, b.y0, b.x1, b.y1) == r.as_tuple():
            return b
    return None


def _contained(r: Rect, boxes: Sequence[Box]) -> List[Box]:
    return [b for b in boxes if rect_contains(r, b)]


def _crossed_by(r: Rect, orient: str, pos: int, boxes: Sequence[Box]) -> List[Box]:
    """Frames whose *interior* is intersected by the cut segment across r."""
    out: List[Box] = []
    for b in _contained(r, boxes):
        if orient == "V":
            if b.x0 < pos < b.x1:
                out.append(b)
        else:
            if b.y0 < pos < b.y1:
                out.append(b)
    return out


def _better(cand: Obj, best: Obj) -> bool:
    """Lexicographic: more frames, more saved area, fewer cuts."""
    c_count, c_area, c_cuts = cand
    b_count, b_area, b_cuts = best
    if c_count != b_count:
        return c_count > b_count
    if c_area != b_area:
        return c_area > b_area
    return c_cuts < b_cuts


def plan_piece(
    piece_id: int,
    root: Rect,
    boxes: Sequence[Box],
) -> Dict:
    """Compute the best plan for one current leaf piece.

    Returns a JSON-friendly dict with keys:
    ``piece_id, rect, score, cut_count, steps, products``.
    ``steps`` is a pre-order flattened cut sequence; every step names the
    (provisional) piece label to cut, orientation and absolute position.
    """

    memo: Dict[Tuple[int, int, int, int], MemoValue] = {}

    def dp(r: Rect) -> MemoValue:
        key = r.as_tuple()
        cached = memo.get(key)
        if cached is not None:
            return cached

        inside = _contained(r, boxes)

        # This piece itself is a finished print: do not cut it.
        exact = _exact_frame(r, inside)
        if exact is not None:
            res: MemoValue = (1, exact.area, 0, None)
            memo[key] = res
            return res

        best: Obj = (0, 0, 0)
        best_action: Optional[Action] = None

        def consider(action: Action, a: MemoValue, b: MemoValue) -> None:
            nonlocal best, best_action
            cand: Obj = (
                a[0] + b[0],
                a[1] + b[1],
                1 + a[2] + b[2],
            )
            if best_action is None or _better(cand, best):
                best = cand
                best_action = action

        # Candidate vertical cuts, deterministic ascending positions.
        xs = sorted(
            {
                e
                for fr in inside
                for e in (fr.x0, fr.x1)
                if r.x0 < e < r.x1
            }
        )
        for x in xs:
            left = Rect(r.x0, r.y0, x, r.y1)
            right = Rect(x, r.y0, r.x1, r.y1)
            consider(("V", x, left, right), dp(left), dp(right))

        # Candidate horizontal cuts, deterministic ascending positions.
        ys = sorted(
            {
                e
                for fr in inside
                for e in (fr.y0, fr.y1)
                if r.y0 < e < r.y1
            }
        )
        for y in ys:
            top = Rect(r.x0, r.y0, r.x1, y)
            bottom = Rect(r.x0, y, r.x1, r.y1)
            consider(("H", y, top, bottom), dp(top), dp(bottom))

        res = (best[0], best[1], best[2], best_action)
        memo[key] = res
        return res

    score, saved_area, n_cuts, _ = dp(root)

    steps: List[Dict] = []
    products: List[Dict] = []

    def emit(r: Rect, label: str, real_id: Optional[int]) -> None:
        _, _, _, action = dp(r)
        exact = _exact_frame(r, _contained(r, boxes))
        if action is None:
            if exact is not None:
                products.append(
                    {
                        "frame_id": exact.id,
                        "name": exact.name,
                        "piece_label": label,
                        "rect": list(r.as_tuple()),
                    }
                )
            return

        orient, pos, ra, rb = action
        if orient == "V":
            la, lb = f"{label}·左", f"{label}·右"
        else:
            la, lb = f"{label}·上", f"{label}·下"

        ruins = _crossed_by(r, orient, pos, boxes)
        step = {
            "piece_id": real_id,
            "piece_label": label,
            "piece_rect": list(r.as_tuple()),
            "orient": orient,
            "position": pos,
            "position_axis": "x" if orient == "V" else "y",
            "child_labels": [la, lb],
            "child_rects": [list(ra.as_tuple()), list(rb.as_tuple())],
            "ruins": [{"frame_id": bx.id, "name": bx.name} for bx in ruins],
        }
        steps.append(step)
        emit(ra, la, None)
        emit(rb, lb, None)

    root_label = f"#{piece_id}"
    emit(root, root_label, piece_id)

    return {
        "piece_id": piece_id,
        "rect": list(root.as_tuple()),
        "score": score,
        "saved_area": saved_area,
        "cut_count": n_cuts,
        "steps": steps,
        "products": products,
    }


def plan_state(
    roots: Sequence[Tuple[int, Rect]],
    boxes: Sequence[Box],
) -> Dict:
    """Plan across all current leaf pieces (independent subproblems)."""

    per_piece: List[Dict] = []
    all_steps: List[Dict] = []
    all_products: List[Dict] = []
    total = 0

    for piece_id, rect in roots:
        plan = plan_piece(piece_id, rect, boxes)
        total += plan["score"]
        for step in plan["steps"]:
            step = dict(step)
            all_steps.append(step)
        all_products.extend(plan["products"])
        per_piece.append(
            {
                "piece_id": piece_id,
                "score": plan["score"],
                "saved_area": plan["saved_area"],
                "cut_count": plan["cut_count"],
                "steps": plan["steps"],
                "products": plan["products"],
                "rect": plan["rect"],
            }
        )

    for g, step in enumerate(all_steps, start=1):
        step["global_seq"] = g

    saved_ids = {p["frame_id"] for p in all_products}
    abandoned = [
        {"frame_id": bx.id, "name": bx.name, "area": bx.area}
        for bx in boxes
        if bx.id not in saved_ids
    ]

    return {
        "max_more": total,
        "steps": all_steps,
        "products": all_products,
        "abandoned": abandoned,
        "pieces": per_piece,
    }

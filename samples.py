"""Built-in sample sheets used by the web UI and demo script."""

from __future__ import annotations

from typing import Any, Dict, List

# A five-frame "pinwheel / windmill" arrangement.  Every vertical line in the
# sheet is strictly inside at least one frame's x-span, and every horizontal
# line is strictly inside at least one frame's y-span, so no safe first
# guillotine cut exists: at least one print must be abandoned.
# The smallest frame is "小画 D"; among the count-4 optima the planner's
# saved-area tie-break chooses to sacrifice D.
PINWHEEL: Dict[str, Any] = {
    "key": "pinwheel",
    "label": "必须舍弃小画：风车式五画（100×100）",
    "name": "风车五画-必舍小画",
    "paper_w": 100,
    "paper_h": 100,
    "frames": [
        {"name": "A 大画", "x": 0,  "y": 0,  "w": 70, "h": 55},
        {"name": "B 大画", "x": 70, "y": 0,  "w": 30, "h": 72},
        {"name": "C 大画", "x": 15, "y": 72, "w": 85, "h": 28},
        {"name": "小画 D", "x": 0,  "y": 55, "w": 15, "h": 45},
        {"name": "E 中画", "x": 15, "y": 55, "w": 55, "h": 17},
    ],
}

# A simple sheet where all three frames survive; handy for trying an
# off-target trim that only creates a sliver.
TRIM: Dict[str, Any] = {
    "key": "trim",
    "label": "偏刀续排练习：三画 + 宽边（120×90）",
    "name": "三画宽边-偏刀练习",
    "paper_w": 120,
    "paper_h": 90,
    "frames": [
        {"name": "左画", "x": 20, "y": 15, "w": 40, "h": 60},
        {"name": "右上画", "x": 70, "y": 15, "w": 35, "h": 25},
        {"name": "右下画", "x": 70, "y": 50, "w": 35, "h": 25},
    ],
}

SAMPLES: List[Dict[str, Any]] = [PINWHEEL, TRIM]
SAMPLE_BY_KEY = {s["key"]: s for s in SAMPLES}

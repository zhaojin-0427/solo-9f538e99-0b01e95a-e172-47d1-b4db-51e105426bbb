"""Input parsing / validation for paper sheets and frames."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

INT_RE = re.compile(r"^[+-]?\d+$")

MAX_FRAMES = 12
MAX_DIM = 100_000


class ValidationError(Exception):
    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def parse_int(value: Any, field: str, errors: List[str]) -> int | None:
    if isinstance(value, bool):
        errors.append(f"{field} 必须是整数。")
        return None
    if isinstance(value, int):
        return value
    if value is None:
        errors.append(f"{field} 不能为空。")
        return None
    s = str(value).strip()
    if not INT_RE.match(s):
        errors.append(f"{field}「{value}」不是整数毫米值。")
        return None
    return int(s)


def _positive_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


def parse_job_payload(data: Any) -> Tuple[str, int, int, List[Dict[str, Any]]]:
    """Validate JSON payload. Raises ValidationError with Chinese messages."""
    errors: List[str] = []
    if not isinstance(data, dict):
        raise ValidationError(["请求内容必须是 JSON 对象。"])

    name = str(data.get("name") or "").strip() or "未命名印张"
    if len(name) > 80:
        errors.append("名称最长 80 个字符。")

    paper_w = parse_int(data.get("paper_w"), "纸张宽", errors)
    paper_h = parse_int(data.get("paper_h"), "纸张高", errors)

    if paper_w is not None and not (1 <= paper_w <= MAX_DIM):
        errors.append(f"纸张宽必须在 1..{MAX_DIM} mm 之间。")
    if paper_h is not None and not (1 <= paper_h <= MAX_DIM):
        errors.append(f"纸张高必须在 1..{MAX_DIM} mm 之间。")

    raw_frames = data.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        errors.append("至少要录入 1 幅画框。")
        raise ValidationError(errors)

    if len(raw_frames) > MAX_FRAMES:
        errors.append(f"最多只能录入 {MAX_FRAMES} 幅画框，当前 {len(raw_frames)} 幅。")

    frames: List[Dict[str, Any]] = []
    for i, rf in enumerate(raw_frames[:MAX_FRAMES]):
        if not isinstance(rf, dict):
            errors.append(f"第 {i + 1} 幅画框格式不正确。")
            continue
        label = str(rf.get("name") or "").strip() or f"画{i + 1}"
        x = parse_int(rf.get("x"), f"第{i + 1}幅「{label}」的 x", errors)
        y = parse_int(rf.get("y"), f"第{i + 1}幅「{label}」的 y", errors)
        w = parse_int(rf.get("w"), f"第{i + 1}幅「{label}」的宽", errors)
        h = parse_int(rf.get("h"), f"第{i + 1}幅「{label}」的高", errors)
        if None in (x, y, w, h):
            continue
        assert x is not None and y is not None and w is not None and h is not None
        if x < 0 or y < 0:
            errors.append(f"画框「{label}」坐标不能为负。")
        if w <= 0 or h <= 0:
            errors.append(f"画框「{label}」的宽、高必须为正整数。")
        if paper_w is not None and x + w > paper_w:
            errors.append(f"画框「{label}」右边 x+宽={x + w} 超出纸宽 {paper_w}。")
        if paper_h is not None and y + h > paper_h:
            errors.append(f"画框「{label}」下边 y+高={y + h} 超出纸高 {paper_h}。")
        frames.append({"name": label[:40], "x": x, "y": y, "w": w, "h": h})

    if not errors:
        for i in range(len(frames)):
            for j in range(i):
                if _positive_overlap(frames[i], frames[j]):
                    errors.append(
                        f"画框「{frames[j]['name']}」与「{frames[i]['name']}」"
                        "存在正面积重叠（允许边贴边）。"
                    )

    if errors:
        raise ValidationError(errors)
    assert paper_w is not None and paper_h is not None
    return name, paper_w, paper_h, frames


def frames_from_form(form: Any) -> Dict[str, Any]:
    """Build a JSON-like payload from repeated form fields."""
    names = form.getlist("fname")
    xs = form.getlist("x")
    ys = form.getlist("y")
    ws = form.getlist("w")
    hs = form.getlist("h")
    n = max(len(xs), len(ys), len(ws), len(hs), len(names))
    frames: List[Dict[str, Any]] = []
    for i in range(n):
        def at(seq: List[str]) -> str:
            return seq[i].strip() if i < len(seq) else ""
        if not any([at(xs), at(ys), at(ws), at(hs)]):
            continue
        frames.append(
            {
                "name": at(names),
                "x": at(xs),
                "y": at(ys),
                "w": at(ws),
                "h": at(hs),
            }
        )
    return {
        "name": form.get("name", ""),
        "paper_w": form.get("paper_w", ""),
        "paper_h": form.get("paper_h", ""),
        "frames": frames,
    }

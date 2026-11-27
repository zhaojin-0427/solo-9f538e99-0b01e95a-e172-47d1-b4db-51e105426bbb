"""SQLite persistence and cut-registration domain logic.

A *piece* row is a node of the cutting tree.  ``status='split'`` pieces have
already been cut and keep their two child rows; only ``active`` leaf pieces
may receive another cut; ``finished`` leaf pieces exactly match a frame and
must not be cut again.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    paper_w    INTEGER NOT NULL,
    paper_h    INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS frames (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ord    INTEGER NOT NULL,
    name   TEXT NOT NULL,
    x      INTEGER NOT NULL,
    y      INTEGER NOT NULL,
    w      INTEGER NOT NULL,
    h      INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
           CHECK (status IN ('pending','done','ruined'))
);

CREATE TABLE IF NOT EXISTS pieces (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES pieces(id) ON DELETE CASCADE,
    ord       INTEGER NOT NULL DEFAULT 0,
    x0        INTEGER NOT NULL,
    y0        INTEGER NOT NULL,
    x1        INTEGER NOT NULL,
    y1        INTEGER NOT NULL,
    status    TEXT NOT NULL DEFAULT 'active'
              CHECK (status IN ('active','split','finished')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS cuts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    piece_id      INTEGER NOT NULL REFERENCES pieces(id),
    orient        TEXT NOT NULL CHECK (orient IN ('V','H')),
    pos           INTEGER NOT NULL,
    suggested_pos INTEGER,
    deviation     INTEGER,
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_frames_job ON frames(job_id);
CREATE INDEX IF NOT EXISTS idx_pieces_job ON pieces(job_id);
CREATE INDEX IF NOT EXISTS idx_cuts_job   ON cuts(job_id);
"""


class CutRejected(Exception):
    """Raised when a cut registration violates one of the rejection rules."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Job / frame creation
# ---------------------------------------------------------------------------


def _mark_finished_matches(
    conn: sqlite3.Connection, job_id: int
) -> List[Dict[str, Any]]:
    """Mark leaf pieces that exactly equal a pending frame as finished."""
    frames = conn.execute(
        "SELECT * FROM frames WHERE job_id=? AND status='pending'", (job_id,)
    ).fetchall()
    finished: List[Dict[str, Any]] = []
    pieces = conn.execute(
        "SELECT * FROM pieces WHERE job_id=? AND status='active'", (job_id,)
    ).fetchall()
    for pc in pieces:
        for fr in frames:
            if (
                pc["x0"] == fr["x"]
                and pc["y0"] == fr["y"]
                and pc["x1"] == fr["x"] + fr["w"]
                and pc["y1"] == fr["y"] + fr["h"]
            ):
                conn.execute(
                    "UPDATE pieces SET status='finished' WHERE id=?", (pc["id"],)
                )
                conn.execute(
                    "UPDATE frames SET status='done' WHERE id=?", (fr["id"],)
                )
                finished.append(
                    {"piece_id": pc["id"], "frame_id": fr["id"], "name": fr["name"]}
                )
                break
    return finished


def create_job(
    db_path: str,
    name: str,
    paper_w: int,
    paper_h: int,
    frames: Sequence[Dict[str, Any]],
) -> int:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO jobs(name, paper_w, paper_h) VALUES (?,?,?)",
            (name, paper_w, paper_h),
        )
        job_id = int(cur.lastrowid)
        for i, fr in enumerate(frames):
            conn.execute(
                """INSERT INTO frames(job_id, ord, name, x, y, w, h)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    job_id,
                    i,
                    fr["name"],
                    fr["x"],
                    fr["y"],
                    fr["w"],
                    fr["h"],
                ),
            )
        conn.execute(
            """INSERT INTO pieces(job_id, parent_id, ord, x0, y0, x1, y1, status)
               VALUES (?,?,?,?,?,?,?, 'active')""",
            (job_id, None, 0, 0, 0, paper_w, paper_h),
        )
        _mark_finished_matches(conn, job_id)
        conn.commit()
        return job_id
    finally:
        conn.close()


def reset_progress(db_path: str, job_id: int) -> None:
    """Delete every registered cut and recreate the single sheet piece."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(job_id)
        conn.execute("DELETE FROM cuts WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM pieces WHERE job_id=?", (job_id,))
        conn.execute(
            "UPDATE frames SET status='pending' WHERE job_id=?", (job_id,)
        )
        conn.execute(
            """INSERT INTO pieces(job_id, parent_id, ord, x0, y0, x1, y1, status)
               VALUES (?,?,?,?,?,?,?, 'active')""",
            (job_id, None, 0, 0, 0, job["paper_w"], job["paper_h"]),
        )
        _mark_finished_matches(conn, job_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_job(db_path: str, job_id: int) -> None:
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_job(db_path: str, job_id: int) -> Optional[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()


def list_jobs(db_path: str) -> List[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()


def list_frames(db_path: str, job_id: int) -> List[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM frames WHERE job_id=? ORDER BY ord, id", (job_id,)
        ).fetchall()
    finally:
        conn.close()


def list_pieces(db_path: str, job_id: int) -> List[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM pieces WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
    finally:
        conn.close()


def list_cuts(db_path: str, job_id: int) -> List[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM cuts WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cut registration
# ---------------------------------------------------------------------------


def _segment_crosses_frame(
    orient: str,
    pos: int,
    px0: int,
    py0: int,
    px1: int,
    py1: int,
    fr: sqlite3.Row,
) -> bool:
    """True if the full cut segment passes through the frame *interior*."""
    fx0, fy0 = fr["x"], fr["y"]
    fx1, fy1 = fr["x"] + fr["w"], fr["y"] + fr["h"]
    if orient == "V":
        if not (fx0 < pos < fx1):
            return False
        # strict overlap of the segment's y interval with frame interior y
        return max(py0, fy0) < min(py1, fy1)
    else:
        if not (fy0 < pos < fy1):
            return False
        return max(px0, fx0) < min(px1, fx1)


def register_cut(
    db_path: str,
    job_id: int,
    piece_id: int,
    orient: str,
    pos: int,
    suggested_pos: Optional[int] = None,
) -> Dict[str, Any]:
    """Validate and persist one actual cut.

    Raises :class:`CutRejected` on the three rejection conditions or on
    invalid arguments; the database is left untouched in that case.
    """
    if orient not in ("V", "H"):
        raise CutRejected("bad_orient", "方向必须是 V（竖切）或 H（横切）。")
    if not isinstance(pos, int):
        raise CutRejected("bad_pos", "刀位必须是整数毫米。")

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")

        job = conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None:
            raise CutRejected("no_job", "印张不存在。")

        piece = conn.execute(
            "SELECT * FROM pieces WHERE id=? AND job_id=?",
            (piece_id, job_id),
        ).fetchone()
        if piece is None:
            raise CutRejected(
                "no_piece", f"找不到属于本印张的纸片 #{piece_id}。"
            )

        child = conn.execute(
            "SELECT 1 FROM pieces WHERE parent_id=? LIMIT 1", (piece_id,)
        ).fetchone()
        if piece["status"] == "split" or child is not None:
            raise CutRejected(
                "already_split",
                f"纸片 #{piece_id} 已经裁过并分成两片，不能在旧纸片上重复登记；"
                "请在当前的子纸片中选择下刀对象。",
            )

        if piece["status"] == "finished":
            raise CutRejected(
                "finished",
                f"纸片 #{piece_id} 已是四边与画框重合的成品，再下刀会裁坏版画，"
                "登记已拒绝。",
            )

        x0, y0, x1, y1 = (
            piece["x0"],
            piece["y0"],
            piece["x1"],
            piece["y1"],
        )
        if orient == "V" and not (x0 < pos < x1):
            raise CutRejected(
                "outside",
                f"竖切刀位 x={pos} 不在纸片 #{piece_id} 内部"
                f"（要求 {x0} < x < {x1} 的整数位置），登记已拒绝，进度未改动。",
            )
        if orient == "H" and not (y0 < pos < y1):
            raise CutRejected(
                "outside",
                f"横切刀位 y={pos} 不在纸片 #{piece_id} 内部"
                f"（要求 {y0} < y < {y1} 的整数位置），登记已拒绝，进度未改动。",
            )

        frames = conn.execute(
            "SELECT * FROM frames WHERE job_id=? AND status='pending'",
            (job_id,),
        ).fetchall()

        # Defensive: an active piece exactly matching a pending frame is a
        # finished product even if its status lagged behind.
        for fr in frames:
            if (
                x0 == fr["x"]
                and y0 == fr["y"]
                and x1 == fr["x"] + fr["w"]
                and y1 == fr["y"] + fr["h"]
            ):
                conn.execute(
                    "UPDATE pieces SET status='finished' WHERE id=?",
                    (piece_id,),
                )
                conn.execute(
                    "UPDATE frames SET status='done' WHERE id=?", (fr["id"],)
                )
                conn.commit()
                raise CutRejected(
                    "finished",
                    f"纸片 #{piece_id} 与画框「{fr['name']}」四边重合，已是成品，"
                    "登记已拒绝。",
                )

        crossed = [
            fr
            for fr in frames
            if _segment_crosses_frame(
                orient, pos, x0, y0, x1, y1, fr
            )
        ]

        deviation: Optional[int] = None
        if suggested_pos is not None:
            deviation = pos - suggested_pos

        cur = conn.execute(
            """INSERT INTO cuts(job_id, piece_id, orient, pos,
                                suggested_pos, deviation)
               VALUES (?,?,?,?,?,?)""",
            (job_id, piece_id, orient, pos, suggested_pos, deviation),
        )
        cut_id = int(cur.lastrowid)

        conn.execute(
            "UPDATE pieces SET status='split' WHERE id=?", (piece_id,)
        )

        if orient == "V":
            children_geom = [
                (x0, y0, pos, y1),
                (pos, y0, x1, y1),
            ]
        else:
            children_geom = [
                (x0, y0, x1, pos),
                (x0, pos, x1, y1),
            ]

        child_ids: List[int] = []
        for ord_i, (cx0, cy0, cx1, cy1) in enumerate(children_geom):
            cc = conn.execute(
                """INSERT INTO pieces(job_id, parent_id, ord,
                                      x0, y0, x1, y1, status)
                   VALUES (?,?,?,?,?,?,?, 'active')""",
                (job_id, piece_id, ord_i, cx0, cy0, cx1, cy1),
            )
            child_ids.append(int(cc.lastrowid))

        ruined_names: List[str] = []
        for fr in crossed:
            conn.execute(
                "UPDATE frames SET status='ruined' WHERE id=?", (fr["id"],)
            )
            ruined_names.append(fr["name"])

        finished = _mark_finished_matches(conn, job_id)

        note_parts: List[str] = []
        if deviation:
            note_parts.append(f"偏 {deviation:+d} mm")
        if ruined_names:
            note_parts.append("切坏 " + "、".join(ruined_names))
        if finished:
            note_parts.append(
                "成品 " + "、".join(x["name"] for x in finished)
            )
        note = "；".join(note_parts)
        conn.execute("UPDATE cuts SET note=? WHERE id=?", (note, cut_id))

        conn.commit()

        return {
            "cut_id": cut_id,
            "piece_id": piece_id,
            "orient": orient,
            "pos": pos,
            "deviation": deviation,
            "child_ids": child_ids,
            "ruined": ruined_names,
            "finished": finished,
            "note": note,
        }
    except CutRejected:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

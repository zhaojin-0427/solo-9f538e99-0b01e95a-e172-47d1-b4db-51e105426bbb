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
              CHECK (status IN ('active','split','finished','claimed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Provenance and return state for a blank leftover piece claimed as a new
-- trial-print sheet.  Source IDs are intentionally plain integers (not FK
-- references): the provenance must survive reset/deletion of the source job.
CREATE TABLE IF NOT EXISTS piece_claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_job_id   INTEGER NOT NULL,
    source_piece_id INTEGER NOT NULL,
    new_job_id      INTEGER NOT NULL,
    source_job_name TEXT NOT NULL DEFAULT '',
    source_paper_w  INTEGER NOT NULL,
    source_paper_h  INTEGER NOT NULL,
    piece_w         INTEGER NOT NULL,
    piece_h         INTEGER NOT NULL,
    piece_geom      TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','returned','new_job_deleted')),
    cut_ever        INTEGER NOT NULL DEFAULT 0,
    reopened        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    returned_at     TEXT
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
CREATE INDEX IF NOT EXISTS idx_claims_new_job ON piece_claims(new_job_id);
CREATE INDEX IF NOT EXISTS idx_claims_source ON piece_claims(source_job_id, source_piece_id);
-- At most one live claim per physical source piece.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_claim_source_piece
    ON piece_claims(source_job_id, source_piece_id)
    WHERE status = 'active';
"""


class CutRejected(Exception):
    """Raised when a cut registration violates one of the rejection rules."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ClaimRejected(Exception):
    """Raised when claiming or returning a leftover piece is not allowed."""

    def __init__(self, code: str, message: str, reasons: Optional[List[str]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reasons = reasons or [code]


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply small in-place migrations for databases created by older builds."""
    pieces_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pieces'"
    ).fetchone()
    if pieces_sql is not None and "claimed" not in (pieces_sql[0] or ""):
        # SQLite cannot ALTER a CHECK constraint.  Rebuild ``pieces`` using
        # the supported SQLite schema-migration procedure.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE pieces__claimed_migration (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id    INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    parent_id INTEGER REFERENCES pieces(id) ON DELETE CASCADE,
                    ord       INTEGER NOT NULL DEFAULT 0,
                    x0        INTEGER NOT NULL,
                    y0        INTEGER NOT NULL,
                    x1        INTEGER NOT NULL,
                    y1        INTEGER NOT NULL,
                    status    TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','split','finished','claimed')),
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                )
                """
            )
            conn.execute(
                """
                INSERT INTO pieces__claimed_migration
                    (id, job_id, parent_id, ord, x0, y0, x1, y1, status, created_at)
                SELECT id, job_id, parent_id, ord, x0, y0, x1, y1, status, created_at
                FROM pieces
                """
            )
            conn.execute("DROP TABLE pieces")
            conn.execute("ALTER TABLE pieces__claimed_migration RENAME TO pieces")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pieces_job ON pieces(job_id)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.DatabaseError(
                f"pieces migration failed foreign-key check: {violations[:3]}"
            )
        conn.execute("PRAGMA foreign_keys=ON")


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
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
        # A claimed trial sheet may never be returned after it has been
        # reopened, even though reset deletes its cut rows.
        conn.execute(
            """
            UPDATE piece_claims
               SET reopened=1
             WHERE new_job_id=? AND status='active'
            """,
            (job_id,),
        )
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
        # Generic deletion is not the same as an eligible "return to source".
        # Keep the source piece locked (status='claimed') and retain an audit
        # row showing that the new sheet was deleted rather than returned.
        conn.execute(
            """
            UPDATE piece_claims
               SET status='new_job_deleted'
             WHERE new_job_id=? AND status='active'
            """,
            (job_id,),
        )
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


def get_piece_by_id(db_path: str, piece_id: int) -> Optional[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM pieces WHERE id=?", (piece_id,)
        ).fetchone()
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
# Leftover-piece claims / provenance / return
# ---------------------------------------------------------------------------


def _positive_rect_overlap(
    x0: int, y0: int, x1: int, y1: int, fr: sqlite3.Row
) -> bool:
    return (
        max(x0, fr["x"]) < min(x1, fr["x"] + fr["w"])
        and max(y0, fr["y"]) < min(y1, fr["y"] + fr["h"])
    )


def _claim_context(
    conn: sqlite3.Connection, source_job_id: int, piece_id: int
) -> Dict[str, Any]:
    """Raw claimability evaluation using an existing transaction/connection."""
    job = conn.execute(
        "SELECT * FROM jobs WHERE id=?", (source_job_id,)
    ).fetchone()
    if job is None:
        return {
            "claimable": False,
            "reason_code": "no_job",
            "reason": f"源印张 #{source_job_id} 不存在。",
            "source_job": None,
            "piece": None,
            "claim": None,
            "overlapping_frames": [],
        }

    piece = conn.execute(
        "SELECT * FROM pieces WHERE id=? AND job_id=?",
        (piece_id, source_job_id),
    ).fetchone()
    if piece is None:
        return {
            "claimable": False,
            "reason_code": "no_piece",
            "reason": f"源印张 #{source_job_id} 上找不到纸片 #{piece_id}。",
            "source_job": job,
            "piece": None,
            "claim": None,
            "overlapping_frames": [],
        }

    claim = conn.execute(
        """
        SELECT * FROM piece_claims
         WHERE source_job_id=? AND source_piece_id=? AND status='active'
         ORDER BY id DESC LIMIT 1
        """,
        (source_job_id, piece_id),
    ).fetchone()
    child = conn.execute(
        "SELECT 1 FROM pieces WHERE parent_id=? LIMIT 1", (piece_id,)
    ).fetchone()
    all_frames = conn.execute(
        "SELECT * FROM frames WHERE job_id=? ORDER BY ord, id",
        (source_job_id,),
    ).fetchall()
    overlapping = [
        fr
        for fr in all_frames
        if _positive_rect_overlap(
            piece["x0"], piece["y0"], piece["x1"], piece["y1"], fr
        )
    ]

    if claim is not None or piece["status"] == "claimed":
        new_job = claim["new_job_id"] if claim is not None else None
        where = f"（已开为印张 #{new_job}）" if new_job is not None else ""
        return {
            "claimable": False,
            "reason_code": "already_claimed",
            "reason": f"纸片 #{piece_id} 已被领用{where}，不能重复领用。",
            "source_job": job,
            "piece": piece,
            "claim": claim,
            "overlapping_frames": overlapping,
        }

    if piece["status"] == "split" or child is not None:
        return {
            "claimable": False,
            "reason_code": "already_split",
            "reason": f"纸片 #{piece_id} 已裁开并分出子纸片，不是可领的活余纸。",
            "source_job": job,
            "piece": piece,
            "claim": None,
            "overlapping_frames": overlapping,
        }

    if piece["status"] == "finished":
        return {
            "claimable": False,
            "reason_code": "finished",
            "reason": f"纸片 #{piece_id} 是与画框四边重合的成品，不是空白余纸。",
            "source_job": job,
            "piece": piece,
            "claim": None,
            "overlapping_frames": overlapping,
        }

    if piece["status"] != "active":
        return {
            "claimable": False,
            "reason_code": "not_active",
            "reason": f"纸片 #{piece_id} 当前状态为 {piece['status']}，不能领用。",
            "source_job": job,
            "piece": piece,
            "claim": None,
            "overlapping_frames": overlapping,
        }

    if overlapping:
        names = "、".join(f"「{fr['name']}」" for fr in overlapping[:5])
        more = " 等" if len(overlapping) > 5 else ""
        return {
            "claimable": False,
            "reason_code": "frame_overlap",
            "reason": (
                f"纸片 #{piece_id} 与原印张画框 {names}{more}存在正面积重叠；"
                "已切坏和已裁成的原画框也计入，不能作为空白余纸领用。"
            ),
            "source_job": job,
            "piece": piece,
            "claim": None,
            "overlapping_frames": overlapping,
        }

    if piece["parent_id"] is None:
        return {
            "claimable": False,
            "reason_code": "not_cut_out",
            "reason": f"纸片 #{piece_id} 是完整原纸，不是落刀切出的余纸片。",
            "source_job": job,
            "piece": piece,
            "claim": None,
            "overlapping_frames": [],
        }

    return {
        "claimable": True,
        "reason_code": None,
        "reason": "",
        "source_job": job,
        "piece": piece,
        "claim": None,
        "overlapping_frames": [],
    }


def _public_claim_context(ctx: Dict[str, Any]) -> Dict[str, Any]:
    piece = ctx["piece"]
    return {
        "claimable": ctx["claimable"],
        "reason_code": ctx["reason_code"],
        "reason": ctx["reason"],
        "source_job": dict(ctx["source_job"]) if ctx["source_job"] is not None else None,
        "piece": dict(piece) if piece is not None else None,
        "claim": dict(ctx["claim"]) if ctx["claim"] is not None else None,
        "overlapping_frames": [dict(f) for f in ctx["overlapping_frames"]],
        "piece_w": piece["x1"] - piece["x0"] if piece is not None else None,
        "piece_h": piece["y1"] - piece["y0"] if piece is not None else None,
    }


def get_claim_context(
    db_path: str, source_job_id: int, piece_id: int
) -> Dict[str, Any]:
    conn = connect(db_path)
    try:
        return _public_claim_context(_claim_context(conn, source_job_id, piece_id))
    finally:
        conn.close()


def list_piece_claim_contexts(
    db_path: str, job_id: int
) -> List[Dict[str, Any]]:
    pieces = list_pieces(db_path, job_id)
    return [get_claim_context(db_path, job_id, p["id"]) for p in pieces]


def claim_and_create_job(
    db_path: str,
    source_job_id: int,
    piece_id: int,
    name: str,
    frames: Sequence[Dict[str, Any]],
    paper_w: Optional[int] = None,
    paper_h: Optional[int] = None,
) -> int:
    """Atomically claim a blank leftover piece and create its new sheet."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ctx = _claim_context(conn, source_job_id, piece_id)
        if not ctx["claimable"]:
            raise ClaimRejected(ctx["reason_code"], ctx["reason"])

        source_job = ctx["source_job"]
        piece = ctx["piece"]
        assert source_job is not None and piece is not None
        actual_w = piece["x1"] - piece["x0"]
        actual_h = piece["y1"] - piece["y0"]
        if paper_w is not None and paper_h is not None:
            if (paper_w, paper_h) != (actual_w, actual_h):
                raise ClaimRejected(
                    "dimension_mismatch",
                    "提交的新印张宽高与余纸片宽高不一致，领用已整笔撤回。",
                )

        cur = conn.execute(
            "INSERT INTO jobs(name, paper_w, paper_h) VALUES (?,?,?)",
            (name, actual_w, actual_h),
        )
        new_job_id = int(cur.lastrowid)
        for i, fr in enumerate(frames):
            conn.execute(
                """INSERT INTO frames(job_id, ord, name, x, y, w, h)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    new_job_id,
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
            (new_job_id, None, 0, 0, 0, actual_w, actual_h),
        )
        _mark_finished_matches(conn, new_job_id)

        conn.execute(
            """
            INSERT INTO piece_claims(
                source_job_id, source_piece_id, new_job_id,
                source_job_name, source_paper_w, source_paper_h,
                piece_w, piece_h, piece_geom
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                source_job_id,
                piece_id,
                new_job_id,
                source_job["name"],
                source_job["paper_w"],
                source_job["paper_h"],
                actual_w,
                actual_h,
                f"[{piece['x0']},{piece['y0']}]-[{piece['x1']},{piece['y1']}]",
            ),
        )
        updated = conn.execute(
            """
            UPDATE pieces SET status='claimed'
             WHERE id=? AND job_id=? AND status='active'
            """,
            (piece_id, source_job_id),
        )
        if updated.rowcount != 1:
            # Defensive race guard; the partial unique index is the final lock.
            raise ClaimRejected(
                "already_claimed",
                f"纸片 #{piece_id} 刚刚已被其他操作领用或改变状态，本次领用整笔撤回。",
            )

        conn.commit()
        return new_job_id
    except ClaimRejected:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ClaimRejected(
            "already_claimed",
            f"纸片 #{piece_id} 已有有效领用记录，本次领用整笔撤回。",
        ) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Readable aliases for callers/tests using domain-language names.
claim_leftover_and_create_job = claim_and_create_job
claim_piece = claim_and_create_job


def get_claim_info(db_path: str, new_job_id: int) -> Optional[Dict[str, Any]]:
    """Return immutable provenance plus current return eligibility."""
    conn = connect(db_path)
    try:
        claim = conn.execute(
            """
            SELECT * FROM piece_claims
             WHERE new_job_id=?
             ORDER BY id DESC LIMIT 1
            """,
            (new_job_id,),
        ).fetchone()
        if claim is None:
            return None
        data = dict(claim)

        source_job = conn.execute(
            "SELECT * FROM jobs WHERE id=?", (claim["source_job_id"],)
        ).fetchone()
        source_piece = conn.execute(
            "SELECT * FROM pieces WHERE id=? AND job_id=?",
            (claim["source_piece_id"], claim["source_job_id"]),
        ).fetchone()
        data["source_job_exists"] = source_job is not None
        data["source_piece_exists"] = source_piece is not None
        data["source_job"] = dict(source_job) if source_job is not None else None
        data["source_piece"] = dict(source_piece) if source_piece is not None else None

        reasons: List[str] = []
        reason_messages: List[str] = []
        if claim["status"] == "returned":
            reasons.append("returned")
            reason_messages.append("该领用已经退回。")
        elif claim["status"] == "new_job_deleted":
            reasons.append("new_job_deleted")
            reason_messages.append("新印张已被普通删除，不能按退回流程恢复原片。")
        else:
            if claim["cut_ever"]:
                reasons.append("has_cuts")
                reason_messages.append("新印张已经登记过落刀")
            if claim["reopened"]:
                reasons.append("reopened")
                reason_messages.append("新印张已经执行过重新开始")
            if source_piece is None:
                reasons.append("source_piece_missing")
                reason_messages.append("原纸片已随源印张重开或删除而不存在")
            elif source_piece["status"] != "claimed":
                reasons.append("source_not_claimed")
                reason_messages.append("原纸片当前不是已领用状态")

        data["return_reasons"] = reasons
        data["return_reason_messages"] = reason_messages
        data["returnable"] = claim["status"] == "active" and not reasons
        data["return_block_message"] = "；".join(reason_messages)
        return data
    finally:
        conn.close()


def return_claimed_job(db_path: str, new_job_id: int) -> Dict[str, Any]:
    """Cancel a wrongly claimed sheet and restore its source piece."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        claim = conn.execute(
            """
            SELECT * FROM piece_claims
             WHERE new_job_id=? AND status='active'
             ORDER BY id DESC LIMIT 1
            """,
            (new_job_id,),
        ).fetchone()
        if claim is None:
            previous = conn.execute(
                """
                SELECT * FROM piece_claims
                 WHERE new_job_id=?
                 ORDER BY id DESC LIMIT 1
                """,
                (new_job_id,),
            ).fetchone()
            if previous is not None:
                if previous["status"] == "returned":
                    raise ClaimRejected("returned", "该领用已经退回，不能重复办理。")
                if previous["status"] == "new_job_deleted":
                    raise ClaimRejected(
                        "new_job_deleted",
                        "新印张已被普通删除，不能按退回流程恢复原片。",
                    )
            raise ClaimRejected(
                "not_claimed", "印张没有有效的领用出处，不能办理余纸退回。"
            )

        reasons: List[str] = []
        messages: List[str] = []
        has_current_cut = conn.execute(
            "SELECT 1 FROM cuts WHERE job_id=? LIMIT 1", (new_job_id,)
        ).fetchone()
        if claim["cut_ever"] or has_current_cut:
            reasons.append("has_cuts")
            messages.append("新印张已经登记过落刀，按规则不能退回")
        if claim["reopened"]:
            reasons.append("reopened")
            messages.append("新印张已经重开过，按规则不能退回")

        source_piece = conn.execute(
            "SELECT * FROM pieces WHERE id=? AND job_id=?",
            (claim["source_piece_id"], claim["source_job_id"]),
        ).fetchone()
        if source_piece is None:
            reasons.append("source_piece_missing")
            messages.append("原纸片已不存在（源印张可能已重开或删除）")
        elif source_piece["status"] != "claimed":
            reasons.append("source_not_claimed")
            messages.append("原纸片不是已领用状态，不能重复恢复")

        if reasons:
            message = "；".join(messages) + "，本次退回整笔撤回。"
            raise ClaimRejected(reasons[0], message, reasons)

        conn.execute(
            """
            UPDATE piece_claims
               SET status='returned', returned_at=datetime('now','localtime')
             WHERE id=?
            """,
            (claim["id"],),
        )
        # Delete the trial sheet and all of its frames/pieces/cuts.  The
        # claim row itself has no FK to jobs and is retained as an audit row.
        conn.execute("DELETE FROM jobs WHERE id=?", (new_job_id,))
        restored = conn.execute(
            """
            UPDATE pieces SET status='active'
             WHERE id=? AND job_id=? AND status='claimed'
            """,
            (claim["source_piece_id"], claim["source_job_id"]),
        )
        if restored.rowcount != 1:
            raise ClaimRejected(
                "source_not_claimed",
                "恢复原纸片状态失败，本次退回整笔撤回。",
            )

        conn.commit()
        return {
            "new_job_id": new_job_id,
            "source_job_id": claim["source_job_id"],
            "source_piece_id": claim["source_piece_id"],
        }
    except ClaimRejected:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Readable aliases for callers/tests using domain-language names.
return_claim = return_claimed_job
return_trial_sheet = return_claimed_job


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

        active_claim = conn.execute(
            """
            SELECT id, new_job_id FROM piece_claims
             WHERE source_job_id=? AND source_piece_id=? AND status='active'
             LIMIT 1
            """,
            (job_id, piece_id),
        ).fetchone()
        if piece["status"] == "claimed" or active_claim is not None:
            new_job_id = active_claim["new_job_id"] if active_claim is not None else None
            link = f"（已开为印张 #{new_job_id}）" if new_job_id is not None else ""
            raise CutRejected(
                "claimed",
                f"纸片 #{piece_id} 已作为空白余纸领用{link}；"
                "在新印张退回之前，同一片纸不能重复登记落刀。",
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

        # A cut on a claimed trial sheet permanently removes return
        # eligibility, even if that sheet is later reset/reopened.
        conn.execute(
            """
            UPDATE piece_claims
               SET cut_ever = cut_ever + 1
             WHERE new_job_id=? AND status='active'
            """,
            (job_id,),
        )

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

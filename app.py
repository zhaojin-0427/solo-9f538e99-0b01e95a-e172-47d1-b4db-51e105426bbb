"""Flask operation page for the guillotine paper-cutter planner."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import storage
from samples import SAMPLE_BY_KEY, SAMPLES
from solver import Box, Rect, plan_state
from validation import ValidationError, frames_from_form, parse_job_payload

ClaimRejected = storage.ClaimRejected
CutRejected = storage.CutRejected


def create_app(db_path: Optional[str] = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("CUTTING_DB", "cutting.db")
    app.secret_key = os.environ.get("SECRET_KEY", "dev-cutting-secret-key")
    storage.init_db(app.config["DB_PATH"])

    # ------------------------------------------------------------------
    # View-model helpers
    # ------------------------------------------------------------------

    def index_job_stats():
        db = app.config["DB_PATH"]
        stats = []
        for j in storage.list_jobs(db):
            fr = storage.list_frames(db, j["id"])
            done = sum(1 for f in fr if f["status"] == "done")
            ruined = sum(1 for f in fr if f["status"] == "ruined")
            stats.append((j, len(fr), done, ruined))
        return stats

    def build_plan(job_id: int) -> Dict[str, Any]:
        db = app.config["DB_PATH"]
        frames = storage.list_frames(db, job_id)
        pieces = storage.list_pieces(db, job_id)
        boxes = [
            Box(
                f["id"],
                f["x"],
                f["y"],
                f["x"] + f["w"],
                f["y"] + f["h"],
                f["name"],
            )
            for f in frames
            if f["status"] == "pending"
        ]
        roots = [
            (p["id"], Rect(p["x0"], p["y0"], p["x1"], p["y1"]))
            for p in pieces
            if p["status"] == "active"
        ]
        return plan_state(roots, boxes)

    def build_diagram(job, frames, pieces, plan) -> Dict[str, Any]:
        width = int(job["paper_w"])
        height = int(job["paper_h"])
        vw, vh, margin = 880.0, 580.0, 34.0
        scale = min((vw - 2 * margin) / width, (vh - 2 * margin) / height)
        pw, ph = width * scale, height * scale
        ox, oy = (vw - pw) / 2.0, (vh - ph) / 2.0

        def X(x: float) -> float:
            return round(ox + x * scale, 1)

        def Y(y: float) -> float:
            return round(oy + y * scale, 1)

        def S(v: float) -> float:
            return round(v * scale, 1)

        piece_views = []
        for p in pieces:
            piece_views.append(
                {
                    "id": p["id"],
                    "status": p["status"],
                    "x": X(p["x0"]),
                    "y": Y(p["y0"]),
                    "w": S(p["x1"] - p["x0"]),
                    "h": S(p["y1"] - p["y0"]),
                    "label_x": X(p["x0"]) + 5,
                    "label_y": Y(p["y0"]) + 15,
                    "geom": f"{p['x0']},{p['y0']}–{p['x1']},{p['y1']}",
                }
            )

        frame_views = []
        for f in frames:
            frame_views.append(
                {
                    "id": f["id"],
                    "name": f["name"],
                    "status": f["status"],
                    "x": X(f["x"]),
                    "y": Y(f["y"]),
                    "w": S(f["w"]),
                    "h": S(f["h"]),
                    "cx": X(f["x"] + f["w"] / 2),
                    "cy": Y(f["y"] + f["h"] / 2),
                }
            )

        line_views = []
        for st in plan.get("steps", []):
            x0, y0, x1, y1 = st["piece_rect"]
            pos = st["position"]
            if st["orient"] == "V":
                lx1 = lx2 = X(pos)
                ly1, ly2 = Y(y0), Y(y1)
                mx, my = X(pos), (Y(y0) + Y(y1)) / 2
            else:
                ly1 = ly2 = Y(pos)
                lx1, lx2 = X(x0), X(x1)
                mx, my = (X(x0) + X(x1)) / 2, Y(pos)
            line_views.append(
                {
                    "seq": st["global_seq"],
                    "x1": round(lx1, 1),
                    "y1": round(ly1, 1),
                    "x2": round(lx2, 1),
                    "y2": round(ly2, 1),
                    "mx": round(mx, 1),
                    "my": round(my, 1),
                    "first": st["global_seq"] == 1,
                }
            )

        return {
            "view_w": vw,
            "view_h": vh,
            "sheet_x": X(0),
            "sheet_y": Y(0),
            "sheet_w": S(width),
            "sheet_h": S(height),
            "pieces": piece_views,
            "frames": frame_views,
            "lines": line_views,
        }

    def _render_index(
        errors: Optional[List[str]] = None,
        old_payload: Optional[Dict[str, Any]] = None,
        status: int = 200,
        claim: Optional[Dict[str, Any]] = None,
    ):
        return (
            render_template(
                "index.html",
                job_stats=index_job_stats(),
                samples=SAMPLES,
                errors=errors,
                old_payload=old_payload,
                claim=claim,
            ),
            status,
        )

    def _claim_ref_from_values(
        source_job_value: Any, piece_value: Any
    ) -> Optional[tuple[int, int]]:
        if source_job_value is None and piece_value is None:
            return None
        if source_job_value == "" and piece_value == "":
            return None
        try:
            return int(source_job_value), int(piece_value)
        except (TypeError, ValueError):
            raise ValueError("领用出处的印张号和纸片号必须是整数。")

    def _json_claim_ref(data: Any) -> Optional[tuple[int, int]]:
        if not isinstance(data, dict):
            return None
        nested = data.get("claim")
        nested = nested if isinstance(nested, dict) else {}
        source_job = (
            data.get("claim_source_job_id")
            or data.get("source_job_id")
            or nested.get("source_job_id")
            or nested.get("source_job_id")
        )
        piece = (
            data.get("claim_piece_id")
            or data.get("source_piece_id")
            or data.get("claimed_piece_id")
            or nested.get("piece_id")
            or nested.get("source_piece_id")
        )
        return _claim_ref_from_values(source_job, piece)

    def _form_claim_ref(form: Any) -> Optional[tuple[int, int]]:
        return _claim_ref_from_values(
            form.get("claim_source_job_id") or form.get("source_job_id"),
            form.get("claim_piece_id")
            or form.get("source_piece_id")
            or form.get("piece_id"),
        )

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @app.get("/")
    def index():
        claim = None
        source_job_id = request.args.get("claim_job", type=int) or request.args.get(
            "source_job_id", type=int
        )
        piece_id = (
            request.args.get("piece", type=int)
            or request.args.get("claim_piece_id", type=int)
            or request.args.get("piece_id", type=int)
            or request.args.get("source_piece_id", type=int)
        )
        # Allow a globally unique piece id without explicitly naming its job.
        if piece_id and not source_job_id:
            piece = storage.get_piece_by_id(app.config["DB_PATH"], piece_id)
            source_job_id = piece["job_id"] if piece is not None else None
        if source_job_id and piece_id:
            ctx = storage.get_claim_context(
                app.config["DB_PATH"], source_job_id, piece_id
            )
            if not ctx["claimable"]:
                flash(ctx["reason"], "error")
                return redirect(url_for("job_page", job_id=source_job_id))
            claim = ctx
        return _render_index(claim=claim)

    @app.get("/jobs/new")
    def new_job_form_route():
        # Convenient bookmarkable alias for the ordinary entry form.
        query = request.query_string.decode()
        return redirect(url_for("index") + (f"?{query}" if query else ""))

    @app.get("/jobs/<int:source_job_id>/pieces/<int:piece_id>/claim")
    @app.get("/jobs/<int:source_job_id>/claim/<int:piece_id>")
    def claim_piece_form_route(source_job_id: int, piece_id: int):
        ctx = storage.get_claim_context(
            app.config["DB_PATH"], source_job_id, piece_id
        )
        if not ctx["claimable"]:
            flash(ctx["reason"], "error")
            return redirect(url_for("job_page", job_id=source_job_id))
        return _render_index(claim=ctx)

    def _create_job_response(
        forced_claim_ref: Optional[tuple[int, int]] = None,
    ):
        db = app.config["DB_PATH"]
        claim_ref = forced_claim_ref
        claim_ctx: Optional[Dict[str, Any]] = None

        if request.is_json:
            payload = request.get_json(silent=True)
            if claim_ref is None:
                try:
                    claim_ref = _json_claim_ref(payload)
                except ValueError as ve:
                    return jsonify({"ok": False, "errors": [str(ve)]}), 400

            if claim_ref is not None:
                source_job_id, piece_id = claim_ref
                claim_ctx = storage.get_claim_context(db, source_job_id, piece_id)
                if not claim_ctx["claimable"]:
                    return (
                        jsonify(
                            {
                                "ok": False,
                                "error_code": claim_ctx["reason_code"],
                                "error": claim_ctx["reason"],
                            }
                        ),
                        409,
                    )
                assert claim_ctx["piece"] is not None
                if isinstance(payload, dict):
                    payload["paper_w"] = claim_ctx["piece_w"]
                    payload["paper_h"] = claim_ctx["piece_h"]

            try:
                name, w, h, frames = parse_job_payload(payload)
            except ValidationError as ve:
                return jsonify({"ok": False, "errors": ve.errors}), 400

            try:
                if claim_ref is not None:
                    source_job_id, piece_id = claim_ref
                    jid = storage.claim_and_create_job(
                        db, source_job_id, piece_id, name, frames, w, h
                    )
                else:
                    jid = storage.create_job(db, name, w, h, frames)
            except ClaimRejected as cr:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error_code": cr.code,
                            "error": cr.message,
                            "reasons": cr.reasons,
                        }
                    ),
                    409,
                )
            return jsonify(
                {
                    "ok": True,
                    "job_id": jid,
                    "new_job_id": jid,
                    "redirect": url_for("job_page", job_id=jid),
                }
            )

        payload = frames_from_form(request.form)
        if claim_ref is None:
            try:
                claim_ref = _form_claim_ref(request.form)
            except ValueError as ve:
                flash(str(ve), "error")
                return redirect(url_for("index"))

        if claim_ref is not None:
            source_job_id, piece_id = claim_ref
            claim_ctx = storage.get_claim_context(db, source_job_id, piece_id)
            if not claim_ctx["claimable"]:
                flash(claim_ctx["reason"], "error")
                if claim_ctx["source_job"] is not None:
                    return redirect(url_for("job_page", job_id=source_job_id))
                return redirect(url_for("index"))
            payload["paper_w"] = claim_ctx["piece_w"]
            payload["paper_h"] = claim_ctx["piece_h"]

        try:
            name, w, h, frames = parse_job_payload(payload)
        except ValidationError as ve:
            return _render_index(
                errors=ve.errors,
                old_payload=payload,
                status=422,
                claim=claim_ctx if claim_ctx and claim_ctx["claimable"] else None,
            )

        try:
            if claim_ref is not None:
                source_job_id, piece_id = claim_ref
                jid = storage.claim_and_create_job(
                    db, source_job_id, piece_id, name, frames, w, h
                )
            else:
                jid = storage.create_job(db, name, w, h, frames)
        except ClaimRejected as cr:
            flash(cr.message, "error")
            return redirect(url_for("job_page", job_id=source_job_id))

        if claim_ref is not None:
            flash(
                f"已领用源印张 #{source_job_id} 的纸片 #{piece_id}，"
                f"并按其余纸宽高 {w}×{h} mm 开成试印新印张 #{jid}。",
                "ok",
            )
        return redirect(url_for("job_page", job_id=jid))

    @app.post("/jobs")
    def create_job_route():
        return _create_job_response()

    @app.post("/jobs/<int:source_job_id>/pieces/<int:piece_id>/claim")
    @app.post("/jobs/<int:source_job_id>/claim/<int:piece_id>")
    def claim_piece_route(source_job_id: int, piece_id: int):
        return _create_job_response((source_job_id, piece_id))

    @app.get("/pieces/<int:piece_id>/claim")
    def claim_piece_form_by_piece_route(piece_id: int):
        piece = storage.get_piece_by_id(app.config["DB_PATH"], piece_id)
        if piece is None:
            flash("纸片不存在。", "error")
            return redirect(url_for("index"))
        return redirect(
            url_for(
                "claim_piece_form_route",
                source_job_id=piece["job_id"],
                piece_id=piece_id,
            )
        )

    @app.post("/jobs/<int:source_job_id>/claim")
    def claim_from_job_route(source_job_id: int):
        data = request.get_json(silent=True) if request.is_json else request.form
        try:
            piece_id = int(data.get("piece_id") or data.get("claim_piece_id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "piece_id 必须是整数。"}), 400
        return _create_job_response((source_job_id, piece_id))

    @app.post("/pieces/<int:piece_id>/claim")
    def claim_piece_by_piece_route(piece_id: int):
        # Compatibility endpoint: piece ids are globally unique.
        piece = storage.get_piece_by_id(app.config["DB_PATH"], piece_id)
        if piece is None:
            if request.is_json:
                return jsonify({"ok": False, "error": "piece not found"}), 404
            flash("纸片不存在。", "error")
            return redirect(url_for("index"))
        return _create_job_response((piece["job_id"], piece_id))

    @app.post("/claims")
    def create_claim_route():
        data = request.get_json(silent=True) if request.is_json else request.form
        try:
            source_job_id = data.get("source_job_id") or data.get("claim_source_job_id")
            piece_id = (
                data.get("piece_id")
                or data.get("source_piece_id")
                or data.get("claim_piece_id")
            )
            ref = _claim_ref_from_values(source_job_id, piece_id)
        except ValueError as ve:
            return jsonify({"ok": False, "error": str(ve)}), 400
        if ref is None:
            return jsonify({"ok": False, "error": "piece_id is required"}), 400
        return _create_job_response(ref)

    @app.post("/samples/<key>")
    def create_sample(key: str):
        sample = SAMPLE_BY_KEY.get(key)
        if sample is None:
            flash("没有这个示例。", "error")
            return redirect(url_for("index"))
        jid = storage.create_job(
            app.config["DB_PATH"],
            sample["name"],
            sample["paper_w"],
            sample["paper_h"],
            sample["frames"],
        )
        return redirect(url_for("job_page", job_id=jid))

    def _load_job_or_none(job_id: int):
        job = storage.get_job(app.config["DB_PATH"], job_id)
        if job is None:
            return None
        return job

    @app.get("/jobs/<int:job_id>")
    def job_page(job_id: int):
        job = _load_job_or_none(job_id)
        if job is None:
            flash("印张不存在。", "error")
            return redirect(url_for("index"))
        db = app.config["DB_PATH"]
        frames = storage.list_frames(db, job_id)
        pieces = storage.list_pieces(db, job_id)
        cuts = storage.list_cuts(db, job_id)
        provenance = storage.get_claim_info(db, job_id)
        claim_contexts = storage.list_piece_claim_contexts(db, job_id)
        claim_ctx_by_piece = {c["piece"]["id"]: c for c in claim_contexts if c["piece"]}
        plan = build_plan(job_id)
        diagram = build_diagram(job, frames, pieces, plan)
        active_pieces = [p for p in pieces if p["status"] == "active"]
        done = sum(1 for f in frames if f["status"] == "done")
        ruined = sum(1 for f in frames if f["status"] == "ruined")
        pending = sum(1 for f in frames if f["status"] == "pending")
        first_step = plan["steps"][0] if plan["steps"] else None
        return render_template(
            "job.html",
            job=job,
            frames=frames,
            pieces=pieces,
            cuts=cuts,
            plan=plan,
            diagram=diagram,
            active_pieces=active_pieces,
            first_step=first_step,
            provenance=provenance,
            claim_ctx_by_piece=claim_ctx_by_piece,
            counts={"done": done, "ruined": ruined, "pending": pending},
        )

    @app.get("/jobs/<int:job_id>/plan.json")
    def plan_json(job_id: int):
        if _load_job_or_none(job_id) is None:
            return jsonify({"ok": False, "error": "job not found"}), 404
        return jsonify({"ok": True, "plan": build_plan(job_id)})

    @app.get("/jobs/<int:job_id>/provenance")
    @app.get("/jobs/<int:job_id>/provenance.json")
    def provenance_json(job_id: int):
        if _load_job_or_none(job_id) is None:
            return jsonify({"ok": False, "error": "job not found"}), 404
        info = storage.get_claim_info(app.config["DB_PATH"], job_id)
        if info is None:
            return jsonify({"ok": True, "provenance": None})
        return jsonify({"ok": True, "provenance": info})

    @app.post("/jobs/<int:job_id>/cuts")
    def register_cut_route(job_id: int):
        if _load_job_or_none(job_id) is None:
            if request.is_json:
                return jsonify({"ok": False, "error": "job not found"}), 404
            flash("印张不存在。", "error")
            return redirect(url_for("index"))

        if request.is_json:
            data = request.get_json(silent=True) or {}
            try:
                piece_id = int(data.get("piece_id"))
                orient = str(data.get("orient", "")).upper()
                pos = int(data.get("pos"))
                sp = data.get("suggested_pos")
                suggested = int(sp) if sp is not None and str(sp) != "" else None
            except (TypeError, ValueError):
                return (
                    jsonify({"ok": False, "error": "参数必须是整数。"}),
                    400,
                )
            try:
                result = storage.register_cut(
                    app.config["DB_PATH"],
                    job_id,
                    piece_id,
                    orient,
                    pos,
                    suggested,
                )
            except storage.CutRejected as cr:
                return (
                    jsonify(
                        {"ok": False, "error_code": cr.code, "error": cr.message}
                    ),
                    409,
                )
            return jsonify(
                {"ok": True, "result": result, "plan": build_plan(job_id)}
            )

        # Normal browser form post -> PRG with flash.
        form = request.form
        try:
            piece_id = int(form.get("piece_id", ""))
            orient = str(form.get("orient", "")).upper()
            pos = int(str(form.get("pos", "")).strip())
            sp = form.get("suggested_pos")
            suggested = int(sp) if sp else None
        except (TypeError, ValueError):
            flash("纸片、方向和整数刀位都要填写正确。", "error")
            return redirect(url_for("job_page", job_id=job_id))

        try:
            result = storage.register_cut(
                app.config["DB_PATH"], job_id, piece_id, orient, pos, suggested
            )
        except storage.CutRejected as cr:
            flash(cr.message, "error")
            return redirect(url_for("job_page", job_id=job_id))

        bits = [f"已登记纸片 #{result['piece_id']} 的"
                f"{'竖' if result['orient'] == 'V' else '横'}刀 @ {result['pos']}mm，"
                "并已按剩余纸片重新排算。"]
        if result["deviation"]:
            d = result["deviation"]
            bits.append(f"相对推荐刀位偏了 {d:+d} mm。")
        if result["ruined"]:
            bits.append("此刀穿过并切坏：" + "、".join(result["ruined"]) + "。")
        if result["finished"]:
            bits.append(
                "新成品：" + "、".join(x["name"] for x in result["finished"]) + "。"
            )
        flash(" ".join(bits), "ok")
        return redirect(url_for("job_page", job_id=job_id))

    @app.post("/jobs/<int:job_id>/reset")
    def reset_route(job_id: int):
        if _load_job_or_none(job_id) is None:
            flash("印张不存在。", "error")
            return redirect(url_for("index"))
        storage.reset_progress(app.config["DB_PATH"], job_id)
        flash("已清空全部落刀登记，纸张恢复为完整印张，重新排算。", "ok")
        return redirect(url_for("job_page", job_id=job_id))

    @app.post("/claims/<int:job_id>/return")
    @app.post("/jobs/<int:job_id>/return-claim")
    @app.post("/jobs/<int:job_id>/return")
    @app.post("/jobs/<int:job_id>/claim/return")
    def return_claim_route(job_id: int):
        db = app.config["DB_PATH"]
        wants_json = request.is_json or (
            "application/json" in request.headers.get("Accept", "")
        )
        if _load_job_or_none(job_id) is None:
            if wants_json:
                return jsonify({"ok": False, "error": "job not found"}), 404
            flash("印张不存在。", "error")
            return redirect(url_for("index"))

        try:
            result = storage.return_claimed_job(db, job_id)
        except ClaimRejected as cr:
            if wants_json:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error_code": cr.code,
                            "error": cr.message,
                            "reasons": cr.reasons,
                        }
                    ),
                    409,
                )
            flash(cr.message, "error")
            return redirect(url_for("job_page", job_id=job_id))

        source_job_id = result["source_job_id"]
        source_piece_id = result["source_piece_id"]
        source_exists = storage.get_job(db, source_job_id) is not None
        redirect_url = (
            url_for("job_page", job_id=source_job_id)
            if source_exists
            else url_for("index")
        )
        if wants_json:
            return jsonify(
                {
                    "ok": True,
                    "result": result,
                    "redirect": redirect_url,
                }
            )
        flash(
            f"试印新印张 #{job_id} 已撤销；源印张 #{source_job_id} 的"
            f"纸片 #{source_piece_id} 已恢复为可裁活纸片。",
            "ok",
        )
        return redirect(redirect_url)

    @app.post("/jobs/<int:job_id>/delete")
    def delete_route(job_id: int):
        storage.delete_job(app.config["DB_PATH"], job_id)
        flash("印张及其裁切进度已删除。", "ok")
        return redirect(url_for("index"))

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)

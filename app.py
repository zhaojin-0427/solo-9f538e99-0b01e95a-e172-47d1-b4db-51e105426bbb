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

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            job_stats=index_job_stats(),
            samples=SAMPLES,
            errors=None,
            old_payload=None,
        )

    @app.post("/jobs")
    def create_job_route():
        if request.is_json:
            payload = request.get_json(silent=True)
            try:
                name, w, h, frames = parse_job_payload(payload)
            except ValidationError as ve:
                return jsonify({"ok": False, "errors": ve.errors}), 400
            jid = storage.create_job(app.config["DB_PATH"], name, w, h, frames)
            return jsonify({"ok": True, "redirect": url_for("job_page", job_id=jid)})

        payload = frames_from_form(request.form)
        try:
            name, w, h, frames = parse_job_payload(payload)
        except ValidationError as ve:
            return (
                render_template(
                    "index.html",
                    job_stats=index_job_stats(),
                    samples=SAMPLES,
                    errors=ve.errors,
                    old_payload=payload,
                ),
                422,
            )
        jid = storage.create_job(app.config["DB_PATH"], name, w, h, frames)
        return redirect(url_for("job_page", job_id=jid))

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
            counts={"done": done, "ruined": ruined, "pending": pending},
        )

    @app.get("/jobs/<int:job_id>/plan.json")
    def plan_json(job_id: int):
        if _load_job_or_none(job_id) is None:
            return jsonify({"ok": False, "error": "job not found"}), 404
        return jsonify({"ok": True, "plan": build_plan(job_id)})

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

    @app.post("/jobs/<int:job_id>/delete")
    def delete_route(job_id: int):
        storage.delete_job(app.config["DB_PATH"], job_id)
        flash("印张及其裁切进度已删除。", "ok")
        return redirect(url_for("index"))

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)

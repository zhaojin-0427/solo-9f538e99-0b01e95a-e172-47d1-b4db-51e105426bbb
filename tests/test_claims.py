import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
import storage  # noqa: E402


class ClaimFlowTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(self.db)
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suffix)
            except OSError:
                pass

    def _create_edge_frame_source(self):
        # Frame occupies x=0..40.  A vertical cut at x=40 creates a
        # 40x100 overlapping left child and a 60x100 blank right child.
        sid = storage.create_job(
            self.db,
            "源印张",
            100,
            100,
            [{"name": "左角画", "x": 0, "y": 0, "w": 40, "h": 40}],
        )
        root = storage.list_pieces(self.db, sid)[0]
        result = storage.register_cut(self.db, sid, root["id"], "V", 40)
        overlap_pid, blank_pid = result["child_ids"]
        return sid, root["id"], overlap_pid, blank_pid

    def _claim_via_http(self, sid, pid, frames=None):
        frames = frames or [{"name": "试印小画", "x": 5, "y": 5, "w": 10, "h": 10}]
        return self.client.post(
            "/jobs",
            json={
                "name": "试印印张",
                "claim_source_job_id": sid,
                "claim_piece_id": pid,
                "frames": frames,
            },
        )

    def test_blank_edge_touching_piece_is_claimable(self):
        sid, _, overlap_pid, blank_pid = self._create_edge_frame_source()

        blank_ctx = storage.get_claim_context(self.db, sid, blank_pid)
        self.assertTrue(blank_ctx["claimable"], blank_ctx["reason"])
        self.assertEqual((blank_ctx["piece_w"], blank_ctx["piece_h"]), (60, 100))

        overlap_ctx = storage.get_claim_context(self.db, sid, overlap_pid)
        self.assertFalse(overlap_ctx["claimable"])
        self.assertEqual(overlap_ctx["reason_code"], "frame_overlap")

    def test_ruined_frame_still_blocks_claim(self):
        sid = storage.create_job(
            self.db,
            "切坏画框源",
            100,
            100,
            [{"name": "会被切坏", "x": 0, "y": 0, "w": 40, "h": 40}],
        )
        root = storage.list_pieces(self.db, sid)[0]
        result = storage.register_cut(self.db, sid, root["id"], "H", 20)
        self.assertIn("会被切坏", result["ruined"])

        for pid in result["child_ids"]:
            ctx = storage.get_claim_context(self.db, sid, pid)
            self.assertFalse(ctx["claimable"])
            self.assertEqual(ctx["reason_code"], "frame_overlap")
            with self.assertRaises(storage.ClaimRejected) as cm:
                storage.claim_and_create_job(self.db, sid, pid, "x", [])
            self.assertEqual(cm.exception.code, "frame_overlap")

    def test_claim_locks_piece_and_provenance_survives_source_delete(self):
        sid, _, _, pid = self._create_edge_frame_source()

        form_page = self.client.get(f"/jobs/{sid}/pieces/{pid}/claim")
        self.assertEqual(form_page.status_code, 200)
        self.assertIn("领用空白余纸".encode(), form_page.data)
        self.assertIn("60×100".encode(), form_page.data)

        resp = self._claim_via_http(sid, pid)
        self.assertEqual(resp.status_code, 200, resp.data)
        new_id = int(resp.get_json()["redirect"].rstrip("/").split("/")[-1])

        piece = storage.get_piece_by_id(self.db, pid)
        self.assertEqual(piece["status"], "claimed")

        source_page = self.client.get(f"/jobs/{sid}")
        self.assertEqual(source_page.status_code, 200)
        self.assertIn("已领用".encode(), source_page.data)
        self.assertIn(f"#{new_id}".encode(), source_page.data)

        new_page = self.client.get(f"/jobs/{new_id}")
        self.assertEqual(new_page.status_code, 200)
        body = new_page.data.decode()
        self.assertIn("出处", body)
        self.assertIn(f"印张 #{sid}", body)
        self.assertIn(f"纸片 #{pid}", body)
        self.assertIn("60×100", body)

        # The claimed source piece cannot receive another cut or claim.
        cut_resp = self.client.post(
            f"/jobs/{sid}/cuts",
            json={"piece_id": pid, "orient": "V", "pos": 50},
        )
        self.assertEqual(cut_resp.status_code, 409)
        self.assertEqual(cut_resp.get_json()["error_code"], "claimed")
        self.assertEqual(len(storage.list_cuts(self.db, sid)), 1)

        second_claim = self._claim_via_http(sid, pid)
        self.assertEqual(second_claim.status_code, 409)
        self.assertEqual(second_claim.get_json()["error_code"], "already_claimed")

        # Deleting the source job/piece must not remove provenance on new sheet.
        delete_source = self.client.post(f"/jobs/{sid}/delete")
        self.assertEqual(delete_source.status_code, 302)
        provenance = storage.get_claim_info(self.db, new_id)
        self.assertIsNotNone(provenance)
        self.assertEqual(provenance["source_job_id"], sid)
        self.assertEqual(provenance["source_piece_id"], pid)

        after_delete = self.client.get(f"/jobs/{new_id}")
        self.assertEqual(after_delete.status_code, 200)
        after_body = after_delete.data.decode()
        self.assertIn(f"印张 #{sid}", after_body)
        self.assertIn(f"纸片 #{pid}", after_body)
        self.assertIn("60×100", after_body)

    def test_return_before_any_cut_restores_piece_only_once(self):
        sid, _, _, pid = self._create_edge_frame_source()
        resp = self._claim_via_http(sid, pid)
        new_id = int(resp.get_json()["redirect"].rstrip("/").split("/")[-1])

        page = self.client.get(f"/jobs/{new_id}")
        self.assertIn("退回余纸".encode(), page.data)

        returned = self.client.post(f"/jobs/{new_id}/return", json={})
        self.assertEqual(returned.status_code, 200, returned.data)
        self.assertTrue(returned.get_json()["ok"])
        self.assertIsNone(storage.get_job(self.db, new_id))
        self.assertEqual(storage.get_piece_by_id(self.db, pid)["status"], "active")

        # Only after return can the same physical piece be cut again.
        cut = self.client.post(
            f"/jobs/{sid}/cuts",
            json={"piece_id": pid, "orient": "V", "pos": 55},
        )
        self.assertEqual(cut.status_code, 200, cut.data)
        self.assertEqual(storage.get_piece_by_id(self.db, pid)["status"], "split")

        ctx = storage.get_claim_context(self.db, sid, pid)
        self.assertFalse(ctx["claimable"])
        self.assertEqual(ctx["reason_code"], "already_split")

    def test_cut_then_reset_still_cannot_return(self):
        sid, _, _, pid = self._create_edge_frame_source()
        resp = self._claim_via_http(sid, pid)
        new_id = int(resp.get_json()["redirect"].rstrip("/").split("/")[-1])
        new_root = storage.list_pieces(self.db, new_id)[0]

        cut = self.client.post(
            f"/jobs/{new_id}/cuts",
            json={"piece_id": new_root["id"], "orient": "V", "pos": 20},
        )
        self.assertEqual(cut.status_code, 200, cut.data)

        blocked = self.client.post(f"/jobs/{new_id}/return", json={})
        self.assertEqual(blocked.status_code, 409)
        payload = blocked.get_json()
        self.assertEqual(payload["error_code"], "has_cuts")
        self.assertIn("has_cuts", payload["reasons"])

        reset = self.client.post(f"/jobs/{new_id}/reset")
        self.assertEqual(reset.status_code, 302)
        info = storage.get_claim_info(self.db, new_id)
        self.assertEqual(info["cut_ever"], 1)
        self.assertEqual(info["reopened"], 1)

        blocked_after_reset = self.client.post(f"/jobs/{new_id}/return", json={})
        self.assertEqual(blocked_after_reset.status_code, 409)
        payload = blocked_after_reset.get_json()
        self.assertIn("has_cuts", payload["reasons"])
        self.assertIn("reopened", payload["reasons"])
        self.assertEqual(storage.get_piece_by_id(self.db, pid)["status"], "claimed")
        self.assertIsNotNone(storage.get_job(self.db, new_id))

    def test_reset_without_cut_also_blocks_return(self):
        sid, _, _, pid = self._create_edge_frame_source()
        new_id = storage.claim_and_create_job(
            self.db, sid, pid, "未切先重开", []
        )
        storage.reset_progress(self.db, new_id)

        with self.assertRaises(storage.ClaimRejected) as cm:
            storage.return_claimed_job(self.db, new_id)
        self.assertEqual(cm.exception.code, "reopened")
        self.assertEqual(storage.get_piece_by_id(self.db, pid)["status"], "claimed")

    def test_source_reset_removes_original_piece_and_blocks_return(self):
        sid, _, _, pid = self._create_edge_frame_source()
        new_id = storage.claim_and_create_job(
            self.db, sid, pid, "源页重开", [
                {"name": "f", "x": 1, "y": 1, "w": 5, "h": 5}
            ]
        )

        storage.reset_progress(self.db, sid)
        self.assertIsNone(storage.get_piece_by_id(self.db, pid))

        info = storage.get_claim_info(self.db, new_id)
        self.assertFalse(info["source_piece_exists"])
        self.assertFalse(info["returnable"])
        self.assertIn("source_piece_missing", info["return_reasons"])

        with self.assertRaises(storage.ClaimRejected) as cm:
            storage.return_claimed_job(self.db, new_id)
        self.assertEqual(cm.exception.code, "source_piece_missing")
        self.assertIsNotNone(storage.get_job(self.db, new_id))

        page = self.client.get(f"/jobs/{new_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("出处".encode(), page.data)

    def test_native_form_claim_and_form_return_prg(self):
        sid, _, _, pid = self._create_edge_frame_source()
        resp = self.client.post(
            "/jobs",
            data={
                "name": "原生表单试印",
                "paper_w": "bad-client-value",
                "paper_h": "also-bad",
                "claim_source_job_id": str(sid),
                "claim_piece_id": str(pid),
                "fname": "画",
                "x": "2",
                "y": "2",
                "w": "8",
                "h": "8",
            },
        )
        self.assertEqual(resp.status_code, 302)
        new_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
        self.assertEqual(storage.get_job(self.db, new_id)["paper_w"], 60)
        self.assertEqual(storage.get_job(self.db, new_id)["paper_h"], 100)

        returned = self.client.post(f"/jobs/{new_id}/return", data={})
        self.assertEqual(returned.status_code, 302)
        self.assertTrue(returned.headers["Location"].endswith(f"/jobs/{sid}"))
        self.assertIsNone(storage.get_job(self.db, new_id))
        self.assertEqual(storage.get_piece_by_id(self.db, pid)["status"], "active")

    def test_provenance_json_survives_delete(self):
        sid, _, _, pid = self._create_edge_frame_source()
        new_id = storage.claim_and_create_job(
            self.db, sid, pid, "出处 JSON", [
                {"name": "f", "x": 1, "y": 1, "w": 4, "h": 4}
            ]
        )
        self.client.post(f"/jobs/{sid}/delete")
        r = self.client.get(f"/jobs/{new_id}/provenance.json")
        self.assertEqual(r.status_code, 200)
        p = r.get_json()["provenance"]
        self.assertEqual((p["source_job_id"], p["source_piece_id"]), (sid, pid))
        self.assertEqual((p["piece_w"], p["piece_h"]), (60, 100))
        self.assertFalse(p["source_job_exists"])

    def test_invalid_claim_submission_is_atomic(self):
        sid, _, _, pid = self._create_edge_frame_source()
        job_count_before = len(storage.list_jobs(self.db))

        resp = self.client.post(
            "/jobs",
            json={
                "claim_source_job_id": sid,
                "claim_piece_id": pid,
                "name": "bad",
                # Deliberately outside the locked 60x100 leftover.
                "frames": [{"x": 0, "y": 0, "w": 61, "h": 10}],
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(any("超出纸宽" in e for e in resp.get_json()["errors"]))
        self.assertEqual(len(storage.list_jobs(self.db)), job_count_before)
        self.assertEqual(storage.get_piece_by_id(self.db, pid)["status"], "active")
        self.assertIsNone(storage.get_claim_info(self.db, 999999))


if __name__ == "__main__":
    unittest.main()

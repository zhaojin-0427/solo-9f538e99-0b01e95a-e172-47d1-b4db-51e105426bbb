import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
import storage  # noqa: E402


class AppFlowTests(unittest.TestCase):
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

    def _create_pinwheel(self):
        resp = self.client.post("/samples/pinwheel", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        loc = resp.headers["Location"]
        job_id = int(loc.rstrip("/").split("/")[-1])
        return job_id

    def test_index_renders(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("裁纸师".encode(), r.data)

    def test_pinwheel_page_says_four_more(self):
        jid = self._create_pinwheel()
        r = self.client.get(f"/jobs/{jid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("最多还能裁成 4".encode(), r.data)
        plan = self.client.get(f"/jobs/{jid}/plan.json").get_json()
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["plan"]["max_more"], 4)
        self.assertEqual(plan["plan"]["steps"][0]["position"], 72)

    def test_validation_rejects_overlap_and_too_many(self):
        payload = {
            "name": "bad",
            "paper_w": 100,
            "paper_h": 100,
            "frames": [
                {"name": "a", "x": 0, "y": 0, "w": 60, "h": 60},
                {"name": "b", "x": 40, "y": 40, "w": 40, "h": 40},
            ],
        }
        r = self.client.post("/jobs", json=payload)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any("重叠" in e for e in r.get_json()["errors"]))

        payload2 = {
            "paper_w": 100,
            "paper_h": 100,
            "frames": [
                {"x": i * 7, "y": 0, "w": 5, "h": 5} for i in range(13)
            ],
        }
        r2 = self.client.post("/jobs", json=payload2)
        self.assertEqual(r2.status_code, 400)

    def test_reject_outside_keeps_progress(self):
        jid = self._create_pinwheel()
        r = self.client.post(
            f"/jobs/{jid}/cuts",
            json={"piece_id": 1, "orient": "H", "pos": 100},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error_code"], "outside")
        cuts = storage.list_cuts(self.db, jid)
        self.assertEqual(len(cuts), 0)

        r0 = self.client.post(
            f"/jobs/{jid}/cuts",
            json={"piece_id": 1, "orient": "V", "pos": 0},
        )
        self.assertEqual(r0.status_code, 409)
        self.assertEqual(r0.get_json()["error_code"], "outside")
        self.assertEqual(len(storage.list_cuts(self.db, jid)), 0)

    def test_reject_finished_piece(self):
        jid = storage.create_job(
            self.db,
            "full",
            50,
            50,
            [{"name": "整版", "x": 0, "y": 0, "w": 50, "h": 50}],
        )
        pieces = storage.list_pieces(self.db, jid)
        self.assertEqual(pieces[0]["status"], "finished")
        r = self.client.post(
            f"/jobs/{jid}/cuts",
            json={"piece_id": pieces[0]["id"], "orient": "V", "pos": 25},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error_code"], "finished")
        self.assertEqual(len(storage.list_cuts(self.db, jid)), 0)

    def test_reject_already_split(self):
        jid = self._create_pinwheel()
        ok = self.client.post(
            f"/jobs/{jid}/cuts",
            json={"piece_id": 1, "orient": "H", "pos": 72, "suggested_pos": 72},
        )
        self.assertEqual(ok.status_code, 200, ok.data)
        again = self.client.post(
            f"/jobs/{jid}/cuts",
            json={"piece_id": 1, "orient": "H", "pos": 30},
        )
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.get_json()["error_code"], "already_split")

    def test_off_target_cut_replans_and_persists(self):
        jid = self._create_pinwheel()
        # Planned H=72, cutter actually cuts H=74: this additionally slices C.
        r = self.client.post(
            f"/jobs/{jid}/cuts",
            json={"piece_id": 1, "orient": "H", "pos": 74, "suggested_pos": 72},
        )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["result"]["deviation"], 2)
        self.assertIn("C 大画", data["result"]["ruined"])
        self.assertIn("小画 D", data["result"]["ruined"])
        # A, B, E can still all be saved.
        self.assertEqual(data["plan"]["max_more"], 3)

        frames = {f["name"]: f["status"] for f in storage.list_frames(self.db, jid)}
        self.assertEqual(frames["C 大画"], "ruined")
        self.assertEqual(frames["小画 D"], "ruined")
        self.assertEqual(frames["A 大画"], "pending")

        # Persistence: a brand-new app on the same SQLite file sees progress.
        app2 = create_app(self.db)
        client2 = app2.test_client()
        plan = client2.get(f"/jobs/{jid}/plan.json").get_json()["plan"]
        self.assertEqual(plan["max_more"], 3)
        cuts = storage.list_cuts(self.db, jid)
        self.assertEqual(len(cuts), 1)
        self.assertEqual(cuts[0]["deviation"], 2)

    def test_form_post_and_flash_flow(self):
        jid = self._create_pinwheel()
        r = self.client.post(
            f"/jobs/{jid}/cuts",
            data={"piece_id": "1", "orient": "H", "pos": "999"},
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("不在纸片".encode(), r.data)
        self.assertEqual(len(storage.list_cuts(self.db, jid)), 0)

    def test_reset_restores_single_piece(self):
        jid = self._create_pinwheel()
        self.client.post(
            f"/jobs/{jid}/cuts",
            json={"piece_id": 1, "orient": "H", "pos": 72},
        )
        self.client.post(f"/jobs/{jid}/reset")
        pieces = storage.list_pieces(self.db, jid)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0]["status"], "active")
        plan = self.client.get(f"/jobs/{jid}/plan.json").get_json()["plan"]
        self.assertEqual(plan["max_more"], 4)


if __name__ == "__main__":
    unittest.main()

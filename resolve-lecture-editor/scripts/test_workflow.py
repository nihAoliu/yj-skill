"""Offline behavioral tests; no Resolve connection, uploads or paid requests."""
import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import workflow as w


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        (self.run / "start.png").write_bytes(b"test first image")
        (self.run / "end.png").write_bytes(b"test last image")
        self.gen = {"shots": [{"id": key, "plan_status": "approved", "start_status": "approved",
            "end_status": "approved", "start_path": str(self.run / "start.png"),
            "end_path": str(self.run / "end.png"), "start_prompt": "before",
            "end_prompt": "after", "motion_prompt": "assemble", "sfx_prompt": "paper only",
            "style_hash": "test-style", "audio_strategy": "native_sfx",
            "generation_parameters": {"duration": 5, "resolution": "720p"}}
            for key in ("B001", "B002")]}
        t = datetime.now(timezone.utc)
        self.quote = {"provider": "synthetic test provider", "quote_id": "test-1",
            "currency": "USD", "checked_at": (t-timedelta(minutes=1)).isoformat(),
            "valid_until": (t+timedelta(hours=1)).isoformat(), "validity_basis": "test fixture",
            "official_source_verified": True, "pricing_complete": True,
            "generation_hash": w.generation_hash(self.gen), "pilot_id": "B001", "total": "1.00",
            "evidence": {"e": {"url": "https://example.com/synthetic-test-only",
                "checked_at": (t-timedelta(minutes=1)).isoformat(), "basis": "not a live price"}},
            "jobs": [{"shot_id": key, "model": "test", "request_parameters": {"duration": 5},
                "requested_seconds": "5", "rounding_rule": "synthetic exact",
                "billing_scope": "synthetic complete", "first_frame_field": "start",
                "last_frame_field": "end", "native_sfx_evidence": "e", "subtotal": "0.50",
                "lines": [{"unit": "second", "quantity": "5", "quantity_basis": "test seconds",
                    "unit_price": "0.10", "certainty": "fixed", "evidence_id": "e"}]}
                for key in ("B001", "B002")]}
        self.write_approval()

    def write_approval(self):
        w.save(self.run / "generation.json", self.gen)
        w.save(self.run / "quote.json", self.quote)
        w.save(self.run / "approval.json", {"quote_hash": w.digest(self.quote),
                                             "confirmation": "synthetic offline approval"})

    def test_fractional_rate_and_nonzero_origin(self):
        self.assertEqual(w.fps("29.97 DF"), Fraction(30000, 1001))
        window = w.frame_window(1000, 2000, "30000/1001", 107892)
        self.assertEqual(window, {"in_frame": 107922, "out_frame": 107952,
                                  "duration_frames": 30, "marker_frame": 30})

    def test_drop_frame_labels(self):
        self.assertEqual(w.tc_frames("01:00:00;00", "29.97", True), 107892)
        self.assertEqual(w.tc_frames("00:01:00;02", "29.97", True), 1800)
        self.assertEqual(w.tc_frames("00:10:00;00", "29.97", True), 17982)
        with self.assertRaises(ValueError):
            w.tc_frames("00:01:00;00", "29.97", True)

    def test_shared_boundaries(self):
        left = w.frame_window(0, 101, "23.976")
        right = w.frame_window(101, 210, "23.976")
        self.assertEqual(left["out_frame"], right["in_frame"])

    def test_plan_overlap_and_title_overlay(self):
        p = {"fps": "25", "start_frame": 90000, "end_frame_exclusive": 90250, "items": [
            {"id": "B1", "kind": "broll", "in_frame": 90000, "out_frame": 90100},
            {"id": "T1", "kind": "title", "in_frame": 90000, "out_frame": 90050}]}
        self.assertEqual(w.validate_plan(p)["items"], 2)
        p["items"].append({"id": "B2", "kind": "broll", "in_frame": 90099, "out_frame": 90150})
        with self.assertRaises(ValueError):
            w.validate_plan(p)

    def test_quote_totals_and_estimate(self):
        self.assertEqual(w.validate_quote(self.quote, self.gen)["remaining"], "0.50")
        self.quote["jobs"][0]["lines"][0]["certainty"] = "metered"
        self.assertEqual(w.validate_quote(self.quote, self.gen)["amount_type"], "estimate")

    def test_reject_incomplete_and_incorrect_price(self):
        for mutate in (lambda q: q.update(pricing_complete=False),
                       lambda q: q.update(total="0.1"),
                       lambda q: q["jobs"][0]["lines"][0].update(unit_price=0.1),
                       lambda q: q["jobs"][0]["lines"][0].update(evidence_id="missing"),
                       lambda q: q["jobs"][0].update(last_frame_field="start")):
            q = copy.deepcopy(self.quote)
            mutate(q)
            with self.assertRaises(ValueError):
                w.validate_quote(q, self.gen)

    def test_changed_image_invalidates_quote(self):
        (self.run / "start.png").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            w.validate_quote(self.quote, self.gen)

    def test_editorial_move_does_not_invalidate_generation(self):
        before = w.generation_hash(self.gen)
        self.gen["shots"][0].update(in_frame=300, gain_db=-18)
        self.assertEqual(before, w.generation_hash(self.gen))

    def test_unapproved_frames_rejected(self):
        self.gen["shots"][0]["end_status"] = "pending"
        with self.assertRaises(ValueError):
            w.generation_hash(self.gen)

    def test_remainder_requires_pilot(self):
        with self.assertRaises(FileNotFoundError):
            w.claim(self.run, "B002")
        w.claim(self.run, "B001")

    def test_atomic_claim_prevents_duplicate_post(self):
        def attempt(_):
            try:
                w.claim(self.run, "B001")
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(16))), 1)

    def test_unknown_submission_cannot_be_reclaimed(self):
        w.claim(self.run, "B001")
        w.update_job(self.run, "test-1", "B001", "unknown_submission", None, "timeout after POST")
        with self.assertRaises(ValueError):
            w.claim(self.run, "B001")
        w.update_job(self.run, "test-1", "B001", "submitted", "real-job", "provider reconciled")

    def test_expired_quote_blocks_post_not_recovery(self):
        w.claim(self.run, "B001")
        self.quote["valid_until"] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.write_approval()
        with self.assertRaises(ValueError):
            w.claim(self.run, "B002")
        w.update_job(self.run, "test-1", "B001", "submitted", "known-job", "saved response")
        w.update_job(self.run, "test-1", "B001", "completed", None, "known output")

    def test_changed_job_id_rejected(self):
        w.claim(self.run, "B001")
        w.update_job(self.run, "test-1", "B001", "submitted", "a", "response")
        with self.assertRaises(ValueError):
            w.update_job(self.run, "test-1", "B001", "completed", "b", "wrong identity")

    def test_pilot_allows_remainder_and_modified_pilot_blocks_it(self):
        pilot = self.run / "pilot.mp4"
        pilot.write_bytes(b"synthetic approved pilot")
        w.save(self.run / "pilot_approval.json", {"quote_hash": w.digest(self.quote),
            "mode": "all_remaining", "path": str(pilot), "file_hash": w.filehash(pilot)})
        pilot.write_bytes(b"changed pilot")
        with self.assertRaises(ValueError):
            w.claim(self.run, "B002")
        pilot.write_bytes(b"synthetic approved pilot")
        self.assertEqual(w.claim(self.run, "B002")["state"], "submitting")


if __name__ == "__main__":
    unittest.main()

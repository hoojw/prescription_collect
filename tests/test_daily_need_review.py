import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from daily_need_review import merge_daily_need_review


class DailyNeedReviewTests(unittest.TestCase):
    @staticmethod
    def record(reason="audit_missing", collected_at="2026-08-27T10:00:00+08:00"):
        return {
            "platform": "MEITUAN",
            "prescription_key": "rx-key-1",
            "prescription_id": "rx-1",
            "order_view_id": "order-1",
            "collected_at": collected_at,
            "image_url": "https://example.invalid/rx.png?token=temporary",
            "need_review_reasons": [reason],
            "medicine_info": [{"drug_name": "测试药品"}],
        }

    def test_empty_batch_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "need_review.json"
            result = merge_daily_need_review(path, [])
            self.assertFalse(path.exists())
            self.assertEqual(result.total, 0)
            self.assertFalse(result.file_written)

    def test_first_record_is_added(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "need_review.json"
            result = merge_daily_need_review(path, [self.record()])
            self.assertEqual((result.added, result.updated, result.unchanged), (1, 0, 0))
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 1)

    def test_volatile_changes_do_not_rewrite_unchanged_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "need_review.json"
            merge_daily_need_review(path, [self.record()])
            later = self.record(collected_at="2026-08-27T10:30:00+08:00")
            later["run_id"] = "20260827_103000"
            later["image_url"] = "https://example.invalid/rx.png?token=refreshed"
            result = merge_daily_need_review(path, [later])
            self.assertEqual((result.added, result.updated, result.unchanged), (0, 0, 1))
            self.assertFalse(result.file_written)

    def test_changed_review_reason_replaces_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "need_review.json"
            merge_daily_need_review(path, [self.record()])
            changed = self.record(reason="diagnosis_missing")
            result = merge_daily_need_review(path, [changed])
            rows = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((result.added, result.updated, result.unchanged), (0, 1, 0))
            self.assertEqual(rows[0]["need_review_reasons"], ["diagnosis_missing"])

    def test_existing_duplicates_are_collapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "need_review.json"
            path.write_text(
                json.dumps([self.record(), self.record()]), encoding="utf-8"
            )
            result = merge_daily_need_review(path, [])
            self.assertEqual(result.existing_duplicates_removed, 1)
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 1)

    def test_resolved_external_hospital_reason_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "need_review.json"
            record = self.record(reason="hospital_source_needs_review")
            record["hospital_ocr"] = {"hospital_source_code": 2}
            record["need_review_reasons"].insert(0, "drug_0_status_206")
            result = merge_daily_need_review(path, [record])
            rows = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result.total, 1)
            self.assertEqual(rows[0]["need_review_reasons"], ["drug_0_status_206"])

    def test_resolved_hospital_only_record_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "need_review.json"
            record = self.record(reason="hospital_source_needs_review")
            record["hospital_source_code"] = 1
            result = merge_daily_need_review(path, [record])
            self.assertEqual(result.total, 0)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

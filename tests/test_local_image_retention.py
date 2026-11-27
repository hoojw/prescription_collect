import json
import os
import pathlib
import tempfile
import time
import unittest

import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from local_image_retention import cleanup_local_images


def write_records(run_dir, records):
    (run_dir / "prescriptions.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
        encoding="utf-8",
    )


def deliverable_record(image_file):
    return {
        "image_file": image_file,
        "cos_status": "success",
        "hospital_source_code": 1,
        "pharmacist_name": "审核药师",
        "patient_info": {
            "illnessDetail": [{"icdName": "高血压", "icdCode": "I10"}]
        },
        "medicine_info": [{
            "drug_name": "测试药品",
            "specification": "10mg*10片/盒",
            "total_unit": "盒",
            "drcode": "123",
            "usage_method": "口服",
            "usage_frequency": "一日1次",
            "dosage_per_time": 1,
            "dosage_unit": "片",
        }],
        "processing": [{"status_code": 100}],
    }


class LocalImageRetentionTests(unittest.TestCase):
    def test_removes_only_cos_uploaded_non_review_images_outside_retained_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            runs = pathlib.Path(temp)
            old = runs / "old"
            newest = runs / "newest"
            old_images = old / "images"
            newest_images = newest / "images"
            old_images.mkdir(parents=True)
            newest_images.mkdir(parents=True)
            for name in ("delete.png", "review.png", "cos_failed.png"):
                (old_images / name).write_bytes(b"image")
            (newest_images / "new.png").write_bytes(b"image")
            write_records(
                old,
                [
                    deliverable_record("images/delete.png"),
                    {
                        "image_file": "images/review.png",
                        "cos_status": "success",
                        "processing": [{"status_code": 301}],
                    },
                    {
                        "image_file": "images/cos_failed.png",
                        "cos_status": "failed",
                        "processing": [{"status_code": 100}],
                    },
                ],
            )
            write_records(
                newest,
                [
                    deliverable_record("images/new.png")
                ],
            )
            now = time.time()
            os.utime(old, (now, now))
            os.utime(newest, (now + 10, now + 10))

            result = cleanup_local_images(runs, image_retention_runs=1)

            self.assertEqual(result.deleted, 1)
            self.assertFalse((old_images / "delete.png").exists())
            self.assertTrue((old_images / "review.png").exists())
            self.assertTrue((old_images / "cos_failed.png").exists())
            self.assertTrue((newest_images / "new.png").exists())

    def test_zero_retention_disables_image_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            runs = pathlib.Path(temp)
            image_dir = runs / "old" / "images"
            image_dir.mkdir(parents=True)
            image_path = image_dir / "keep.png"
            image_path.write_bytes(b"image")
            write_records(
                image_dir.parent,
                [
                    deliverable_record("images/keep.png")
                ],
            )

            result = cleanup_local_images(runs, image_retention_runs=0)

            self.assertEqual(result.deleted, 0)
            self.assertTrue(image_path.exists())

    def test_handles_date_grouped_runs_without_counting_date_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            runs = pathlib.Path(temp)
            old = runs / "20260818" / "20260818_101533"
            newest = runs / "20260819" / "20260819_101533"
            for run_dir, name in ((old, "old.png"), (newest, "new.png")):
                image_dir = run_dir / "images"
                image_dir.mkdir(parents=True)
                (image_dir / name).write_bytes(b"image")
                write_records(
                    run_dir,
                    [deliverable_record(f"images/{name}")],
                )
            now = time.time()
            os.utime(old, (now, now))
            os.utime(newest, (now + 10, now + 10))

            result = cleanup_local_images(runs, image_retention_runs=1)

            self.assertEqual(result.deleted, 1)
            self.assertFalse((old / "images" / "old.png").exists())
            self.assertTrue((newest / "images" / "new.png").exists())


if __name__ == "__main__":
    unittest.main()

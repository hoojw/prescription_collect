import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from prescription_delivery import (
    _date_from_record,
    format_collect_time,
    record_needs_review,
    review_reasons_for_record,
)


class DeliveryReviewTests(unittest.TestCase):
    @staticmethod
    def valid_record(**overrides):
        record = {
            "hospital_source_code": 1,
            "processing": [{"drug_index": 0, "status_code": 100}],
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
        }
        record.update(overrides)
        return record

    def test_utc_collection_time_is_rendered_in_shanghai_time(self):
        self.assertEqual(
            format_collect_time("2026-08-21T07:29:00.568Z"),
            "2026-08-21 15:29:00",
        )

    def test_existing_shanghai_offset_is_not_shifted_twice(self):
        self.assertEqual(
            format_collect_time("2026-08-21T15:29:00+08:00"),
            "2026-08-21 15:29:00",
        )

    def test_naive_platform_time_is_preserved(self):
        self.assertEqual(
            format_collect_time("2026-08-21 15:29:00"),
            "2026-08-21 15:29:00",
        )

    def test_upload_date_keeps_prescription_timestamp(self):
        self.assertEqual(
            _date_from_record(
                {
                    "prescription_time": "2026-08-17T10:15:33+08:00",
                    "collected_at": "2026-08-18T09:00:00+08:00",
                }
            ),
            "2026-08-17 10:15:33",
        )

    def test_upload_date_falls_back_to_collection_timestamp(self):
        self.assertEqual(
            _date_from_record(
                {
                    "prescription_time": "",
                    "collected_at": "2026-08-18T09:00:00+08:00",
                }
            ),
            "2026-08-18 09:00:00",
        )

    def test_ocr_and_catalog_success_are_uploadable(self):
        for code in (100, 110, 120):
            record = self.valid_record(
                processing=[{"drug_index": 0, "status_code": code}]
            )
            self.assertFalse(record_needs_review(record))
            self.assertEqual(review_reasons_for_record(record), [])

    def test_any_failed_drug_moves_entire_prescription_to_review(self):
        record = self.valid_record(
            processing=[
                {"drug_index": 0, "status_code": 100},
                {"drug_index": 1, "status_code": 301},
            ],
        )
        self.assertTrue(record_needs_review(record))
        self.assertEqual(
            review_reasons_for_record(record),
            ["drug_1_status_301"],
        )

    def test_hospital_source_review_is_not_uploadable(self):
        record = self.valid_record(hospital_source_code=3)
        self.assertEqual(
            review_reasons_for_record(record),
            ["hospital_source_needs_review"],
        )

    def test_missing_processing_is_reviewed_conservatively(self):
        self.assertEqual(
            review_reasons_for_record(
                self.valid_record(processing=None)
            ),
            ["processing_status_missing"],
        )

    def test_meituan_missing_drcode_is_not_uploadable(self):
        record = self.valid_record(
            processing=[
                {
                    "drug_index": 0,
                    "status_code": 100,
                    "drcode_status": 11,
                }
            ],
        )
        self.assertEqual(
            review_reasons_for_record(record),
            ["drug_0_drcode_status_11"],
        )

    def test_successful_drcode_status_remains_uploadable(self):
        record = self.valid_record(
            processing=[
                {
                    "drug_index": 0,
                    "status_code": 100,
                    "drcode_status": 2,
                }
            ],
        )
        self.assertEqual(review_reasons_for_record(record), [])

    def test_missing_audit_is_not_uploadable(self):
        record = self.valid_record(pharmacist_name="")
        self.assertEqual(review_reasons_for_record(record), ["audit_missing"])

    def test_missing_diagnosis_is_not_uploadable(self):
        record = self.valid_record(patient_info={})
        self.assertEqual(review_reasons_for_record(record), ["diagnosis_missing"])

    def test_doctor_remains_optional(self):
        record = self.valid_record(physician_name="", hospital_name="外部医院")
        self.assertEqual(review_reasons_for_record(record), [])

    def test_empty_dosage_unit_is_not_uploadable(self):
        record = self.valid_record()
        record["medicine_info"][0]["dosage_unit"] = ""
        self.assertEqual(
            review_reasons_for_record(record),
            ["drug_0_dosage_unit_missing"],
        )



if __name__ == "__main__":
    unittest.main()

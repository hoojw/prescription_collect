import unittest

from platform_required_validation import (
    attach_platform_required_review,
    missing_field_counts,
    validate_platform_required_fields,
)


def complete_record():
    return {
        "order_view_id": "order-1",
        "prescription_id": "rx-1",
        "prescription_time": "2026-08-04T10:00:00+08:00",
        "image_url": "https://example.test/rx.png",
        "total_fee": 29.0,
        "patient_info": {"name": "患者", "gender": "女", "age": 40},
        "icd_name": "普通感冒",
        "drug_infos": [
            {"drugName": "药品", "packingSpec": "10片/盒", "drugCount": 1}
        ],
    }


class PlatformRequiredValidationTests(unittest.TestCase):
    def test_complete_meituan_record_has_no_missing_platform_fields(self):
        missing, sources = validate_platform_required_fields(complete_record(), "MEITUAN")
        self.assertEqual(missing, [])
        self.assertEqual(sources, [])

    def test_missing_fields_are_reported_without_treating_optional_fields_as_missing(self):
        record = complete_record()
        record["total_fee"] = None
        record["patient_info"]["name"] = ""
        record["drug_infos"][0]["packingSpec"] = ""
        missing, _ = validate_platform_required_fields(record, "MEITUAN")
        self.assertEqual(
            missing,
            [
                "total_fee",
                "patient_info.name",
                "drug_infos[0].packingSpec",
            ],
        )

    def test_meituan_reuses_existing_patient_diagnosis_and_name_spec_fallbacks(self):
        record = complete_record()
        record["patient_info"] = {
            "medicalUserName": "患者",
            "gender": "女",
            "age": 40,
            "illnessDetail": [{"icdName": "干眼症", "icdCode": "H04.1"}],
        }
        record["icd_name"] = ""
        record["drug_infos"] = [
            {
                "drugName": "[施图伦]七叶洋地黄双苷滴眼液(0.006mg:0.040mg)*0.4ml*10支/盒",
                "packingSpec": None,
                "drugCount": 1,
            }
        ]

        missing, sources = validate_platform_required_fields(record, "MEITUAN")

        self.assertEqual(missing, [])
        self.assertEqual(sources, [])

    def test_meituan_still_reviews_missing_explicit_and_name_suffix_specification(self):
        record = complete_record()
        record["drug_infos"][0] = {
            "drugName": "无规格药品名称",
            "packingSpec": None,
            "drugCount": 1,
        }

        missing, _ = validate_platform_required_fields(record, "MEITUAN")

        self.assertIn("drug_infos[0].packingSpec", missing)

    def test_jd_requires_platform_sku_but_meituan_uses_catalog_rule(self):
        record = complete_record()
        missing, _ = validate_platform_required_fields(record, "JINGDONG")
        self.assertIn("drug_infos[0].drcode", missing)
        missing, _ = validate_platform_required_fields(record, "MEITUAN")
        self.assertNotIn("drug_infos[0].drcode", missing)

    def test_review_annotation_and_counts(self):
        record = complete_record()
        record["total_fee"] = None
        missing, sources = validate_platform_required_fields(record, "MEITUAN")
        review = attach_platform_required_review(record, "MEITUAN", missing, sources)
        self.assertEqual(review["need_review_reasons"], ["platform_required_missing"])
        self.assertFalse(review["upload_eligible"])
        self.assertEqual(missing_field_counts([review]), {"total_fee": 1})


if __name__ == "__main__":
    unittest.main()

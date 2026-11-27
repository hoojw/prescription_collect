import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from drug_catalog import (
    STATUS_CATALOG_DOSE_INVALID,
    STATUS_CATALOG_NAME_NOT_FOUND,
    STATUS_QUALITATIVE_30_DAY_SUCCESS,
    DrugCatalog,
    catalog_entry_from_row,
    clinical_key,
    clinical_specification,
)


def maintenance_row(
    drug_id: str,
    specification: str,
    frequency: str = "一日1-4次",
    dose: str = "适量",
):
    return {
        "药品ID": drug_id,
        "药品名称": "整蛋白型肠内营养剂(粉剂)",
        "规格": specification,
        "包装数量": 1,
        "使用频度": frequency,
        "每次剂量": dose,
        "用药总量": 1,
        "金额": 54.5,
        "用药天数": 1,
    }


class DrugCatalogQualitativeFallbackTests(unittest.TestCase):
    def test_duration_per_purchase_unit_scales_with_box_count(self):
        entry = catalog_entry_from_row(
            {
                "药品ID": "salbutamol-200-days-per-box",
                "药品名称": "硫酸沙丁胺醇吸入气雾剂",
                "规格": "100μg*200揿/罐/盒",
                "每购买单位用药天数": 200,
                "用药天数购买单位": "盒",
            }
        )
        drug = {
            "drugName": "[万托林]硫酸沙丁胺醇吸入气雾剂100µg*200揿/罐/盒",
            "drugCount": 3,
            "purchase_quantity": 3,
            "purchase_unit": "盒",
            "dose_per_time": 0.005,
            "dose_unit": "瓶",
            "times_per_day": 1,
        }
        DrugCatalog([entry]).apply_if_needed(drug)
        self.assertEqual(drug["duration_days"], 600)
        self.assertEqual(drug["status_code"], 110)
        self.assertEqual(
            drug["duration_rule_code"],
            "catalog_duration_days_per_purchase_unit",
        )

    def test_duration_per_purchase_unit_requires_exact_specification(self):
        entry = catalog_entry_from_row(
            {
                "药品ID": "salbutamol-200-days-per-box",
                "药品名称": "硫酸沙丁胺醇吸入气雾剂",
                "规格": "100μg*200揿/罐/盒",
                "每盒用药天数": 200,
            }
        )
        drug = {
            "drugName": "硫酸沙丁胺醇吸入气雾剂100μg*100揿/罐/盒",
            "drugCount": 3,
            "purchase_quantity": 3,
            "purchase_unit": "盒",
        }
        DrugCatalog([entry]).apply_if_needed(drug)
        self.assertNotEqual(drug["status_code"], 110)

    def test_exact_catalog_duration_overrides(self):
        cases = (
            ("吡美莫司乳膏", "1%*15g/支/盒", 30),
            ("多磺酸粘多糖乳膏", "14g/支/盒", 10),
            ("水飞蓟素胶囊", "140mg*10粒/板/盒", 3),
            (
                "沙美特罗替卡松吸入粉雾剂(含1个准纳器)",
                "(50μg:250μg)*60泡/盒",
                30,
            ),
        )
        for name, specification, expected_days in cases:
            with self.subTest(name=name):
                drug = {"drugName": name, "packingSpec": specification}
                entry = catalog_entry_from_row(
                    {
                        "药品ID": f"fixed-{expected_days}",
                        "药品名称": name,
                        "规格": specification,
                        "固定用药天数": expected_days,
                    }
                )
                DrugCatalog([entry]).apply_if_needed(drug)
                self.assertEqual(drug["duration_days"], expected_days)
                self.assertEqual(drug["status_code"], 110)
                self.assertEqual(drug["duration_rule_code"], "catalog_explicit_duration_days")

    def test_catalog_duration_override_requires_exact_specification(self):
        drug = {"drugName": "吡美莫司乳膏", "packingSpec": "1%*10g/支/盒"}
        entry = catalog_entry_from_row(
            {
                "药品ID": "fixed-30",
                "药品名称": "吡美莫司乳膏",
                "规格": "1%*15g/支/盒",
                "固定用药天数": 30,
            }
        )
        DrugCatalog([entry]).apply_if_needed(drug)
        self.assertNotEqual(drug["status_code"], 110)

    def test_new_maintenance_headers_and_qualitative_values_are_indexed(self):
        entry = catalog_entry_from_row(
            maintenance_row("drug-1", "320g")
        )
        self.assertEqual(entry["id"], "drug-1")
        self.assertEqual(entry["q"], ["适量"])
        self.assertEqual(entry["x"], STATUS_CATALOG_DOSE_INVALID)
        self.assertEqual(entry["t"], 1)
        self.assertEqual(entry["e"], None)

    def test_bulk_powder_pack_variants_share_clinical_key(self):
        keys = {
            clinical_key(
                clinical_specification(
                    "整蛋白型肠内营养剂(粉剂)",
                    specification,
                )
            )
            for specification in (
                "320g",
                "320g/听",
                "320g6瓶装",
                "320g 12瓶装",
            )
        }
        self.assertEqual(len(keys), 1)
        self.assertNotEqual(keys, {""})

    def test_qualitative_source_and_catalog_default_to_30_days(self):
        catalog = DrugCatalog(
            [
                catalog_entry_from_row(
                    maintenance_row("drug-1", "320g")
                ),
                catalog_entry_from_row(
                    maintenance_row("drug-2", "320g/听")
                ),
                catalog_entry_from_row(
                    maintenance_row("drug-3", "320g6瓶装")
                ),
            ]
        )
        drug = {
            "drugName": "整蛋白型肠内营养剂(粉剂)",
            "packingSpec": "320g 12瓶装",
            "drugCount": 1,
            "usage_text": "口服，必要时，一次320.0g",
            "usage_method": "口服",
            "frequency_text": "必要时",
            "times_per_day": None,
            "dose_per_time": 320,
            "dose_unit": "g",
            "usage_source": "prescription_image_paddleocr",
        }
        catalog.apply_if_needed(drug)
        self.assertEqual(
            drug["status_code"],
            STATUS_QUALITATIVE_30_DAY_SUCCESS,
        )
        self.assertEqual(drug["duration_days"], 30)
        self.assertEqual(
            drug["duration_calculation_reason"],
            "qualitative_usage_catalog_default_30_days",
        )

    def test_catalog_missing_does_not_trigger_30_day_default(self):
        catalog = DrugCatalog([])
        drug = {
            "drugName": "整蛋白型肠内营养剂(粉剂)",
            "packingSpec": "320g 12瓶装",
            "frequency_text": "必要时",
            "dose_per_time": 320,
            "dose_unit": "g",
        }
        catalog.apply_if_needed(drug)
        self.assertEqual(drug["status_code"], STATUS_CATALOG_NAME_NOT_FOUND)
        self.assertIsNone(drug["duration_days"])

    def test_catalog_qualitative_without_source_qualitative_stays_review(self):
        catalog = DrugCatalog(
            [catalog_entry_from_row(maintenance_row("drug-1", "320g"))]
        )
        drug = {
            "drugName": "整蛋白型肠内营养剂(粉剂)",
            "packingSpec": "320g 12瓶装",
            "usage_ocr_status": "not_found",
        }
        catalog.apply_if_needed(drug)
        self.assertEqual(drug["status_code"], STATUS_CATALOG_DOSE_INVALID)
        self.assertIsNone(drug["duration_days"])


if __name__ == "__main__":
    unittest.main()

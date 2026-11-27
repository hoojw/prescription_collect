import pathlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from prescription_delivery import ApiSubmitResult
import server_meituan_prescription_collect as collector


class FakeTab:
    def __init__(self, pages):
        self.pages = iter(pages)


class CollectorTests(unittest.TestCase):
    def test_login_check_retries_target_navigation_race(self):
        class FlakyLoginTab:
            def __init__(self):
                self.calls = 0

            def run_js(self, _expression):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError(
                        "CDP Runtime.evaluate failed: Inspected target navigated or closed"
                    )
                return '{"httpStatus": 200, "code": 0}'

        tab = FlakyLoginTab()
        with mock.patch.object(collector.time, "sleep"):
            self.assertTrue(collector.check_login(tab))
        self.assertEqual(tab.calls, 2)

    def test_summary_keeps_api_response_fields_and_uncertain_status(self):
        summary = collector.RunSummary(run_id="run", date_range="2026-07-20..2026-07-20")
        result = ApiSubmitResult(
            success=False,
            http_status=200,
            err_code=99999,
            message="服务器开小差了",
            response={"success": False, "errCode": 99999, "errMsg": "服务器开小差了"},
        )
        collector.apply_api_result_to_summary(summary, result, "runs/run/api_response.json")
        self.assertEqual(summary.api_status, "uncertain_response")
        self.assertEqual(summary.api_err_code, 99999)
        self.assertFalse(summary.api_response_success)
        self.assertEqual(summary.api_response_file, "runs/run/api_response.json")
        self.assertIsNone(summary.api_total_count)

    def test_date_range_includes_entire_day(self):
        start, end, label = collector.parse_date_range("2026-07-01", "2026-07-02", 3)
        self.assertEqual(label, "2026-07-01..2026-07-02")
        self.assertGreater(end - start, 47 * 3600)

    def test_safe_file_component(self):
        self.assertEqual(collector.safe_file_component("12/34:*", "x"), "12_34")

    def test_strip_meituan_expiry_suffix(self):
        self.assertEqual(
            collector.strip_meituan_expiry_suffix(
                "坎地沙坦酯片 8mg*7片/板/盒【27年4月到期】"
            ),
            "坎地沙坦酯片 8mg*7片/板/盒",
        )
        self.assertEqual(
            collector.strip_meituan_expiry_suffix("某药10片/盒[有效期至2027-04]"),
            "某药10片/盒",
        )
        self.assertEqual(
            collector.strip_meituan_expiry_suffix("某药10片/盒【儿童装】"),
            "某药10片/盒【儿童装】",
        )

    def test_normalize_meituan_drug_keeps_changed_source_for_audit(self):
        normalized = collector.normalize_meituan_drug(
            {
                "drugName": "坎地沙坦酯片 8mg*7片/板/盒【27年4月到期】",
                "packingSpec": "8mg*7片/板/盒【临期】",
                "count": 3,
            }
        )
        self.assertEqual(normalized["drugName"], "坎地沙坦酯片 8mg*7片/板/盒")
        self.assertEqual(normalized["packingSpec"], "8mg*7片/板/盒")
        self.assertEqual(
            normalized["source_drug_name"],
            "坎地沙坦酯片 8mg*7片/板/盒【27年4月到期】",
        )
        self.assertEqual(normalized["source_packing_spec"], "8mg*7片/板/盒【临期】")
        self.assertEqual(normalized["count"], 3)

    def test_total_fee_is_yuan_decimal_8_2(self):
        self.assertEqual(collector.normalize_total_fee(272.6), 272.6)
        self.assertEqual(collector.normalize_total_fee("12.345"), 12.35)
        self.assertEqual(collector.normalize_total_fee("999999.99"), 999999.99)
        self.assertIsNone(collector.normalize_total_fee("1000000.00"))
        self.assertIsNone(collector.normalize_total_fee("invalid"))

    def test_program_directory_is_accessible(self):
        self.assertTrue(pathlib.Path(collector.__file__).resolve().parent.is_dir())

    def test_store_context_requires_exact_expected_poi_id(self):
        self.assertEqual(
            collector.validate_store_context("32581900", "32581900"),
            "32581900",
        )
        with self.assertRaisesRegex(RuntimeError, "expected_poi_id is required"):
            collector.validate_store_context("32581900", "")
        with self.assertRaisesRegex(RuntimeError, "current browser store poiId is missing"):
            collector.validate_store_context("", "32581900")
        with self.assertRaisesRegex(RuntimeError, "browser store mismatch"):
            collector.validate_store_context("999", "32581900")

    def test_normalize_prescriptions(self):
        order = {
            "order_view_id": "o1",
            "status_desc": "待发货",
            "poi_id": "32581900",
            "total_fee": 272.6,
        }
        detail = {
            "payload": {
                "data": {
                    "patientInfo": {"medicalUserName": "测试"},
                    "drugInfos": [{"drugName": "药品"}],
                    "pharmacistName": "药师",
                    "icdName": "测试诊断",
                }
            },
            "infos": [
                {
                    "rpId": "p1",
                    "prescriptionUrl": ["https://example.invalid/p1.png"],
                }
            ]
        }
        rows = collector.normalize_prescriptions(order, detail)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["prescription_id"], "p1")
        self.assertEqual(rows[0]["patient_info"]["medicalUserName"], "测试")
        self.assertEqual(rows[0]["drug_infos"][0]["drugName"], "药品")
        self.assertEqual(rows[0]["pharmacist_name"], "药师")
        self.assertEqual(rows[0]["icd_name"], "测试诊断")
        self.assertEqual(rows[0]["structured_fields_source"], "meituan_order_detail_api")
        self.assertEqual(rows[0]["image_role"], "field_supplement_and_verification")
        self.assertEqual(rows[0]["image_status"], "pending")
        self.assertEqual(rows[0]["poi_id"], "32581900")
        self.assertEqual(rows[0]["total_fee"], 272.6)
        self.assertNotIn("totalFee", rows[0])

    def test_normalize_prescriptions_cleans_expiry_before_downstream_processing(self):
        order = {"order_view_id": "o1"}
        detail = {
            "payload": {
                "data": {
                    "drugInfos": [
                        {
                            "drugName": "坎地沙坦酯片 8mg*7片/板/盒【27年4月到期】",
                            "packingSpec": "8mg*7片/板/盒",
                        }
                    ]
                }
            },
            "infos": [{"rpId": "p1", "prescriptionUrl": ["https://example.invalid/p1.png"]}],
        }
        rows = collector.normalize_prescriptions(order, detail)
        self.assertEqual(rows[0]["drug_infos"][0]["drugName"], "坎地沙坦酯片 8mg*7片/板/盒")
        self.assertIn("source_drug_name", rows[0]["drug_infos"][0])

    def test_multiple_images_have_distinct_keys(self):
        order = {"order_view_id": "o1", "status_desc": "已完成"}
        detail = {
            "infos": [
                {
                    "prescriptionIds": ["p1"],
                    "prescriptionUrl": ["https://example.invalid/1.png", "https://example.invalid/2.png"],
                }
            ]
        }
        rows = collector.normalize_prescriptions(order, detail)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["prescription_key"], rows[1]["prescription_key"])

    def test_duplicate_detail_infos_are_collapsed(self):
        order = {"order_view_id": "o1", "status_desc": "已完成"}
        duplicate_info = {
            "prescriptionIds": ["p1"],
            "prescriptionUrl": ["https://example.invalid/p1.png"],
        }
        detail = {"infos": [dict(duplicate_info), dict(duplicate_info)]}
        rows = collector.normalize_prescriptions(order, detail)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["prescription_id"], "p1")
        self.assertEqual(rows[0]["image_url"], "https://example.invalid/p1.png")

    def test_comma_separated_ids_map_to_distinct_info_images(self):
        order = {"order_view_id": "o1", "status_desc": "已完成"}
        detail = {
            "infos": [
                {
                    "prescriptionIds": "p1,p2",
                    "prescriptionUrl": ["https://example.invalid/p1.png"],
                },
                {
                    "prescriptionIds": "p1,p2",
                    "prescriptionUrl": ["https://example.invalid/p2.png"],
                },
            ]
        }
        rows = collector.normalize_prescriptions(order, detail)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["prescription_id"] for row in rows], ["p1", "p2"])
        self.assertNotEqual(rows[0]["prescription_key"], rows[1]["prescription_key"])

    def test_multi_prescription_order_is_skipped(self):
        records = [
            {"prescription_id": "p1", "image_url": "u1"},
            {"prescription_id": "p2", "image_url": "u2"},
        ]
        self.assertTrue(collector.is_multi_prescription_order(records))

    def test_single_prescription_order_is_not_skipped(self):
        self.assertFalse(collector.is_multi_prescription_order([{"prescription_id": "p1"}]))

    def test_multi_drug_ocr_status_is_partial(self):
        status = collector.aggregate_usage_ocr_status(
            [
                {"usage_ocr_status": "success"},
                {"usage_ocr_status": "not_found"},
            ]
        )
        self.assertEqual(status, "partial")

    def test_header_ocr_fills_only_missing_web_fields(self):
        record = {"patient_info": {}, "department_name": ""}
        applied = collector.apply_patient_header_fields(
            record,
            {
                "birth_date": "2007-03-02",
                "birth_date_ocr_status": "success",
                "department": "内科",
                "department_ocr_status": "success",
            },
        )
        self.assertEqual(record["patient_info"]["birth_date"], "2007-03-02")
        self.assertEqual(record["patient_info"]["department"], "内科")
        self.assertEqual(
            record["patient_info_field_sources"]["birth_date"],
            "prescription_image_paddleocr",
        )
        self.assertTrue(applied["birth_date"])
        self.assertTrue(applied["department"])

    def test_header_ocr_never_overwrites_web_fields_or_applies_low_confidence(self):
        record = {
            "patient_info": {"birthDate": "2000-01-01", "departmentName": "外科"},
            "department_name": "",
        }
        applied = collector.apply_patient_header_fields(
            record,
            {
                "birth_date": "2007-03-02",
                "birth_date_ocr_status": "success",
                "department": "内科",
                "department_ocr_status": "low_confidence",
            },
        )
        self.assertEqual(record["patient_info"]["birthDate"], "2000-01-01")
        self.assertEqual(record["patient_info"]["departmentName"], "外科")
        self.assertNotIn("birth_date", record["patient_info"])
        self.assertNotIn("department", record["patient_info"])
        self.assertFalse(applied["birth_date"])
        self.assertFalse(applied["department"])
        self.assertEqual(
            record["patient_info_field_sources"],
            {"birth_date": "meituan_order_detail_api", "department": "meituan_order_detail_api"},
        )

    def test_state_store_deduplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "state.sqlite3"
            state = collector.StateStore(path)
            row = {
                "prescription_key": "k1",
                "prescription_id": "p1",
                "order_view_id": "o1",
            }
            self.assertFalse(state.contains("k1"))
            state.mark(row, "hash", "run")
            self.assertTrue(state.contains("k1"))
            self.assertTrue(state.contains_order("o1"))
            state.mark_api(["k1"], "success", "accepted")
            self.assertTrue(state.contains("k1"))
            self.assertTrue(state.contains_order("o1"))
            delivery = state.delivery_state("k1")
            self.assertEqual(delivery["api_status"], "success")
            self.assertEqual(delivery["upload_attempts"], 1)
            state.mark_api(["k1"], "uncertain_response", "response contradicted storage")
            self.assertTrue(state.contains("k1"))
            self.assertTrue(state.contains_order("o1"))
            delivery = state.delivery_state("k1")
            self.assertEqual(delivery["api_status"], "uncertain_response")
            self.assertEqual(delivery["upload_attempts"], 2)
            self.assertFalse(state.contains_order("o2"))
            state.close()

    def test_retention_keeps_latest_three_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            runs = pathlib.Path(temp)
            now = time.time()
            children = []
            for index in range(5):
                child = runs / f"run_{index}"
                child.mkdir()
                os.utime(child, (now + index, now + index))
                children.append(child)
            removed = collector.purge_old_runs(runs, retention_runs=3)
            self.assertEqual({path.name for path in removed}, {"run_0", "run_1"})
            self.assertEqual(
                {path.name for path in runs.iterdir()},
                {"run_2", "run_3", "run_4"},
            )


if __name__ == "__main__":
    unittest.main()

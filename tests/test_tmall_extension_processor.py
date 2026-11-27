import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tmall_extension_processor import (
    ProcessorSettings,
    drug_infos_from_order,
    final_review_reasons,
    is_used_prescription,
    load_settings,
    process_batch,
)
from tmall_extension_receiver.store import BatchStore
from tmall_prescription_ocr import parse_tmall_header_lines
from prescription_delivery import ApiSubmitResult


class FakeOCR:
    def recognize_diagnosis(self, _path):
        return {
            "disease_name": "慢性肝炎",
            "disease_code": "",
            "diagnosis_ocr_status": "success",
            "diagnosis_ocr_confidence": 0.98,
            "age": "41",
            "age_ocr_status": "success",
            "age_ocr_confidence": 0.99,
        }

    def enrich_usage(self, drugs, _path):
        output = []
        for source in drugs:
            drug = dict(source)
            drug.update(
                {
                    "usage_method": "口服",
                    "frequency_text": "一日三次",
                    "dose_per_time": 1,
                    "dose_unit": "片",
                    "duration_days": 24,
                    "duration_calculation_status": "success",
                    "duration_calculation_reason": "calculated",
                    "usage_ocr_status": "success",
                }
            )
            output.append(drug)
        return output, {"usage_found": len(output)}

    def timing_metrics(self, _image_count=0):
        return {"ocr_predict_calls": 2}


class ProcessorTests(unittest.TestCase):
    def test_only_used_prescriptions_are_eligible_for_processing(self):
        self.assertTrue(is_used_prescription({"prescription_status": "已使用"}))
        self.assertTrue(is_used_prescription({"prescription_status": "3"}))
        self.assertFalse(is_used_prescription({"prescription_status": "有效"}))
        self.assertFalse(is_used_prescription({"prescription_status": "已过期"}))
        self.assertFalse(is_used_prescription({"prescription_status": ""}))

    def test_example_config_enables_tianmao_production_api(self):
        settings = load_settings(
            pathlib.Path(__file__).resolve().parents[1]
            / "tmall_extension_receiver"
            / "processor.example.json"
        )
        self.assertTrue(settings.api_submit_enabled)
        self.assertEqual(
            settings.api_url,
            "https://api.yizhujk.com/public/prescriptionUpload/importJson",
        )
        self.assertEqual(settings.api_platform, "TIANMAO")
        self.assertEqual(settings.app_code, "pachong")

    def test_order_products_become_upload_drugs(self):
        drugs = drug_infos_from_order(
            {
                "products": [
                    {
                        "drug_name": "双环醇片 50mg*18片/盒",
                        "drcode": "merchant-1",
                        "quantity": 4,
                    }
                ]
            }
        )
        self.assertEqual(len(drugs), 1)
        self.assertEqual(drugs[0]["drugName"], "双环醇片")
        self.assertEqual(drugs[0]["packingSpec"], "50mg*18片/盒")
        self.assertEqual(drugs[0]["drcode"], "merchant-1")

    def test_missing_platform_fields_use_one_stable_reason(self):
        record = {
            "platform_required_missing_fields": ["audit", "total_fee"],
            "image_status": "downloaded",
            "diagnosis_ocr_status": "success",
            "age_ocr_status": "success",
            "cos_status": "success",
            "processing": [{"drug_index": 0, "status_code": 100, "drcode_status": 1}],
        }
        self.assertEqual(final_review_reasons(record, []), ["platform_required_missing"])

    def test_batch_produces_upload_json_and_submits_enabled_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            store = BatchStore(root / "state.sqlite3", root)
            stored = store.save(
                {
                    "schema_version": 2,
                    "platform": "TIANMAO",
                    "batch_id": "tmall-test-1",
                    "validation_stage": "server_after_ingest",
                    "records": [
                        {
                            "prescription_id": "RX-1",
                            "order_id": "ORDER-1",
                            "patient_name": "测试患者",
                            "patient_gender": "男",
                            "prescription_time": "2026-08-03 12:00:00",
                            "prescription_status": "已使用",
                            "hospital_name": "测试互联网医院",
                            "doctor": "测试医生",
                            "audit": "测试药师",
                            "image_url": "https://example.invalid/rx.jpg",
                            "order": {
                                "order_id": "ORDER-1",
                                "total_fee": "29.00",
                                "products": [
                                    {
                                        "drug_name": "双环醇片 50mg*18片/盒",
                                        "drcode": "merchant-1",
                                        "quantity": 4,
                                    }
                                ],
                            },
                            "orders": [
                                {
                                    "order_id": "ORDER-1",
                                    "total_fee": "29.00",
                                    "products": [
                                        {
                                            "drug_name": "双环醇片 50mg*18片/盒",
                                            "drcode": "merchant-1",
                                            "quantity": 4,
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )
            settings = ProcessorSettings(
                output_dir=root,
                database_path=root / "state.sqlite3",
                drug_catalog_index=root / "catalog.json",
                drug_catalog_enabled=False,
                cos_upload_enabled=True,
                cos_folder_prefix="/files",
                store_id="tmall-test",
                upload_hospital="天津北辰保民医院久远互联网医院",
                request_retries=1,
                request_timeout_seconds=10,
                ocr_min_confidence=0.75,
                ocr_cpu_threads=1,
                api_submit_enabled=True,
                api_url="https://api.invalid/importJson",
                api_platform="TIANMAO",
                app_code="pachong",
                api_timeout_seconds=300,
            )

            def fake_download(_url, image_dir, stem, _retries, _timeout):
                path = image_dir / f"{stem}.jpg"
                path.write_bytes(b"image")
                return path, "hash"

            with mock.patch("tmall_extension_processor.download_image", side_effect=fake_download), mock.patch(
                "tmall_extension_processor.upload_image",
                return_value={
                    "object_key": "files/tianmao/tmall-test/2026-08-03/RX-1.jpg",
                    "image_file_url": "https://cos.invalid/RX-1.jpg",
                },
            ), mock.patch(
                "tmall_extension_processor.submit_json_file",
                return_value=ApiSubmitResult(
                    success=True,
                    http_status=200,
                    err_code=0,
                    message="操作成功",
                    response={"success": True, "errCode": 0, "errMsg": "操作成功"},
                ),
            ):
                summary = process_batch(
                    stored.queue_path,
                    settings,
                    FakeOCR(),
                    None,
                    mock.sentinel.cos_settings,
                    mock.sentinel.cos_client,
                )

            self.assertEqual(summary["upload_records"], 1)
            self.assertEqual(summary["need_review_records"], 0)
            self.assertEqual(summary["api_status"], "success")
            self.assertEqual(summary["api_http_status"], 200)
            self.assertEqual(summary["api_message"], "操作成功")
            self.assertEqual(summary["api_response_file"], "api_response.json")
            self.assertTrue((root / "runs" / "tmall-test-1" / "api_response.json").is_file())
            payload = json.loads(
                (root / "runs" / "tmall-test-1" / "prescription_upload.json").read_text(
                    encoding="utf-8"
                )
            )
            record = payload[0]["data_list"][0]
            self.assertEqual(payload[0]["hospital"], "天津北辰保民医院久远互联网医院")
            self.assertEqual(record["hospital_name"], "测试互联网医院")
            self.assertEqual(record["diagnosis"]["disease_name"], "慢性肝炎")
            self.assertEqual(record["patient_info"]["age"], "41")
            self.assertEqual(record["medicine_info"][0]["duration_days"], 24)
            self.assertEqual(record["medicine_info"][0]["drcode"], "merchant-1")
            self.assertEqual(record["total_fee"], 29.0)

    def test_header_parser_reuses_one_ocr_pass_for_age_and_diagnosis(self):
        result = parse_tmall_header_lines(
            [
                {"text": "年龄：", "score": 0.99, "x": 600, "y": 20, "width": 80, "height": 30},
                {"text": "57岁", "score": 0.98, "x": 700, "y": 21, "width": 60, "height": 30},
                {"text": "临床诊断：心衰", "score": 0.97, "x": 20, "y": 110, "width": 260, "height": 30},
            ]
        )
        self.assertEqual(result["age"], "57")
        self.assertEqual(result["age_ocr_status"], "success")
        self.assertEqual(result["disease_name"], "心衰")


if __name__ == "__main__":
    unittest.main()

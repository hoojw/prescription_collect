import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alihealth_jst_forwarder.signing import signature_matches
from alihealth_local_client.client import (
    QimenAlihealthClient,
    QimenClientError,
    QimenResult,
    service_data,
)
from alihealth_local_client.config import Settings
from alihealth_local_client.fetch import date_range_milliseconds, fetch
from alihealth_local_client.product_index import TmallProductIndex, build_product_index


def settings(output_dir=None):
    return Settings(
        gateway_url="https://example.qimen.alibaba.com/router/qmtest",
        search_method="example.alihealth.prescriptions.search",
        detail_method="example.alihealth.prescription.detail",
        caller_app_key="caller-key",
        caller_app_secret="caller-secret",
        target_app_key="target-key",
        sign_method="md5",
        hospital_id="586522974",
        output_dir=pathlib.Path(output_dir or "/var/lib/prescription-collector/test"),
        product_index_path=pathlib.Path("/var/lib/prescription-collector/test/catalog.json"),
        lookback_days=1,
        max_search_span_days=31,
        page_size=100,
        max_pages=10,
        request_timeout_seconds=40,
        request_retries=0,
    )


class FakeResponse:
    status_code = 200

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class ClientTests(unittest.TestCase):
    def test_signed_json_body_is_sent_to_qimen(self):
        top_payload = {"mock_response": {"service_result": {"success": True, "data": {}}}}
        session = FakeSession(
            FakeResponse(
                {
                    "flag1": "success",
                    "sub_code1": "",
                    "sub_message1": "",
                    "payload1": json.dumps(top_payload),
                }
            )
        )
        client = QimenAlihealthClient(settings(), session=session)
        body = {"hospital_id": "586522974", "rx_no": "2608030897110097"}
        result = client.detail(body)
        self.assertEqual(result.payload, top_payload)
        _, kwargs = session.calls[0]
        params = kwargs["params"]
        raw_body = kwargs["data"]
        self.assertEqual(json.loads(raw_body), body)
        self.assertTrue(signature_matches(params, raw_body, "caller-secret"))
        self.assertEqual(params["method"], settings().detail_method)
        self.assertEqual(params["target_app_key"], "target-key")

    def test_qimen_failure_is_not_treated_as_top_payload(self):
        session = FakeSession(
            FakeResponse(
                {
                    "flag1": "failure",
                    "sub_code1": "forbidden-caller",
                    "sub_message1": "not allowed",
                    "payload1": "",
                }
            )
        )
        client = QimenAlihealthClient(settings(), session=session)
        with self.assertRaises(QimenClientError) as captured:
            client.detail({"hospital_id": "586522974", "rx_no": "1"})
        self.assertEqual(captured.exception.code, "forbidden-caller")

    def test_legacy_unsuffixed_response_is_still_accepted(self):
        top_payload = {"legacy": True}
        session = FakeSession(
            FakeResponse(
                {
                    "flag": "success",
                    "sub_code": "",
                    "sub_message": "",
                    "payload": json.dumps(top_payload),
                }
            )
        )
        client = QimenAlihealthClient(settings(), session=session)
        result = client.detail({"hospital_id": "586522974", "rx_no": "1"})
        self.assertEqual(result.payload, top_payload)

    def test_top_error_response_is_preserved_as_business_error(self):
        with self.assertRaises(QimenClientError) as captured:
            service_data(
                {"error_response": {"code": 11, "sub_code": "isv.permission-api-package-limit"}},
                "detail",
            )
        self.assertEqual(captured.exception.code, "isv.permission-api-package-limit")


class FakeFetchClient:
    def search(self, request_data):
        payload = {
            "alibaba_alihealth_asyncprescribe_prescription_search_response": {
                "service_result": {
                    "success": True,
                    "data": {
                        "rx_nos": ["rx-1", "rx-2", "rx-2"],
                        "page_no": request_data["page_no"],
                        "page_size": request_data["page_size"],
                        "total": 3,
                    },
                }
            }
        }
        return QimenResult(payload=payload, elapsed_ms=5)

    def detail(self, request_data):
        if request_data["rx_no"] == "rx-2":
            raise QimenClientError("mock-detail-error", "mock failure", retryable=True)
        payload = {
            "alibaba_alihealth_asyncprescribe_prescription_detail_response": {
                "service_result": {
                    "success": True,
                    "data": {
                        "rx_no": request_data["rx_no"],
                        "drug_list": {
                            "drug_dto": {
                                "drug_name": "厄贝沙坦片",
                                "spec": "75mg*7片/盒",
                                "total": "2盒",
                            }
                        },
                    },
                }
            }
        }
        return QimenResult(payload=payload, elapsed_ms=7)


class FetchTests(unittest.TestCase):
    @staticmethod
    def product_index():
        payload = build_product_index(
            [
                {
                    "id": "item-1",
                    "title": "[安来]厄贝沙坦片75mg*7片/盒",
                    "price": "8.00",
                    "sku.skuId": "sku-1",
                    "sku.skuPrice": "8.00",
                    "sku.skuOuterId": "merchant-code",
                    "_extra_values": ["套餐类型:1盒装;"],
                    "_source_file": "test.xlsx",
                    "_source_row": 4,
                }
            ],
            expected_products=1,
        )
        return TmallProductIndex(payload["products"])

    def test_inclusive_date_range_uses_shanghai_milliseconds(self):
        start_ms, end_ms = date_range_milliseconds(
            dt.date(2026, 8, 10),
            dt.date(2026, 8, 10),
        )
        timezone = ZoneInfo("Asia/Shanghai")
        self.assertEqual(
            start_ms,
            int(dt.datetime(2026, 8, 10, tzinfo=timezone).timestamp() * 1000),
        )
        self.assertEqual(end_ms - start_ms + 1, 24 * 60 * 60 * 1000)

    def test_fetch_deduplicates_ids_and_isolates_detail_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, summary = fetch(
                settings(temporary),
                start_date=dt.date(2026, 8, 10),
                end_date=dt.date(2026, 8, 10),
                client=FakeFetchClient(),
                product_index=self.product_index(),
            )
            self.assertEqual(summary.rx_ids, 2)
            self.assertEqual(summary.duplicate_rx_ids, 1)
            self.assertEqual(summary.details_ok, 1)
            self.assertEqual(summary.detail_errors, 1)
            self.assertEqual(summary.catalog_success, 1)
            self.assertEqual(summary.catalog_needs_review, 0)
            self.assertEqual(summary.status, "needs_review")
            details = (run_dir / "details.jsonl").read_text(encoding="utf-8").splitlines()
            errors = (run_dir / "detail_errors.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(details), 1)
            self.assertEqual(len(errors), 1)
            processed = (run_dir / "catalog_processed.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(processed), 1)
            record = json.loads(processed[0])
            self.assertEqual(record["catalog"]["total_fee"], 16.0)
            self.assertEqual(record["catalog"]["drugs"][0]["drcode"], "merchant-code")
            self.assertEqual(
                json.loads((run_dir / "need_review.json").read_text(encoding="utf-8")),
                [],
            )
            self.assertTrue((run_dir / "summary.json").is_file())

    def test_unmatched_product_is_written_only_to_need_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, summary = fetch(
                settings(temporary),
                start_date=dt.date(2026, 8, 10),
                end_date=dt.date(2026, 8, 10),
                limit=1,
                client=FakeFetchClient(),
                product_index=TmallProductIndex([]),
            )
            self.assertEqual(summary.catalog_success, 0)
            self.assertEqual(summary.catalog_needs_review, 1)
            self.assertEqual(summary.need_review_records, 1)
            self.assertEqual(summary.status, "needs_review")
            self.assertEqual(
                (run_dir / "catalog_processed.jsonl").read_text(encoding="utf-8"),
                "",
            )
            review = json.loads(
                (run_dir / "need_review.json").read_text(encoding="utf-8")
            )
            self.assertEqual(review[0]["catalog"]["reasons"], ["catalog_not_found"])


if __name__ == "__main__":
    unittest.main()

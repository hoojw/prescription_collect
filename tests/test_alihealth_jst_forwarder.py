import datetime as dt
import json
import os
import pathlib
import sys
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alihealth_jst_forwarder.app import ForwarderApplication
from alihealth_jst_forwarder.config import ConfigurationError, Settings, load_settings
from alihealth_jst_forwarder.signing import sign_request, signature_matches
from alihealth_jst_forwarder.top_client import (
    DETAIL_API,
    SEARCH_API,
    AlihealthTopClient,
    TopRelayResult,
)


class FakeTopClient:
    def __init__(self):
        self.calls = []

    def call(self, kind, request_data):
        self.calls.append((kind, request_data))
        return TopRelayResult(raw_json='{"mock":true}', http_status=200, elapsed_ms=4)


class FakeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"service_result": {"success": True}}


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


def settings():
    return Settings(
        listen_host="127.0.0.1",
        listen_port=8088,
        api_paths=("/", "/qimen"),
        max_body_bytes=1024 * 1024,
        clock_skew_seconds=600,
        search_method="example.alihealth.prescriptions.search",
        detail_method="example.alihealth.prescription.detail",
        allowed_caller_app_keys=frozenset({"caller-key"}),
        qimen_target_app_key="target-key",
        qimen_target_app_secret="target-secret",
        top_app_key="target-key",
        top_app_secret="target-secret",
        top_gateway_url="https://eco.taobao.com/router/rest",
        top_sign_method="hmac",
        top_timeout_seconds=20,
        top_retries=0,
        max_page_size=100,
        max_search_span_milliseconds=31 * 86400 * 1000,
        success_flag="success",
        failure_flag="failure",
    )


def signed_query(method, body, *, caller="caller-key", target="target-key"):
    params = {
        "method": method,
        "app_key": caller,
        "target_app_key": target,
        "timestamp": dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
    }
    params["sign"] = sign_request(params, body, "target-secret", "md5")
    return urlencode(params)


class SigningTests(unittest.TestCase):
    def test_md5_signature_round_trip(self):
        params = {"method": "a.b.c", "app_key": "123", "sign_method": "md5"}
        body = b'{"value":1}'
        params["sign"] = sign_request(params, body, "secret", "md5")
        self.assertTrue(signature_matches(params, body, "secret"))

    def test_body_change_invalidates_signature(self):
        params = {"method": "a.b.c", "app_key": "123", "sign_method": "hmac_md5"}
        params["sign"] = sign_request(params, b"{}", "secret", "hmac_md5")
        self.assertFalse(signature_matches(params, b'{"changed":true}', "secret"))

    def test_extra_signed_parameter_is_included_dynamically(self):
        params = {
            "method": "a.b.c",
            "app_key": "123",
            "sign_method": "md5",
            "qimen_injected_parameter": "value",
        }
        params["sign"] = sign_request(params, b"{}", "secret", "md5")
        self.assertTrue(signature_matches(params, b"{}", "secret"))
        params["qimen_injected_parameter"] = "changed"
        self.assertFalse(signature_matches(params, b"{}", "secret"))


class ConfigurationTests(unittest.TestCase):
    def test_example_placeholders_cannot_start_production_service(self):
        config_path = pathlib.Path(__file__).resolve().parents[1] / (
            "alihealth_jst_forwarder/config.example.json"
        )
        environment = {
            "ALIHEALTH_TOP_APP_KEY": "real-top-key",
            "ALIHEALTH_TOP_APP_SECRET": "real-top-secret",
            "QIMEN_TARGET_APP_KEY": "real-target-key",
            "QIMEN_TARGET_APP_SECRET": "real-target-secret",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(ConfigurationError):
                load_settings(config_path)


class TopClientTests(unittest.TestCase):
    def test_search_uses_fixed_top_method_and_json_business_parameter(self):
        session = FakeSession()
        client = AlihealthTopClient(settings(), session=session)
        request_data = {
            "hospital_id": "586522974",
            "start_time": 1786377600000,
            "end_time": 1786463999000,
            "page_no": 1,
            "page_size": 20,
        }
        result = client.call("search", request_data)
        self.assertEqual(result.http_status, 200)
        _, kwargs = session.calls[0]
        form = kwargs["data"]
        self.assertEqual(form["method"], SEARCH_API)
        self.assertEqual(json.loads(form["search_request"]), request_data)
        self.assertNotIn("detail_request", form)
        self.assertTrue(form["sign"])

    def test_detail_uses_fixed_top_method(self):
        session = FakeSession()
        client = AlihealthTopClient(settings(), session=session)
        client.call("detail", {"hospital_id": "586522974", "rx_no": "1"})
        form = session.calls[0][1]["data"]
        self.assertEqual(form["method"], DETAIL_API)
        self.assertNotIn("search_request", form)


class ForwarderTests(unittest.TestCase):
    def setUp(self):
        self.top = FakeTopClient()
        self.app = ForwarderApplication(settings(), self.top)

    def post(self, method, value, **kwargs):
        body = json.dumps(value, separators=(",", ":")).encode()
        query = signed_query(method, body, **kwargs)
        result = self.app.handle("POST", "/", query, {"content-type": "application/json"}, body)
        return result, json.loads(result.body)

    def test_search_is_forwarded_to_fixed_top_operation(self):
        result, response = self.post(
            settings().search_method,
            {
                "hospital_id": "586522974",
                "start_time": 1786377600000,
                "end_time": 1786463999000,
                "page_no": 1,
                "page_size": 20,
            },
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(response["flag1"], "success")
        self.assertEqual(json.loads(response["payload1"]), {"mock": True})
        self.assertEqual(self.top.calls[0][0], "search")

    def test_detail_is_forwarded(self):
        _, response = self.post(
            settings().detail_method,
            {"hospital_id": "586522974", "rx_no": "2608030897110097"},
        )
        self.assertEqual(response["flag1"], "success")
        self.assertEqual(self.top.calls[0][0], "detail")

    def test_invalid_signature_returns_qimen_failure_shape(self):
        body = b"{}"
        query = signed_query(settings().detail_method, body).replace("sign=", "sign=BAD")
        result = self.app.handle("POST", "/", query, {"content-type": "application/json"}, body)
        response = json.loads(result.body)
        self.assertEqual(response["flag1"], "failure")
        self.assertEqual(response["sub_code1"], "sign-check-failure")
        self.assertEqual(response["sub_message1"], "Illegal request")
        self.assertEqual(self.top.calls, [])

    def test_unknown_caller_is_rejected_before_top_call(self):
        _, response = self.post(
            settings().detail_method,
            {"hospital_id": "586522974", "rx_no": "1"},
            caller="other-key",
        )
        self.assertEqual(response["sub_code1"], "sign-check-failure")
        self.assertEqual(response["sub_message1"], "Illegal request")
        self.assertEqual(self.top.calls, [])

    def test_page_size_is_bounded(self):
        _, response = self.post(
            settings().search_method,
            {
                "hospital_id": "586522974",
                "start_time": 1786377600000,
                "end_time": 1786463999000,
                "page_no": 1,
                "page_size": 101,
            },
        )
        self.assertEqual(response["sub_code1"], "invalid-request")

    def test_seconds_timestamp_is_rejected_instead_of_silently_forwarded(self):
        _, response = self.post(
            settings().search_method,
            {
                "hospital_id": "586522974",
                "start_time": 1786377600,
                "end_time": 1786463999,
                "page_no": 1,
                "page_size": 20,
            },
        )
        self.assertEqual(response["sub_code1"], "invalid-request")
        self.assertEqual(self.top.calls, [])

    def test_health_check_does_not_require_signature(self):
        result = self.app.handle("GET", "/healthz", "", {}, b"")
        self.assertEqual(result.status, 200)
        self.assertEqual(json.loads(result.body), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()

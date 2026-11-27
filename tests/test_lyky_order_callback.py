import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lyky_order_callback.app import OrderCallbackApplication
from lyky_order_callback.config import ConfigurationError, Settings, load_settings
from lyky_order_callback.signing import sign_form, signature_matches
from lyky_order_callback.store import OrderStore


def settings(database_path: pathlib.Path) -> Settings:
    return Settings(
        listen_host="127.0.0.1",
        listen_port=8092,
        callback_path="/callbacks/lyky/orders/original",
        database_path=database_path,
        app_id="demo-app-id",
        app_secret="demo-app-secret",
        timestamp_tolerance_seconds=600,
        max_body_bytes=1024 * 1024,
    )


def order_body(**updates):
    body = {
        "id": 15,
        "merchant_id": 17,
        "store_id": 16,
        "store_name": "测试店铺",
        "platform": 5,
        "out_trade_no": "26946281246719954",
        "is_recipe": 0,
        "products": [],
    }
    body.update(updates)
    return body


def signed_form(body, *, timestamp=None, signature="auto"):
    form = {
        "app_id": "demo-app-id",
        "timestamp": str(timestamp or int(time.time())),
        "version": "v1",
        "body": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    }
    form["signature"] = sign_form(form, "demo-app-secret") if signature == "auto" else signature
    return urlencode(form).encode("utf-8")


class SigningTests(unittest.TestCase):
    def test_documented_signature_vector(self):
        form = {
            "app_id": "dcc8ce40b7f76e21fcbeefe63497f690f",
            "timestamp": "1634124321",
            "version": "v1",
            "signature": "",
            "body": "{}",
        }
        self.assertEqual(
            sign_form(form, "sdasklhdah2342jk4234h23kjsdas"),
            "ZjczYTA2ODcxMTJkYTEyOGMwOTM3Y2Y3NDJiZWU0ZWI=",
        )

    def test_signature_includes_added_system_parameter(self):
        form = {"app_id": "a", "timestamp": "1", "version": "v1", "body": "{}"}
        form["signature"] = sign_form(form, "secret")
        self.assertTrue(signature_matches(form, "secret"))
        form["nonce"] = "changed"
        self.assertFalse(signature_matches(form, "secret"))


class ConfigurationTests(unittest.TestCase):
    def test_secret_is_loaded_from_environment_not_json(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = pathlib.Path(temp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "app_id": "real-app-id",
                        "app_secret_env": "LYKY_CALLBACK_TEST_SECRET",
                        "database_path": str(pathlib.Path(temp) / "orders.sqlite3"),
                    }
                ),
                encoding="utf-8",
            )
            previous = os.environ.get("LYKY_CALLBACK_TEST_SECRET")
            os.environ["LYKY_CALLBACK_TEST_SECRET"] = "real-secret"
            try:
                loaded = load_settings(config_path)
            finally:
                if previous is None:
                    os.environ.pop("LYKY_CALLBACK_TEST_SECRET", None)
                else:
                    os.environ["LYKY_CALLBACK_TEST_SECRET"] = previous
        self.assertEqual(loaded.app_id, "real-app-id")
        self.assertEqual(loaded.app_secret, "real-secret")

    def test_example_configuration_cannot_be_started(self):
        config_path = pathlib.Path(__file__).resolve().parents[1] / (
            "lyky_order_callback/config.example.json"
        )
        with self.assertRaises(ConfigurationError):
            load_settings(config_path)


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings(pathlib.Path(self.temp.name) / "orders.sqlite3")
        self.app = OrderCallbackApplication(self.settings, OrderStore(self.settings.database_path))

    def tearDown(self):
        self.temp.cleanup()

    def post(self, body, **kwargs):
        return self.app.handle(
            "POST",
            self.settings.callback_path,
            {"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
            signed_form(body, **kwargs),
        )

    def event_count(self):
        import sqlite3

        connection = sqlite3.connect(self.settings.database_path)
        try:
            return connection.execute("select count(*) from original_order_events").fetchone()[0]
        finally:
            connection.close()

    def test_valid_delivery_is_stored_before_acknowledgement(self):
        result = self.post(order_body())
        self.assertEqual(result.status, 200)
        self.assertEqual(json.loads(result.body), {"data": "ok"})
        self.assertEqual(self.event_count(), 1)

    def test_repeated_delivery_is_idempotent(self):
        payload = order_body()
        self.assertEqual(self.post(payload).status, 200)
        self.assertEqual(self.post(payload).status, 200)
        self.assertEqual(self.event_count(), 1)

    def test_changed_order_payload_is_retained_as_new_event(self):
        self.assertEqual(self.post(order_body(status=1)).status, 200)
        self.assertEqual(self.post(order_body(status=2, updated_at="1700000000")).status, 200)
        self.assertEqual(self.event_count(), 2)

    def test_invalid_signature_is_rejected_without_persisting(self):
        result = self.post(order_body(), signature="not-valid")
        self.assertEqual(result.status, 401)
        self.assertEqual(json.loads(result.body)["code"], "invalid_signature")
        self.assertEqual(self.event_count(), 0)

    def test_stale_timestamp_is_rejected_without_persisting(self):
        result = self.post(order_body(), timestamp=int(time.time()) - 1000)
        self.assertEqual(result.status, 401)
        self.assertEqual(json.loads(result.body)["code"], "expired_request")
        self.assertEqual(self.event_count(), 0)

    def test_wrong_content_type_is_rejected(self):
        result = self.app.handle(
            "POST", self.settings.callback_path, {"content-type": "application/json"}, b"{}"
        )
        self.assertEqual(result.status, 415)

    def test_missing_order_identity_is_rejected(self):
        result = self.post(order_body(out_trade_no=""))
        self.assertEqual(result.status, 400)
        self.assertEqual(json.loads(result.body)["code"], "invalid_body")


if __name__ == "__main__":
    unittest.main()

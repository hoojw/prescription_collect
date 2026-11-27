"""HTTP-neutral request validation and acknowledgement for original-order pushes."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from .config import Settings
from .signing import signature_matches
from .store import OrderStore


LOGGER = logging.getLogger("lyky_order_callback")


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


class RequestError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class OrderCallbackApplication:
    def __init__(self, settings: Settings, store: OrderStore | None = None) -> None:
        self.settings = settings
        self.store = store or OrderStore(settings.database_path)

    @staticmethod
    def _json_result(payload: dict[str, Any], status: int = 200) -> HttpResult:
        return HttpResult(
            status=status,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def _parse_form(self, raw_body: bytes) -> dict[str, str]:
        try:
            text = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RequestError("invalid_encoding", "request body must be UTF-8") from exc
        try:
            pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise RequestError("invalid_form", "malformed form body") from exc
        result: dict[str, str] = {}
        for key, value in pairs:
            if key in result:
                raise RequestError("duplicate_parameter", f"duplicate parameter: {key}")
            result[key] = value
        return result

    def _verify_timestamp(self, value: str) -> None:
        if not value.isdigit() or len(value) != 10:
            raise RequestError("invalid_timestamp", "timestamp must be a 10-digit Unix second value")
        supplied = int(value)
        if abs(int(time.time()) - supplied) > self.settings.timestamp_tolerance_seconds:
            raise RequestError("expired_request", "timestamp is outside the permitted window", 401)

    @staticmethod
    def _required_text(body: dict[str, Any], name: str) -> str:
        value = str(body.get(name) or "").strip()
        if not value:
            raise RequestError("invalid_body", f"body.{name} is required")
        return value

    def _parse_order(self, form: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
        if form.get("app_id") != self.settings.app_id:
            raise RequestError("unknown_app", "app_id is not accepted", 401)
        if not signature_matches(form, self.settings.app_secret):
            raise RequestError("invalid_signature", "signature verification failed", 401)
        self._verify_timestamp(form.get("timestamp", ""))
        if form.get("version") != "v1":
            raise RequestError("unsupported_version", "only version v1 is accepted")
        body_text = form.get("body", "")
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RequestError("invalid_body", "body must be JSON") from exc
        if not isinstance(body, dict):
            raise RequestError("invalid_body", "body must be a JSON object")

        self._required_text(body, "id")
        self._required_text(body, "out_trade_no")
        self._required_text(body, "store_id")
        products = body.get("products")
        if products is not None and not isinstance(products, list):
            raise RequestError("invalid_body", "body.products must be an array when provided")

        documented_fields = (
            "merchant_id", "store_name", "platform", "is_recipe", "prescription_url",
            "consignee", "mobile", "province", "city", "area", "address", "pay_price",
            "goods_price", "discount_price", "refund_status", "status", "create_time",
            "pay_time", "created_at", "updated_at", "products",
        )
        warnings = [f"missing:{name}" for name in documented_fields if name not in body]
        return body, warnings

    @staticmethod
    def _order_log_key(body: dict[str, Any]) -> str:
        return hashlib.sha256(
            f"{body.get('id')}:{body.get('out_trade_no')}".encode("utf-8")
        ).hexdigest()[:12]

    def handle(
        self,
        http_method: str,
        path: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> HttpResult:
        if path == "/healthz":
            if http_method != "GET":
                return self._json_result({"status": "method_not_allowed"}, 405)
            return self._json_result({"status": "ok"})
        if path != self.settings.callback_path:
            return self._json_result({"status": "not_found"}, 404)
        if http_method != "POST":
            return self._json_result({"data": "error", "code": "method_not_allowed"}, 405)
        if len(raw_body) > self.settings.max_body_bytes:
            return self._json_result({"data": "error", "code": "request_too_large"}, 413)
        if "application/x-www-form-urlencoded" not in headers.get("content-type", "").lower():
            return self._json_result({"data": "error", "code": "unsupported_content_type"}, 415)

        started = time.monotonic()
        try:
            form = self._parse_form(raw_body)
            body, warnings = self._parse_order(form)
            stored = self.store.save(self.settings.app_id, body, warnings)
            LOGGER.info(
                "outcome=accepted event_id=%s duplicate=%s order_key=%s elapsed_ms=%s",
                stored.event_id,
                stored.duplicate,
                self._order_log_key(body),
                int((time.monotonic() - started) * 1000),
            )
            # The provider documents this exact response as the success acknowledgement.
            return self._json_result({"data": "ok"})
        except RequestError as exc:
            LOGGER.warning(
                "outcome=rejected code=%s elapsed_ms=%s",
                exc.code,
                int((time.monotonic() - started) * 1000),
            )
            return self._json_result({"data": "error", "code": exc.code}, exc.status)
        except Exception:
            LOGGER.exception("outcome=storage_error")
            return self._json_result({"data": "error", "code": "storage_error"}, 500)

"""HTTP-neutral application logic for the Qimen-facing relay."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from .config import Settings
from .signing import SignatureError, signature_matches
from .top_client import AlihealthTopClient, TopRelayError


LOGGER = logging.getLogger("alihealth_jst_forwarder")


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


class RequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ForwarderApplication:
    def __init__(self, settings: Settings, top_client: AlihealthTopClient | None = None) -> None:
        self.settings = settings
        self.top_client = top_client or AlihealthTopClient(settings)

    def _json_result(self, payload: dict[str, Any], status: int = 200) -> HttpResult:
        return HttpResult(
            status=status,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def _success(self, raw_payload: str) -> HttpResult:
        return self._json_result(
            {
                "flag1": self.settings.success_flag,
                "sub_code1": "",
                "sub_message1": "",
                "payload1": raw_payload,
            }
        )

    def _failure(self, code: str, message: str) -> HttpResult:
        return self._json_result(
            {
                "flag1": self.settings.failure_flag,
                "sub_code1": code,
                "sub_message1": message,
                "payload1": "",
            }
        )

    @staticmethod
    def _query_params(query_string: str) -> dict[str, str]:
        pairs = parse_qsl(query_string, keep_blank_values=True, encoding="utf-8", errors="strict")
        params: dict[str, str] = {}
        for key, value in pairs:
            if key in params:
                raise RequestError("invalid-request", f"duplicate query parameter: {key}")
            params[key] = value
        return params

    def _verify_timestamp(self, value: str) -> None:
        try:
            supplied = dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            )
        except ValueError as exc:
            raise RequestError("invalid-timestamp", "timestamp must be yyyy-MM-dd HH:mm:ss") from exc
        delta = abs((dt.datetime.now(ZoneInfo("Asia/Shanghai")) - supplied).total_seconds())
        if delta > self.settings.clock_skew_seconds:
            raise RequestError("expired-request", "request timestamp is outside the allowed window")

    def _verify_qimen(self, params: dict[str, str], raw_body: bytes) -> None:
        caller = params.get("app_key", "")
        if caller not in self.settings.allowed_caller_app_keys:
            raise RequestError("sign-check-failure", "Illegal request")

        target = (
            params.get("target_app_key")
            or params.get("targetAppkey")
            or params.get("targetAppKey")
            or ""
        )
        if target != self.settings.qimen_target_app_key:
            raise RequestError("sign-check-failure", "Illegal request")
        self._verify_timestamp(params.get("timestamp", ""))
        try:
            valid = signature_matches(params, raw_body, self.settings.qimen_target_app_secret)
        except SignatureError as exc:
            raise RequestError("sign-check-failure", "Illegal request") from exc
        if not valid:
            raise RequestError("sign-check-failure", "Illegal request")

    def _parse_body(self, raw_body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError("invalid-json", "request body must be UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise RequestError("invalid-json", "request body must be a JSON object")
        return value

    @staticmethod
    def _nonempty_text(value: Any, name: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise RequestError("invalid-request", f"{name} is required")
        return text

    @staticmethod
    def _integer(value: Any, name: str, minimum: int = 0) -> int:
        if isinstance(value, bool):
            raise RequestError("invalid-request", f"{name} must be an integer")
        if isinstance(value, float) and not value.is_integer():
            raise RequestError("invalid-request", f"{name} must be an integer")
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise RequestError("invalid-request", f"{name} must be an integer") from exc
        if result < minimum:
            raise RequestError("invalid-request", f"{name} must be >= {minimum}")
        return result

    def _epoch_milliseconds(self, value: Any, name: str) -> int:
        result = self._integer(value, name, 1)
        if result < 1_000_000_000_000 or result > 9_999_999_999_999:
            raise RequestError(
                "invalid-request",
                f"{name} must be a 13-digit Unix timestamp in milliseconds",
            )
        return result

    def _search_request(self, body: dict[str, Any]) -> dict[str, Any]:
        data = body.get("search_request", body)
        if not isinstance(data, dict):
            raise RequestError("invalid-request", "search_request must be an object")
        allowed = {"hospital_id", "start_time", "end_time", "page_no", "page_size"}
        extras = sorted(set(data) - allowed)
        if extras:
            raise RequestError("invalid-request", "unexpected fields: " + ", ".join(extras))
        start = self._epoch_milliseconds(data.get("start_time"), "start_time")
        end = self._epoch_milliseconds(data.get("end_time"), "end_time")
        if end < start:
            raise RequestError("invalid-request", "end_time must be >= start_time")
        if end - start > self.settings.max_search_span_milliseconds:
            raise RequestError("invalid-request", "requested search range is too large")
        page_size = self._integer(data.get("page_size", 100), "page_size", 1)
        if page_size > self.settings.max_page_size:
            raise RequestError("invalid-request", "page_size exceeds configured maximum")
        return {
            "hospital_id": self._nonempty_text(data.get("hospital_id"), "hospital_id"),
            "start_time": start,
            "end_time": end,
            "page_no": self._integer(data.get("page_no", 1), "page_no", 1),
            "page_size": page_size,
        }

    def _detail_request(self, body: dict[str, Any]) -> dict[str, Any]:
        data = body.get("detail_request", body)
        if not isinstance(data, dict):
            raise RequestError("invalid-request", "detail_request must be an object")
        allowed = {"hospital_id", "rx_no"}
        extras = sorted(set(data) - allowed)
        if extras:
            raise RequestError("invalid-request", "unexpected fields: " + ", ".join(extras))
        return {
            "hospital_id": self._nonempty_text(data.get("hospital_id"), "hospital_id"),
            "rx_no": self._nonempty_text(data.get("rx_no"), "rx_no"),
        }

    def handle(
        self,
        http_method: str,
        path: str,
        query_string: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> HttpResult:
        if path == "/healthz":
            if http_method != "GET":
                return self._json_result({"status": "method_not_allowed"}, status=405)
            return self._json_result({"status": "ok"})

        if path not in self.settings.api_paths:
            return self._json_result({"status": "not_found"}, status=404)
        if http_method != "POST":
            return self._failure("method-not-allowed", "POST is required")
        if len(raw_body) > self.settings.max_body_bytes:
            return self._failure("request-too-large", "request body exceeds configured maximum")
        content_type = headers.get("content-type", "").lower()
        if "application/json" not in content_type:
            return self._failure("unsupported-content-type", "application/json is required")

        relay_id = uuid.uuid4().hex[:16]
        started = time.monotonic()
        method_name = ""
        caller = ""
        try:
            params = self._query_params(query_string)
            method_name = params.get("method", "")
            caller = params.get("app_key", "")
            self._verify_qimen(params, raw_body)
            body = self._parse_body(raw_body)
            if method_name == self.settings.search_method:
                kind = "search"
                request_data = self._search_request(body)
            elif method_name == self.settings.detail_method:
                kind = "detail"
                request_data = self._detail_request(body)
            else:
                raise RequestError("unsupported-api", "API method is not enabled by this service")
            result = self.top_client.call(kind, request_data)
            LOGGER.info(
                "relay_id=%s method=%s caller=%s outcome=success upstream_ms=%s total_ms=%s",
                relay_id,
                method_name,
                caller,
                result.elapsed_ms,
                int((time.monotonic() - started) * 1000),
            )
            return self._success(result.raw_json)
        except RequestError as exc:
            LOGGER.warning(
                "relay_id=%s method=%s caller=%s outcome=rejected code=%s total_ms=%s",
                relay_id,
                method_name,
                caller,
                exc.code,
                int((time.monotonic() - started) * 1000),
            )
            return self._failure(exc.code, str(exc))
        except TopRelayError as exc:
            LOGGER.error(
                "relay_id=%s method=%s caller=%s outcome=upstream_error code=%s total_ms=%s",
                relay_id,
                method_name,
                caller,
                exc.code,
                int((time.monotonic() - started) * 1000),
            )
            return self._failure(exc.code, str(exc))
        except Exception:
            LOGGER.exception("relay_id=%s outcome=internal_error", relay_id)
            return self._failure("internal-error", "internal relay error")

"""Signed Qimen client limited to Alihealth prescription search and detail."""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import requests

from alihealth_jst_forwarder.signing import sign_request

from .config import Settings


class QimenClientError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class QimenResult:
    payload: dict[str, Any]
    elapsed_ms: int


def _response_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QimenClientError("invalid-qimen-response", "Qimen response must be an object")
    if "flag1" in value or "flag" in value:
        return value
    candidates = [
        item
        for item in value.values()
        if isinstance(item, dict) and ("flag1" in item or "flag" in item)
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise QimenClientError("invalid-qimen-response", "Qimen response envelope was not found")


class QimenAlihealthClient:
    def __init__(self, settings: Settings, session: Any | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()

    @staticmethod
    def _timestamp() -> str:
        return dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    def _call(self, method: str, body: dict[str, Any]) -> QimenResult:
        if method not in {self.settings.search_method, self.settings.detail_method}:
            raise QimenClientError("unsupported-api", "method is not in the local allowlist")
        raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        params: dict[str, str] = {
            "method": method,
            "app_key": self.settings.caller_app_key,
            "target_app_key": self.settings.target_app_key,
            "timestamp": self._timestamp(),
            "format": "json",
            "v": "2.0",
            "sign_method": self.settings.sign_method,
        }
        params["sign"] = sign_request(
            params,
            raw_body,
            self.settings.caller_app_secret,
            self.settings.sign_method,
        )

        attempts = self.settings.request_retries + 1
        started = time.monotonic()
        for attempt in range(attempts):
            try:
                response = self.session.post(
                    self.settings.gateway_url,
                    params=params,
                    data=raw_body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json;charset=utf-8",
                        "User-Agent": "prescription-collector-qimen/0.1",
                    },
                    timeout=(5, self.settings.request_timeout_seconds),
                )
            except requests.Timeout as exc:
                if attempt + 1 < attempts:
                    continue
                raise QimenClientError(
                    "qimen-timeout",
                    "Qimen request timed out",
                    retryable=True,
                ) from exc
            except requests.RequestException as exc:
                if attempt + 1 < attempts:
                    continue
                raise QimenClientError(
                    "qimen-network-error",
                    "Qimen request failed",
                    retryable=True,
                ) from exc

            if not 200 <= int(response.status_code) < 300:
                if int(response.status_code) >= 500 and attempt + 1 < attempts:
                    continue
                raise QimenClientError(
                    "qimen-http-error",
                    f"Qimen returned HTTP {response.status_code}",
                    retryable=int(response.status_code) >= 500,
                )
            try:
                decoded = response.json()
            except ValueError as exc:
                raise QimenClientError(
                    "invalid-qimen-response",
                    "Qimen returned invalid JSON",
                ) from exc
            envelope = _response_envelope(decoded)
            flag = envelope.get("flag1", envelope.get("flag"))
            if str(flag or "").strip().lower() != "success":
                code = str(
                    envelope.get("sub_code1")
                    or envelope.get("sub_code")
                    or "qimen-call-failed"
                ).strip()
                message = str(
                    envelope.get("sub_message1")
                    or envelope.get("sub_message")
                    or "Qimen call failed"
                ).strip()
                raise QimenClientError(code, message)
            payload = envelope.get("payload1", envelope.get("payload"))
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise QimenClientError(
                        "invalid-forwarder-payload",
                        "forwarder payload is not JSON",
                    ) from exc
            if not isinstance(payload, dict):
                raise QimenClientError(
                    "invalid-forwarder-payload",
                    "forwarder payload must be a JSON object",
                )
            return QimenResult(
                payload=payload,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        raise QimenClientError("qimen-network-error", "Qimen request failed")

    def search(self, request_data: dict[str, Any]) -> QimenResult:
        return self._call(self.settings.search_method, request_data)

    def detail(self, request_data: dict[str, Any]) -> QimenResult:
        return self._call(self.settings.detail_method, request_data)


def service_data(payload: dict[str, Any], operation: str) -> dict[str, Any]:
    """Extract service_result.data while preserving TOP business failures."""
    error_response = payload.get("error_response")
    if isinstance(error_response, dict):
        code = str(error_response.get("sub_code") or error_response.get("code") or "top-error")
        message = str(error_response.get("sub_msg") or error_response.get("msg") or "TOP error")
        raise QimenClientError(code, message)

    response_key = (
        "alibaba_alihealth_asyncprescribe_prescription_"
        f"{operation}_response"
    )
    response_root = payload.get(response_key)
    if not isinstance(response_root, dict):
        response_root = payload
    service_result = response_root.get("service_result")
    if not isinstance(service_result, dict):
        raise QimenClientError(
            "invalid-top-response",
            f"{operation} service_result was not found",
        )
    success = service_result.get("success")
    success_text = str(success).strip().lower()
    if success is not True and success != 1 and success_text not in {"true", "1"}:
        code = str(service_result.get("err_code") or "top-business-error")
        message = str(service_result.get("err_message") or "Alihealth request failed")
        raise QimenClientError(code, message)
    data = service_result.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise QimenClientError("invalid-top-response", "service_result.data is not JSON") from exc
    if not isinstance(data, dict):
        raise QimenClientError("invalid-top-response", "service_result.data must be an object")
    return data

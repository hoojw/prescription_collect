"""Minimal TOP client restricted to the two Alihealth prescription APIs."""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import Settings
from .signing import sign_request


SEARCH_API = "alibaba.alihealth.asyncprescribe.prescription.search"
DETAIL_API = "alibaba.alihealth.asyncprescribe.prescription.detail"


@dataclass(frozen=True)
class TopRelayResult:
    raw_json: str
    http_status: int
    elapsed_ms: int


class TopRelayError(RuntimeError):
    def __init__(self, code: str, message: str, http_status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = int(http_status)


class AlihealthTopClient:
    def __init__(self, settings: Settings, session: Any | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()

    @staticmethod
    def _timestamp() -> str:
        return dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    def call(self, kind: str, request_data: dict[str, Any]) -> TopRelayResult:
        if kind == "search":
            api_method = SEARCH_API
            business_name = "search_request"
        elif kind == "detail":
            api_method = DETAIL_API
            business_name = "detail_request"
        else:
            raise TopRelayError("unsupported-api", f"unsupported relay kind: {kind}")

        params: dict[str, str] = {
            "method": api_method,
            "app_key": self.settings.top_app_key,
            "timestamp": self._timestamp(),
            "v": "2.0",
            "format": "json",
            "sign_method": self.settings.top_sign_method,
            business_name: json.dumps(
                request_data,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        params["sign"] = sign_request(
            params=params,
            body=b"",
            secret=self.settings.top_app_secret,
            sign_method=self.settings.top_sign_method,
        )

        attempts = self.settings.top_retries + 1
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self.session.post(
                    self.settings.top_gateway_url,
                    data=params,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                        "User-Agent": "alihealth-jst-forwarder/0.1",
                    },
                    timeout=(3, self.settings.top_timeout_seconds),
                )
            except requests.Timeout as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                raise TopRelayError("upstream-timeout", "Alihealth TOP request timed out") from exc
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                raise TopRelayError("upstream-network-error", "Alihealth TOP request failed") from exc

            elapsed_ms = int((time.monotonic() - started) * 1000)
            if not 200 <= int(response.status_code) < 300:
                raise TopRelayError(
                    "upstream-http-error",
                    f"Alihealth TOP returned HTTP {response.status_code}",
                    response.status_code,
                )
            try:
                parsed = response.json()
            except ValueError as exc:
                raise TopRelayError("upstream-invalid-json", "Alihealth TOP returned invalid JSON") from exc
            raw = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
            return TopRelayResult(raw_json=raw, http_status=int(response.status_code), elapsed_ms=elapsed_ms)

        raise TopRelayError("upstream-network-error", str(last_error or "unknown upstream error"))

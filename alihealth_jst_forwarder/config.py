"""Configuration loading for the Jushita relay."""

from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when deployment configuration is incomplete or unsafe."""


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().upper()
    return normalized.startswith("REPLACE_") or normalized.startswith("REPLACECOMPANY.")


def _positive_int(value: Any, name: str, minimum: int = 1) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if result < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}")
    return result


@dataclass(frozen=True)
class Settings:
    listen_host: str
    listen_port: int
    api_paths: tuple[str, ...]
    max_body_bytes: int
    clock_skew_seconds: int
    search_method: str
    detail_method: str
    allowed_caller_app_keys: frozenset[str]
    qimen_target_app_key: str
    qimen_target_app_secret: str
    top_app_key: str
    top_app_secret: str
    top_gateway_url: str
    top_sign_method: str
    top_timeout_seconds: int
    top_retries: int
    max_page_size: int
    max_search_span_milliseconds: int
    success_flag: str
    failure_flag: str


def load_settings(path: str | pathlib.Path) -> Settings:
    config_path = pathlib.Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"invalid JSON config: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("config root must be an object")

    methods = data.get("qimen_methods") or {}
    if not isinstance(methods, dict):
        raise ConfigurationError("qimen_methods must be an object")
    search_method = str(methods.get("search") or "").strip()
    detail_method = str(methods.get("detail") or "").strip()
    if not search_method or not detail_method or search_method == detail_method:
        raise ConfigurationError("two different qimen_methods are required")
    method_pattern = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+){2,}")
    if any(
        method_pattern.fullmatch(value) is None or _is_placeholder(value)
        for value in (search_method, detail_method)
    ):
        raise ConfigurationError(
            "qimen_methods must be final lowercase API names with at least three words"
        )

    callers = data.get("allowed_caller_app_keys") or []
    if not isinstance(callers, list):
        raise ConfigurationError("allowed_caller_app_keys must be a list")
    allowed_callers = frozenset(str(value).strip() for value in callers if str(value).strip())
    if not allowed_callers or any(_is_placeholder(value) for value in allowed_callers):
        raise ConfigurationError("allowed_caller_app_keys must contain real AppKeys")

    paths = data.get("api_paths") or ["/", "/qimen"]
    if not isinstance(paths, list) or not paths:
        raise ConfigurationError("api_paths must be a non-empty list")
    api_paths = tuple(str(value).strip() for value in paths if str(value).startswith("/"))
    if not api_paths:
        raise ConfigurationError("api_paths must contain absolute paths")

    top_app_key = os.environ.get("ALIHEALTH_TOP_APP_KEY", "").strip()
    top_app_secret = os.environ.get("ALIHEALTH_TOP_APP_SECRET", "").strip()
    target_key = os.environ.get("QIMEN_TARGET_APP_KEY", top_app_key).strip()
    target_secret = os.environ.get("QIMEN_TARGET_APP_SECRET", top_app_secret).strip()
    missing = [
        name
        for name, value in (
            ("ALIHEALTH_TOP_APP_KEY", top_app_key),
            ("ALIHEALTH_TOP_APP_SECRET", top_app_secret),
            ("QIMEN_TARGET_APP_KEY", target_key),
            ("QIMEN_TARGET_APP_SECRET", target_secret),
        )
        if not value or _is_placeholder(value)
    ]
    if missing:
        raise ConfigurationError("missing secret environment values: " + ", ".join(missing))

    top_timeout = _positive_int(data.get("top_timeout_seconds", 20), "top_timeout_seconds")
    if top_timeout >= 30:
        raise ConfigurationError("top_timeout_seconds must be below Qimen's 30-second limit")
    top_gateway_url = str(
        data.get("top_gateway_url") or "https://eco.taobao.com/router/rest"
    ).strip()
    if urlsplit(top_gateway_url).scheme.lower() != "https":
        raise ConfigurationError("top_gateway_url must use HTTPS")
    top_sign_method = str(data.get("top_sign_method") or "hmac").strip().lower()
    if top_sign_method.replace("_", "-") not in {
        "md5",
        "hmac",
        "hmac-md5",
        "hmac-sha256",
    }:
        raise ConfigurationError("unsupported top_sign_method")
    listen_port = _positive_int(data.get("listen_port", 8088), "listen_port")
    if listen_port > 65535:
        raise ConfigurationError("listen_port must be <= 65535")
    top_retries = _positive_int(data.get("top_retries", 0), "top_retries", minimum=0)
    if top_retries > 1:
        raise ConfigurationError("top_retries must be 0 or 1")

    return Settings(
        listen_host=str(data.get("listen_host") or "127.0.0.1"),
        listen_port=listen_port,
        api_paths=api_paths,
        max_body_bytes=_positive_int(data.get("max_body_bytes", 1048576), "max_body_bytes"),
        clock_skew_seconds=_positive_int(data.get("clock_skew_seconds", 600), "clock_skew_seconds"),
        search_method=search_method,
        detail_method=detail_method,
        allowed_caller_app_keys=allowed_callers,
        qimen_target_app_key=target_key,
        qimen_target_app_secret=target_secret,
        top_app_key=top_app_key,
        top_app_secret=top_app_secret,
        top_gateway_url=top_gateway_url,
        top_sign_method=top_sign_method,
        top_timeout_seconds=top_timeout,
        top_retries=top_retries,
        max_page_size=_positive_int(data.get("max_page_size", 100), "max_page_size"),
        max_search_span_milliseconds=_positive_int(
            data.get("max_search_span_milliseconds", 2678400000),
            "max_search_span_milliseconds",
        ),
        success_flag=str(data.get("success_flag") or "success"),
        failure_flag=str(data.get("failure_flag") or "failure"),
    )

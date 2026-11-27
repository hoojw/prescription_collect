"""Configuration for the outside-Jushita Qimen caller."""

from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when a caller configuration is incomplete or unsafe."""


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if result < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}")
    return result


def _placeholder(value: str) -> bool:
    normalized = value.strip().upper()
    return normalized.startswith("REPLACE_") or normalized.startswith("REPLACECOMPANY.")


@dataclass(frozen=True)
class Settings:
    gateway_url: str
    search_method: str
    detail_method: str
    caller_app_key: str
    caller_app_secret: str
    target_app_key: str
    sign_method: str
    hospital_id: str
    output_dir: pathlib.Path
    product_index_path: pathlib.Path
    lookback_days: int
    max_search_span_days: int
    page_size: int
    max_pages: int
    request_timeout_seconds: int
    request_retries: int


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

    gateway_url = str(data.get("gateway_url") or "").strip()
    parsed_gateway = urlsplit(gateway_url)
    if (
        parsed_gateway.scheme.lower() != "https"
        or not parsed_gateway.netloc
        or parsed_gateway.path not in {"/router/qm", "/router/qmtest"}
    ):
        raise ConfigurationError(
            "gateway_url must be the Qimen HTTPS /router/qm or /router/qmtest endpoint"
        )
    if _placeholder(parsed_gateway.netloc):
        raise ConfigurationError("gateway_url placeholder must be replaced")

    methods = data.get("qimen_methods") or {}
    if not isinstance(methods, dict):
        raise ConfigurationError("qimen_methods must be an object")
    search_method = str(methods.get("search") or "").strip()
    detail_method = str(methods.get("detail") or "").strip()
    method_pattern = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+){2,}")
    if (
        search_method == detail_method
        or any(
            not value
            or method_pattern.fullmatch(value) is None
            or _placeholder(value)
            for value in (search_method, detail_method)
        )
    ):
        raise ConfigurationError("two final lowercase Qimen API names are required")

    caller_app_key = os.environ.get("QIMEN_CALLER_APP_KEY", "").strip()
    caller_app_secret = os.environ.get("QIMEN_CALLER_APP_SECRET", "").strip()
    if (
        not caller_app_key
        or not caller_app_secret
        or _placeholder(caller_app_key)
        or _placeholder(caller_app_secret)
    ):
        raise ConfigurationError(
            "QIMEN_CALLER_APP_KEY and QIMEN_CALLER_APP_SECRET must contain real values"
        )

    target_app_key = str(data.get("target_app_key") or "").strip()
    if not target_app_key or _placeholder(target_app_key):
        raise ConfigurationError("target_app_key must contain the Jushita target AppKey")

    sign_method = str(data.get("sign_method") or "md5").strip().lower()
    if sign_method.replace("_", "-") not in {
        "md5",
        "hmac",
        "hmac-md5",
        "hmac-sha256",
    }:
        raise ConfigurationError("unsupported sign_method")

    hospital_id = str(data.get("hospital_id") or "").strip()
    if not hospital_id or _placeholder(hospital_id):
        raise ConfigurationError("hospital_id must be configured")

    output_dir = pathlib.Path(str(data.get("output_dir") or "")).expanduser()
    if not output_dir.is_absolute():
        raise ConfigurationError("output_dir must be an absolute path")

    product_index_path = pathlib.Path(
        str(data.get("product_index_path") or "")
    ).expanduser()
    if not product_index_path.is_absolute():
        raise ConfigurationError("product_index_path must be an absolute path")

    page_size = _integer(data.get("page_size", 100), "page_size", 1)
    if page_size > 100:
        raise ConfigurationError("page_size must be <= 100")
    request_retries = _integer(data.get("request_retries", 2), "request_retries", 0)
    if request_retries > 3:
        raise ConfigurationError("request_retries must be <= 3")

    lookback_days = _integer(data.get("lookback_days", 1), "lookback_days", 1)
    max_search_span_days = _integer(
        data.get("max_search_span_days", 31),
        "max_search_span_days",
        1,
    )
    if lookback_days > max_search_span_days:
        raise ConfigurationError("lookback_days must be <= max_search_span_days")

    return Settings(
        gateway_url=gateway_url,
        search_method=search_method,
        detail_method=detail_method,
        caller_app_key=caller_app_key,
        caller_app_secret=caller_app_secret,
        target_app_key=target_app_key,
        sign_method=sign_method,
        hospital_id=hospital_id,
        output_dir=output_dir,
        product_index_path=product_index_path,
        lookback_days=lookback_days,
        max_search_span_days=max_search_span_days,
        page_size=page_size,
        max_pages=_integer(data.get("max_pages", 1000), "max_pages", 1),
        request_timeout_seconds=_integer(
            data.get("request_timeout_seconds", 40),
            "request_timeout_seconds",
            1,
        ),
        request_retries=request_retries,
    )

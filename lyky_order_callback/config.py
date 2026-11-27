"""Configuration loading for the Lyky original-order callback receiver."""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any


class ConfigurationError(ValueError):
    """Raised when callback deployment configuration is incomplete or unsafe."""


def _placeholder(value: str) -> bool:
    return value.strip().upper().startswith("REPLACE_")


def _positive_int(value: Any, field: str, minimum: int = 1) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field} must be an integer") from exc
    if result < minimum:
        raise ConfigurationError(f"{field} must be >= {minimum}")
    return result


@dataclass(frozen=True)
class Settings:
    listen_host: str
    listen_port: int
    callback_path: str
    database_path: pathlib.Path
    app_id: str
    app_secret: str
    timestamp_tolerance_seconds: int
    max_body_bytes: int


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

    app_id = str(data.get("app_id") or "").strip()
    if not app_id or _placeholder(app_id):
        raise ConfigurationError("app_id must be configured")
    secret_env = str(data.get("app_secret_env") or "LYKY_APP_SECRET").strip()
    if not secret_env:
        raise ConfigurationError("app_secret_env must be configured")
    app_secret = os.environ.get(secret_env, "").strip()
    if not app_secret or _placeholder(app_secret):
        raise ConfigurationError(f"missing secret environment value: {secret_env}")

    callback_path = str(
        data.get("callback_path") or "/callbacks/lyky/orders/original"
    ).strip()
    if not callback_path.startswith("/") or "?" in callback_path or "#" in callback_path:
        raise ConfigurationError("callback_path must be an absolute path without query text")

    database_path = pathlib.Path(
        str(data.get("database_path") or "/var/lib/lyky-order-callback/orders.sqlite3")
    )
    if not database_path.is_absolute():
        raise ConfigurationError("database_path must be absolute")

    port = _positive_int(data.get("listen_port", 8092), "listen_port")
    if port > 65535:
        raise ConfigurationError("listen_port must be <= 65535")

    return Settings(
        listen_host=str(data.get("listen_host") or "127.0.0.1").strip(),
        listen_port=port,
        callback_path=callback_path,
        database_path=database_path,
        app_id=app_id,
        app_secret=app_secret,
        timestamp_tolerance_seconds=_positive_int(
            data.get("timestamp_tolerance_seconds", 600),
            "timestamp_tolerance_seconds",
        ),
        max_body_bytes=_positive_int(data.get("max_body_bytes", 1048576), "max_body_bytes"),
    )


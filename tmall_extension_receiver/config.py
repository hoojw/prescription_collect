"""Configuration loader for the internal Tmall extension receiver."""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any


class ConfigurationError(ValueError):
    """Raised when the receiver configuration is incomplete or unsafe."""


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
    ingest_path: str
    output_dir: pathlib.Path
    database_path: pathlib.Path
    bearer_token: str
    max_body_bytes: int
    max_records_per_batch: int


def load_settings(path: str | pathlib.Path) -> Settings:
    config_path = pathlib.Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"invalid JSON config: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("config root must be an object")

    token_env = str(payload.get("bearer_token_env") or "TMALL_INGEST_TOKEN").strip()
    if not token_env:
        raise ConfigurationError("bearer_token_env must be configured")
    bearer_token = os.environ.get(token_env, "").strip()
    if len(bearer_token) < 32:
        raise ConfigurationError(f"{token_env} must contain at least 32 characters")

    ingest_path = str(payload.get("ingest_path") or "/internal/tmall/prescription-batches").strip()
    if not ingest_path.startswith("/") or "?" in ingest_path or "#" in ingest_path:
        raise ConfigurationError("ingest_path must be an absolute path without query text")

    output_dir = pathlib.Path(
        str(payload.get("output_dir") or "/var/lib/prescription-collector/stores/tmall-extension")
    )
    database_path = pathlib.Path(
        str(payload.get("database_path") or output_dir / "state.sqlite3")
    )
    if not output_dir.is_absolute() or not database_path.is_absolute():
        raise ConfigurationError("output_dir and database_path must be absolute")

    listen_port = _positive_int(payload.get("listen_port", 8095), "listen_port")
    if listen_port > 65535:
        raise ConfigurationError("listen_port must be <= 65535")

    return Settings(
        listen_host=str(payload.get("listen_host") or "127.0.0.1").strip(),
        listen_port=listen_port,
        ingest_path=ingest_path,
        output_dir=output_dir,
        database_path=database_path,
        bearer_token=bearer_token,
        max_body_bytes=_positive_int(
            payload.get("max_body_bytes", 20 * 1024 * 1024),
            "max_body_bytes",
        ),
        max_records_per_batch=_positive_int(
            payload.get("max_records_per_batch", 5000),
            "max_records_per_batch",
        ),
    )


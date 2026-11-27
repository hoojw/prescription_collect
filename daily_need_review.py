#!/usr/bin/env python3
"""Maintain one deduplicated need-review snapshot per store and calendar day."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import contextlib
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


VOLATILE_FIELDS = frozenset(
    {
        "api_message",
        "api_status",
        "api_uploaded_at",
        "collected_at",
        "image_file",
        "last_seen_at",
        "run_id",
        "upload_attempts",
    }
)


@dataclass(frozen=True)
class DailyReviewMergeResult:
    total: int
    added: int
    updated: int
    unchanged: int
    existing_duplicates_removed: int
    file_written: bool


def review_identity(record: dict[str, Any]) -> str:
    prescription_key = str(record.get("prescription_key") or "").strip()
    if prescription_key:
        return f"key:{prescription_key}"
    platform = str(record.get("platform") or "").strip().upper()
    order_id = str(record.get("order_view_id") or record.get("order_id") or "").strip()
    prescription_id = str(
        record.get("prescription_id") or record.get("prescription_number") or ""
    ).strip()
    if order_id or prescription_id:
        return f"fallback:{platform}:{order_id}:{prescription_id}"
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "digest:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_value(value: Any, field_name: str = "") -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_value(item, key)
            for key, item in sorted(value.items())
            if key not in VOLATILE_FIELDS
        }
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    if field_name in {"image_url", "image_file_url"} and isinstance(value, str):
        parts = urlsplit(value)
        if parts.scheme and parts.netloc:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return value


def review_fingerprint(record: dict[str, Any]) -> str:
    canonical = json.dumps(
        _stable_value(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_existing(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"daily need_review must be a JSON array: {path}")
    return [item for item in payload if isinstance(item, dict)]


def _hospital_source_code(record: dict[str, Any]) -> int:
    value = record.get("hospital_source_code")
    if value in (None, ""):
        hospital_ocr = record.get("hospital_ocr")
        if isinstance(hospital_ocr, dict):
            value = hospital_ocr.get("hospital_source_code")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_review_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Drop resolved dynamic reasons and return None when nothing remains."""
    normalized = dict(record)
    reason_key = (
        "need_review_reasons"
        if "need_review_reasons" in normalized
        else "review_reasons"
        if "review_reasons" in normalized
        else ""
    )
    if not reason_key:
        return normalized
    reasons = [str(item) for item in normalized.get(reason_key) or [] if str(item)]
    if _hospital_source_code(normalized) in {1, 2}:
        reasons = [item for item in reasons if item != "hospital_source_needs_review"]
    if not reasons:
        return None
    normalized[reason_key] = reasons
    return normalized


def _atomic_write(path: pathlib.Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)

def merge_daily_need_review(
    path: pathlib.Path,
    records: Iterable[dict[str, Any]],
) -> DailyReviewMergeResult:
    target = pathlib.Path(path)
    existing = _read_existing(target)
    ordered_keys: list[str] = []
    by_key: dict[str, dict[str, Any]] = {}
    existing_duplicates = 0
    existing_normalized = False
    for source in existing:
        record = normalize_review_record(source)
        if record is None:
            existing_normalized = True
            continue
        if record != source:
            existing_normalized = True
        key = review_identity(record)
        if key in by_key:
            existing_duplicates += 1
        else:
            ordered_keys.append(key)
        by_key[key] = record

    added = 0
    updated = 0
    unchanged = 0
    for source in records:
        record = normalize_review_record(source)
        if record is None:
            continue
        key = review_identity(record)
        previous = by_key.get(key)
        if previous is None:
            ordered_keys.append(key)
            by_key[key] = record
            added += 1
        elif review_fingerprint(previous) == review_fingerprint(record):
            unchanged += 1
        else:
            by_key[key] = record
            updated += 1

    merged = [by_key[key] for key in ordered_keys]
    changed = bool(added or updated or existing_duplicates or existing_normalized)
    if not merged:
        if target.exists():
            target.unlink()
            changed = True
        return DailyReviewMergeResult(0, added, updated, unchanged, existing_duplicates, changed)
    if changed or not target.exists():
        _atomic_write(target, merged)
        return DailyReviewMergeResult(
            len(merged), added, updated, unchanged, existing_duplicates, True
        )
    return DailyReviewMergeResult(
        len(merged), added, updated, unchanged, existing_duplicates, False
    )

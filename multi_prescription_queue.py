#!/usr/bin/env python3
"""Append-only queue for platform orders that expose multiple prescriptions."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development environment
    fcntl = None  # type: ignore[assignment]


DEFAULT_QUEUE_PATH = Path(
    "/var/lib/prescription-collector/multi_prescription_orders.jsonl"
)


def _unique_strings(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def append_multi_prescription_order(
    *,
    platform: Any,
    store_id: Any,
    store_name: Any,
    order_id: Any,
    prescription_count: Any,
    prescription_ids: Iterable[Any] = (),
    queue_path: Optional[Path] = None,
) -> bool:
    """Append one pending order unless its platform/store/order key already exists."""
    platform_text = str(platform or "").strip().upper()
    store_id_text = str(store_id or "").strip()
    order_id_text = str(order_id or "").strip()
    if not platform_text or not store_id_text or not order_id_text:
        raise ValueError("platform, store_id and order_id are required")
    try:
        count = int(prescription_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("prescription_count must be an integer") from exc
    if count < 2:
        raise ValueError("multi-prescription queue requires at least two prescriptions")

    queue_key = f"{platform_text}:{store_id_text}:{order_id_text}"
    payload = {
        "schema_version": 1,
        "queue_key": queue_key,
        "status": "pending",
        "reason": "multiple_prescriptions_in_order",
        "platform": platform_text,
        "store_id": store_id_text,
        "store_name": str(store_name or "").strip(),
        "order_id": order_id_text,
        "prescription_count": count,
        "prescription_ids": _unique_strings(prescription_ids),
        "first_seen_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    path = Path(queue_path or DEFAULT_QUEUE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            for line in handle:
                try:
                    existing = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if str(existing.get("queue_key") or "") == queue_key:
                    return False
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.chmod(path, 0o600)
            return True
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

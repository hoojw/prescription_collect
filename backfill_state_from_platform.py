#!/usr/bin/env python3
"""Backfill collector SQLite state from platform prescription identifiers only.

This utility performs no image download, OCR, COS upload or delivery API call.
It stores only prescription/order identifiers and derived deduplication keys.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
from typing import Any, Iterable


def prescription_key(platform: str, store_id: str, prescription_id: str) -> str:
    platform_name = str(platform or "").strip().upper()
    store = str(store_id or "").strip()
    rx_id = str(prescription_id or "").strip()
    if not platform_name or not store or not rx_id:
        raise ValueError("platform, store_id and prescription_id are required")
    if platform_name == "JINGDONG":
        seed = f"JINGDONG:{store}:{rx_id}"
    elif platform_name == "MEITUAN":
        # Current Meituan delivery accepts only one prescription per order;
        # its stable key is the prescription id itself.
        seed = rx_id
    else:
        raise ValueError(f"unsupported platform: {platform_name}")
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def write_jsonl(path: pathlib.Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def insert_state_rows(
    state_path: pathlib.Path,
    records: list[dict[str, Any]],
    run_id: str,
) -> tuple[int, int]:
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    inserted = 0
    existing = 0
    connection = sqlite3.connect(state_path)
    try:
        for record in records:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO prescriptions (
                    prescription_key, prescription_id, order_view_id,
                    first_seen_at, last_seen_at, image_sha256, run_id,
                    cos_status, cos_url, api_status, api_message,
                    api_uploaded_at, upload_attempts
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, 'not_requested', NULL,
                          'not_requested', 'platform_id_backfill', NULL, 0)
                """,
                (
                    record["prescription_key"],
                    record["prescription_id"],
                    record["order_view_id"],
                    now,
                    now,
                    run_id,
                ),
            )
            if cursor.rowcount == 1:
                inserted += 1
            else:
                existing += 1
        connection.commit()
    finally:
        connection.close()
    state_path.chmod(0o600)
    return inserted, existing


def select_missing_records(
    state_path: pathlib.Path,
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Return unique identifiers not already present in the store state."""
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        unique.setdefault(str(record["prescription_key"]), record)
    connection = sqlite3.connect(state_path)
    try:
        existing_keys = {
            key
            for key in unique
            if connection.execute(
                "SELECT 1 FROM prescriptions WHERE prescription_key=?", (key,)
            ).fetchone()
            is not None
        }
    finally:
        connection.close()
    missing = [record for key, record in unique.items() if key not in existing_keys]
    return missing, len(existing_keys), len(records) - len(unique)


def collect_jingdong(
    config: dict[str, Any], start: str, end: str, limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from jd_prescription_api import JdBrowserTransport, JdPrescriptionApi
    from server_jd_prescription_collect import (
        CdpPage,
        HOME_URL,
        StateStore,
        parse_date_range,
        split_indexed_values,
        validate_store_context,
    )

    start_date, end_date, label = parse_date_range(
        start, end, int(config.get("lookback_days") or 3)
    )
    store_id = str(config.get("expected_vender_id") or "").strip()
    shop_name = str(config.get("expected_shop_name") or "").strip()
    output_dir = pathlib.Path(str(config["output_dir"])).resolve()
    platform_identifiers = len(records)
    records, existing_before_commit, duplicate_identifiers = select_missing_records(
        output_dir / "state.sqlite3", records
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    StateStore(output_dir / "state.sqlite3").close()
    records: list[dict[str, Any]] = []
    skipped_multi = 0
    detail_errors = 0
    with CdpPage(int(config["chrome_port"]), HOME_URL) as page:
        api = JdPrescriptionApi(
            JdBrowserTransport(page, int(config.get("request_retries") or 3))
        )
        rx_ids, scan = api.enumerate_rx_ids(
            start_date,
            end_date,
            int(config.get("max_pages") or 1000),
            int(config.get("page_size") or 10),
            str(config.get("rx_status_type") or "2"),
        )
        for rx_id in rx_ids:
            if limit and len(records) >= limit:
                break
            try:
                vo = api.detail(rx_id).raw
                validate_store_context(vo, store_id, shop_name)
                ext = vo.get("rxExtendsInfo") or {}
                rx_nos = split_indexed_values(ext.get("rxNo"), preserve_empty=True)
                rx_nos = [str(value or "").strip() for value in rx_nos if str(value or "").strip()]
                if len(rx_nos) != 1:
                    if len(rx_nos) > 1:
                        skipped_multi += 1
                    else:
                        detail_errors += 1
                    continue
                prescription_id = rx_nos[0]
                order_id = str(vo.get("orderId") or "").strip()
                if not order_id:
                    detail_errors += 1
                    continue
            except Exception:
                detail_errors += 1
                continue
            records.append(
                {
                    "prescription_key": prescription_key(
                        "JINGDONG", store_id, prescription_id
                    ),
                    "prescription_id": prescription_id,
                    "order_view_id": order_id,
                    "platform": "JINGDONG",
                    "store_id": store_id,
                    "source": "platform_id_backfill",
                }
            )
    return records, {
        "date_range": label,
        "listed": len(rx_ids),
        "pages_scanned": scan.get("pages_scanned", 0),
        "skipped_multi_prescription_orders": skipped_multi,
        "detail_errors": detail_errors,
    }


def collect_meituan(
    config: dict[str, Any], start: str, end: str, limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from server_meituan_prescription_collect import (
        CdpPage,
        HOME_URL,
        StateStore,
        enumerate_rx_orders,
        get_prescription_detail,
        is_multi_prescription_order,
        normalize_prescriptions,
        parse_date_range,
        validate_browser_store,
    )

    start_ts, end_ts, label = parse_date_range(
        start, end, int(config.get("lookback_days") or 3)
    )
    store_id = str(config.get("expected_poi_id") or "").strip()
    output_dir = pathlib.Path(str(config["output_dir"])).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    StateStore(output_dir / "state.sqlite3").close()
    records: list[dict[str, Any]] = []
    skipped_multi = 0
    detail_errors = 0
    with CdpPage(int(config["chrome_port"]), HOME_URL) as page:
        validate_browser_store(page, store_id)
        orders, scan = enumerate_rx_orders(
            page,
            start_ts,
            end_ts,
            int(config.get("max_pages") or 10000),
            int(config.get("page_size") or 40),
            {
                str(value).strip()
                for value in (config.get("statuses") or [])
                if str(value).strip()
            },
        )
        for order in orders:
            if limit and len(records) >= limit:
                break
            try:
                detail = get_prescription_detail(
                    page,
                    order["order_view_id"],
                    int(config.get("request_retries") or 3),
                )
                if detail.get("status") != "ok":
                    detail_errors += 1
                    continue
                normalized = normalize_prescriptions(order, detail)
                if is_multi_prescription_order(normalized):
                    skipped_multi += 1
                    continue
            except Exception:
                detail_errors += 1
                continue
            for source in normalized:
                prescription_id = str(source.get("prescription_id") or "").strip()
                if not prescription_id:
                    detail_errors += 1
                    continue
                records.append(
                    {
                        "prescription_key": prescription_key(
                            "MEITUAN", store_id, prescription_id
                        ),
                        "prescription_id": prescription_id,
                        "order_view_id": str(source.get("order_view_id") or ""),
                        "platform": "MEITUAN",
                        "store_id": store_id,
                        "source": "platform_id_backfill",
                    }
                )
                if limit and len(records) >= limit:
                    break
    return records, {
        "date_range": label,
        "listed": len(orders),
        "pages_scanned": scan.get("pages_scanned", 0),
        "skipped_multi_prescription_orders": skipped_multi,
        "detail_errors": detail_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    platform = str(config.get("platform") or "").strip().upper()
    if platform == "JINGDONG":
        records, summary = collect_jingdong(config, args.start, args.end, args.limit)
    elif platform == "MEITUAN":
        records, summary = collect_meituan(config, args.start, args.end, args.limit)
    else:
        raise ValueError(f"unsupported platform in config: {platform}")

    output_dir = pathlib.Path(str(config["output_dir"])).resolve()
    run_id = "backfill_" + dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir / "backfill_runs" / run_id
    output_path = run_dir / "prescription_ids.jsonl"
    write_jsonl(output_path, records)
    inserted = existing = 0
    if args.commit:
        inserted, existing = insert_state_rows(
            output_dir / "state.sqlite3", records, run_id
        )
    result = {
        "platform": platform,
        "config": str(args.config),
        **summary,
        "platform_identifiers": platform_identifiers,
        "missing_identifiers": len(records),
        "existing_before_commit": existing_before_commit,
        "duplicate_identifiers": duplicate_identifiers,
        "commit": bool(args.commit),
        "inserted": inserted,
        "already_in_state": existing,
        "output_file": str(output_path),
        "ocr": "not_run",
        "cos": "not_requested",
        "api": "not_requested",
    }
    (run_dir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

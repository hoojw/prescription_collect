#!/usr/bin/env python3
"""Re-run OCR for one review file and optionally submit newly valid records."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shutil
import sqlite3
from typing import Any
from urllib.parse import urlparse

from drug_catalog import DrugCatalog
from prescription_delivery import (
    build_interface_payload,
    response_snapshot,
    review_reasons_for_record,
    submit_json_file,
    write_interface_json,
)
from prescription_usage_ocr import PaddleUsageOCR, build_upload_medicine_info
from server_jd_prescription_collect import apply_catalog_fallback as jd_catalog_fallback
from server_meituan_prescription_collect import (
    apply_catalog_fallback as meituan_catalog_fallback,
)


SUCCESS_CODES = {100, 110, 120}
SUCCESS_DRCODE_CODES = {1, 2, 3, 4}


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def make_ocr(config: dict[str, Any]) -> PaddleUsageOCR:
    return PaddleUsageOCR(
        min_confidence=float(config.get("ocr_min_confidence", 0.75)),
        crop_top=float(config.get("ocr_crop_top_ratio", 0.30)),
        crop_bottom=float(config.get("ocr_crop_bottom_ratio", 0.70)),
        single_crop_top=float(config.get("ocr_single_crop_top_ratio", 0.35)),
        single_crop_bottom=float(config.get("ocr_single_crop_bottom_ratio", 0.53)),
        multi_crop_top=float(config.get("ocr_multi_crop_top_ratio", 0.35)),
        multi_crop_bottom=float(config.get("ocr_multi_crop_bottom_ratio", 0.70)),
        usage_preprocessing_mode=str(
            config.get("usage_ocr_preprocessing_mode", "blue_mask")
        ),
        usage_grayscale_threshold=config.get("usage_ocr_grayscale_threshold", 210),
        cpu_threads=3,
    )


def resolve_image(
    record: dict[str, Any], config: dict[str, Any], review_path: pathlib.Path
) -> pathlib.Path:
    relative = pathlib.Path(str(record.get("image_file") or ""))
    if str(relative).startswith("runs/"):
        return pathlib.Path(str(config["output_dir"])) / relative
    return review_path.parent / relative


def reprocess_records(
    records: list[dict[str, Any]],
    config: dict[str, Any],
    review_path: pathlib.Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    platform = str(config.get("platform") or "").upper()
    engine = make_ocr(config)
    catalog = (
        DrugCatalog.load(pathlib.Path(str(config["drug_catalog_index"])))
        if config.get("drug_catalog_enabled")
        else None
    )
    catalog_fallback = (
        meituan_catalog_fallback if platform == "MEITUAN" else jd_catalog_fallback
    )
    ready: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for record in records:
        image_path = resolve_image(record, config, review_path)
        drugs, metrics = engine.enrich(record.get("drug_infos") or [], image_path)
        final_drugs, processing, _, _ = catalog_fallback(drugs, catalog)
        if platform == "MEITUAN":
            for index, drug in enumerate(final_drugs):
                if index < len(processing):
                    processing[index]["drcode_status"] = int(
                        drug.get("drcode_status") or 0
                    )
        updated = dict(record)
        updated["usage_ocr"] = metrics
        updated["drug_infos"] = final_drugs
        updated["processing"] = processing
        updated["medicine_info"] = build_upload_medicine_info(final_drugs)
        updated.pop("need_review_reasons", None)
        reasons = review_reasons_for_record(updated)
        if reasons:
            updated["need_review_reasons"] = reasons
            remaining.append(updated)
        else:
            ready.append(updated)
    return ready, remaining


def mark_api_success(
    state_path: pathlib.Path,
    records: list[dict[str, Any]],
    message: str,
) -> int:
    keys = [str(record.get("prescription_key") or "") for record in records]
    keys = [key for key in keys if key]
    if not keys:
        return 0
    uploaded_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    connection = sqlite3.connect(state_path)
    try:
        placeholders = ",".join("?" for _ in keys)
        cursor = connection.execute(
            f"""
            UPDATE prescriptions
            SET api_status='success', api_message=?, api_uploaded_at=?,
                upload_attempts=upload_attempts+1
            WHERE prescription_key IN ({placeholders})
            """,
            [message, uploaded_at, *keys],
        )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--review", required=True, type=pathlib.Path)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--require-api-host", default="api.yizhujk.com")
    args = parser.parse_args()

    config_path = args.config.expanduser().resolve()
    review_path = args.review.expanduser().resolve()
    config = load_json(config_path)
    records = load_json(review_path)
    if not isinstance(records, list):
        raise ValueError("need_review.json must contain a JSON list")
    ready, remaining = reprocess_records(records, config, review_path)
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    audit_path = review_path.with_name(f"need_review_reprocess_{stamp}.json")
    write_interface_json(audit_path, {"ready": ready, "remaining": remaining})

    result_summary: dict[str, Any] = {
        "review_file": str(review_path),
        "input_records": len(records),
        "ready_records": len(ready),
        "remaining_records": len(remaining),
        "submitted": False,
        "api_status": "not_requested",
        "audit_file": str(audit_path),
    }
    if not args.submit or not ready:
        print(json.dumps(result_summary, ensure_ascii=False))
        return 0

    api_url = str(config.get("api_url") or "")
    if urlparse(api_url).hostname != args.require_api_host:
        raise ValueError(f"refusing non-production API host: {api_url}")
    for record in ready:
        if str(record.get("cos_status") or "") != "success" or not str(
            record.get("image_file_url") or ""
        ).startswith("https://"):
            raise ValueError("ready record lacks a verified COS image URL")

    source = str(config.get("source") or ("美团" if config["platform"] == "MEITUAN" else "京东"))
    payload = build_interface_payload(
        ready,
        str(config.get("hospital_name") or config.get("internal_hospital_name") or ""),
        source=source,
    )
    upload_path = review_path.with_name(f"prescription_reprocessed_upload_{stamp}.json")
    response_path = review_path.with_name(f"reprocess_api_response_{stamp}.json")
    write_interface_json(upload_path, payload)
    result = submit_json_file(
        upload_path,
        api_url,
        str(config["platform"]),
        str(config.get("app_code") or "pachong"),
        int(config.get("api_timeout_seconds") or 300),
    )
    write_interface_json(response_path, response_snapshot(result))
    result_summary.update(
        {
            "submitted": True,
            "api_status": result.outcome,
            "api_http_status": result.http_status,
            "api_message": result.message,
            "upload_file": str(upload_path),
            "response_file": str(response_path),
        }
    )
    if result.outcome != "success":
        print(json.dumps(result_summary, ensure_ascii=False))
        return 2

    backup_path = review_path.with_name(f"need_review_before_reprocess_{stamp}.json")
    shutil.copy2(review_path, backup_path)
    write_interface_json(review_path, remaining)
    updated_rows = mark_api_success(
        pathlib.Path(str(config["output_dir"])) / "state.sqlite3",
        ready,
        result.message,
    )
    result_summary["sqlite_rows_marked_success"] = updated_rows
    result_summary["backup_file"] = str(backup_path)
    print(json.dumps(result_summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


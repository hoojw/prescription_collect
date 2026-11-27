#!/usr/bin/env python3
"""Drain raw Tmall extension batches and produce upload/review artifacts.

The worker is intentionally platform-browser independent.  It consumes batches
accepted by ``tmall_extension_receiver``, loads PaddleOCR once per drain, and
processes every queued prescription serially.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import requests

from cos_image_upload import CosSettings, create_cos_client, upload_image
from drug_catalog import (
    DrugCatalog,
    compact_processing_result,
    status_code_for_drug,
)
from prescription_delivery import (
    build_interface_record,
    response_snapshot,
    submit_json_file,
    write_interface_json,
)
from prescription_usage_ocr import (
    build_upload_medicine_info,
    infer_purchase_unit,
    merge_same_drugs,
    split_drug_name_specification,
)
from tmall_prescription_ocr import TmallPrescriptionOCR


SUCCESS_STATUS_CODES = frozenset({100, 110, 120})
SAFE_COMPONENT_RE = re.compile(r"[^0-9A-Za-z._-]+")


def now_iso() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def safe_component(value: Any, fallback: str) -> str:
    cleaned = SAFE_COMPONENT_RE.sub("_", str(value or "").strip()).strip("._-")
    return cleaned or fallback


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def decimal_number(value: Any) -> Optional[float]:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if number < 0:
        return None
    return float(number.quantize(Decimal("0.01")))


def is_used_prescription(source: dict[str, Any]) -> bool:
    return text(source.get("prescription_status")) in {"3", "已使用"}


@dataclass(frozen=True)
class ProcessorSettings:
    output_dir: pathlib.Path
    database_path: pathlib.Path
    drug_catalog_index: pathlib.Path
    drug_catalog_enabled: bool
    cos_upload_enabled: bool
    cos_folder_prefix: str
    store_id: str
    upload_hospital: str
    request_retries: int
    request_timeout_seconds: int
    ocr_min_confidence: float
    ocr_cpu_threads: int
    api_submit_enabled: bool
    api_url: str
    api_platform: str
    app_code: str
    api_timeout_seconds: int


def load_settings(path: pathlib.Path) -> ProcessorSettings:
    payload = json.loads(path.read_text(encoding="utf-8"))
    output_dir = pathlib.Path(
        text(payload.get("output_dir"))
        or "/var/lib/prescription-collector/stores/tmall-extension"
    )
    database_path = pathlib.Path(
        text(payload.get("database_path")) or str(output_dir / "state.sqlite3")
    )
    return ProcessorSettings(
        output_dir=output_dir,
        database_path=database_path,
        drug_catalog_index=pathlib.Path(
            text(payload.get("drug_catalog_index"))
            or "/opt/prescription-collector/drug_catalog_index.json"
        ),
        drug_catalog_enabled=bool(payload.get("drug_catalog_enabled", True)),
        cos_upload_enabled=bool(payload.get("cos_upload_enabled", True)),
        cos_folder_prefix=text(payload.get("cos_folder_prefix")) or "/files",
        store_id=text(payload.get("store_id")) or "tmall-extension",
        upload_hospital=text(payload.get("upload_hospital"))
        or "天津北辰保民医院久远互联网医院",
        request_retries=max(1, int(payload.get("request_retries") or 3)),
        request_timeout_seconds=max(10, int(payload.get("request_timeout_seconds") or 60)),
        ocr_min_confidence=float(payload.get("ocr_min_confidence") or 0.75),
        ocr_cpu_threads=max(1, int(payload.get("ocr_cpu_threads") or 3)),
        api_submit_enabled=bool(payload.get("api_submit_enabled", False)),
        api_url=text(payload.get("api_url"))
        or "https://api.yizhujk.com/public/prescriptionUpload/importJson",
        api_platform=text(payload.get("api_platform")) or "TIANMAO",
        app_code=text(payload.get("app_code")) or "pachong",
        api_timeout_seconds=max(30, int(payload.get("api_timeout_seconds") or 300)),
    )


def update_batch_status(
    database_path: pathlib.Path,
    batch_id: str,
    status: str,
    payload_path: pathlib.Path,
) -> None:
    connection = sqlite3.connect(database_path, timeout=10)
    try:
        connection.execute(
            "UPDATE tmall_extension_batches SET status=?, payload_path=? WHERE batch_id=?",
            (status, str(payload_path), batch_id),
        )
        connection.commit()
    finally:
        connection.close()


def download_image(
    url: str,
    image_dir: pathlib.Path,
    stem: str,
    retries: int,
    timeout_seconds: int,
) -> tuple[pathlib.Path, str]:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 prescription-collector"},
                timeout=(10, timeout_seconds),
            )
            response.raise_for_status()
            content = response.content
            if not content:
                raise RuntimeError("empty image response")
            content_type = text(response.headers.get("Content-Type")).lower()
            if "png" in content_type:
                suffix = ".png"
            elif "webp" in content_type:
                suffix = ".webp"
            else:
                suffix = ".jpg"
            path = image_dir / f"{safe_component(stem, 'prescription')}{suffix}"
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(content)
            temporary.replace(path)
            return path, hashlib.sha256(content).hexdigest()
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2**attempt, 8))
    raise RuntimeError(type(last_error).__name__ if last_error else "image_download_failed")


def order_for_record(record: dict[str, Any]) -> tuple[Optional[dict[str, Any]], list[str]]:
    orders = record.get("orders")
    if isinstance(orders, list):
        valid = [item for item in orders if isinstance(item, dict)]
    else:
        order = record.get("order")
        valid = [order] if isinstance(order, dict) else []
    if len(valid) > 1:
        return None, ["multiple_completed_orders"]
    if not valid:
        return None, ["platform_required_missing"]
    return valid[0], []


def drug_infos_from_order(order: dict[str, Any]) -> list[dict[str, Any]]:
    products = order.get("products")
    if not isinstance(products, list):
        return []
    drugs: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        title = text(product.get("drug_name"))
        name, specification = split_drug_name_specification(title, "")
        quantity = product.get("quantity")
        drugs.append(
            {
                "drugName": name or title,
                "source_drug_name": title,
                "packingSpec": specification,
                "drugCount": quantity,
                "purchase_quantity": quantity,
                "purchase_unit": infer_purchase_unit(name or title, specification),
                "drcode": text(product.get("drcode")),
                "drcode_status": 1 if text(product.get("drcode")) else 11,
                "source_index": product.get("source_index"),
            }
        )
    return merge_same_drugs(drugs)


def platform_missing(record: dict[str, Any], order: Optional[dict[str, Any]]) -> list[str]:
    required = {
        "prescription_id": record.get("prescription_id"),
        "order_id": record.get("order_id"),
        "hospital_name": record.get("hospital_name"),
        "doctor": record.get("doctor"),
        "patient_name": record.get("patient_name"),
        "patient_gender": record.get("patient_gender"),
        "audit": record.get("audit"),
        "image_url": record.get("image_url"),
        "order": order,
    }
    missing = [name for name, value in required.items() if value in (None, "", [], {})]
    if order is not None:
        if not order.get("products"):
            missing.append("products")
        if decimal_number(order.get("total_fee")) is None:
            missing.append("total_fee")
    return missing


def final_review_reasons(record: dict[str, Any], initial: Iterable[str]) -> list[str]:
    reasons = list(initial)
    missing_fields = record.get("platform_required_missing_fields") or []
    if missing_fields:
        reasons.append("platform_required_missing")
    if record.get("image_status") != "downloaded":
        reasons.append("image_download_failed")
    if record.get("diagnosis_ocr_status") != "success":
        reasons.append("diagnosis_ocr_failed")
    if record.get("age_ocr_status") != "success":
        reasons.append("age_ocr_failed")
    if record.get("cos_status") != "success":
        reasons.append("cos_upload_failed")
    processing = record.get("processing") or []
    if not processing:
        reasons.append("processing_status_missing")
    for item in processing:
        if int(item.get("status_code") or 0) not in SUCCESS_STATUS_CODES:
            reasons.append(f"drug_{item.get('drug_index', 0)}_status_{item.get('status_code', 0)}")
        if int(item.get("drcode_status") or 0) != 1:
            reasons.append(f"drug_{item.get('drug_index', 0)}_drcode_missing")
    return list(dict.fromkeys(reasons))


def make_record(source: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform": "TIANMAO",
        "prescription_id": text(source.get("prescription_id")),
        "prescription_type": "普通",
        "prescription_time": text(source.get("prescription_time")),
        "collected_at": text(source.get("collected_at")) or now_iso(),
        "order_view_id": text(order.get("order_id") or source.get("order_id")),
        "hospital_name": text(source.get("hospital_name")),
        "physician_name": text(source.get("doctor")),
        "pharmacist_name": text(source.get("audit")),
        "patient_info": {
            "name": text(source.get("patient_name")),
            "gender": text(source.get("patient_gender")),
            "age": text(source.get("patient_age")),
            "birth_date": "",
            "fee_type": text(source.get("fee_type")) or "自费",
            "department": text(source.get("department")),
            "illnessDetail": [],
        },
        "total_fee": decimal_number(order.get("total_fee")),
        "source_image_url": text(source.get("image_url")),
        "structured_fields_source": "tmall_extension_platform_api",
        "image_role": "diagnosis_and_usage_ocr",
    }


def payload_for_records(
    records: list[dict[str, Any]],
    upload_hospital: str,
) -> list[dict[str, Any]]:
    hospital = text(upload_hospital)
    if not hospital:
        raise ValueError("upload_hospital is required")
    data_list = [
        build_interface_record(record, hospital, source="天猫")
        for record in records
    ]
    return [{"hospital": hospital, "data_list": data_list}]


def process_batch(
    queue_path: pathlib.Path,
    settings: ProcessorSettings,
    ocr: TmallPrescriptionOCR,
    catalog: Optional[DrugCatalog],
    cos_settings: Optional[CosSettings],
    cos_client: Any,
) -> dict[str, Any]:
    source_batch = json.loads(queue_path.read_text(encoding="utf-8"))
    batch_id = safe_component(source_batch.get("batch_id"), "tmall-batch")
    run_dir = settings.output_dir / "runs" / batch_id
    image_dir = run_dir / "images"
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    image_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    processed_path = settings.output_dir / "processing" / f"{batch_id}.json"
    processed_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.replace(queue_path, processed_path)
    update_batch_status(settings.database_path, batch_id, "processing", processed_path)

    upload_records: list[dict[str, Any]] = []
    review_records: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    skipped_not_used = 0
    started = time.perf_counter()
    for index, source in enumerate(source_batch.get("records") or []):
        if not is_used_prescription(source):
            skipped_not_used += 1
            continue
        initial_reasons: list[str] = []
        order, order_reasons = order_for_record(source)
        initial_reasons.extend(order_reasons)
        if order is None:
            placeholder_order = {"order_id": source.get("order_id"), "products": [], "total_fee": None}
            record = make_record(source, placeholder_order)
            record["platform_required_missing_fields"] = platform_missing(source, None)
            record["image_status"] = "not_processed"
            record["diagnosis_ocr_status"] = "not_processed"
            record["age_ocr_status"] = "not_processed"
            record["cos_status"] = "not_processed"
            record["processing"] = []
        else:
            record = make_record(source, order)
            record["platform_required_missing_fields"] = platform_missing(source, order)
            drugs = drug_infos_from_order(order)
            image_path: Optional[pathlib.Path] = None
            try:
                image_path, image_hash = download_image(
                    text(source.get("image_url")), image_dir,
                    f"{record['prescription_id']}_{index}", settings.request_retries,
                    settings.request_timeout_seconds,
                )
                record["image_status"] = "downloaded"
                record["image_file"] = str(image_path.relative_to(run_dir))
                record["image_sha256"] = image_hash
            except Exception as exc:
                record["image_status"] = "download_failed"
                record["image_error"] = type(exc).__name__

            if image_path is not None:
                try:
                    diagnosis = ocr.recognize_diagnosis(image_path)
                    record.update(diagnosis)
                    if diagnosis.get("age_ocr_status") == "success":
                        record["patient_info"]["age"] = text(diagnosis.get("age"))
                    record["icd_name"] = diagnosis.get("disease_name") or ""
                    record["patient_info"]["illnessDetail"] = [
                        {"icdName": record["icd_name"], "icdCode": ""}
                    ] if record["icd_name"] else []
                    drugs, usage_metrics = ocr.enrich_usage(drugs, image_path)
                    record["usage_ocr"] = usage_metrics
                except Exception as exc:
                    record["diagnosis_ocr_status"] = "error"
                    record["age_ocr_status"] = "error"
                    record["ocr_error"] = type(exc).__name__
                    for drug in drugs:
                        drug.update({
                            "usage_ocr_status": "error",
                            "duration_calculation_status": "needs_review",
                            "duration_calculation_reason": "ocr_runtime_error",
                        })

            processing: list[dict[str, Any]] = []
            for drug_index, drug in enumerate(drugs):
                if catalog is not None:
                    catalog.apply_if_needed(drug)
                else:
                    drug["status_code"] = status_code_for_drug(drug)
                item = compact_processing_result(drug, drug_index)
                item["drcode_status"] = int(drug.get("drcode_status") or 11)
                processing.append(item)
            record["drug_infos"] = drugs
            record["medicine_info"] = build_upload_medicine_info(drugs)
            record["processing"] = processing

            if image_path is not None and settings.cos_upload_enabled:
                try:
                    result = upload_image(
                        image_path,
                        record["prescription_id"],
                        settings=cos_settings,
                        client=cos_client,
                        platform="TIANMAO",
                        store_id=settings.store_id,
                        business_date=record["prescription_time"] or record["collected_at"],
                    )
                    record.update({
                        "cos_status": "success",
                        "cos_object_key": result["object_key"],
                        "image_file_url": result["image_file_url"],
                    })
                except Exception as exc:
                    record["cos_status"] = "failed"
                    record["cos_error"] = type(exc).__name__
            elif not settings.cos_upload_enabled:
                record["cos_status"] = "disabled"

        reasons = final_review_reasons(record, initial_reasons)
        if reasons:
            record["need_review_reasons"] = reasons
            review_records.append(record)
        else:
            upload_records.append(record)
        all_records.append(record)

    prescriptions_path = run_dir / "prescriptions.jsonl"
    with prescriptions_path.open("w", encoding="utf-8") as handle:
        for record in all_records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    write_interface_json(run_dir / "need_review.json", review_records)
    upload_json_path = run_dir / "prescription_upload.json"
    write_interface_json(
        upload_json_path,
        payload_for_records(upload_records, settings.upload_hospital),
    )
    api_status = "not_submitted"
    api_http_status: Optional[int] = None
    api_message = ""
    api_response_file = ""
    if settings.api_submit_enabled and upload_records:
        try:
            api_result = submit_json_file(
                upload_json_path,
                settings.api_url,
                settings.api_platform,
                settings.app_code,
                settings.api_timeout_seconds,
            )
            api_status = api_result.outcome
            api_http_status = int(api_result.http_status)
            api_message = text(api_result.message)
            api_response_path = run_dir / "api_response.json"
            write_interface_json(api_response_path, response_snapshot(api_result))
            api_response_file = api_response_path.name
        except Exception as exc:
            api_status = "failed"
            api_message = type(exc).__name__
    elif settings.api_submit_enabled:
        api_status = "no_upload_records"
    summary = {
        "batch_id": batch_id,
        "platform": "TIANMAO",
        "received_records": len(source_batch.get("records") or []),
        "processed_records": len(all_records),
        "skipped_not_used": skipped_not_used,
        "upload_records": len(upload_records),
        "need_review_records": len(review_records),
        "diagnosis_ocr_success": sum(
            record.get("diagnosis_ocr_status") == "success" for record in all_records
        ),
        "diagnosis_ocr_failed": sum(
            record.get("diagnosis_ocr_status") != "success" for record in all_records
        ),
        "age_ocr_success": sum(
            record.get("age_ocr_status") == "success" for record in all_records
        ),
        "age_ocr_failed": sum(
            record.get("age_ocr_status") != "success" for record in all_records
        ),
        "api_status": api_status,
        "api_http_status": api_http_status,
        "api_message": api_message,
        "api_response_file": api_response_file,
        "ocr_mode": "original",
        "ocr_serial": True,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        **ocr.timing_metrics(len(all_records)),
        "finished_at": now_iso(),
    }
    write_interface_json(run_dir / "summary.json", summary)
    completed_dir = settings.output_dir / "processed"
    completed_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    completed_path = completed_dir / processed_path.name
    os.replace(processed_path, completed_path)
    update_batch_status(settings.database_path, batch_id, "completed", completed_path)
    print(
        f"TMALL_BATCH_END batch_id={batch_id} records={len(all_records)} "
        f"upload={len(upload_records)} review={len(review_records)} "
        f"skipped_not_used={skipped_not_used} "
        f"api_status={api_status} "
        f"summary={run_dir / 'summary.json'}",
        flush=True,
    )
    return summary


def drain(settings: ProcessorSettings) -> int:
    inbox = settings.output_dir / "inbox"
    inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    queue = sorted(inbox.glob("*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name))
    if not queue:
        print("TMALL_QUEUE_EMPTY", flush=True)
        return 0
    catalog = DrugCatalog.load(settings.drug_catalog_index) if settings.drug_catalog_enabled else None
    cos_settings = CosSettings.from_env() if settings.cos_upload_enabled else None
    cos_client = create_cos_client(cos_settings)[1] if cos_settings else None
    ocr = TmallPrescriptionOCR(
        min_confidence=settings.ocr_min_confidence,
        cpu_threads=settings.ocr_cpu_threads,
    )
    failed = 0
    for queue_path in queue:
        batch_id = queue_path.stem
        print(f"TMALL_BATCH_START batch_id={batch_id}", flush=True)
        try:
            process_batch(queue_path, settings, ocr, catalog, cos_settings, cos_client)
        except Exception as exc:
            failed += 1
            error_dir = settings.output_dir / "failed"
            error_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            candidate = settings.output_dir / "processing" / queue_path.name
            source = candidate if candidate.exists() else queue_path
            target = error_dir / queue_path.name
            if source.exists():
                os.replace(source, target)
            try:
                update_batch_status(settings.database_path, batch_id, "failed", target)
            except Exception:
                pass
            print(f"TMALL_BATCH_FAILED batch_id={batch_id} error={type(exc).__name__}", file=sys.stderr, flush=True)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Serial Tmall extension OCR queue worker")
    parser.add_argument("--config", required=True, type=pathlib.Path)
    args = parser.parse_args()
    return drain(load_settings(args.config))


if __name__ == "__main__":
    raise SystemExit(main())

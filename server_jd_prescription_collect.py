#!/usr/bin/env python3
"""Collect JD merchant prescription data from an already-authorized Chrome profile.

The collector uses Chrome DevTools Protocol and same-origin browser requests. It
never stores browser cookies or automates login/CAPTCHA flows. JD list and detail
pages are server-rendered HTML; the list supplies rxId values and each detail page
contains a JSON-compatible ``var vo = {...}`` object.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import quote, urlparse

import requests
import websocket

from collector_progress import report_store_progress
from daily_need_review import merge_daily_need_review
from diagnosis_maintenance import (
    DEFAULT_DB_PATH as DEFAULT_DIAGNOSIS_MAINTENANCE_DB,
    DEFAULT_OUTPUT_DIR as DEFAULT_DIAGNOSIS_OUTPUT_DIR,
    write_unmaintained_report,
)
from local_image_retention import cleanup_local_images
from run_directory_layout import iter_run_dirs, remove_empty_date_dirs
from drug_text_normalization import strip_expiry_suffix
from cos_image_upload import CosSettings, create_cos_client, upload_image
from jd_prescription_api import (
    JdAuthenticationError,
    JdBrowserTransport,
    JdPrescriptionApi,
    extract_drug_items,
)
from multi_prescription_queue import append_multi_prescription_order
from drug_catalog import (
    STATUS_CATALOG_SUCCESS,
    STATUS_QUALITATIVE_30_DAY_SUCCESS,
    STATUS_CODE_VERSION,
    DrugCatalog,
    compact_processing_result,
    status_code_for_drug,
)
from prescription_delivery import (
    build_interface_payload,
    review_reasons_for_record,
    response_snapshot,
    submit_json_file,
    write_interface_json,
)
from prescription_usage_ocr import (
    PaddleUsageOCR,
    build_upload_medicine_info,
    decimal_value,
    split_drug_name_specification,
)
from platform_required_validation import (
    attach_platform_required_review,
    missing_field_counts,
    validate_platform_required_fields,
)


# 京麦外层页面只负责承载应用；处方列表实际运行在这个 iframe 地址中。
# 保留 _JDMOMID_，否则未建立完整京麦上下文时旧域名可能直接返回 404。
HOME_URL = "https://rx.shop.jd.com/rx/rxInfo_list.action?_JDMOMID_=1559"
JDM_ENTRY_URL = (
    "https://shop.jd.com/jdm/legacy/rx.shop.jd.com/"
    "rx/rxInfo_list.action?_JDMOMID_=1559"
)
RX_TYPE_NAMES = {1: "一键购药", 2: "复诊续方", 3: "用户上传"}

DEFAULT_CONFIG: dict[str, Any] = {
    "chrome_port": 9433,
    "expected_vender_id": "",
    "expected_shop_name": "",
    "internal_hospital_name": "天津北辰保民医院有限公司",
    "internal_output_hospital_name": "天津北辰保民互联网医院",
    "output_dir": "./data-jingdong",
    "lookback_days": 3,
    "max_pages": 1000,
    "page_size": 10,
    "rx_status_type": "2",
    "limit": 0,
    "request_retries": 3,
    "retention_days": 0,
    "retention_runs": 3,
    "image_retention_runs": 0,
    "ocr_enabled": True,
    "ocr_min_confidence": 0.75,
    "ocr_crop_top_ratio": 0.34,
    "ocr_crop_bottom_ratio": 0.68,
    "ocr_single_crop_top_ratio": 0.38,
    "ocr_single_crop_bottom_ratio": 0.62,
    "ocr_multi_crop_top_ratio": 0.34,
    "ocr_multi_crop_bottom_ratio": 0.72,
    "usage_ocr_preprocessing_mode": "grayscale",
    "usage_ocr_grayscale_threshold": 210,
    "cos_upload_enabled": False,
    "cos_folder_prefix": "",
    "api_submit_enabled": False,
    "hospital_name": "天津北辰保民医院久远互联网医院",
    "default_fee_type": "自费",
    "api_url": "https://testapi.yizhujk.com/public/prescriptionUpload/importJson",
    "platform": "JINGDONG",
    "app_code": "pachong",
    "source": "京东",
    "api_timeout_seconds": 300,
    "drug_catalog_enabled": True,
    "drug_catalog_index": "./drug_catalog_index.json",
    "compact_processing_json": True,
    "diagnosis_maintenance_db": str(DEFAULT_DIAGNOSIS_MAINTENANCE_DB),
    "diagnosis_output_dir": str(DEFAULT_DIAGNOSIS_OUTPUT_DIR),
}


def log(message: str) -> None:
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    print(f"[{stamp}] {message}", flush=True)


def restricted_mkdir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def restricted_write(path: pathlib.Path, data: bytes) -> None:
    path.write_bytes(data)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def atomic_json(path: pathlib.Path, payload: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    restricted_write(
        temp,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    os.replace(temp, path)


def append_jsonl(path: pathlib.Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def load_config(path: pathlib.Path) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config must be a JSON object")
        config.update(loaded)

    env_map = {
        "JINGDONG_CHROME_PORT": ("chrome_port", int),
        "JINGDONG_EXPECTED_VENDER_ID": ("expected_vender_id", str),
        "JINGDONG_EXPECTED_SHOP_NAME": ("expected_shop_name", str),
        "JINGDONG_OUTPUT_DIR": ("output_dir", str),
        "JINGDONG_LOOKBACK_DAYS": ("lookback_days", int),
        "JINGDONG_MAX_PAGES": ("max_pages", int),
        "JINGDONG_RX_STATUS_TYPE": ("rx_status_type", str),
        "JINGDONG_COS_FOLDER_PREFIX": ("cos_folder_prefix", str),
    }
    for env_name, (key, convert) in env_map.items():
        value = os.environ.get(env_name)
        if value not in (None, ""):
            config[key] = convert(value)
    return config


def safe_file_component(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "")).strip("._")
    return cleaned[:120] or fallback


def decimal_output(value: Optional[Decimal]) -> Optional[Any]:
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return int(normalized)
    return float(normalized)


def normalize_total_fee(value: Any) -> Optional[float]:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if amount < 0 or amount > Decimal("999999.99"):
        return None
    return float(amount)


def parse_date_range(
    start: Optional[str],
    end: Optional[str],
    lookback_days: int,
) -> tuple[str, str, str]:
    if bool(start) != bool(end):
        raise ValueError("--start and --end must be provided together")
    if start and end:
        start_date = dt.datetime.strptime(start, "%Y-%m-%d").date()
        end_date = dt.datetime.strptime(end, "%Y-%m-%d").date()
    else:
        end_date = dt.date.today()
        start_date = end_date - dt.timedelta(days=max(1, lookback_days) - 1)
    if end_date < start_date:
        raise ValueError("end date is earlier than start date")
    return start_date.isoformat(), end_date.isoformat(), f"{start_date}..{end_date}"


class CdpPage:
    """Small Chrome DevTools client that creates and closes one temporary tab."""

    def __init__(self, port: int, url: str):
        self.base = f"http://127.0.0.1:{port}"
        self._message_id = 0
        self.target_id = ""
        target = self._new_page(url)
        self.target_id = str(target.get("id") or "")
        self.ws = websocket.create_connection(
            target["webSocketDebuggerUrl"],
            timeout=30,
            suppress_origin=True,
        )
        self.call("Runtime.enable")
        self.wait_ready()

    def _new_page(self, url: str) -> dict[str, Any]:
        encoded = quote(url, safe="")
        response = requests.put(f"{self.base}/json/new?{encoded}", timeout=10)
        if response.status_code == 405:
            response = requests.get(f"{self.base}/json/new?{encoded}", timeout=10)
        response.raise_for_status()
        return response.json()

    def call(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        self._message_id += 1
        payload: dict[str, Any] = {"id": self._message_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.ws.send(json.dumps(payload))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != self._message_id:
                continue
            if "error" in message:
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return message.get("result") or {}

    def run_js(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": 60000,
            },
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(f"browser JavaScript failed: {result['exceptionDetails']}")
        remote = result.get("result") or {}
        if "value" in remote:
            return remote["value"]
        return remote.get("description")

    def wait_ready(self, timeout: int = 30) -> None:
        deadline = time.time() + timeout
        last_state = ""
        while time.time() < deadline:
            with contextlib.suppress(Exception):
                last_state = str(self.run_js("document.readyState"))
                if last_state in {"interactive", "complete"}:
                    time.sleep(2)
                    return
            time.sleep(0.5)
        raise TimeoutError(f"browser page did not become ready; state={last_state!r}")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.ws.close()
        if self.target_id:
            with contextlib.suppress(Exception):
                requests.get(f"{self.base}/json/close/{self.target_id}", timeout=5)

    def __enter__(self) -> "CdpPage":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def normalize_identity_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).strip()


def validate_store_context(
    vo: dict[str, Any],
    expected_vender_id: Any,
    expected_shop_name: Any,
) -> tuple[str, str]:
    expected_vender = str(expected_vender_id or "").strip()
    expected_shop = normalize_identity_text(expected_shop_name)
    if not expected_vender:
        raise RuntimeError("expected_vender_id is required for store-safe collection")
    if not expected_shop:
        raise RuntimeError("expected_shop_name is required for store-safe collection")
    actual_vender, actual_shop_raw = extract_store_identity(vo)
    actual_shop = normalize_identity_text(actual_shop_raw)
    if actual_vender != expected_vender:
        raise RuntimeError(
            f"browser store mismatch: expected venderId={expected_vender}, current venderId={actual_vender}"
        )
    if actual_shop != expected_shop:
        raise RuntimeError(
            f"browser store mismatch: expected shopName={expected_shop_name}, current shopName={actual_shop_raw}"
        )
    return actual_vender, actual_shop_raw


def extract_store_identity(vo: dict[str, Any]) -> tuple[str, str]:
    """Read the current JD identity without trusting it as configured identity."""
    ext = vo.get("rxExtendsInfo") or {}
    if not isinstance(ext, dict):
        ext = {}
    actual_vender = str(vo.get("venderId") or "").strip()
    actual_shop_raw = str(ext.get("shopName") or vo.get("shopName") or "").strip()
    if not actual_vender or not normalize_identity_text(actual_shop_raw):
        raise RuntimeError("JD detail is missing venderId or shopName")
    return actual_vender, actual_shop_raw


def split_indexed_values(value: Any, preserve_empty: bool = False) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        items = ["" if item is None else str(item).strip() for item in value]
    else:
        items = [item.strip() for item in re.split(r"[,，]", str(value))]
    return items if preserve_empty else [item for item in items if item]


def align_optional(values: list[str], count: int, field: str) -> tuple[list[str], str]:
    if not values:
        return [""] * count, ""
    if len(values) != count:
        return [], f"{field}_count_mismatch:{len(values)}!={count}"
    return values, ""


def select_delivery_prescription(
    ext: dict[str, Any],
    internal_hospital_name: str,
) -> dict[str, Any]:
    # 这些字段按相同索引描述同一张处方。不能过滤中间空值，否则会把
    # 后续处方的信息向前挪动，造成医院、医生或图片错配。
    rx_nos = split_indexed_values(ext.get("rxNo"), preserve_empty=True)
    hospitals = split_indexed_values(ext.get("hospitalName"), preserve_empty=True)
    doctors = split_indexed_values(ext.get("doctorName"), preserve_empty=True)
    pharmacists = split_indexed_values(ext.get("pharmacistName"), preserve_empty=True)
    images = split_indexed_values(ext.get("rxPicUrlList"), preserve_empty=True)
    image_source = "rxPicUrlList"

    if not rx_nos:
        return {"status": "review", "reason": "rx_no_missing"}
    count = len(rx_nos)
    if any(not item for item in rx_nos):
        return {"status": "review", "reason": "rx_no_empty_index"}
    if count > 1:
        return {
            "status": "skipped",
            "reason": "multiple_prescriptions_in_order",
            "prescription_count": count,
            "prescription_ids": rx_nos,
        }
    if len(hospitals) != count:
        return {
            "status": "review",
            "reason": f"hospital_name_count_mismatch:{len(hospitals)}!={count}",
        }
    if len(images) != count:
        return {
            "status": "review",
            "reason": f"image_count_mismatch:{len(images)}!={count}",
        }
    if any(not item for item in hospitals):
        return {"status": "review", "reason": "hospital_name_empty_index"}
    if any(not item for item in images):
        return {"status": "review", "reason": "rx_pic_url_empty_index"}
    doctors, error = align_optional(doctors, count, "doctor_name")
    if error:
        return {"status": "review", "reason": error}
    pharmacists, error = align_optional(pharmacists, count, "pharmacist_name")
    if error:
        return {"status": "review", "reason": error}

    selected_index = 0
    selection_reason = "single_prescription"

    return {
        "status": "selected",
        "index": selected_index,
        "reason": selection_reason,
        "rx_no": rx_nos[selected_index],
        "hospital_name": hospitals[selected_index],
        "doctor_name": doctors[selected_index],
        "pharmacist_name": pharmacists[selected_index],
        "image_url": images[selected_index],
        "image_source": image_source,
        "prescription_count": count,
        "unselected": [],
    }


def timestamp_to_iso(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return ""
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    try:
        return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def normalize_jd_drug(source: dict[str, Any]) -> dict[str, Any]:
    raw_sku_name = str(
        source.get("sku_name")
        if source.get("sku_name") is not None
        else source.get("skuName")
        or ""
    ).strip()
    sku_name = strip_expiry_suffix(raw_sku_name)
    bundle_boxes = Decimal("1")
    bundle_match = re.search(r"(\d+(?:\.\d+)?)\s*盒装\s*$", sku_name)
    base_sku_name = sku_name
    if bundle_match:
        bundle_boxes = Decimal(bundle_match.group(1))
        base_sku_name = sku_name[: bundle_match.start()].strip()
    drug_name, specification = split_drug_name_specification(base_sku_name)
    buy_num = decimal_value(
        source.get("buy_num")
        if source.get("buy_num") is not None
        else source.get("buyNum")
    ) or Decimal("0")
    if buy_num < 0:
        buy_num = Decimal("0")

    total_boxes = buy_num * bundle_boxes

    sku_id = str(
        source.get("sku_id")
        if source.get("sku_id") is not None
        else source.get("skuId")
        or ""
    ).strip()
    price = normalize_total_fee(
        source.get("unit_price")
        if source.get("unit_price") is not None
        else source.get("price")
    )
    drug = {
        "drugName": drug_name or sku_name,
        "packingSpec": specification,
        "drugCount": decimal_output(total_boxes),
        "drugPrice": price,
        "drcode": sku_id,
        "jd_sku_id": sku_id,
        "jd_sku_name": raw_sku_name,
        "jd_source_index": source.get("source_index"),
        "jd_buy_num": decimal_output(buy_num),
        "jd_bundle_boxes_per_sale": decimal_output(bundle_boxes),
        "jd_purchase_quantity": decimal_output(total_boxes),
        "jd_purchase_unit": "盒",
    }
    if raw_sku_name and raw_sku_name != sku_name:
        drug["source_drug_name"] = raw_sku_name
    return drug


def merge_jd_drugs(drugs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for source in drugs:
        if not isinstance(source, dict):
            continue
        drug = normalize_jd_drug(source)
        key = "|".join(
            [
                normalize_identity_text(drug.get("drugName")).lower(),
                normalize_identity_text(drug.get("packingSpec")).lower(),
                str(drug.get("drcode") or ""),
            ]
        )
        if key not in by_key:
            by_key[key] = drug
            merged.append(drug)
            continue
        target = by_key[key]
        target["drugCount"] = decimal_output(
            (decimal_value(target.get("drugCount")) or Decimal("0"))
            + (decimal_value(drug.get("drugCount")) or Decimal("0"))
        )
        target["jd_purchase_quantity"] = decimal_output(
            (decimal_value(target.get("jd_purchase_quantity")) or Decimal("0"))
            + (decimal_value(drug.get("jd_purchase_quantity")) or Decimal("0"))
        )
    return merged


def restore_jd_purchase_fields(drugs: Iterable[dict[str, Any]]) -> None:
    for drug in drugs:
        drug["purchase_quantity"] = drug.get("jd_purchase_quantity")
        drug["purchase_unit"] = str(drug.get("jd_purchase_unit") or "盒")
        drug["total_quantity"] = drug.get("jd_purchase_quantity")
        drug["total_unit"] = str(drug.get("jd_purchase_unit") or "盒")
        drug["drcode"] = str(drug.get("jd_sku_id") or drug.get("drcode") or "")


def normalize_detail_record(
    vo: dict[str, Any],
    expected_vender_id: str,
    expected_shop_name: str,
    internal_hospital_name: str,
    internal_output_hospital_name: str,
    default_fee_type: str,
) -> dict[str, Any]:
    actual_vender, actual_shop = validate_store_context(
        vo, expected_vender_id, expected_shop_name
    )
    ext = vo.get("rxExtendsInfo") or {}
    if not isinstance(ext, dict):
        return {"status": "review", "reason": "rx_extends_info_missing"}
    selection = select_delivery_prescription(ext, internal_hospital_name)
    if selection.get("status") == "skipped":
        return {
            "status": "skipped",
            "reason": "multiple_prescriptions_in_order",
            "rx_id": str(vo.get("rxId") or ext.get("rxId") or ""),
            "order_id": str(vo.get("orderId") or ""),
            "vender_id": actual_vender,
            "shop_name": actual_shop,
            "prescription_count": selection.get("prescription_count"),
            "prescription_ids": selection.get("prescription_ids") or [],
        }
    if selection.get("status") != "selected":
        review = {
            "status": "review",
            "reason": str(selection.get("reason") or "selection_failed"),
            "rx_id": str(vo.get("rxId") or ext.get("rxId") or ""),
            "order_id": str(vo.get("orderId") or ""),
            "vender_id": actual_vender,
            "shop_name": actual_shop,
        }
        for key in (
            "prescription_count",
            "cos_upload_skipped",
            "api_upload_skipped",
            "prescriptions",
        ):
            if key in selection:
                review[key] = selection[key]
        return review

    order_id = str(vo.get("orderId") or "").strip()
    rx_id = str(vo.get("rxId") or ext.get("rxId") or "").strip()
    prescription_id = str(selection.get("rx_no") or "").strip()
    if not order_id:
        return {"status": "review", "reason": "order_id_missing", "rx_id": rx_id}
    if not prescription_id:
        return {"status": "review", "reason": "selected_rx_no_missing", "rx_id": rx_id}

    source_hospital = str(selection.get("hospital_name") or "").strip()
    is_internal = normalize_identity_text(source_hospital) == normalize_identity_text(
        internal_hospital_name
    )
    hospital_name = (
        str(internal_output_hospital_name or "").strip() or source_hospital
        if is_internal
        else "外部医院"
    )
    physician_name = str(selection.get("doctor_name") or "").strip() if is_internal else ""

    raw_drugs = extract_drug_items(vo)
    if not raw_drugs:
        return {
            "status": "review",
            "reason": "rx_sku_list_missing",
            "rx_id": rx_id,
            "order_id": order_id,
            "prescription_id": prescription_id,
        }
    invalid_drug_indexes = [
        int(item.get("source_index") or 0)
        for item in raw_drugs
        if not str(item.get("sku_id") or "").strip()
        or not str(item.get("sku_name") or "").strip()
        or (decimal_value(item.get("buy_num")) or Decimal("0")) <= 0
    ]
    if invalid_drug_indexes:
        return {
            "status": "review",
            "reason": "rx_sku_item_missing_required_field",
            "rx_id": rx_id,
            "order_id": order_id,
            "prescription_id": prescription_id,
            "invalid_drug_indexes": invalid_drug_indexes,
        }
    drugs = merge_jd_drugs(raw_drugs)

    rx_type_value = vo.get("rxType") or ext.get("rxType")
    try:
        rx_type_number = int(rx_type_value)
    except (TypeError, ValueError):
        rx_type_number = 0
    prescription_time = timestamp_to_iso(
        ext.get("storeTime")
        or ext.get("created")
        or vo.get("created")
        or vo.get("orderTime")
    )
    collected_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    patient_info = {
        "name": str(ext.get("patientName") or ""),
        "gender": ext.get("sex"),
        "age": ext.get("age"),
        "birth_date": str(ext.get("birthday") or ""),
        "fee_type": str(default_fee_type or ""),
        "department": str(ext.get("rxDepartment") or ""),
    }
    return {
        "status": "selected",
        "record": {
            "prescription_key": hashlib.sha256(
                f"JINGDONG:{actual_vender}:{prescription_id}".encode("utf-8")
            ).hexdigest(),
            "prescription_id": prescription_id,
            "order_view_id": order_id,
            "jd_rx_id": rx_id,
            "jd_vender_id": actual_vender,
            "jd_shop_name": actual_shop,
            "jd_rx_type": rx_type_number,
            "prescription_type": RX_TYPE_NAMES.get(rx_type_number, "普通"),
            "order_status": str(
                vo.get("rxStatusDesc") or vo.get("rxStatusName") or "审核通过"
            ),
            "image_index": int(selection.get("index") or 0),
            "image_url": str(selection.get("image_url") or ""),
            "image_source": str(selection.get("image_source") or ""),
            "physician_name": physician_name,
            "pharmacist_name": str(selection.get("pharmacist_name") or ""),
            "hospital_pharmacist_name": "",
            "prescription_time": prescription_time,
            "patient_info": patient_info,
            "drug_infos": drugs,
            "icd_name": str(ext.get("diagnoseResult") or ""),
            "department_name": str(ext.get("rxDepartment") or ""),
            "hospital_name": hospital_name,
            "source_hospital_name": source_hospital,
            "hospital_source_code": 1 if is_internal else 2,
            "total_fee": normalize_total_fee(
                vo.get("skuSum")
                if vo.get("skuSum") is not None
                else vo.get("orderSum")
            ),
            "prescription_validity": "该处方有效期为3天，若病情变化，请及时就医",
            "structured_fields_source": "jingdong_rx_detail_page",
            "image_role": "usage_and_duration_ocr",
            "image_status": "pending",
            "selection": {
                "reason": selection.get("reason"),
                "prescription_count": selection.get("prescription_count"),
                "selected_index": selection.get("index"),
                "unselected": selection.get("unselected") or [],
            },
            "collected_at": collected_at,
        },
    }


def choose_extension(url: str, content_type: str) -> str:
    match = re.search(r"\.(png|jpe?g|webp)(?:$|[?@])", urlparse(url).path, re.IGNORECASE)
    if match:
        ext = match.group(1).lower()
        return ".jpg" if ext == "jpeg" else f".{ext}"
    lowered = content_type.lower()
    if "jpeg" in lowered:
        return ".jpg"
    if "webp" in lowered:
        return ".webp"
    return ".png"


def download_image(
    url: str,
    image_dir: pathlib.Path,
    stem: str,
    retries: int,
) -> tuple[pathlib.Path, str]:
    last_error = "unknown error"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": HOME_URL}
    for attempt in range(1, max(1, retries) + 1):
        try:
            response = requests.get(url, headers=headers, timeout=(10, 60))
            response.raise_for_status()
            if not response.content:
                raise RuntimeError("empty image response")
            content_type = response.headers.get("content-type", "")
            if content_type and "image" not in content_type.lower():
                raise RuntimeError(f"unexpected image content-type: {content_type}")
            extension = choose_extension(url, content_type)
            path = image_dir / f"{safe_file_component(stem, 'prescription')}{extension}"
            restricted_write(path, response.content)
            return path, hashlib.sha256(response.content).hexdigest()
        except Exception as exc:
            last_error = str(exc)
            if attempt < max(1, retries):
                time.sleep(min(2**attempt, 8))
    raise RuntimeError(last_error)


class StateStore:
    def __init__(self, path: pathlib.Path):
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS prescriptions (
                prescription_key TEXT PRIMARY KEY,
                prescription_id TEXT NOT NULL,
                order_view_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                image_sha256 TEXT,
                run_id TEXT NOT NULL,
                cos_status TEXT NOT NULL DEFAULT 'pending',
                cos_url TEXT,
                api_status TEXT NOT NULL DEFAULT 'pending',
                api_message TEXT,
                api_uploaded_at TEXT,
                upload_attempts INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.commit()
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    def contains(self, key: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM prescriptions WHERE prescription_key=?", (key,)
        ).fetchone() is not None

    def mark(self, record: dict[str, Any], image_sha256: str, run_id: str) -> None:
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        self.connection.execute(
            """
            INSERT INTO prescriptions (
                prescription_key, prescription_id, order_view_id,
                first_seen_at, last_seen_at, image_sha256, run_id,
                cos_status, cos_url, api_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(prescription_key) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                image_sha256=excluded.image_sha256,
                run_id=excluded.run_id,
                cos_status=excluded.cos_status,
                cos_url=excluded.cos_url
            """,
            (
                record["prescription_key"],
                str(record.get("prescription_id") or ""),
                str(record.get("order_view_id") or ""),
                now,
                now,
                image_sha256,
                run_id,
                str(record.get("cos_status") or "pending"),
                str(record.get("image_file_url") or ""),
            ),
        )
        self.connection.commit()

    def mark_api(self, keys: Iterable[str], status: str, message: str) -> None:
        clean_keys = [str(key) for key in keys if str(key)]
        uploaded_at = (
            dt.datetime.now().astimezone().isoformat(timespec="seconds")
            if status in {"success", "uncertain_response"}
            else None
        )
        self.connection.executemany(
            """
            UPDATE prescriptions
            SET api_status=?, api_message=?, api_uploaded_at=?,
                upload_attempts=upload_attempts+1
            WHERE prescription_key=?
            """,
            [(status, message[:1000], uploaded_at, key) for key in clean_keys],
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class SingleInstance:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "SingleInstance":
        import fcntl

        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another collector instance is already running") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is not None:
            self.handle.close()


def purge_old_runs(
    runs_dir: pathlib.Path,
    retention_days: int = 0,
    retention_runs: int = 0,
) -> None:
    children = iter_run_dirs(runs_dir)
    remove: set[pathlib.Path] = set()
    if retention_days > 0:
        cutoff = time.time() - retention_days * 86400
        remove.update(child for child in children if child.stat().st_mtime < cutoff)
    if retention_runs > 0:
        remove.update(children[retention_runs:])
    for child in sorted(remove):
        shutil.rmtree(child)
    remove_empty_date_dirs(runs_dir)


@dataclass
class RunSummary:
    run_id: str
    date_range: str
    expected_vender_id: str = ""
    expected_shop_name: str = ""
    current_vender_id: str = ""
    current_shop_name: str = ""
    pages_scanned: int = 0
    total_pages: int = 0
    rx_ids: int = 0
    details_ok: int = 0
    detail_errors: int = 0
    selection_review: int = 0
    multi_prescription_orders_skipped: int = 0
    multi_prescription_orders_queued: int = 0
    new_prescriptions: int = 0
    duplicates: int = 0
    download_errors: int = 0
    ocr_success: int = 0
    ocr_no_usage: int = 0
    ocr_errors: int = 0
    ocr_no_text: int = 0
    ocr_low_confidence: int = 0
    usage_not_found: int = 0
    usage_parse_failed: int = 0
    drug_usage_match_failed: int = 0
    duration_calculation_failed: int = 0
    ocr_images: int = 0
    ocr_thread_limit: int = 2
    ocr_model_load_seconds: float = 0.0
    ocr_first_inference_seconds: float = 0.0
    ocr_warmup_seconds: float = 0.0
    ocr_inference_seconds: float = 0.0
    ocr_predict_calls: int = 0
    ocr_average_seconds: float = 0.0
    run_elapsed_seconds: float = 0.0
    duration_success: int = 0
    duration_needs_review: int = 0
    catalog_success: int = 0
    catalog_needs_review: int = 0
    internal_hospital_prescriptions: int = 0
    external_hospital_prescriptions: int = 0
    total_fee_success: int = 0
    total_fee_missing: int = 0
    cos_success: int = 0
    cos_errors: int = 0
    need_review_records: int = 0
    need_review_file: str = ""
    need_review_daily_records: int = 0
    need_review_added_records: int = 0
    need_review_updated_records: int = 0
    need_review_unchanged_records: int = 0
    platform_required_missing_records: int = 0
    platform_required_missing_field_counts: dict[str, int] = field(default_factory=dict)
    upload_json_records: int = 0
    upload_json_file: str = ""
    unmaintained_diagnosis_records: int = 0
    unmaintained_diagnosis_added_records: int = 0
    unmaintained_diagnosis_file: str = ""
    api_status: str = "not_requested"
    api_http_status: int = 0
    api_message: str = ""
    api_response_file: str = ""
    local_images_deleted: int = 0
    local_images_retained: int = 0
    local_image_cleanup_errors: int = 0
    status: str = "running"
    error: str = ""
    schema_version: int = 6
    status_code_version: int = STATUS_CODE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def apply_catalog_fallback(
    drug_infos: Iterable[dict[str, Any]],
    catalog: Optional[DrugCatalog],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    enriched: list[dict[str, Any]] = []
    processing: list[dict[str, Any]] = []
    catalog_success = 0
    catalog_review = 0
    for index, source in enumerate(drug_infos):
        drug = dict(source)
        if catalog is not None:
            catalog.apply_if_needed(drug)
        else:
            drug["status_code"] = status_code_for_drug(drug)
        code = int(drug.get("status_code") or status_code_for_drug(drug))
        if code in {
            STATUS_CATALOG_SUCCESS,
            STATUS_QUALITATIVE_30_DAY_SUCCESS,
        }:
            catalog_success += 1
        elif code >= 300:
            catalog_review += 1
        enriched.append(drug)
        processing.append(compact_processing_result(drug, index))
    return enriched, processing, catalog_success, catalog_review


def apply_api_result(summary: RunSummary, result: Any, response_file: str) -> None:
    summary.api_status = str(result.outcome)
    summary.api_http_status = int(result.http_status)
    summary.api_message = str(result.message or "")
    summary.api_response_file = response_file


def collect(config: dict[str, Any], start: Optional[str], end: Optional[str]) -> pathlib.Path:
    collect_started = time.perf_counter()
    output_dir = pathlib.Path(str(config["output_dir"])).expanduser().resolve()
    restricted_mkdir(output_dir)
    runs_dir = output_dir / "runs"
    restricted_mkdir(runs_dir)
    run_started_at = dt.datetime.now().astimezone()
    run_id = run_started_at.strftime("%Y%m%d_%H%M%S")
    date_dir = runs_dir / run_started_at.strftime("%Y%m%d")
    restricted_mkdir(date_dir)
    run_dir = date_dir / run_id
    restricted_mkdir(run_dir)
    image_dir = run_dir / "images"
    restricted_mkdir(image_dir)
    records_path = run_dir / "prescriptions.jsonl"
    review_path = run_dir / "review.jsonl"
    need_review_path = runs_dir / run_id[:8] / "need_review.json"
    upload_json_path = run_dir / "prescription_upload.json"
    api_response_path = run_dir / "api_response.json"
    summary_path = run_dir / "summary.json"
    health_path = output_dir / "last_run.json"

    start_date, end_date, label = parse_date_range(
        start, end, int(config.get("lookback_days") or 3)
    )
    expected_vender = str(config.get("expected_vender_id") or "").strip()
    expected_shop = str(config.get("expected_shop_name") or "").strip()
    summary = RunSummary(
        run_id=run_id,
        date_range=label,
        expected_vender_id=expected_vender,
        expected_shop_name=expected_shop,
    )
    atomic_json(summary_path, summary.as_dict())

    state = StateStore(output_dir / "state.sqlite3")
    usage_ocr: Optional[PaddleUsageOCR] = None
    catalog: Optional[DrugCatalog] = None
    need_review_records: list[dict[str, Any]] = []
    delivery_records: list[dict[str, Any]] = []
    delivery_keys: list[str] = []
    cos_enabled = bool(config.get("cos_upload_enabled", False))
    api_enabled = bool(config.get("api_submit_enabled", False))
    cos_settings: Optional[CosSettings] = None
    cos_client: Any = None

    try:
        if api_enabled and not cos_enabled:
            raise ValueError("api_submit_enabled requires cos_upload_enabled")
        if cos_enabled:
            cos_settings, cos_client = create_cos_client(CosSettings.from_env())
            prefix = str(config.get("cos_folder_prefix") or "").strip()
            if prefix:
                cos_settings = replace(cos_settings, folder_prefix=prefix)
        if bool(config.get("drug_catalog_enabled", True)):
            catalog = DrugCatalog.load(pathlib.Path(str(config["drug_catalog_index"])))

        with CdpPage(int(config["chrome_port"]), HOME_URL) as tab:
            jd_api = JdPrescriptionApi(
                JdBrowserTransport(
                    tab,
                    int(config.get("request_retries") or 3),
                )
            )
            rx_ids, scan = jd_api.enumerate_rx_ids(
                start_date,
                end_date,
                int(config.get("max_pages") or 1000),
                int(config.get("page_size") or 10),
                str(config.get("rx_status_type") or "2"),
            )
            summary.pages_scanned = scan["pages_scanned"]
            summary.total_pages = scan["total_pages"]
            summary.rx_ids = len(rx_ids)
            log(
                f"range={label} pages={summary.pages_scanned} rx_ids={summary.rx_ids}"
            )

            limit = int(config.get("limit") or 0)
            detail_attempts = 0
            consecutive_detail_errors = 0
            progress_store = f"JINGDONG:{expected_vender}"
            for rx_id in rx_ids:
                if limit and summary.new_prescriptions >= limit:
                    break
                detail_attempts += 1
                report_store_progress(
                    log,
                    store=progress_store,
                    processed=detail_attempts - 1,
                    total=len(rx_ids),
                    saved=summary.new_prescriptions,
                    review=len(need_review_records),
                    errors=(
                        summary.detail_errors
                        + summary.download_errors
                        + summary.ocr_errors
                        + summary.cos_errors
                    ),
                )
                try:
                    vo = jd_api.detail(rx_id).raw
                except JdAuthenticationError:
                    raise
                except Exception as exc:
                    summary.detail_errors += 1
                    consecutive_detail_errors += 1
                    append_jsonl(
                        review_path,
                        [
                            {
                                "status": "review",
                                "reason": "detail_request_or_parse_error",
                                "rx_id": rx_id,
                                "error": type(exc).__name__,
                            }
                        ],
                    )
                    failure_rate = summary.detail_errors / max(1, detail_attempts)
                    if consecutive_detail_errors >= 3:
                        raise RuntimeError(
                            "stopped after 3 consecutive JD detail failures"
                        ) from exc
                    if detail_attempts >= 10 and failure_rate > 0.20:
                        raise RuntimeError(
                            "stopped because JD detail failure rate exceeded 20%"
                        ) from exc
                    continue
                consecutive_detail_errors = 0
                if not isinstance(vo, dict) or not vo:
                    review = {
                        "platform": "JINGDONG",
                        "rx_id": rx_id,
                        "order_view_id": "",
                        "prescription_id": "",
                        "prescription_time": "",
                        "image_url": "",
                        "patient_info": {},
                        "icd_name": "",
                        "drug_infos": [],
                        "total_fee": None,
                    }
                    missing, sources = validate_platform_required_fields(review, "JINGDONG")
                    need_review_records.append(
                        attach_platform_required_review(review, "JINGDONG", missing, sources)
                    )
                    summary.platform_required_missing_records += 1
                    continue
                normalized = normalize_detail_record(
                    vo,
                    expected_vender,
                    expected_shop,
                    str(config.get("internal_hospital_name") or ""),
                    str(config.get("internal_output_hospital_name") or ""),
                    str(config.get("default_fee_type") or ""),
                )
                summary.details_ok += 1
                if normalized.get("status") == "skipped":
                    summary.multi_prescription_orders_skipped += 1
                    if append_multi_prescription_order(
                        platform="JINGDONG",
                        store_id=expected_vender,
                        store_name=expected_shop,
                        order_id=normalized.get("order_id") or rx_id,
                        prescription_count=normalized.get("prescription_count"),
                        prescription_ids=normalized.get("prescription_ids") or (),
                    ):
                        summary.multi_prescription_orders_queued += 1
                    continue
                if normalized.get("status") != "selected":
                    summary.selection_review += 1
                    review = dict(normalized)
                    review["platform"] = "JINGDONG"
                    review["order_view_id"] = review.get("order_id") or ""
                    review["prescription_id"] = review.get("prescription_id") or review.get("rx_no") or ""
                    review["prescription_time"] = review.get("prescription_time") or ""
                    review["image_url"] = review.get("image_url") or ""
                    review["patient_info"] = review.get("patient_info") or {}
                    review["icd_name"] = review.get("icd_name") or ""
                    review["drug_infos"] = review.get("drug_infos") or []
                    missing, sources = validate_platform_required_fields(review, "JINGDONG")
                    need_review_records.append(
                        attach_platform_required_review(review, "JINGDONG", missing, sources)
                    )
                    continue
                record = normalized["record"]
                if not summary.current_vender_id:
                    summary.current_vender_id = str(record.get("jd_vender_id") or "")
                    summary.current_shop_name = str(record.get("jd_shop_name") or "")
                if state.contains(str(record["prescription_key"])):
                    summary.duplicates += 1
                    continue
                missing, sources = validate_platform_required_fields(record, "JINGDONG")
                if missing:
                    need_review_records.append(
                        attach_platform_required_review(record, "JINGDONG", missing, sources)
                    )
                    continue

                if record.get("hospital_source_code") == 1:
                    summary.internal_hospital_prescriptions += 1
                else:
                    summary.external_hospital_prescriptions += 1
                if record.get("total_fee") is None:
                    summary.total_fee_missing += 1
                else:
                    summary.total_fee_success += 1

                try:
                    image_path, image_hash = download_image(
                        str(record.get("image_url") or ""),
                        image_dir,
                        str(record.get("prescription_id") or rx_id),
                        int(config.get("request_retries") or 3),
                    )
                except Exception as exc:
                    summary.download_errors += 1
                    record["image_status"] = "download_failed"
                    record["image_error"] = type(exc).__name__
                    append_jsonl(review_path, [record])
                    continue
                record["image_status"] = "downloaded"
                record["image_file"] = str(image_path.relative_to(run_dir))
                record["image_sha256"] = image_hash

                drugs = [dict(item) for item in (record.get("drug_infos") or [])]
                if bool(config.get("ocr_enabled", True)):
                    summary.ocr_images += 1
                    try:
                        if usage_ocr is None:
                            usage_ocr = PaddleUsageOCR(
                                min_confidence=float(config.get("ocr_min_confidence", 0.75)),
                                crop_top=float(config.get("ocr_crop_top_ratio", 0.34)),
                                crop_bottom=float(config.get("ocr_crop_bottom_ratio", 0.68)),
                                single_crop_top=float(
                                    config.get("ocr_single_crop_top_ratio", 0.38)
                                ),
                                single_crop_bottom=float(
                                    config.get("ocr_single_crop_bottom_ratio", 0.62)
                                ),
                                multi_crop_top=float(
                                    config.get("ocr_multi_crop_top_ratio", 0.34)
                                ),
                                multi_crop_bottom=float(
                                    config.get("ocr_multi_crop_bottom_ratio", 0.72)
                                ),
                                usage_preprocessing_mode=str(
                                    config.get("usage_ocr_preprocessing_mode", "grayscale")
                                ),
                                usage_grayscale_threshold=config.get(
                                    "usage_ocr_grayscale_threshold", 210
                                ),
                            )
                        drugs, metrics = usage_ocr.enrich(drugs, image_path)
                        record["usage_ocr"] = metrics
                        summary.ocr_success += int(metrics.get("usage_found") or 0)
                        summary.ocr_no_usage += sum(
                            1
                            for drug in drugs
                            if str(drug.get("usage_ocr_status") or "") == "not_found"
                        )
                        failure_counts = metrics.get("failure_counts") or {}
                        summary.ocr_no_text += int(
                            failure_counts.get("ocr_no_text") or 0
                        )
                        summary.ocr_low_confidence += int(
                            failure_counts.get("ocr_low_confidence") or 0
                        )
                        summary.usage_not_found += int(
                            failure_counts.get("usage_not_found") or 0
                        )
                        summary.usage_parse_failed += int(
                            failure_counts.get("usage_parse_failed") or 0
                        )
                        summary.drug_usage_match_failed += int(
                            failure_counts.get("drug_usage_match_failed") or 0
                        )
                        summary.duration_calculation_failed += int(
                            failure_counts.get("duration_calculation_failed") or 0
                        )
                    except Exception as exc:
                        summary.ocr_errors += 1
                        for drug in drugs:
                            drug["usage_source"] = "prescription_image_paddleocr"
                            drug["usage_ocr_status"] = "error"
                            drug["usage_failure_type"] = "ocr_runtime_error"
                            drug["duration_calculation_status"] = "needs_review"
                            drug["duration_calculation_reason"] = "ocr_runtime_error"
                        record["usage_ocr"] = {"status": "error", "error": type(exc).__name__}

                drugs, processing, catalog_success, catalog_review = apply_catalog_fallback(
                    drugs, catalog
                )
                restore_jd_purchase_fields(drugs)
                record["drug_infos"] = drugs
                record["processing"] = processing
                summary.catalog_success += catalog_success
                summary.catalog_needs_review += catalog_review
                summary.duration_success += sum(
                    1
                    for drug in drugs
                    if drug.get("duration_calculation_status") == "success"
                )
                summary.duration_needs_review += sum(
                    1
                    for drug in drugs
                    if drug.get("duration_calculation_status") != "success"
                )
                record["medicine_info"] = build_upload_medicine_info(drugs)

                record["cos_status"] = "pending" if cos_enabled else "disabled"
                if cos_enabled:
                    try:
                        cos_result = upload_image(
                            image_path,
                            str(record.get("prescription_id") or rx_id),
                            image_index=0,
                            settings=cos_settings,
                            client=cos_client,
                            platform=str(config.get("platform") or "JINGDONG"),
                            store_id=str(
                                config.get("expected_vender_id")
                                or record.get("jd_vender_id")
                                or ""
                            ),
                            business_date=(
                                record.get("prescription_time")
                                or record.get("collected_at")
                            ),
                        )
                    except Exception as exc:
                        summary.cos_errors += 1
                        record["cos_status"] = "failed"
                        record["cos_error"] = type(exc).__name__
                    else:
                        summary.cos_success += 1
                        record["cos_status"] = "success"
                        record["cos_object_key"] = cos_result["object_key"]
                        record["image_file_url"] = cos_result["image_file_url"]
                        record["cos_etag"] = cos_result["etag"]

                review_reasons = review_reasons_for_record(record)
                if review_reasons:
                    review_record = dict(record)
                    review_record["need_review_reasons"] = review_reasons
                    need_review_records.append(review_record)
                if bool(config.get("compact_processing_json", True)):
                    record.pop("drug_infos", None)
                append_jsonl(records_path, [record])
                state.mark(record, image_hash, run_id)
                if record.get("cos_status") == "success" and not review_reasons:
                    delivery_records.append(record)
                    delivery_keys.append(str(record["prescription_key"]))
                summary.new_prescriptions += 1

            report_store_progress(
                log,
                store=progress_store,
                processed=detail_attempts,
                total=len(rx_ids),
                saved=summary.new_prescriptions,
                review=len(need_review_records),
                errors=(
                    summary.detail_errors
                    + summary.download_errors
                    + summary.ocr_errors
                    + summary.cos_errors
                ),
                force=True,
            )

        if (
            summary.rx_ids > 0
            and summary.details_ok == 0
            and summary.platform_required_missing_records == 0
        ):
            raise RuntimeError("all JD detail requests failed or could not be parsed")

        review_merge = merge_daily_need_review(need_review_path, need_review_records)
        summary.need_review_records = len(need_review_records)
        summary.need_review_daily_records = review_merge.total
        summary.need_review_added_records = review_merge.added
        summary.need_review_updated_records = review_merge.updated
        summary.need_review_unchanged_records = review_merge.unchanged
        if need_review_path.is_file():
            summary.need_review_file = str(need_review_path.relative_to(output_dir))
        platform_reviews = [
            item
            for item in need_review_records
            if "platform_required_missing" in (item.get("need_review_reasons") or [])
        ]
        summary.platform_required_missing_records = len(platform_reviews)
        summary.platform_required_missing_field_counts = missing_field_counts(platform_reviews)

        if delivery_records:
            payload = build_interface_payload(
                delivery_records,
                str(config.get("hospital_name") or config.get("internal_hospital_name") or ""),
                source=str(config.get("source") or "京东"),
            )
            write_interface_json(upload_json_path, payload)
            summary.upload_json_records = len(delivery_records)
            summary.upload_json_file = str(upload_json_path.relative_to(output_dir))
            try:
                diagnosis_file, diagnosis_count, diagnosis_added = write_unmaintained_report(
                    upload_json_path,
                    output_dir=pathlib.Path(
                        str(config.get("diagnosis_output_dir") or DEFAULT_DIAGNOSIS_OUTPUT_DIR)
                    ),
                    db_path=pathlib.Path(
                        str(
                            config.get("diagnosis_maintenance_db")
                            or DEFAULT_DIAGNOSIS_MAINTENANCE_DB
                        )
                    ),
                    store_key=output_dir.name,
                    run_id=run_id,
                )
                summary.unmaintained_diagnosis_records = diagnosis_count
                summary.unmaintained_diagnosis_added_records = diagnosis_added
                summary.unmaintained_diagnosis_file = str(diagnosis_file)
            except Exception as exc:
                summary.unmaintained_diagnosis_file = f"failed: {exc}"
            if api_enabled:
                result = submit_json_file(
                    upload_json_path,
                    str(config["api_url"]),
                    str(config.get("platform") or "JINGDONG"),
                    str(config.get("app_code") or "pachong"),
                    int(config.get("api_timeout_seconds") or 300),
                )
                atomic_json(api_response_path, response_snapshot(result))
                apply_api_result(
                    summary, result, str(api_response_path.relative_to(output_dir))
                )
                state.mark_api(delivery_keys, summary.api_status, result.message)
                if result.outcome == "failed":
                    raise RuntimeError(
                        f"prescription API submission failed: {result.message}"
                    )
            else:
                summary.api_status = "ready_not_submitted"
        elif need_review_records:
            summary.api_status = "review_only_not_submitted"
        elif api_enabled and summary.new_prescriptions > 0:
            summary.api_status = "blocked_no_cos_images"
            raise RuntimeError("no prescriptions have a verified COS image URL")
        elif api_enabled:
            summary.api_status = "no_new_records"

        summary.status = (
            "needs_review"
            if summary.selection_review > 0
            or summary.detail_errors > 0
            or summary.download_errors > 0
            or summary.duration_needs_review > 0
            or summary.catalog_needs_review > 0
            or summary.need_review_records > 0
            or summary.cos_errors > 0
            or summary.api_status == "uncertain_response"
            else "success"
        )
    except Exception as exc:
        summary.status = "failed"
        summary.error = str(exc)
        raise
    finally:
        if usage_ocr is not None:
            for key, value in usage_ocr.timing_metrics(summary.ocr_images).items():
                setattr(summary, key, value)
        with contextlib.suppress(OSError):
            image_dir.rmdir()
        summary.run_elapsed_seconds = round(time.perf_counter() - collect_started, 6)
        state.close()
        image_cleanup = cleanup_local_images(
            runs_dir,
            int(config.get("image_retention_runs") or 0),
        )
        summary.local_images_deleted = image_cleanup.deleted
        summary.local_images_retained = image_cleanup.retained
        summary.local_image_cleanup_errors = image_cleanup.errors
        atomic_json(summary_path, summary.as_dict())
        atomic_json(health_path, summary.as_dict())
        purge_old_runs(
            runs_dir,
            int(config.get("retention_days") or 0),
            int(config.get("retention_runs") or 0),
        )

    log(
        f"completed new={summary.new_prescriptions} duplicates={summary.duplicates} "
        f"selection_review={summary.selection_review} "
        f"multi_order_skipped={summary.multi_prescription_orders_skipped} "
        f"multi_order_queued={summary.multi_prescription_orders_queued} "
        f"detail_errors={summary.detail_errors} "
        f"download_errors={summary.download_errors} ocr_success={summary.ocr_success} "
        f"ocr_images={summary.ocr_images} "
        f"ocr_model_load_seconds={summary.ocr_model_load_seconds} "
        f"ocr_warmup_seconds={summary.ocr_warmup_seconds} "
        f"ocr_inference_seconds={summary.ocr_inference_seconds} "
        f"run_elapsed_seconds={summary.run_elapsed_seconds} "
        f"duration_success={summary.duration_success} catalog_success={summary.catalog_success} "
        f"need_review={summary.need_review_records} "
        f"platform_required_missing={summary.platform_required_missing_records} "
        f"cos_success={summary.cos_success} api_status={summary.api_status}"
    )
    return summary_path


def discover_store(config: dict[str, Any]) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {
        "platform": "JINGDONG",
        "logged_in": False,
        "store_discovered": False,
    }
    try:
        check_start, check_end, _ = parse_date_range(
            None,
            None,
            int(config.get("lookback_days") or 3),
        )
        with CdpPage(int(config["chrome_port"]), HOME_URL) as tab:
            jd_api = JdPrescriptionApi(
                JdBrowserTransport(
                    tab,
                    int(config.get("request_retries") or 3),
                )
            )
            page = jd_api.list_page(
                check_start,
                check_end,
                1,
                1,
                str(config.get("rx_status_type") or "2"),
            )
            result["logged_in"] = True
            rx_ids = page.get("rx_ids") or []
            if not rx_ids:
                result["message"] = (
                    "login is valid, but no detail sample was available for store discovery"
                )
                return result, 2
            vo = jd_api.detail(str(rx_ids[0])).raw
            vender, shop = extract_store_identity(vo)
            result.update(
                {
                    "current_vender_id": vender,
                    "current_shop_name": shop,
                    "store_discovered": True,
                }
            )
            return result, 0
    except Exception as exc:
        result["message"] = str(exc)
        return result, 2


def check_login(config: dict[str, Any]) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {
        "logged_in": False,
        "expected_vender_id": str(config.get("expected_vender_id") or ""),
        "expected_shop_name": str(config.get("expected_shop_name") or ""),
        "store_match": False,
        "store_verified": False,
    }
    try:
        check_start, check_end, _ = parse_date_range(
            None,
            None,
            int(config.get("lookback_days") or 3),
        )
        with CdpPage(int(config["chrome_port"]), HOME_URL) as tab:
            jd_api = JdPrescriptionApi(
                JdBrowserTransport(
                    tab,
                    int(config.get("request_retries") or 3),
                )
            )
            page = jd_api.list_page(
                check_start,
                check_end,
                1,
                1,
                str(config.get("rx_status_type") or "2"),
            )
            result["logged_in"] = True
            rx_ids = page.get("rx_ids") or []
            if not rx_ids:
                result["message"] = "login is valid, but no detail sample was available for store verification"
                return result, 2
            vo = jd_api.detail(str(rx_ids[0])).raw
            vender, shop = validate_store_context(
                vo,
                config.get("expected_vender_id"),
                config.get("expected_shop_name"),
            )
            result.update(
                {
                    "current_vender_id": vender,
                    "current_shop_name": shop,
                    "store_match": True,
                    "store_verified": True,
                }
            )
            return result, 0
    except Exception as exc:
        result["message"] = str(exc)
        return result, 2


def main() -> int:
    os.chdir(pathlib.Path(__file__).resolve().parent)
    parser = argparse.ArgumentParser(
        description="Collect authorized JD merchant prescriptions and supplementary images."
    )
    parser.add_argument("--config", default="jd-config.json")
    parser.add_argument("--start", help="start audit date YYYY-MM-DD")
    parser.add_argument("--end", help="end audit date YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="override maximum new prescriptions; 0 means unlimited")
    parser.add_argument("--skip-api-submit", action="store_true")
    parser.add_argument("--check-login", action="store_true")
    parser.add_argument(
        "--discover-store",
        action="store_true",
        help="read the logged-in store identity without collecting or submitting data",
    )
    args = parser.parse_args()

    config_path = pathlib.Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    catalog_path = pathlib.Path(str(config.get("drug_catalog_index") or "./drug_catalog_index.json"))
    if not catalog_path.is_absolute():
        catalog_path = (config_path.parent / catalog_path).resolve()
    config["drug_catalog_index"] = str(catalog_path)
    if args.limit is not None:
        config["limit"] = max(0, args.limit)
    if args.skip_api_submit:
        config["api_submit_enabled"] = False

    if args.discover_store:
        result, exit_code = discover_store(config)
        print(json.dumps(result, ensure_ascii=False))
        return exit_code

    if args.check_login:
        result, exit_code = check_login(config)
        print(json.dumps(result, ensure_ascii=False))
        return exit_code

    output_dir = pathlib.Path(str(config["output_dir"])).expanduser().resolve()
    restricted_mkdir(output_dir)
    try:
        with SingleInstance(output_dir / ".collector.lock"):
            summary_path = collect(config, args.start, args.end)
    except Exception as exc:
        log(f"failed: {exc}")
        return 1
    print(f"SUMMARY_PATH={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

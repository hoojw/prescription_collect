#!/usr/bin/env python3
"""Web-field-first Meituan prescription collector for an authorized merchant account.

The collector attaches to a Chrome instance that already has an authorized
Meituan merchant login. It executes same-origin requests inside that browser,
stores structured order-detail fields, and downloads prescription images as
supplementary evidence.

It does not automate login, CAPTCHA, account switching, or anti-bot bypasses.
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
import sys
import time
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
from prescription_usage_ocr import (
    PaddleUsageOCR,
    build_upload_medicine_info,
    merge_same_drugs,
)
from cos_image_upload import CosSettings, create_cos_client, upload_image
from multi_prescription_queue import append_multi_prescription_order
from prescription_delivery import (
    build_interface_payload,
    review_reasons_for_record,
    response_snapshot,
    submit_json_file,
    write_interface_json,
)
from drug_catalog import (
    STATUS_CATALOG_SUCCESS,
    STATUS_QUALITATIVE_30_DAY_SUCCESS,
    STATUS_CODE_VERSION,
    DrugCatalog,
    compact_processing_result,
    status_code_for_drug,
)
from medicine_sku import (
    DRCODE_AMBIGUOUS,
    DRCODE_DISABLED,
    DRCODE_STATUS_VERSION,
    MedicineSkuIndex,
)
from meituan_product_catalog import refresh_sku_index
from platform_required_validation import (
    attach_platform_required_review,
    missing_field_counts,
    validate_platform_required_fields,
)


HOME_URL = "https://yiyao.meituan.com/main/frame#/page/home"

DEFAULT_CONFIG = {
    "chrome_port": 9223,
    "expected_poi_id": "",
    "output_dir": "./data",
    "lookback_days": 3,
    "max_pages": 20,
    "page_size": 50,
    "statuses": [],
    "limit": 0,
    "request_retries": 3,
    "retention_days": 0,
    "retention_runs": 3,
    "image_retention_runs": 0,
    "ocr_enabled": True,
    "ocr_min_confidence": 0.75,
    "ocr_crop_top_ratio": 0.30,
    "ocr_crop_bottom_ratio": 0.70,
    "ocr_single_crop_top_ratio": 0.35,
    "ocr_single_crop_bottom_ratio": 0.53,
    "ocr_multi_crop_top_ratio": 0.35,
    "ocr_multi_crop_bottom_ratio": 0.70,
    "usage_ocr_preprocessing_mode": "blue_mask",
    "header_ocr_enabled": True,
    "header_ocr_crop_left_ratio": 0.48,
    "header_ocr_crop_right_ratio": 0.96,
    "header_ocr_crop_top_ratio": 0.20,
    "header_ocr_crop_bottom_ratio": 0.35,
    "hospital_ocr_enabled": True,
    "hospital_ocr_crop_left_ratio": 0.10,
    "hospital_ocr_crop_right_ratio": 0.90,
    "hospital_ocr_crop_top_ratio": 0.02,
    "hospital_ocr_crop_bottom_ratio": 0.20,
    "header_ocr_grayscale_threshold": 225,
    "hospital_ocr_grayscale_threshold": 140,
    "cos_upload_enabled": False,
    "cos_folder_prefix": "",
    "api_submit_enabled": False,
    "hospital_name": "天津北辰保民互联网医院",
    "api_url": "https://testapi.yizhujk.com/public/prescriptionUpload/importJson",
    "platform": "MEITUAN",
    "app_code": "pachong",
    "api_timeout_seconds": 300,
    "drug_catalog_enabled": True,
    "drug_catalog_index": "./drug_catalog_index.json",
    "compact_processing_json": True,
    "medicine_sku_enabled": True,
    "medicine_sku_index": "./medicine_sku_index.json",
    "medicine_sku_refresh_enabled": False,
    "medicine_sku_refresh_page_size": 100,
    "medicine_sku_refresh_max_pages": 1000,
    "medicine_sku_cache_ttl_hours": 72,
    "medicine_sku_refresh_retries": 2,
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


def load_config(path: pathlib.Path) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config must be a JSON object")
        config.update(loaded)

    env_map = {
        "MEITUAN_CHROME_PORT": ("chrome_port", int),
        "MEITUAN_EXPECTED_POI_ID": ("expected_poi_id", str),
        "MEITUAN_OUTPUT_DIR": ("output_dir", str),
        "MEITUAN_LOOKBACK_DAYS": ("lookback_days", int),
        "MEITUAN_MAX_PAGES": ("max_pages", int),
        "MEITUAN_COS_FOLDER_PREFIX": ("cos_folder_prefix", str),
    }
    for env_name, (key, convert) in env_map.items():
        value = os.environ.get(env_name)
        if value not in (None, ""):
            config[key] = convert(value)
    return config


def safe_file_component(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "")).strip("._")
    return cleaned[:120] or fallback


def strip_meituan_expiry_suffix(value: Any) -> str:
    """Backward-compatible alias for the shared expiry cleanup rule."""
    return strip_expiry_suffix(value)


def normalize_meituan_drug(source: Any) -> Any:
    """Clean Meituan web fields while retaining changed source values for audit."""
    if not isinstance(source, dict):
        return source

    drug = dict(source)
    raw_name = str(drug.get("drugName") or "").strip()
    cleaned_name = strip_meituan_expiry_suffix(raw_name)
    if raw_name and cleaned_name != raw_name:
        drug["source_drug_name"] = raw_name
        drug["drugName"] = cleaned_name

    raw_spec = str(drug.get("packingSpec") or "").strip()
    cleaned_spec = strip_meituan_expiry_suffix(raw_spec)
    if raw_spec and cleaned_spec != raw_spec:
        drug["source_packing_spec"] = raw_spec
        drug["packingSpec"] = cleaned_spec

    return drug


def normalize_meituan_drugs(value: Any) -> list[Any]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [normalize_meituan_drug(item) for item in value]


def normalize_total_fee(value: Any) -> Optional[float]:
    """Normalize Meituan orderResult.total as yuan decimal(8,2)."""
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
) -> tuple[int, int, str]:
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
    tz = dt.datetime.now().astimezone().tzinfo
    start_dt = dt.datetime.combine(start_date, dt.time.min, tzinfo=tz)
    end_dt = dt.datetime.combine(end_date, dt.time.max, tzinfo=tz)
    return int(start_dt.timestamp()), int(end_dt.timestamp()), f"{start_date}..{end_date}"


class CdpPage:
    """Small Chrome DevTools client; creates one temporary tab and closes it."""

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


def run_json(tab: CdpPage, expression: str) -> dict[str, Any]:
    raw = tab.run_js(expression)
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("browser request did not return JSON; login may be expired") from exc
    else:
        value = raw
    if not isinstance(value, dict):
        raise RuntimeError(f"expected browser JSON object, got {type(value).__name__}")
    return value


def check_login(tab: CdpPage) -> bool:
    script = r"""
    (() => {
      var x = new XMLHttpRequest();
      x.open('GET', '/queryMenu', false);
      x.send();
      try {
        var j = JSON.parse(x.responseText);
        return JSON.stringify({httpStatus:x.status, code:j.code});
      } catch (e) {
        return JSON.stringify({httpStatus:x.status, code:null});
      }
    })()
    """
    navigation_errors = (
        "inspected target navigated or closed",
        "execution context was destroyed",
        "cannot find context with specified id",
    )
    for attempt in range(3):
        try:
            result = run_json(tab, script)
            return result.get("httpStatus") == 200 and result.get("code") == 0
        except RuntimeError as exc:
            message = str(exc).lower()
            if attempt >= 2 or not any(marker in message for marker in navigation_errors):
                raise
            time.sleep(0.5 * (attempt + 1))
    return False


def current_poi_id(tab: CdpPage) -> str:
    script = r"""
    (() => {
      function getc(n) {
        var m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]*)'));
        return m ? decodeURIComponent(m[1]) : '';
      }
      return getc('wmPoiId') || getc('poi_id') || '';
    })()
    """
    return str(tab.run_js(script) or "").strip()


def validate_store_context(actual_poi_id: Any, expected_poi_id: Any) -> str:
    actual = str(actual_poi_id or "").strip()
    expected = str(expected_poi_id or "").strip()
    if not expected:
        raise RuntimeError("expected_poi_id is required for store-safe collection")
    if not actual:
        raise RuntimeError("current browser store poiId is missing")
    if actual != expected:
        raise RuntimeError(
            f"browser store mismatch: expected poiId={expected}, current poiId={actual}"
        )
    return actual


def validate_browser_store(tab: CdpPage, expected_poi_id: Any) -> str:
    if not check_login(tab):
        raise RuntimeError("Meituan login is missing or expired; manual re-login is required")
    return validate_store_context(current_poi_id(tab), expected_poi_id)


def list_orders_page(
    tab: CdpPage,
    start_ts: int,
    end_ts: int,
    page: int,
    page_size: int,
    cursor: Any,
) -> dict[str, Any]:
    cursor_literal = json.dumps(cursor, ensure_ascii=False)
    script = f"""
    (() => {{
      function getc(n) {{
        var m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]*)'));
        return m ? decodeURIComponent(m[1]) : '';
      }}
      var poi = getc('wmPoiId') || getc('poi_id');
      var token = getc('token');
      var acctId = getc('acctId') || getc('acct_id');
      var acctName = getc('acct_name');
      var x = new XMLHttpRequest();
      x.open('POST', '/health/b2c/order/list/search?wmPoiId=' + encodeURIComponent(poi) + '&bizScene=pc&sceneType=103', false);
      x.setRequestHeader('content-type', 'application/json');
      x.send(JSON.stringify({{
        token: token, acctId: acctId, wmPoiId: poi, acctName: acctName,
        startOrderTime: {int(start_ts)}, endOrderTime: {int(end_ts)},
        page: {int(page)}, size: {int(page_size)}, cursor: {cursor_literal}, ascOrder: false
      }}));
      return JSON.stringify({{httpStatus:x.status, body:x.responseText}});
    }})()
    """
    envelope = run_json(tab, script)
    if envelope.get("httpStatus") != 200:
        raise RuntimeError(f"order list HTTP {envelope.get('httpStatus')}")
    body = envelope.get("body")
    payload = json.loads(body) if isinstance(body, str) else body
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise RuntimeError(f"order list API rejected request: code={getattr(payload, 'get', lambda *_: None)('code')}")
    data = payload.get("data") or {}
    content = data.get("content") or []
    rows: list[dict[str, Any]] = []
    for item in content:
        order = (item or {}).get("orderResult") or {}
        if not order.get("isRxOrder"):
            continue
        rows.append(
            {
                "order_view_id": str(order.get("orderViewId") or ""),
                "status_desc": str(order.get("b2cOrderMainStatusDesc") or ""),
                "has_prescription_detail": bool(order.get("hasPrescriptionDetail")),
                "poi_id": str(order.get("poiId") or ""),
                "total_fee": normalize_total_fee(order.get("total")),
            }
        )
    return {
        "api_total": int(data.get("total") or 0),
        "total_pages": int(data.get("totalPage") or 0),
        "cursor": data.get("cursor") or "",
        "content_count": len(content),
        "orders": rows,
    }


def enumerate_rx_orders(
    tab: CdpPage,
    start_ts: int,
    end_ts: int,
    max_pages: int,
    page_size: int,
    statuses: set[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    orders: list[dict[str, Any]] = []
    seen_orders: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: Any = ""
    api_total = total_pages = pages_scanned = 0

    for page in range(1, max_pages + 1):
        result = list_orders_page(tab, start_ts, end_ts, page, page_size, cursor)
        pages_scanned += 1
        api_total = result["api_total"]
        total_pages = result["total_pages"]
        for order in result["orders"]:
            order_id = order["order_view_id"]
            if not order_id or order_id in seen_orders:
                continue
            if statuses and order["status_desc"] not in statuses:
                continue
            seen_orders.add(order_id)
            orders.append(order)

        next_cursor = result.get("cursor") or ""
        cursor_key = json.dumps(next_cursor, ensure_ascii=False, sort_keys=True)
        if result["content_count"] == 0:
            break
        if total_pages and page >= total_pages:
            break
        if not next_cursor or cursor_key in seen_cursors:
            break
        seen_cursors.add(cursor_key)
        cursor = next_cursor

    return orders, {
        "api_total": api_total,
        "total_pages": total_pages,
        "pages_scanned": pages_scanned,
    }


def get_prescription_detail(tab: CdpPage, order_view_id: str, retries: int) -> dict[str, Any]:
    quoted_id = json.dumps(str(order_view_id))
    script = f"""
    (() => {{
      function getc(n) {{
        var m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]*)'));
        return m ? decodeURIComponent(m[1]) : '';
      }}
      var poi = getc('wmPoiId') || getc('poi_id');
      var x = new XMLHttpRequest();
      x.open('GET', '/v1/prescription/history/detail/query?orderViewId=' + encodeURIComponent({quoted_id}) + '&wmPoiId=' + encodeURIComponent(poi), false);
      x.send();
      return JSON.stringify({{httpStatus:x.status, body:x.responseText}});
    }})()
    """
    last_error = "unknown error"
    for attempt in range(1, max(1, retries) + 1):
        try:
            envelope = run_json(tab, script)
            if envelope.get("httpStatus") != 200:
                raise RuntimeError(f"HTTP {envelope.get('httpStatus')}")
            body = envelope.get("body")
            payload = json.loads(body) if isinstance(body, str) else body
            if not isinstance(payload, dict):
                raise RuntimeError("detail response is not an object")
            if payload.get("code") != 0:
                raise RuntimeError(f"API code={payload.get('code')} message={payload.get('msg') or payload.get('message')}")
            infos = (payload.get("data") or {}).get("prescriptionInfos") or []
            return {"status": "ok" if infos else "no_image", "payload": payload, "infos": infos}
        except Exception as exc:  # retry a transient session/backend failure
            last_error = str(exc)
            if attempt < max(1, retries):
                time.sleep(min(2**attempt, 8))
    return {"status": "error", "reason": last_error, "infos": []}


def as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def aggregate_usage_ocr_status(drug_infos: Iterable[dict[str, Any]]) -> str:
    statuses = [
        str(drug.get("usage_ocr_status") or "not_found")
        for drug in drug_infos
        if isinstance(drug, dict)
    ]
    if statuses and all(status == "success" for status in statuses):
        return "success"
    if statuses and all(status == "low_confidence" for status in statuses):
        return "low_confidence"
    if any(status in {"success", "low_confidence"} for status in statuses):
        return "partial"
    return "not_found"


def apply_catalog_fallback(
    drug_infos: Iterable[dict[str, Any]],
    catalog: Optional[DrugCatalog],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Apply the strict catalog only when OCR dose or frequency is missing."""
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


def apply_sku_matches(
    drug_infos: Iterable[dict[str, Any]],
    sku_index: Optional[MedicineSkuIndex],
    poi_id: Any,
) -> tuple[int, int, int]:
    success = missing = ambiguous = 0
    for drug in drug_infos:
        if sku_index is None:
            drug["drcode"] = ""
            drug["drcode_status"] = DRCODE_DISABLED
        else:
            sku_index.apply(drug, poi_id)
        status = int(drug.get("drcode_status") or DRCODE_DISABLED)
        if 1 <= status <= 4 and drug.get("drcode"):
            success += 1
        else:
            missing += 1
            if status == DRCODE_AMBIGUOUS:
                ambiguous += 1
    return success, missing, ambiguous


def apply_patient_header_fields(
    record: dict[str, Any],
    header_result: dict[str, Any],
) -> dict[str, bool]:
    """Fill missing patient fields from successful OCR without overwriting web data."""
    patient = record.get("patient_info")
    if not isinstance(patient, dict):
        patient = {}
        record["patient_info"] = patient
    sources = record.get("patient_info_field_sources")
    if not isinstance(sources, dict):
        sources = {}
        record["patient_info_field_sources"] = sources

    birth_keys = ("birth_date", "birthDate", "birthday", "medicalUserBirthday")
    web_birth = next((patient.get(key) for key in birth_keys if patient.get(key)), "")
    web_department = (
        next(
            (patient.get(key) for key in ("department", "departmentName") if patient.get(key)),
            "",
        )
        or record.get("department_name")
        or ""
    )
    if web_birth:
        sources["birth_date"] = "meituan_order_detail_api"
    if web_department:
        sources["department"] = "meituan_order_detail_api"

    applied = {"birth_date": False, "department": False}
    if not web_birth and header_result.get("birth_date_ocr_status") == "success":
        value = str(header_result.get("birth_date") or "")
        if value:
            patient["birth_date"] = value
            sources["birth_date"] = "prescription_image_paddleocr"
            applied["birth_date"] = True
    if not web_department and header_result.get("department_ocr_status") == "success":
        value = str(header_result.get("department") or "")
        if value:
            patient["department"] = value
            sources["department"] = "prescription_image_paddleocr"
            applied["department"] = True

    record["patient_field_ocr"] = header_result
    return applied


def normalize_prescriptions(
    order: dict[str, Any],
    detail: dict[str, Any],
) -> list[dict[str, Any]]:
    payload = detail.get("payload") or {}
    detail_data = payload.get("data") or {}
    if not isinstance(detail_data, dict):
        detail_data = {}
    shared_patient_info = detail_data.get("patientInfo") or {}
    shared_drug_infos = detail_data.get("drugInfos") or []
    infos = detail.get("infos") or []
    if isinstance(infos, dict):
        infos = [infos]
    records: list[dict[str, Any]] = []
    seen_record_identities: set[tuple[str, str]] = set()
    for info_index, info in enumerate(infos):
        if not isinstance(info, dict):
            continue
        ids = []
        for raw_id in as_list(
            info.get("prescriptionIds")
            or info.get("prescriptionId")
            or info.get("rpId")
            or detail_data.get("prescriptionIds")
        ):
            ids.extend(
                part.strip()
                for part in re.split(r"[,，;；]+", str(raw_id or ""))
                if part.strip()
            )
        urls = as_list(info.get("prescriptionUrl") or info.get("prescriptionUrls"))
        if len(infos) > 1 and len(urls) == 1 and len(ids) == len(infos):
            ids = [ids[info_index]]
        count = max(len(ids), len(urls), 1)
        for image_index in range(count):
            prescription_id = str(ids[image_index] if image_index < len(ids) else (ids[0] if ids else ""))
            image_url = str(urls[image_index] if image_index < len(urls) else (urls[0] if urls else ""))
            record_identity = (prescription_id, image_url)
            if record_identity != ("", ""):
                if record_identity in seen_record_identities:
                    continue
                seen_record_identities.add(record_identity)
            if count > 1:
                key_seed = f"{prescription_id}:{info_index}:{image_index}:{image_url}"
            else:
                key_seed = prescription_id or f"{order['order_view_id']}:{info_index}:{image_index}:{image_url}"
            record: dict[str, Any] = {
                "prescription_key": hashlib.sha256(key_seed.encode("utf-8")).hexdigest(),
                "prescription_id": prescription_id,
                "order_view_id": order["order_view_id"],
                "order_status": order.get("status_desc", ""),
                "image_index": image_index,
                "image_url": image_url,
                "physician_name": info.get("physicianName") or "",
                "pharmacist_name": info.get("pharmacistName") or detail_data.get("pharmacistName") or "",
                "prescription_time": info.get("prescriptionTime") or "",
                "prescription_type": info.get("prescriptionType") or "",
                "patient_info": shared_patient_info or info.get("patientInfo") or {},
                "drug_infos": normalize_meituan_drugs(
                    shared_drug_infos or info.get("drugInfos") or []
                ),
                "audit_view_id": detail_data.get("auditViewId") or "",
                "audit_time": detail_data.get("auditTime") or "",
                "audit_reason": detail_data.get("auditReason") or "",
                "audit_status": detail_data.get("auditStatus"),
                "privacy_phone": detail_data.get("privacyPhone") or "",
                "rp_type": detail_data.get("rpType"),
                "city_name": detail_data.get("cityName") or "",
                "poi_name": detail_data.get("poiName") or "",
                "poi_id": detail_data.get("poiId") or order.get("poi_id") or "",
                "total_fee": order.get("total_fee"),
                "department_name": detail_data.get("departmentName") or "",
                "disposal_opinions": detail_data.get("disposalOpinions") or "",
                "icd_name": detail_data.get("icdName") or "",
                "dosage": detail_data.get("dosage") or "",
                "hospital_pharmacist_name": detail_data.get("hospitalPharmacistName") or "",
                "structured_fields_source": "meituan_order_detail_api",
                "image_role": "field_supplement_and_verification",
                "image_status": "pending" if image_url else "not_available",
                "collected_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            records.append(record)
    return records


def is_multi_prescription_order(records: Sequence[dict[str, Any]]) -> bool:
    """Whether an order must be skipped because the platform returned multiple prescriptions."""
    return len(records) > 1


def choose_extension(url: str, content_type: str) -> str:
    match = re.search(r"\.(png|jpe?g|webp)(?:@|$)", urlparse(url).path, re.IGNORECASE)
    if match:
        ext = match.group(1).lower()
        return ".jpg" if ext == "jpeg" else f".{ext}"
    content_type = content_type.lower()
    if "jpeg" in content_type:
        return ".jpg"
    if "webp" in content_type:
        return ".webp"
    return ".png"


def download_image(url: str, image_dir: pathlib.Path, stem: str, retries: int) -> tuple[pathlib.Path, str]:
    last_error = "unknown error"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": HOME_URL}
    for attempt in range(1, max(1, retries) + 1):
        try:
            response = requests.get(url, headers=headers, timeout=(10, 60))
            response.raise_for_status()
            if not response.content:
                raise RuntimeError("empty image response")
            extension = choose_extension(url, response.headers.get("content-type", ""))
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
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS prescriptions (
                prescription_key TEXT PRIMARY KEY,
                prescription_id TEXT,
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
        existing_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(prescriptions)").fetchall()
        }
        migrations = {
            "cos_status": "TEXT NOT NULL DEFAULT 'pending'",
            "cos_url": "TEXT",
            "api_status": "TEXT NOT NULL DEFAULT 'pending'",
            "api_message": "TEXT",
            "api_uploaded_at": "TEXT",
            "upload_attempts": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in migrations.items():
            if column not in existing_columns:
                self.connection.execute(
                    f"ALTER TABLE prescriptions ADD COLUMN {column} {definition}"
                )
        self.connection.commit()
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    def contains(self, key: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM prescriptions WHERE prescription_key=?", (key,)
        ).fetchone()
        return row is not None

    def contains_order(self, order_view_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM prescriptions WHERE order_view_id=? LIMIT 1",
            (order_view_id,),
        ).fetchone()
        return row is not None

    def mark(self, record: dict[str, Any], image_sha256: str, run_id: str) -> None:
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        self.connection.execute(
            """
            INSERT INTO prescriptions (
                prescription_key, prescription_id, order_view_id,
                first_seen_at, last_seen_at, image_sha256, run_id,
                cos_status, cos_url, api_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(prescription_key) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                image_sha256=excluded.image_sha256,
                run_id=excluded.run_id,
                cos_status=excluded.cos_status,
                cos_url=excluded.cos_url
            """,
            (
                record["prescription_key"],
                record.get("prescription_id", ""),
                record["order_view_id"],
                now,
                now,
                image_sha256,
                run_id,
                str(record.get("cos_status") or "pending"),
                str(record.get("image_file_url") or ""),
                "pending",
            ),
        )
        self.connection.commit()

    def mark_api(
        self,
        prescription_keys: Iterable[str],
        status: str,
        message: str,
    ) -> None:
        keys = [str(key) for key in prescription_keys if str(key)]
        if not keys:
            return
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
            [(status, message[:1000], uploaded_at, key) for key in keys],
        )
        self.connection.commit()

    def delivery_state(self, key: str) -> Optional[dict[str, Any]]:
        row = self.connection.execute(
            """
            SELECT cos_status, cos_url, api_status, api_message,
                   api_uploaded_at, upload_attempts
            FROM prescriptions WHERE prescription_key=?
            """,
            (key,),
        ).fetchone()
        if row is None:
            return None
        names = (
            "cos_status",
            "cos_url",
            "api_status",
            "api_message",
            "api_uploaded_at",
            "upload_attempts",
        )
        return dict(zip(names, row))

    def close(self) -> None:
        self.connection.close()


class SingleInstance:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "SingleInstance":
        self.handle = self.path.open("a+b")
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)
        self.handle.seek(0)
        if self.handle.read(1) == b"":
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another collector run is still active") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle:
            self.handle.close()


def append_jsonl(path: pathlib.Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def purge_old_runs(
    runs_dir: pathlib.Path,
    retention_days: int = 0,
    retention_runs: int = 0,
) -> list[pathlib.Path]:
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
    return sorted(remove)


@dataclass
class RunSummary:
    run_id: str
    date_range: str
    expected_poi_id: str = ""
    current_poi_id: str = ""
    pages_scanned: int = 0
    api_total: int = 0
    rx_orders: int = 0
    details_ok: int = 0
    no_image: int = 0
    detail_errors: int = 0
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
    total_fee_success: int = 0
    total_fee_missing: int = 0
    drcode_success: int = 0
    drcode_missing: int = 0
    drcode_ambiguous: int = 0
    sku_refresh_status: str = "not_requested"
    sku_refresh_products: int = 0
    sku_refresh_entries: int = 0
    sku_refresh_pages: int = 0
    sku_refresh_seconds: float = 0.0
    sku_refresh_cache_age_seconds: Optional[float] = None
    sku_refresh_error: str = ""
    birth_date_ocr_success: int = 0
    department_ocr_success: int = 0
    birth_date_ocr_applied: int = 0
    department_ocr_applied: int = 0
    patient_field_ocr_low_confidence: int = 0
    patient_field_ocr_not_found: int = 0
    patient_field_ocr_errors: int = 0
    internal_hospital_prescriptions: int = 0
    external_hospital_prescriptions: int = 0
    hospital_source_needs_review: int = 0
    hospital_ocr_errors: int = 0
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
    api_response_success: Optional[bool] = None
    api_err_code: Any = None
    api_task_id: str = ""
    api_total_count: Optional[int] = None
    api_success_count: Optional[int] = None
    api_fail_count: Optional[int] = None
    api_has_error_excel: Optional[bool] = None
    api_response_file: str = ""
    processed_orders_skipped: int = 0
    local_images_deleted: int = 0
    local_images_retained: int = 0
    local_image_cleanup_errors: int = 0
    status: str = "running"
    error: str = ""
    schema_version: int = 12
    status_code_version: int = STATUS_CODE_VERSION
    drcode_status_version: int = DRCODE_STATUS_VERSION

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def optional_response_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def apply_api_result_to_summary(
    summary: RunSummary,
    result: Any,
    response_file: str,
) -> None:
    summary.api_status = str(result.outcome)
    summary.api_http_status = int(result.http_status)
    summary.api_message = str(result.message or "")
    summary.api_err_code = result.err_code
    summary.api_response_file = str(response_file)
    response_body = result.response if isinstance(result.response, dict) else {}
    response_data = response_body.get("data")
    if not isinstance(response_data, dict):
        response_data = {}
    response_success = response_body.get("success")
    summary.api_response_success = (
        response_success if isinstance(response_success, bool) else None
    )
    summary.api_task_id = str(response_data.get("taskId") or "")
    summary.api_total_count = optional_response_int(response_data.get("totalCount"))
    summary.api_success_count = optional_response_int(response_data.get("successCount"))
    summary.api_fail_count = optional_response_int(response_data.get("failCount"))
    has_error_excel = response_data.get("hasErrorExcel")
    summary.api_has_error_excel = (
        has_error_excel if isinstance(has_error_excel, bool) else None
    )


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
    need_review_path = runs_dir / run_id[:8] / "need_review.json"
    upload_json_path = run_dir / "prescription_upload.json"
    api_response_path = run_dir / "api_response.json"
    summary_path = run_dir / "summary.json"
    health_path = output_dir / "last_run.json"

    start_ts, end_ts, label = parse_date_range(start, end, int(config["lookback_days"]))
    expected_poi_id = str(config.get("expected_poi_id") or "").strip()
    summary = RunSummary(
        run_id=run_id,
        date_range=label,
        expected_poi_id=expected_poi_id,
    )
    atomic_json(summary_path, summary.as_dict())

    state = StateStore(output_dir / "state.sqlite3")
    usage_ocr: Optional[PaddleUsageOCR] = None
    catalog: Optional[DrugCatalog] = None
    sku_index: Optional[MedicineSkuIndex] = None
    need_review_records: list[dict[str, Any]] = []
    delivery_records: list[dict[str, Any]] = []
    delivery_keys: list[str] = []
    cos_enabled = bool(config.get("cos_upload_enabled", False))
    api_enabled = bool(config.get("api_submit_enabled", False))
    hospital_ocr_enabled = bool(config.get("hospital_ocr_enabled", True))
    cos_settings: Optional[CosSettings] = None
    cos_client: Any = None
    try:
        if api_enabled and not cos_enabled:
            raise ValueError("api_submit_enabled requires cos_upload_enabled")
        if cos_enabled:
            cos_settings, cos_client = create_cos_client(CosSettings.from_env())
            configured_cos_prefix = str(config.get("cos_folder_prefix") or "").strip()
            if configured_cos_prefix:
                cos_settings = replace(cos_settings, folder_prefix=configured_cos_prefix)
        if bool(config.get("drug_catalog_enabled", True)):
            catalog = DrugCatalog.load(pathlib.Path(str(config["drug_catalog_index"])))
        if bool(config.get("medicine_sku_enabled", True)) and not bool(
            config.get("medicine_sku_refresh_enabled", False)
        ):
            sku_index = MedicineSkuIndex.load(pathlib.Path(str(config["medicine_sku_index"])))
        with CdpPage(int(config["chrome_port"]), HOME_URL) as tab:
            summary.current_poi_id = validate_browser_store(tab, expected_poi_id)
            if bool(config.get("medicine_sku_enabled", True)) and bool(
                config.get("medicine_sku_refresh_enabled", False)
            ):
                refresh = refresh_sku_index(
                    tab,
                    expected_poi_id,
                    pathlib.Path(str(config["medicine_sku_index"])),
                    page_size=int(config.get("medicine_sku_refresh_page_size") or 100),
                    max_pages=int(
                        config.get("medicine_sku_refresh_max_pages") or 1000
                    ),
                    cache_ttl_hours=float(
                        config.get("medicine_sku_cache_ttl_hours") or 0
                    ),
                    retries=int(config.get("medicine_sku_refresh_retries") or 2),
                )
                sku_index = refresh.index
                summary.sku_refresh_status = refresh.status
                summary.sku_refresh_products = refresh.products
                summary.sku_refresh_entries = refresh.entries
                summary.sku_refresh_pages = refresh.pages
                summary.sku_refresh_seconds = refresh.elapsed_seconds
                summary.sku_refresh_cache_age_seconds = refresh.cache_age_seconds
                summary.sku_refresh_error = refresh.error
                log(
                    f"sku_refresh={refresh.status} products={refresh.products} "
                    f"entries={refresh.entries} pages={refresh.pages} "
                    f"elapsed_s={refresh.elapsed_seconds}"
                )

            statuses = {str(item) for item in (config.get("statuses") or []) if str(item)}
            orders, scan = enumerate_rx_orders(
                tab,
                start_ts,
                end_ts,
                int(config["max_pages"]),
                int(config["page_size"]),
                statuses,
            )
            summary.pages_scanned = scan["pages_scanned"]
            summary.api_total = scan["api_total"]
            summary.rx_orders = len(orders)
            mismatched_poi_ids = sorted(
                {
                    str(order.get("poi_id") or "").strip()
                    for order in orders
                    if str(order.get("poi_id") or "").strip()
                    and str(order.get("poi_id") or "").strip() != expected_poi_id
                }
            )
            if mismatched_poi_ids:
                raise RuntimeError(
                    "order list contains unexpected poiId values: "
                    + ",".join(mismatched_poi_ids)
                )
            log(
                f"range={label} pages={summary.pages_scanned} "
                f"api_orders={summary.api_total} rx_orders={summary.rx_orders}"
            )

            limit = int(config.get("limit") or 0)
            seen_prescription_keys: set[str] = set()
            orders_processed = 0
            progress_store = f"MEITUAN:{expected_poi_id}"
            for order_position, order in enumerate(orders, start=1):
                if limit and summary.new_prescriptions >= limit:
                    break
                orders_processed = order_position
                report_store_progress(
                    log,
                    store=progress_store,
                    processed=order_position - 1,
                    total=len(orders),
                    saved=summary.new_prescriptions,
                    review=len(need_review_records),
                    errors=(
                        summary.detail_errors
                        + summary.download_errors
                        + summary.ocr_errors
                        + summary.cos_errors
                    ),
                )
                if state.contains_order(order["order_view_id"]):
                    summary.processed_orders_skipped += 1
                    continue
                detail = get_prescription_detail(
                    tab,
                    order["order_view_id"],
                    int(config["request_retries"]),
                )
                if detail["status"] == "error":
                    summary.detail_errors += 1
                    continue
                if detail["status"] == "no_image":
                    summary.no_image += 1
                    review = {
                        **order,
                        "platform": "MEITUAN",
                        "prescription_id": "",
                        "prescription_time": "",
                        "image_url": "",
                        "patient_info": {},
                        "icd_name": "",
                        "drug_infos": [],
                    }
                    missing, sources = validate_platform_required_fields(review, "MEITUAN")
                    need_review_records.append(
                        attach_platform_required_review(review, "MEITUAN", missing, sources)
                    )
                    continue
                summary.details_ok += 1
                records = normalize_prescriptions(
                    order,
                    detail,
                )
                if not records:
                    review = {
                        **order,
                        "platform": "MEITUAN",
                        "prescription_id": "",
                        "prescription_time": "",
                        "image_url": "",
                        "patient_info": {},
                        "icd_name": "",
                        "drug_infos": [],
                    }
                    missing, sources = validate_platform_required_fields(review, "MEITUAN")
                    need_review_records.append(
                        attach_platform_required_review(review, "MEITUAN", missing, sources)
                    )
                    continue
                if is_multi_prescription_order(records):
                    summary.multi_prescription_orders_skipped += 1
                    if append_multi_prescription_order(
                        platform="MEITUAN",
                        store_id=expected_poi_id,
                        store_name=config.get("hospital_name"),
                        order_id=order.get("order_view_id"),
                        prescription_count=len(records),
                        prescription_ids=(record.get("prescription_id") for record in records),
                    ):
                        summary.multi_prescription_orders_queued += 1
                    continue
                for record in records:
                    if limit and summary.new_prescriptions >= limit:
                        break
                    if record["prescription_key"] in seen_prescription_keys:
                        summary.duplicates += 1
                        continue
                    if state.contains(record["prescription_key"]):
                        summary.duplicates += 1
                        continue
                    missing, sources = validate_platform_required_fields(record, "MEITUAN")
                    if missing:
                        need_review_records.append(
                            attach_platform_required_review(record, "MEITUAN", missing, sources)
                        )
                        continue
                    seen_prescription_keys.add(str(record["prescription_key"]))
                    raw_drug_infos = record.get("drug_infos") or []
                    if isinstance(raw_drug_infos, dict):
                        raw_drug_infos = [raw_drug_infos]
                    merged_drug_infos = merge_same_drugs(raw_drug_infos)
                    record["raw_drug_count"] = len(raw_drug_infos)
                    record["unique_drug_count"] = len(merged_drug_infos)
                    record["drug_processing_mode"] = (
                        "single_drug_fast_path" if len(merged_drug_infos) <= 1 else "multi_drug_matching"
                    )
                    record["drug_infos"] = merged_drug_infos
                    if record.get("total_fee") is None:
                        summary.total_fee_missing += 1
                    else:
                        summary.total_fee_success += 1
                    record["cos_status"] = "pending" if cos_enabled else "disabled"
                    image_hash = ""
                    image_path: Optional[pathlib.Path] = None
                    if record["image_url"]:
                        stem_base = record.get("prescription_id") or record["order_view_id"]
                        stem = f"{stem_base}_{record['image_index']}"
                        try:
                            image_path, image_hash = download_image(
                                record["image_url"],
                                image_dir,
                                str(stem),
                                int(config["request_retries"]),
                            )
                        except Exception:
                            summary.download_errors += 1
                            record["image_status"] = "download_failed"
                        else:
                            record["image_status"] = "downloaded"
                            record["image_file"] = str(image_path.relative_to(output_dir))
                            record["image_sha256"] = image_hash
                            if bool(config.get("ocr_enabled", True)):
                                summary.ocr_images += 1
                                try:
                                    if usage_ocr is None:
                                        usage_ocr = PaddleUsageOCR(
                                            min_confidence=float(config.get("ocr_min_confidence", 0.75)),
                                            crop_top=float(config.get("ocr_crop_top_ratio", 0.30)),
                                            crop_bottom=float(config.get("ocr_crop_bottom_ratio", 0.70)),
                                            single_crop_top=float(
                                                config.get("ocr_single_crop_top_ratio", 0.35)
                                            ),
                                            single_crop_bottom=float(
                                                config.get("ocr_single_crop_bottom_ratio", 0.53)
                                            ),
                                            multi_crop_top=float(
                                                config.get("ocr_multi_crop_top_ratio", 0.35)
                                            ),
                                            multi_crop_bottom=float(
                                                config.get("ocr_multi_crop_bottom_ratio", 0.70)
                                            ),
                                            header_crop_left=float(
                                                config.get("header_ocr_crop_left_ratio", 0.48)
                                            ),
                                            header_crop_right=float(
                                                config.get("header_ocr_crop_right_ratio", 0.96)
                                            ),
                                            header_crop_top=float(
                                                config.get("header_ocr_crop_top_ratio", 0.20)
                                            ),
                                            header_crop_bottom=float(
                                                config.get("header_ocr_crop_bottom_ratio", 0.35)
                                            ),
                                            hospital_crop_left=float(
                                                config.get("hospital_ocr_crop_left_ratio", 0.10)
                                            ),
                                            hospital_crop_right=float(
                                                config.get("hospital_ocr_crop_right_ratio", 0.90)
                                            ),
                                            hospital_crop_top=float(
                                                config.get("hospital_ocr_crop_top_ratio", 0.02)
                                            ),
                                            hospital_crop_bottom=float(
                                                config.get("hospital_ocr_crop_bottom_ratio", 0.20)
                                            ),
                                            usage_preprocessing_mode=str(
                                                config.get(
                                                    "usage_ocr_preprocessing_mode", "blue_mask"
                                                )
                                            ),
                                            header_grayscale_threshold=config.get(
                                                "header_ocr_grayscale_threshold"
                                            ),
                                            hospital_grayscale_threshold=config.get(
                                                "hospital_ocr_grayscale_threshold", 140
                                            ),
                                        )
                                    drug_infos = record.get("drug_infos") or []
                                    if isinstance(drug_infos, dict):
                                        drug_infos = [drug_infos]
                                    enriched, metrics = usage_ocr.enrich(drug_infos, image_path)
                                    record["drug_infos"] = enriched
                                    record["usage_ocr"] = metrics
                                    record["usage_ocr_status"] = aggregate_usage_ocr_status(enriched)
                                    record["usage_ocr_lines_checked"] = metrics["ocr_lines_checked"]
                                    record["usage_ocr_crop_mode"] = metrics["crop_mode"]
                                    record["usage_ocr_crop_top_ratio"] = metrics["crop_top_ratio"]
                                    record["usage_ocr_crop_bottom_ratio"] = metrics[
                                        "crop_bottom_ratio"
                                    ]
                                    record["usage_ocr_fallback_used"] = metrics["fallback_used"]
                                    record["usage_ocr_preprocessing_mode"] = metrics[
                                        "usage_preprocessing_mode"
                                    ]
                                    record["usage_ocr_blue_mask_used"] = metrics[
                                        "usage_blue_mask_used"
                                    ]
                                    record["usage_ocr_original_anchor_used"] = metrics[
                                        "usage_original_anchor_ocr_used"
                                    ]
                                    summary.ocr_success += metrics["usage_found"]
                                    summary.ocr_no_usage += max(0, len(enriched) - metrics["usage_found"])
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
                                    record["usage_ocr_status"] = "error"
                                    record["usage_ocr_error"] = type(exc).__name__
                                    for drug in record.get("drug_infos") or []:
                                        if isinstance(drug, dict):
                                            drug.update(
                                                {
                                                    "usage_source": "prescription_image_paddleocr",
                                                    "usage_ocr_status": "error",
                                                    "usage_failure_type": "ocr_runtime_error",
                                                    "duration_calculation_status": "needs_review",
                                                    "duration_calculation_reason": "ocr_runtime_error",
                                                }
                                            )
                                if bool(config.get("header_ocr_enabled", True)):
                                    try:
                                        if usage_ocr is None:
                                            usage_ocr = PaddleUsageOCR(
                                                min_confidence=float(
                                                    config.get("ocr_min_confidence", 0.75)
                                                ),
                                                crop_top=float(
                                                    config.get("ocr_crop_top_ratio", 0.30)
                                                ),
                                                crop_bottom=float(
                                                    config.get("ocr_crop_bottom_ratio", 0.70)
                                                ),
                                                single_crop_top=float(
                                                    config.get("ocr_single_crop_top_ratio", 0.35)
                                                ),
                                                single_crop_bottom=float(
                                                    config.get("ocr_single_crop_bottom_ratio", 0.53)
                                                ),
                                                multi_crop_top=float(
                                                    config.get("ocr_multi_crop_top_ratio", 0.35)
                                                ),
                                                multi_crop_bottom=float(
                                                    config.get("ocr_multi_crop_bottom_ratio", 0.70)
                                                ),
                                                header_crop_left=float(
                                                    config.get("header_ocr_crop_left_ratio", 0.48)
                                                ),
                                                header_crop_right=float(
                                                    config.get("header_ocr_crop_right_ratio", 0.96)
                                                ),
                                                header_crop_top=float(
                                                    config.get("header_ocr_crop_top_ratio", 0.20)
                                                ),
                                                header_crop_bottom=float(
                                                    config.get("header_ocr_crop_bottom_ratio", 0.35)
                                                ),
                                                hospital_crop_left=float(
                                                    config.get("hospital_ocr_crop_left_ratio", 0.10)
                                                ),
                                                hospital_crop_right=float(
                                                    config.get("hospital_ocr_crop_right_ratio", 0.90)
                                                ),
                                                hospital_crop_top=float(
                                                    config.get("hospital_ocr_crop_top_ratio", 0.02)
                                                ),
                                                hospital_crop_bottom=float(
                                                    config.get("hospital_ocr_crop_bottom_ratio", 0.20)
                                                ),
                                                usage_preprocessing_mode=str(
                                                    config.get(
                                                        "usage_ocr_preprocessing_mode", "blue_mask"
                                                    )
                                                ),
                                                header_grayscale_threshold=config.get(
                                                    "header_ocr_grayscale_threshold"
                                                ),
                                                hospital_grayscale_threshold=config.get(
                                                    "hospital_ocr_grayscale_threshold", 140
                                                ),
                                            )
                                        header_result = usage_ocr.extract_header_fields(image_path)
                                        applied = apply_patient_header_fields(record, header_result)
                                        for field in ("birth_date", "department"):
                                            status = str(
                                                header_result.get(f"{field}_ocr_status")
                                                or "not_found"
                                            )
                                            if status == "success":
                                                if field == "birth_date":
                                                    summary.birth_date_ocr_success += 1
                                                else:
                                                    summary.department_ocr_success += 1
                                            elif status == "low_confidence":
                                                summary.patient_field_ocr_low_confidence += 1
                                            else:
                                                summary.patient_field_ocr_not_found += 1
                                        if applied["birth_date"]:
                                            summary.birth_date_ocr_applied += 1
                                        if applied["department"]:
                                            summary.department_ocr_applied += 1
                                    except Exception as exc:
                                        summary.patient_field_ocr_errors += 1
                                        record["patient_field_ocr"] = {
                                            "birth_date_ocr_status": "error",
                                            "department_ocr_status": "error",
                                            "field_source": "prescription_image_paddleocr",
                                            "error": type(exc).__name__,
                                        }
                                if hospital_ocr_enabled:
                                    try:
                                        if usage_ocr is None:
                                            usage_ocr = PaddleUsageOCR(
                                                min_confidence=float(
                                                    config.get("ocr_min_confidence", 0.75)
                                                ),
                                                crop_top=float(
                                                    config.get("ocr_crop_top_ratio", 0.30)
                                                ),
                                                crop_bottom=float(
                                                    config.get("ocr_crop_bottom_ratio", 0.70)
                                                ),
                                                single_crop_top=float(
                                                    config.get("ocr_single_crop_top_ratio", 0.35)
                                                ),
                                                single_crop_bottom=float(
                                                    config.get("ocr_single_crop_bottom_ratio", 0.53)
                                                ),
                                                multi_crop_top=float(
                                                    config.get("ocr_multi_crop_top_ratio", 0.35)
                                                ),
                                                multi_crop_bottom=float(
                                                    config.get("ocr_multi_crop_bottom_ratio", 0.70)
                                                ),
                                                header_crop_left=float(
                                                    config.get("header_ocr_crop_left_ratio", 0.48)
                                                ),
                                                header_crop_right=float(
                                                    config.get("header_ocr_crop_right_ratio", 0.96)
                                                ),
                                                header_crop_top=float(
                                                    config.get("header_ocr_crop_top_ratio", 0.20)
                                                ),
                                                header_crop_bottom=float(
                                                    config.get("header_ocr_crop_bottom_ratio", 0.35)
                                                ),
                                                hospital_crop_left=float(
                                                    config.get("hospital_ocr_crop_left_ratio", 0.10)
                                                ),
                                                hospital_crop_right=float(
                                                    config.get("hospital_ocr_crop_right_ratio", 0.90)
                                                ),
                                                hospital_crop_top=float(
                                                    config.get("hospital_ocr_crop_top_ratio", 0.02)
                                                ),
                                                hospital_crop_bottom=float(
                                                    config.get("hospital_ocr_crop_bottom_ratio", 0.20)
                                                ),
                                                usage_preprocessing_mode=str(
                                                    config.get(
                                                        "usage_ocr_preprocessing_mode", "blue_mask"
                                                    )
                                                ),
                                                header_grayscale_threshold=config.get(
                                                    "header_ocr_grayscale_threshold"
                                                ),
                                                hospital_grayscale_threshold=config.get(
                                                    "hospital_ocr_grayscale_threshold", 140
                                                ),
                                            )
                                        internal_hospital_name = str(
                                            config.get("hospital_name")
                                            or "天津北辰保民互联网医院"
                                        )
                                        hospital_result = usage_ocr.extract_hospital_fields(
                                            image_path,
                                            internal_hospital_name,
                                            config.get("internal_hospital_aliases") or [],
                                        )
                                        record["hospital_ocr"] = hospital_result
                                        source_code = int(
                                            hospital_result.get("hospital_source_code") or 3
                                        )
                                        record["hospital_source_code"] = source_code
                                        if source_code == 1:
                                            record["hospital_name"] = internal_hospital_name
                                            summary.internal_hospital_prescriptions += 1
                                        elif source_code == 2:
                                            record["hospital_name"] = "外部医院"
                                            summary.external_hospital_prescriptions += 1
                                        else:
                                            summary.hospital_source_needs_review += 1
                                    except Exception as exc:
                                        summary.hospital_ocr_errors += 1
                                        summary.hospital_source_needs_review += 1
                                        record["hospital_source_code"] = 3
                                        record["hospital_ocr"] = {
                                            "hospital_source": "review",
                                            "hospital_source_code": 3,
                                            "hospital_ocr_status": "error",
                                            "hospital_ocr_reason": "ocr_runtime_error",
                                            "error": type(exc).__name__,
                                        }
                                else:
                                    record["hospital_name"] = str(
                                        config.get("hospital_name")
                                        or "天津北辰保民互联网医院"
                                    )
                                    record["hospital_source_code"] = 0
                            if cos_enabled:
                                try:
                                    cos_result = upload_image(
                                        image_path,
                                        str(record.get("prescription_id") or record["order_view_id"]),
                                        image_index=0,
                                        settings=cos_settings,
                                        client=cos_client,
                                        platform=str(config.get("platform") or "MEITUAN"),
                                        store_id=str(
                                            config.get("expected_poi_id")
                                            or record.get("poi_id")
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
                    final_drugs, processing, catalog_success, catalog_review = apply_catalog_fallback(
                        record.get("drug_infos") or [], catalog
                    )
                    record["drug_infos"] = final_drugs
                    sku_success, sku_missing, sku_ambiguous = apply_sku_matches(
                        final_drugs, sku_index, record.get("poi_id")
                    )
                    for index, drug in enumerate(final_drugs):
                        if index < len(processing):
                            processing[index]["drcode_status"] = int(
                                drug.get("drcode_status") or DRCODE_DISABLED
                            )
                    record["processing"] = processing
                    summary.catalog_success += catalog_success
                    summary.catalog_needs_review += catalog_review
                    summary.drcode_success += sku_success
                    summary.drcode_missing += sku_missing
                    summary.drcode_ambiguous += sku_ambiguous
                    summary.duration_success += sum(
                        1
                        for drug in final_drugs
                        if drug.get("duration_calculation_status") == "success"
                    )
                    summary.duration_needs_review += sum(
                        1
                        for drug in final_drugs
                        if drug.get("duration_calculation_status") != "success"
                    )
                    record["medicine_info"] = build_upload_medicine_info(final_drugs)
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
                processed=orders_processed,
                total=len(orders),
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
                str(config.get("hospital_name") or "天津北辰保民互联网医院"),
                source="美团",
            )
            write_interface_json(upload_json_path, payload)
            with contextlib.suppress(OSError):
                upload_json_path.chmod(0o600)
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
                    str(config.get("platform") or "MEITUAN"),
                    str(config.get("app_code") or "pachong"),
                    int(config.get("api_timeout_seconds") or 300),
                )
                atomic_json(api_response_path, response_snapshot(result))
                apply_api_result_to_summary(
                    summary,
                    result,
                    str(api_response_path.relative_to(output_dir)),
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
            if summary.hospital_source_needs_review > 0 and summary.cos_success > 0:
                summary.api_status = "blocked_hospital_review"
            else:
                summary.api_status = "blocked_no_cos_images"
                raise RuntimeError("no prescriptions have a verified COS image URL")
        elif api_enabled:
            summary.api_status = "no_new_records"

        summary.status = (
            "needs_review"
            if summary.hospital_source_needs_review > 0
            or summary.duration_needs_review > 0
            or summary.catalog_needs_review > 0
            or summary.need_review_records > 0
            or summary.api_status in {"uncertain_response", "blocked_hospital_review"}
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
        f"multi_order_skipped={summary.multi_prescription_orders_skipped} "
        f"multi_order_queued={summary.multi_prescription_orders_queued} "
        f"no_image={summary.no_image} detail_errors={summary.detail_errors} "
        f"download_errors={summary.download_errors} ocr_success={summary.ocr_success} "
        f"ocr_no_usage={summary.ocr_no_usage} ocr_errors={summary.ocr_errors} "
        f"ocr_images={summary.ocr_images} "
        f"ocr_model_load_seconds={summary.ocr_model_load_seconds} "
        f"ocr_warmup_seconds={summary.ocr_warmup_seconds} "
        f"ocr_inference_seconds={summary.ocr_inference_seconds} "
        f"run_elapsed_seconds={summary.run_elapsed_seconds} "
        f"birth_ocr={summary.birth_date_ocr_success} "
        f"department_ocr={summary.department_ocr_success} "
        f"patient_ocr_errors={summary.patient_field_ocr_errors} "
        f"duration_success={summary.duration_success} "
        f"catalog_success={summary.catalog_success} "
        f"catalog_review={summary.catalog_needs_review} "
        f"need_review={summary.need_review_records} "
        f"platform_required_missing={summary.platform_required_missing_records} "
        f"cos_success={summary.cos_success} "
        f"total_fee_success={summary.total_fee_success} "
        f"drcode_success={summary.drcode_success} drcode_missing={summary.drcode_missing} "
        f"cos_errors={summary.cos_errors} api_status={summary.api_status}"
    )
    return summary_path


def main() -> int:
    os.chdir(pathlib.Path(__file__).resolve().parent)
    parser = argparse.ArgumentParser(description="Collect Meituan prescription fields and supplementary images.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--start", help="start date YYYY-MM-DD")
    parser.add_argument("--end", help="end date YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="override maximum new prescriptions; 0 means unlimited")
    parser.add_argument("--skip-api-submit", action="store_true", help="prepare COS images and JSON without POST")
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
    sku_path = pathlib.Path(str(config.get("medicine_sku_index") or "./medicine_sku_index.json"))
    if not sku_path.is_absolute():
        sku_path = (config_path.parent / sku_path).resolve()
    config["medicine_sku_index"] = str(sku_path)
    if args.limit is not None:
        config["limit"] = max(0, args.limit)
    if args.skip_api_submit:
        config["api_submit_enabled"] = False
    if args.discover_store:
        with CdpPage(int(config["chrome_port"]), HOME_URL) as tab:
            logged_in = check_login(tab)
            actual_poi_id = current_poi_id(tab) if logged_in else ""
        discovered = bool(logged_in and actual_poi_id)
        print(
            json.dumps(
                {
                    "platform": "MEITUAN",
                    "logged_in": logged_in,
                    "current_poi_id": actual_poi_id,
                    "store_discovered": discovered,
                },
                ensure_ascii=False,
            )
        )
        return 0 if discovered else 2

    if args.check_login:
        with CdpPage(int(config["chrome_port"]), HOME_URL) as tab:
            logged_in = check_login(tab)
            actual_poi_id = current_poi_id(tab) if logged_in else ""
        expected_poi_id = str(config.get("expected_poi_id") or "").strip()
        store_match = bool(
            logged_in and expected_poi_id and actual_poi_id == expected_poi_id
        )
        print(
            json.dumps(
                {
                    "logged_in": logged_in,
                    "expected_poi_id": expected_poi_id,
                    "current_poi_id": actual_poi_id,
                    "store_match": store_match,
                },
                ensure_ascii=False,
            )
        )
        return 0 if store_match else 2

    with CdpPage(int(config["chrome_port"]), HOME_URL) as tab:
        validate_browser_store(tab, config.get("expected_poi_id"))

    output_dir = pathlib.Path(str(config["output_dir"])).expanduser().resolve()
    restricted_mkdir(output_dir)
    try:
        with SingleInstance(output_dir / ".collector.lock"):
            summary = collect(config, args.start, args.end)
    except Exception as exc:
        log(f"failed: {exc}")
        return 1
    print(f"SUMMARY_PATH={summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

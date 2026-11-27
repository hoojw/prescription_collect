#!/usr/bin/env python3
"""Build a strict Meituan medicine SKU index from the logged-in store catalog."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

from drug_text_normalization import strip_expiry_suffix
from medicine_sku import (
    SKU_SCHEMA_VERSION,
    MedicineSkuIndex,
    identifier,
    match_name_and_spec,
    normalize_match_text,
)


PRODUCT_LIST_PATH = "/reuse/health/product/retail/r/searchListPageV2"


@dataclass
class SkuRefreshResult:
    status: str
    index: MedicineSkuIndex
    path: pathlib.Path
    products: int = 0
    entries: int = 0
    pages: int = 0
    elapsed_seconds: float = 0.0
    cache_age_seconds: Optional[float] = None
    error: str = ""


def _run_json(tab: Any, expression: str) -> dict[str, Any]:
    raw = tab.run_js(expression)
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise RuntimeError(
            f"Meituan product API returned {type(value).__name__}, expected object"
        )
    return value


def fetch_product_page(
    tab: Any,
    poi_id: str,
    page_num: int,
    page_size: int,
) -> dict[str, Any]:
    """Fetch one catalog page through the already authenticated browser."""
    form = urlencode(
        {
            "wmPoiId": str(poi_id),
            "pageNum": int(page_num),
            "pageSize": int(page_size),
            "needTag": 0,
            "name": "",
            "brandId": 0,
            "tagId": 0,
            "searchWord": "",
            "state": 0,
            "sortType": "",
            "sortKey": "",
            "saleStatus": 0,
            "limitSale": 0,
            "needCombinationSpu": -1,
            "noStockAutoClear": -1,
            "problemType": 1,
            "noSingleDeliveryType": 0,
        }
    )
    path_literal = json.dumps(PRODUCT_LIST_PATH)
    form_literal = json.dumps(form)
    script = f"""
    (() => {{
      var x = new XMLHttpRequest();
      x.open('POST', {path_literal}, false);
      x.setRequestHeader(
        'content-type',
        'application/x-www-form-urlencoded;charset=UTF-8'
      );
      x.send({form_literal});
      return JSON.stringify({{httpStatus:x.status, body:x.responseText}});
    }})()
    """
    envelope = _run_json(tab, script)
    if int(envelope.get("httpStatus") or 0) != 200:
        raise RuntimeError(f"Meituan product list HTTP {envelope.get('httpStatus')}")
    body = envelope.get("body")
    payload = json.loads(body) if isinstance(body, str) else body
    if not isinstance(payload, dict) or payload.get("code") != 0:
        code = payload.get("code") if isinstance(payload, dict) else None
        raise RuntimeError(f"Meituan product list rejected request: code={code}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("productList"), list):
        raise RuntimeError("Meituan product list response schema changed")
    return data


def _entry(
    poi_id: str,
    sku_id: Any,
    upc: Any,
    title: Any,
    specification: Any,
) -> Optional[dict[str, str]]:
    sku = identifier(sku_id)
    if not sku:
        return None
    clean_title = strip_expiry_suffix(title)
    name, derived_spec = match_name_and_spec(clean_title, specification)
    return {
        "p": identifier(poi_id),
        "s": sku,
        "u": identifier(upc),
        "t": normalize_match_text(clean_title),
        "n": normalize_match_text(name, strip_brand=True),
        "c": normalize_match_text(specification or derived_spec),
    }


def entries_from_products(
    products: list[dict[str, Any]],
    poi_id: str,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for product in products:
        if not isinstance(product, dict):
            continue
        title = product.get("name") or product.get("nameWithoutSupplement") or ""
        skus = product.get("wmProductSkus")
        if not isinstance(skus, list) or not skus:
            skus = [
                {
                    "id": product.get("skuId"),
                    "upcCode": product.get("upcCode"),
                    "spec": "",
                }
            ]
        for sku in skus:
            if not isinstance(sku, dict):
                continue
            entry = _entry(
                poi_id,
                sku.get("id") or product.get("skuId"),
                sku.get("upcCode")
                or sku.get("packageUpc")
                or product.get("upcCode"),
                title,
                sku.get("spec"),
            )
            if entry is None:
                continue
            key = tuple(entry[field] for field in ("p", "s", "u", "t", "n", "c"))
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    return entries


def fetch_complete_catalog(
    tab: Any,
    poi_id: str,
    page_size: int = 100,
    max_pages: int = 1000,
) -> tuple[list[dict[str, Any]], int]:
    products: list[dict[str, Any]] = []
    seen_product_ids: set[str] = set()
    expected_total: Optional[int] = None
    pages = 0
    effective_page_size = max(1, min(int(page_size), 200))
    for page_num in range(1, max(1, int(max_pages)) + 1):
        data = fetch_product_page(tab, poi_id, page_num, effective_page_size)
        pages += 1
        try:
            total = int(data.get("totalCount") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Meituan product totalCount is invalid") from exc
        if total < 0:
            raise RuntimeError("Meituan product totalCount is negative")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError(
                "Meituan product totalCount changed during pagination"
            )
        page_products = data.get("productList") or []
        for item in page_products:
            if not isinstance(item, dict):
                continue
            product_id = identifier(item.get("id"))
            if product_id:
                if product_id in seen_product_ids:
                    raise RuntimeError(
                        f"duplicate Meituan product across pages: id={product_id}"
                    )
                seen_product_ids.add(product_id)
            products.append(item)
        if len(products) >= expected_total:
            break
        if not page_products:
            break
        returned_size = int(data.get("pageSize") or len(page_products) or 1)
        expected_pages = math.ceil(expected_total / max(1, returned_size))
        if page_num >= expected_pages and len(products) < expected_total:
            break
    expected_total = int(expected_total or 0)
    if len(products) != expected_total:
        raise RuntimeError(
            f"incomplete Meituan product catalog: expected={expected_total}, "
            f"received={len(products)}"
        )
    return products, pages


def _atomic_write_index(
    path: pathlib.Path,
    poi_id: str,
    product_count: int,
    entries: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with_context_mode = 0o700
    try:
        path.parent.chmod(with_context_mode)
    except OSError:
        pass
    payload = {
        "schema_version": SKU_SCHEMA_VERSION,
        "source": "meituan_product_list_api",
        "poi_id": str(poi_id),
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "product_count": int(product_count),
        "entry_count": len(entries),
        "entries": entries,
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    os.replace(temp, path)


def _valid_cache_age(
    path: pathlib.Path,
    poi_id: str,
    ttl_seconds: int,
) -> float | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != SKU_SCHEMA_VERSION:
            return None
        if str(payload.get("poi_id") or "") != str(poi_id):
            return None
        if not isinstance(payload.get("entries"), list):
            return None
        age = max(0.0, time.time() - path.stat().st_mtime)
        return age if age <= max(0, int(ttl_seconds)) else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def refresh_sku_index(
    tab: Any,
    poi_id: str,
    path: pathlib.Path,
    page_size: int = 100,
    max_pages: int = 1000,
    cache_ttl_hours: float = 72,
    retries: int = 2,
) -> SkuRefreshResult:
    """Refresh atomically; on failure use only a fresh, store-matched cache."""
    started = time.perf_counter()
    last_error = ""
    for attempt in range(1, max(1, int(retries)) + 1):
        try:
            products, pages = fetch_complete_catalog(
                tab,
                str(poi_id),
                page_size=page_size,
                max_pages=max_pages,
            )
            entries = entries_from_products(products, str(poi_id))
            if products and not entries:
                raise RuntimeError("Meituan product catalog contains no usable SKU")
            _atomic_write_index(path, str(poi_id), len(products), entries)
            return SkuRefreshResult(
                status="success",
                index=MedicineSkuIndex.load(path),
                path=path,
                products=len(products),
                entries=len(entries),
                pages=pages,
                elapsed_seconds=round(time.perf_counter() - started, 6),
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max(1, int(retries)):
                time.sleep(min(2**attempt, 4))

    ttl_seconds = int(max(0.0, float(cache_ttl_hours)) * 3600)
    cache_age = _valid_cache_age(path, str(poi_id), ttl_seconds)
    if cache_age is not None:
        index = MedicineSkuIndex.load(path)
        return SkuRefreshResult(
            status="fallback_cache",
            index=index,
            path=path,
            elapsed_seconds=round(time.perf_counter() - started, 6),
            cache_age_seconds=round(cache_age, 3),
            error=last_error,
        )
    return SkuRefreshResult(
        status="unavailable",
        index=MedicineSkuIndex.load(pathlib.Path(str(path) + ".unavailable")),
        path=path,
        elapsed_seconds=round(time.perf_counter() - started, 6),
        error=last_error,
    )

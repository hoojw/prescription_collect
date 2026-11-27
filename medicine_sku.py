#!/usr/bin/env python3
"""Strict matching from Meituan prescription drugs to platform sku_id."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from prescription_usage_ocr import normalize_text, split_drug_name_specification, strip_brand_prefix


SKU_SCHEMA_VERSION = 1
DRCODE_STATUS_VERSION = 1

DRCODE_DIRECT_SKU = 1
DRCODE_MATCH_UPC = 2
DRCODE_MATCH_TITLE = 3
DRCODE_MATCH_NAME_SPEC = 4
DRCODE_NOT_FOUND = 11
DRCODE_AMBIGUOUS = 12
DRCODE_INDEX_UNAVAILABLE = 13
DRCODE_DISABLED = 14


def identifier(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return re.sub(r"\.0$", "", text)


def normalize_match_text(value: Any, strip_brand: bool = False) -> str:
    text = strip_brand_prefix(value) if strip_brand else str(value or "")
    text = unicodedata.normalize("NFKC", text)
    text = normalize_text(text).lower()
    text = text.translate(str.maketrans({"×": "*", "：": ":", "（": "(", "）": ")"}))
    return re.sub(r"\s+", "", text)


def match_name_and_spec(drug_name: Any, packing_spec: Any = "") -> tuple[str, str]:
    """Derive a generic name even when the API also supplies packingSpec."""
    derived_name, derived_spec = split_drug_name_specification(drug_name, "")
    explicit_spec = str(packing_spec or "").strip()
    return derived_name, explicit_spec or derived_spec


def sku_entry_from_row(row: dict[str, Any]) -> dict[str, str]:
    title = row.get("商品标题")
    spec = row.get("规格")
    name, derived_spec = match_name_and_spec(title, spec)
    return {
        "p": identifier(row.get("门店ID")),
        "s": identifier(row.get("sku_id(平台生成商品编码)")),
        "u": identifier(row.get("条形码(UPC/EAN码)")),
        "t": normalize_match_text(title),
        "n": normalize_match_text(name, strip_brand=True),
        "c": normalize_match_text(spec or derived_spec),
    }


@dataclass
class SkuMatch:
    drcode: str
    status: int


class MedicineSkuIndex:
    def __init__(self, entries: Iterable[dict[str, Any]], load_status: int = 0):
        self.load_status = load_status
        self.known_skus: set[str] = set()
        self.by_upc: dict[tuple[str, str], set[str]] = {}
        self.by_title: dict[tuple[str, str], set[str]] = {}
        self.by_name_spec: dict[tuple[str, str, str], set[str]] = {}
        for raw in entries:
            entry = {key: identifier(raw.get(key)) for key in ("p", "s", "u")}
            entry.update({key: str(raw.get(key) or "") for key in ("t", "n", "c")})
            sku = entry["s"]
            if not sku:
                continue
            self.known_skus.add(sku)
            if entry["u"]:
                self.by_upc.setdefault((entry["p"], entry["u"]), set()).add(sku)
            if entry["t"]:
                self.by_title.setdefault((entry["p"], entry["t"]), set()).add(sku)
            if entry["n"] and entry["c"]:
                self.by_name_spec.setdefault((entry["p"], entry["n"], entry["c"]), set()).add(sku)

    @classmethod
    def load(cls, path: Path) -> "MedicineSkuIndex":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls([], DRCODE_INDEX_UNAVAILABLE)
        if payload.get("schema_version") != SKU_SCHEMA_VERSION or not isinstance(payload.get("entries"), list):
            return cls([], DRCODE_INDEX_UNAVAILABLE)
        return cls(payload["entries"])

    @staticmethod
    def _choose(candidates: set[str], success_status: int) -> Optional[SkuMatch]:
        if len(candidates) == 1:
            return SkuMatch(next(iter(candidates)), success_status)
        if len(candidates) > 1:
            return SkuMatch("", DRCODE_AMBIGUOUS)
        return None

    @staticmethod
    def _candidates_with_single_poi_fallback(mapping: dict, poi_key: tuple, suffix: tuple) -> set[str]:
        candidates = set(mapping.get(poi_key, set()))
        if candidates or poi_key[0]:
            return candidates
        for key, values in mapping.items():
            if key[1:] == suffix:
                candidates.update(values)
        return candidates

    def match(self, drug: dict[str, Any], poi_id: Any) -> SkuMatch:
        direct = identifier(drug.get("skuId") or drug.get("sku_id"))
        if direct:
            return SkuMatch(direct, DRCODE_DIRECT_SKU)
        if self.load_status:
            return SkuMatch("", self.load_status)

        poi = identifier(poi_id)
        upc = identifier(drug.get("upcCode") or drug.get("upc_code"))
        if upc:
            result = self._choose(
                self._candidates_with_single_poi_fallback(self.by_upc, (poi, upc), (upc,)),
                DRCODE_MATCH_UPC,
            )
            if result:
                return result

        title = normalize_match_text(drug.get("drugName"))
        if title:
            result = self._choose(
                self._candidates_with_single_poi_fallback(self.by_title, (poi, title), (title,)),
                DRCODE_MATCH_TITLE,
            )
            if result:
                return result

        name, spec = match_name_and_spec(drug.get("drugName"), drug.get("packingSpec"))
        name_key = normalize_match_text(name, strip_brand=True)
        spec_key = normalize_match_text(spec)
        if name_key and spec_key:
            result = self._choose(
                self._candidates_with_single_poi_fallback(
                    self.by_name_spec,
                    (poi, name_key, spec_key),
                    (name_key, spec_key),
                ),
                DRCODE_MATCH_NAME_SPEC,
            )
            if result:
                return result
        return SkuMatch("", DRCODE_NOT_FOUND)

    def apply(self, drug: dict[str, Any], poi_id: Any) -> dict[str, Any]:
        match = self.match(drug, poi_id)
        drug["drcode"] = match.drcode
        drug["drcode_status"] = match.status
        return drug

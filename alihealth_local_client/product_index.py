"""Build and use a strict single-box Tmall medicine product index."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from prescription_usage_ocr import (
    normalize_text,
    split_drug_name_specification,
    strip_brand_prefix,
)


SCHEMA_VERSION = 1
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
MONEY_QUANTUM = Decimal("0.01")
MAX_TOTAL_FEE = Decimal("999999.99")

MATCH_EXACT = 1
MATCH_NAME_SPEC = 2
MATCH_DIRECT_CODE = 3
MATCH_NOT_FOUND = 11
MATCH_AMBIGUOUS = 12
MATCH_INDEX_UNAVAILABLE = 13


class ProductIndexError(ValueError):
    """Raised when source exports cannot produce a safe product index."""


def identifier(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    return re.sub(r"\.0$", "", text)


def normalized(value: Any, *, strip_brand: bool = False) -> str:
    text = strip_brand_prefix(value) if strip_brand else str(value or "")
    text = unicodedata.normalize("NFKC", text)
    text = normalize_text(text).lower()
    text = text.translate(
        str.maketrans(
            {
                "×": "*",
                "：": ":",
                "（": "(",
                "）": ")",
                "【": "[",
                "】": "]",
            }
        )
    )
    return re.sub(r"\s+", "", text)


def decimal_price(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ProductIndexError(f"{name} is not a decimal price: {value!r}") from exc
    if result <= 0:
        raise ProductIndexError(f"{name} must be greater than zero")
    return result.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _column_number(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    result = 0
    for character in letters.upper():
        result = result * 26 + ord(character) - ord("A") + 1
    return result - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values: list[str] = []
    for item in root.findall(f"{{{NS_MAIN}}}si"):
        values.append("".join(node.text or "" for node in item.iter(f"{{{NS_MAIN}}}t")))
    return values


def _sheet_paths(archive: zipfile.ZipFile) -> list[str]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        node.attrib["Id"]: node.attrib["Target"]
        for node in relationships.findall(f"{{{NS_REL_PKG}}}Relationship")
    }
    paths: list[str] = []
    for sheet in workbook.findall(f".//{{{NS_MAIN}}}sheet"):
        relationship_id = sheet.attrib[f"{{{NS_REL_DOC}}}id"]
        target = targets[relationship_id].lstrip("/")
        if target.startswith("xl/"):
            paths.append(target)
        else:
            paths.append("xl/" + target)
    return paths


def _worksheet_rows(
    archive: zipfile.ZipFile,
    path: str,
    shared_strings: list[str],
) -> list[list[str]]:
    root = ET.fromstring(archive.read(path))
    rows: list[list[str]] = []
    for row_node in root.findall(f".//{{{NS_MAIN}}}row"):
        values: dict[int, str] = {}
        for cell in row_node.findall(f"{{{NS_MAIN}}}c"):
            column = _column_number(cell.attrib.get("r", ""))
            cell_type = cell.attrib.get("t", "")
            if cell_type == "inlineStr":
                value = "".join(
                    node.text or "" for node in cell.iter(f"{{{NS_MAIN}}}t")
                )
            else:
                value_node = cell.find(f"{{{NS_MAIN}}}v")
                value = value_node.text if value_node is not None and value_node.text else ""
                if cell_type == "s" and value:
                    value = shared_strings[int(value)]
            values[column] = value
        if values:
            width = max(values) + 1
            rows.append([values.get(index, "") for index in range(width)])
        else:
            rows.append([])
    return rows


def parse_tmall_export(path: pathlib.Path) -> list[dict[str, Any]]:
    """Parse one Tmall fast-edit export without requiring an Excel library."""
    with zipfile.ZipFile(path) as archive:
        strings = _shared_strings(archive)
        sheet_paths = _sheet_paths(archive)
        if len(sheet_paths) < 2:
            raise ProductIndexError(f"{path.name}: expected main and hidden schema sheets")
        main_rows = _worksheet_rows(archive, sheet_paths[0], strings)
        schema_rows = _worksheet_rows(archive, sheet_paths[1], strings)
    if len(main_rows) < 4 or len(schema_rows) < 3:
        raise ProductIndexError(f"{path.name}: unexpected export structure")

    field_names = schema_rows[2]
    parsed: list[dict[str, Any]] = []
    for row_number, row in enumerate(main_rows[3:], start=4):
        record: dict[str, Any] = {}
        extra_values: list[str] = []
        for index, value in enumerate(row):
            text = str(value or "").strip()
            if not text:
                continue
            field_name = field_names[index].strip() if index < len(field_names) else ""
            if field_name:
                record[field_name] = text
            else:
                extra_values.append(text)
        if not identifier(record.get("id")) and not identifier(record.get("sku.skuId")):
            continue
        if not identifier(record.get("id")) or not identifier(record.get("sku.skuId")):
            raise ProductIndexError(f"{path.name}:{row_number}: item or SKU id is missing")
        record["_source_file"] = path.name
        record["_source_row"] = row_number
        record["_extra_values"] = extra_values
        parsed.append(record)
    return parsed


BOX_ATTRIBUTE_RE = re.compile(r"(?:套餐类型\s*[:：]\s*)?(\d+)\s*盒装")
OUTER_MULTIPLIER_RE = re.compile(r"\*(\d+)\s*$")


def _sales_box_count(row: dict[str, Any]) -> tuple[int | None, str]:
    for value in row.get("_extra_values") or []:
        match = BOX_ATTRIBUTE_RE.search(str(value))
        if match:
            return int(match.group(1)), str(value)
    return None, ""


def _outer_box_count(value: Any) -> int:
    text = identifier(value)
    match = OUTER_MULTIPLIER_RE.search(text)
    return int(match.group(1)) if match else 1


def _entry_match_keys(title: str, shopping_title: str) -> dict[str, list[Any]]:
    full_keys: set[str] = set()
    name_spec_keys: set[tuple[str, str]] = set()
    for source in (title, shopping_title):
        if not source:
            continue
        full_keys.add(normalized(source))
        full_keys.add(normalized(source, strip_brand=True))
        name, spec = split_drug_name_specification(source, "")
        name_key = normalized(name, strip_brand=True)
        spec_key = normalized(spec)
        if name_key and spec_key:
            name_spec_keys.add((name_key, spec_key))
    return {
        "full": sorted(value for value in full_keys if value),
        "name_spec": [list(value) for value in sorted(name_spec_keys)],
    }


def build_product_index(
    rows: Iterable[dict[str, Any]],
    *,
    sources: list[dict[str, Any]] | None = None,
    expected_products: int = 0,
) -> dict[str, Any]:
    raw_rows = list(rows)
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_skus: set[str] = set()
    for row in raw_rows:
        item_id = identifier(row.get("id"))
        sku_id = identifier(row.get("sku.skuId"))
        if not item_id or not sku_id:
            raise ProductIndexError("every row must contain id and sku.skuId")
        if sku_id in seen_skus:
            raise ProductIndexError(f"duplicate sku.skuId: {sku_id}")
        seen_skus.add(sku_id)
        by_item[item_id].append(row)
    if expected_products and len(by_item) != expected_products:
        raise ProductIndexError(
            f"expected {expected_products} products but parsed {len(by_item)}"
        )

    products: list[dict[str, Any]] = []
    price_mismatches = 0
    multi_sku_products = 0
    for item_id, item_rows in sorted(by_item.items()):
        if len(item_rows) > 1:
            multi_sku_products += 1
        candidates: list[tuple[dict[str, Any], str]] = []
        for row in item_rows:
            sales_count, sales_attribute = _sales_box_count(row)
            outer_count = _outer_box_count(row.get("sku.skuOuterId"))
            if sales_count is not None and sales_count != outer_count:
                raise ProductIndexError(
                    f"item {item_id} SKU {identifier(row.get('sku.skuId'))}: "
                    "sales attribute and merchant code box counts conflict"
                )
            effective_count = sales_count if sales_count is not None else outer_count
            if effective_count == 1:
                candidates.append((row, sales_attribute))
        if len(candidates) != 1:
            raise ProductIndexError(
                f"item {item_id}: expected exactly one single-box SKU, found {len(candidates)}"
            )
        row, sales_attribute = candidates[0]
        drcode = identifier(row.get("sku.skuOuterId"))
        platform_sku_id = identifier(row.get("sku.skuId"))
        if not drcode:
            raise ProductIndexError(f"item {item_id}: single-box merchant SKU code is missing")
        sku_price = decimal_price(row.get("sku.skuPrice"), "sku.skuPrice")
        item_price = decimal_price(row.get("price"), "price")
        if sku_price != item_price:
            price_mismatches += 1
        title = str(row.get("title") or "").strip()
        shopping_title = str(row.get("shopping_title") or "").strip()
        if not title:
            raise ProductIndexError(f"item {item_id}: title is missing")
        products.append(
            {
                "item_id": item_id,
                "title": title,
                "shopping_title": shopping_title,
                "drcode": drcode,
                "platform_sku_id": platform_sku_id,
                "single_box_price": format(sku_price, ".2f"),
                "item_price": format(item_price, ".2f"),
                "sales_attribute": sales_attribute,
                "source_file": str(row.get("_source_file") or ""),
                "source_row": int(row.get("_source_row") or 0),
                "sku_count": len(item_rows),
                "match_keys": _entry_match_keys(title, shopping_title),
            }
        )

    full_key_items: dict[str, set[str]] = defaultdict(set)
    for product in products:
        for key in product["match_keys"]["full"]:
            full_key_items[key].add(product["item_id"])
    conflicts = [
        {"match_key": key, "item_ids": sorted(item_ids)}
        for key, item_ids in sorted(full_key_items.items())
        if len(item_ids) > 1
    ]
    affected_items = {
        item_id for conflict in conflicts for item_id in conflict["item_ids"]
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
            timespec="seconds"
        ),
        "selection_rule": {
            "drcode_source": "single_box_sku.skuOuterId",
            "platform_sku_id_source": "single_box_sku.skuId",
            "unit_price_source": "single_box_sku.skuPrice",
            "primary_box_count": "sales_attribute",
            "box_count_cross_check": "sku.skuOuterId_multiplier",
        },
        "sources": sources or [],
        "summary": {
            "product_count": len(products),
            "sku_count": len(raw_rows),
            "single_sku_products": len(products) - multi_sku_products,
            "multi_sku_products": multi_sku_products,
            "item_price_mismatches": price_mismatches,
            "duplicate_full_match_keys": len(conflicts),
            "products_affected_by_full_key_conflicts": len(affected_items),
        },
        "match_key_conflicts": conflicts,
        "products": products,
    }


def source_metadata(path: pathlib.Path, row_count: int) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "file_name": path.name,
        "sha256": digest,
        "size_bytes": path.stat().st_size,
        "sku_rows": row_count,
    }


def write_index(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


@dataclass(frozen=True)
class ProductMatch:
    entry: dict[str, Any] | None
    status: int


class TmallProductIndex:
    def __init__(self, products: Iterable[dict[str, Any]], load_status: int = 0) -> None:
        self.products = list(products)
        self.load_status = load_status
        self.by_drcode: dict[str, set[int]] = defaultdict(set)
        self.by_platform_sku: dict[str, set[int]] = defaultdict(set)
        self.by_full: dict[str, set[int]] = defaultdict(set)
        self.by_name_spec: dict[tuple[str, str], set[int]] = defaultdict(set)
        for index, entry in enumerate(self.products):
            if identifier(entry.get("drcode")):
                self.by_drcode[identifier(entry.get("drcode"))].add(index)
            if identifier(entry.get("platform_sku_id")):
                self.by_platform_sku[identifier(entry.get("platform_sku_id"))].add(index)
            keys = entry.get("match_keys") or {}
            for value in keys.get("full") or []:
                if value:
                    self.by_full[str(value)].add(index)
            for value in keys.get("name_spec") or []:
                if isinstance(value, list) and len(value) == 2 and all(value):
                    self.by_name_spec[(str(value[0]), str(value[1]))].add(index)

    @classmethod
    def load(cls, path: pathlib.Path) -> "TmallProductIndex":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls([], MATCH_INDEX_UNAVAILABLE)
        if payload.get("schema_version") != SCHEMA_VERSION or not isinstance(
            payload.get("products"), list
        ):
            return cls([], MATCH_INDEX_UNAVAILABLE)
        return cls(payload["products"])

    def _choose(self, candidates: set[int], status: int) -> ProductMatch | None:
        if len(candidates) == 1:
            return ProductMatch(self.products[next(iter(candidates))], status)
        if len(candidates) > 1:
            return ProductMatch(None, MATCH_AMBIGUOUS)
        return None

    @staticmethod
    def _drug_name_spec_keys(drug: dict[str, Any]) -> set[tuple[str, str]]:
        spec = str(drug.get("spec") or drug.get("packing_spec") or "").strip()
        spec_key = normalized(spec)
        keys: set[tuple[str, str]] = set()
        for field in ("drug_name", "common_drug_name", "drugName"):
            value = str(drug.get(field) or "").strip()
            if not value:
                continue
            name, derived_spec = split_drug_name_specification(value, "")
            name_key = normalized(name, strip_brand=True)
            effective_spec = spec_key or normalized(derived_spec)
            if name_key and effective_spec:
                keys.add((name_key, effective_spec))
        return keys

    @staticmethod
    def _drug_full_keys(drug: dict[str, Any]) -> set[str]:
        spec = str(drug.get("spec") or drug.get("packing_spec") or "").strip()
        keys: set[str] = set()
        for field in ("drug_name", "common_drug_name", "drugName"):
            value = str(drug.get(field) or "").strip()
            if not value:
                continue
            keys.add(normalized(value))
            keys.add(normalized(value, strip_brand=True))
            if spec:
                keys.add(normalized(value + spec))
                keys.add(normalized(strip_brand_prefix(value) + spec))
        return {value for value in keys if value}

    def match(self, drug: dict[str, Any]) -> ProductMatch:
        if self.load_status:
            return ProductMatch(None, self.load_status)
        direct_code = identifier(
            drug.get("drcode")
            or drug.get("sku_outer_id")
            or drug.get("merchant_sku_code")
        )
        if direct_code:
            result = self._choose(self.by_drcode.get(direct_code, set()), MATCH_DIRECT_CODE)
            if result:
                return result
        direct_sku = identifier(drug.get("sku_id") or drug.get("skuId"))
        if direct_sku:
            result = self._choose(
                self.by_platform_sku.get(direct_sku, set()),
                MATCH_DIRECT_CODE,
            )
            if result:
                return result

        exact_candidates: set[int] = set()
        for key in self._drug_full_keys(drug):
            exact_candidates.update(self.by_full.get(key, set()))
        result = self._choose(exact_candidates, MATCH_EXACT)
        if result:
            return result

        name_spec_candidates: set[int] = set()
        for key in self._drug_name_spec_keys(drug):
            name_spec_candidates.update(self.by_name_spec.get(key, set()))
        result = self._choose(name_spec_candidates, MATCH_NAME_SPEC)
        if result:
            return result
        return ProductMatch(None, MATCH_NOT_FOUND)


def detail_drugs(detail: dict[str, Any]) -> list[dict[str, Any]]:
    value: Any = detail.get("drug_list") or []
    if isinstance(value, dict):
        value = value.get("drug_dto") or []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def box_quantity(value: Any) -> int | None:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+)(?:\.0+)?\s*(?:盒)?", text)
    if not match:
        return None
    result = int(match.group(1))
    return result if result > 0 else None


def apply_product_index(
    detail: dict[str, Any],
    index: TmallProductIndex,
) -> dict[str, Any]:
    drugs = detail_drugs(detail)
    processed: list[dict[str, Any]] = []
    reasons: list[str] = []
    total_fee = Decimal("0")
    for drug in drugs:
        match = index.match(drug)
        output = dict(drug)
        output["catalog_match_status"] = match.status
        quantity = box_quantity(drug.get("total"))
        if match.entry is None:
            if match.status == MATCH_AMBIGUOUS:
                reasons.append("catalog_ambiguous")
            elif match.status == MATCH_INDEX_UNAVAILABLE:
                reasons.append("catalog_index_unavailable")
            else:
                reasons.append("catalog_not_found")
            output["drcode"] = ""
        elif quantity is None:
            reasons.append("box_quantity_invalid")
            output["drcode"] = str(match.entry["drcode"])
        else:
            unit_price = Decimal(str(match.entry["single_box_price"]))
            line_total = (unit_price * quantity).quantize(
                MONEY_QUANTUM,
                rounding=ROUND_HALF_UP,
            )
            output.update(
                {
                    "drcode": str(match.entry["drcode"]),
                    "platform_sku_id": str(match.entry["platform_sku_id"]),
                    "single_box_price": float(unit_price),
                    "box_quantity": quantity,
                    "line_total": float(line_total),
                }
            )
            total_fee += line_total
        processed.append(output)

    if not drugs:
        reasons.append("drug_list_missing")
    if total_fee > MAX_TOTAL_FEE:
        reasons.append("total_fee_out_of_range")
    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "status": "needs_review" if unique_reasons else "success",
        "reasons": unique_reasons,
        "drugs": processed,
        "total_fee": None if unique_reasons else float(total_fee),
    }


def build_from_files(
    paths: list[pathlib.Path],
    *,
    expected_products: int = 0,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for path in paths:
        parsed = parse_tmall_export(path)
        rows.extend(parsed)
        sources.append(source_metadata(path, len(parsed)))
    return build_product_index(
        rows,
        sources=sources,
        expected_products=expected_products,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build strict Tmall single-box product index")
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--expect-products", type=int, default=0)
    parser.add_argument("files", nargs="+", type=pathlib.Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        paths = [path.expanduser().resolve() for path in args.files]
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise ProductIndexError("source files not found: " + ", ".join(missing))
        payload = build_from_files(paths, expected_products=max(0, args.expect_products))
        write_index(args.output.expanduser().resolve(), payload)
    except (OSError, zipfile.BadZipFile, ET.ParseError, ProductIndexError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    summary = payload["summary"]
    print(
        f"completed products={summary['product_count']} skus={summary['sku_count']} "
        f"multi_sku={summary['multi_sku_products']} "
        f"price_mismatches={summary['item_price_mismatches']} "
        f"output={args.output.expanduser().resolve()}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

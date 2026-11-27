#!/usr/bin/env python3
"""Business drug catalog indexing and strict C-level regimen matching."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from prescription_usage_ocr import (
    calculate_duration,
    decimal_output,
    decimal_value,
    infer_purchase_unit,
    normalize_text,
    normalize_unit,
    parse_number,
    split_drug_name_specification,
)


CATALOG_SCHEMA_VERSION = 1
STATUS_CODE_VERSION = 2

STATUS_OCR_SUCCESS = 100
STATUS_CATALOG_SUCCESS = 110
STATUS_QUALITATIVE_30_DAY_SUCCESS = 120

STATUS_USAGE_NOT_FOUND = 201
STATUS_DOSE_MISSING = 202
STATUS_FREQUENCY_MISSING = 203
STATUS_PACKAGE_UNPARSED = 204
STATUS_PACKAGE_UNIT_UNRESOLVED = 205
STATUS_UNIT_CONVERSION_FAILED = 206
STATUS_DRUG_FORM_OCR_UNIT_CONFLICT = 207
STATUS_OCR_CATALOG_CONFLICT = 208
STATUS_OCR_LOW_CONFIDENCE = 209

STATUS_CATALOG_NAME_NOT_FOUND = 301
STATUS_CATALOG_CLINICAL_SPEC_MISMATCH = 302
STATUS_CATALOG_REGIMEN_CONFLICT = 303
STATUS_CATALOG_DOSE_INVALID = 304
STATUS_CATALOG_FREQUENCY_INVALID = 305
STATUS_CATALOG_UNAVAILABLE = 306
STATUS_CATALOG_SCHEMA_INVALID = 307


STATUS_LABELS = {
    STATUS_OCR_SUCCESS: "ocr_duration_success",
    STATUS_CATALOG_SUCCESS: "catalog_duration_success",
    STATUS_QUALITATIVE_30_DAY_SUCCESS: "qualitative_usage_catalog_30_day_success",
    STATUS_USAGE_NOT_FOUND: "usage_not_found",
    STATUS_DOSE_MISSING: "dose_missing",
    STATUS_FREQUENCY_MISSING: "frequency_missing",
    STATUS_PACKAGE_UNPARSED: "package_unparsed",
    STATUS_PACKAGE_UNIT_UNRESOLVED: "package_unit_unresolved",
    STATUS_UNIT_CONVERSION_FAILED: "unit_conversion_failed",
    STATUS_DRUG_FORM_OCR_UNIT_CONFLICT: "drug_form_ocr_unit_conflict",
    STATUS_OCR_CATALOG_CONFLICT: "ocr_catalog_conflict",
    STATUS_OCR_LOW_CONFIDENCE: "ocr_low_confidence",
    STATUS_CATALOG_NAME_NOT_FOUND: "catalog_name_not_found",
    STATUS_CATALOG_CLINICAL_SPEC_MISMATCH: "catalog_clinical_spec_mismatch",
    STATUS_CATALOG_REGIMEN_CONFLICT: "catalog_regimen_conflict",
    STATUS_CATALOG_DOSE_INVALID: "catalog_dose_invalid",
    STATUS_CATALOG_FREQUENCY_INVALID: "catalog_frequency_invalid",
    STATUS_CATALOG_UNAVAILABLE: "catalog_unavailable",
    STATUS_CATALOG_SCHEMA_INVALID: "catalog_schema_invalid",
}

QUALITATIVE_USAGE_TOKENS = ("适量", "必要时")
QUALITATIVE_DEFAULT_DURATION_DAYS = 30


def _text(value: Any) -> str:
    return str(value or "").strip()


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _package_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", normalize_text(value))
    return re.sub(r"\s+", "", normalized).lower()


def normalize_drug_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value))
    text = re.sub(r"^\s*[\[【][^\]】]+[\]】]\s*", "", text)
    text = text.translate(str.maketrans({"（": "(", "）": ")", "：": ":", "－": "-"}))
    return re.sub(r"\s+", "", text).lower()


def normalize_dosage_form(name: Any, hint: Any = "") -> str:
    candidate = unicodedata.normalize("NFKC", _text(hint))
    name_text = unicodedata.normalize("NFKC", _text(name))
    combined = f"{candidate}|{name_text}"
    form_patterns = (
        (r"软胶囊|胶囊剂?|胶丸", "胶囊"),
        (r"滴丸|丸剂?|水丸|蜜丸", "丸"),
        (r"肠溶片|缓释片|控释片|分散片|咀嚼片|泡腾片|含片|片剂?|片(?:\([^)]*\))?$", "片"),
        (r"颗粒剂?|冲剂", "颗粒"),
        (r"粉剂?|散剂?", "粉剂"),
        (r"口服液|口服溶液", "口服液"),
        (r"注射液|注射剂", "注射液"),
        (r"乳膏", "乳膏"),
        (r"软膏", "软膏"),
        (r"凝胶", "凝胶"),
        (r"鼻喷雾剂", "鼻喷雾剂"),
        (r"气雾剂|喷雾剂", "喷雾剂"),
        (r"滴眼液", "滴眼液"),
        (r"滴耳液", "滴耳液"),
    )
    for pattern, result in form_patterns:
        if re.search(pattern, combined):
            return result
    return candidate


def _mass_to_mg(value: Any, unit: Any) -> Optional[Decimal]:
    number = decimal_value(value)
    normalized = normalize_unit(unit)
    if number is None:
        return None
    factors = {"μg": Decimal("0.001"), "mg": Decimal("1"), "g": Decimal("1000")}
    return number * factors[normalized] if normalized in factors else None


def _volume_to_ml(value: Any, unit: Any) -> Optional[Decimal]:
    number = decimal_value(value)
    normalized = normalize_unit(unit)
    if number is None:
        return None
    factors = {"μl": Decimal("0.001"), "ml": Decimal("1"), "L": Decimal("1000")}
    return number * factors[normalized] if normalized in factors else None


def clinical_specification(name: Any, specification: Any, form_hint: Any = "") -> Optional[Dict[str, Any]]:
    """Build a strict clinical key; commercial pack counts are intentionally omitted."""
    spec = normalize_text(specification)
    form = normalize_dosage_form(name, form_hint)
    if not spec:
        return None

    components: Dict[str, Any] = {}

    percentages = re.findall(r"([0-9]+(?:\.[0-9]+)?)%", spec)
    if percentages:
        components["percent"] = [_decimal_text(Decimal(item)) for item in percentages]

    number = r"[0-9]+(?:\.[0-9]+)?"
    mass_unit = r"μg|微克|ug|mcg|mg|毫克|g|克"
    volume_unit = r"ml|毫升|L|升|μl|微升"

    mass_volume = re.search(
        rf"({number})({volume_unit}):({number})({mass_unit})",
        spec,
        re.IGNORECASE,
    )
    if mass_volume:
        volume_ml = _volume_to_ml(mass_volume.group(1), mass_volume.group(2))
        mass_mg = _mass_to_mg(mass_volume.group(3), mass_volume.group(4))
        if volume_ml is not None and mass_mg is not None:
            components["mass_volume"] = [_decimal_text(mass_mg), _decimal_text(volume_ml)]
    else:
        mass_volume = re.search(
            rf"({number})({mass_unit})[:/]({number})({volume_unit})",
            spec,
            re.IGNORECASE,
        )
        if mass_volume:
            mass_mg = _mass_to_mg(mass_volume.group(1), mass_volume.group(2))
            volume_ml = _volume_to_ml(mass_volume.group(3), mass_volume.group(4))
            if volume_ml is not None and mass_mg is not None:
                components["mass_volume"] = [_decimal_text(mass_mg), _decimal_text(volume_ml)]

    concentration = re.search(
        rf"({number})({mass_unit})/(?:({number}))?({volume_unit})",
        spec,
        re.IGNORECASE,
    )
    if concentration:
        mass_mg = _mass_to_mg(concentration.group(1), concentration.group(2))
        denominator_ml = _volume_to_ml(concentration.group(3) or "1", concentration.group(4))
        if mass_mg is not None and denominator_ml is not None and denominator_ml > 0:
            components["concentration_mg_ml"] = _decimal_text(mass_mg / denominator_ml)

    ratios = []
    for ratio in re.finditer(
        rf"({number})({mass_unit}):({number})({mass_unit})",
        spec,
        re.IGNORECASE,
    ):
        left = _mass_to_mg(ratio.group(1), ratio.group(2))
        right = _mass_to_mg(ratio.group(3), ratio.group(4))
        if left is not None and right is not None:
            ratios.append([_decimal_text(left), _decimal_text(right)])
    if ratios:
        components["mass_ratios_mg"] = ratios

    count_unit = r"片|粒|袋|支|瓶|丸|贴|枚|管|喷|揿|次|滴|包|吸|泡|颗|张|s|S|t|T"
    strength = re.search(
        rf"({number})({mass_unit})(?=\*(?:{number})(?:{count_unit}|(?:\*|/|$)))",
        spec,
        re.IGNORECASE,
    )
    if strength:
        strength_mg = _mass_to_mg(strength.group(1), strength.group(2))
        if strength_mg is not None:
            components["unit_strength_mg"] = _decimal_text(strength_mg)

    # Nutrition powders and similar bulk preparations often express the
    # clinically relevant unit content as ``320g``, ``320g/听`` or
    # ``320g 12瓶装``.  Pack counts are commercial information and are not
    # part of the C-level clinical key.
    if form in {"粉剂", "颗粒"} and "unit_strength_mg" not in components:
        unit_content = re.search(
            rf"({number})({mass_unit})(?=\s*(?:/)?(?:听|瓶|罐|袋|盒|桶|支|$|[0-9]+\s*(?:听|瓶|罐|袋|盒|桶|支)))",
            spec,
            re.IGNORECASE,
        )
        if unit_content:
            content_mg = _mass_to_mg(
                unit_content.group(1),
                unit_content.group(2),
            )
            if content_mg is not None:
                components["unit_content_mg"] = _decimal_text(content_mg)

    if not components:
        return None
    return {"form": form, **components}


def clinical_key(specification: Optional[Dict[str, Any]]) -> str:
    if not specification:
        return ""
    return json.dumps(specification, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_route(value: Any) -> str:
    route = normalize_text(value)
    if route in {"冲服", "嚼服", "口服"}:
        return "口服"
    return route


def qualitative_usage_tokens(*values: Any) -> tuple[str, ...]:
    text = "|".join(normalize_text(value) for value in values if _text(value))
    return tuple(token for token in QUALITATIVE_USAGE_TOKENS if token in text)


def parse_catalog_frequency(value: Any) -> Optional[Decimal]:
    text = normalize_text(value)
    if not text:
        return None
    token = rf"[0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十]+"

    range_match = re.fullmatch(
        rf"(?:一天|一日|每日|每天)({token})\s*[-~～至到]\s*({token})次",
        text,
    )
    if range_match:
        lower = parse_number(range_match.group(1))
        upper = parse_number(range_match.group(2))
        if lower and upper and lower > 0 and upper >= lower:
            return lower

    match = re.fullmatch(rf"(?:一天|一日|每日|每天)({token})次", text)
    if match:
        return parse_number(match.group(1))
    match = re.fullmatch(rf"(?:每)?({token})周({token})次", text)
    if match:
        weeks = parse_number(match.group(1))
        occurrences = parse_number(match.group(2))
        return occurrences / (weeks * Decimal("7")) if weeks and occurrences else None
    match = re.fullmatch(rf"(?:一周|每周)({token})次", text)
    if match:
        occurrences = parse_number(match.group(1))
        return occurrences / Decimal("7") if occurrences else None
    match = re.fullmatch(rf"(?:每)?({token})小时(?:一|1)次", text)
    if match:
        hours = parse_number(match.group(1))
        return Decimal("24") / hours if hours else None
    match = re.fullmatch(rf"({token})(?:日|天)(?:一|1)次", text)
    if match:
        days = parse_number(match.group(1))
        return Decimal("1") / days if days else None
    if text in {"隔日一次", "隔日1次", "隔天一次", "隔天1次"}:
        return Decimal("0.5")
    if text in {"睡前一次", "睡前1次", "临睡前"}:
        return Decimal("1")
    return None


def parse_catalog_dose(value: Any, separate_unit: Any = "") -> tuple[Optional[Decimal], str]:
    unit = normalize_unit(separate_unit)
    number = decimal_value(value)
    if number is not None:
        return number, unit

    text = normalize_text(value)
    if not text or qualitative_usage_tokens(text):
        return None, unit
    number_token = r"[0-9]+(?:\.[0-9]+)?"
    match = re.match(
        rf"^({number_token})(?:\s*[-~～至到]\s*({number_token}))?\s*([A-Za-zμ\u4e00-\u9fff]+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, unit
    lower = decimal_value(match.group(1))
    upper = decimal_value(match.group(2))
    if upper is not None and (lower is None or upper < lower):
        return None, unit
    return lower, unit or normalize_unit(match.group(3))


def _dose_signature(value: Any, unit: Any) -> Optional[list[str]]:
    number = decimal_value(value)
    normalized = normalize_unit(unit)
    if number is None or number <= 0 or not normalized:
        return None
    mass = _mass_to_mg(number, normalized)
    if mass is not None:
        return [_decimal_text(mass), "mg"]
    volume = _volume_to_ml(number, normalized)
    if volume is not None:
        return [_decimal_text(volume), "ml"]
    if normalized == "万单位":
        return [_decimal_text(number * Decimal("10000")), "IU"]
    return [_decimal_text(number), normalized]


def catalog_entry_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    name = _text(row.get("药品名称"))
    spec = _text(row.get("药品规格") or row.get("规格"))
    name_key = normalize_drug_name(name)
    clinical = clinical_specification(name, spec, row.get("剂型"))
    raw_dose = (
        row.get("推荐用量")
        if row.get("推荐用量") not in (None, "")
        else row.get("每次剂量")
    )
    raw_frequency = (
        row.get("常用频次")
        if row.get("常用频次") not in (None, "")
        else row.get("使用频度")
    )
    dose, unit = parse_catalog_dose(raw_dose, row.get("用量单位"))
    times = parse_catalog_frequency(raw_frequency)
    route = normalize_route(row.get("给药途径"))
    qualitative = qualitative_usage_tokens(raw_dose, raw_frequency)
    raw_explicit_duration = next(
        (
            row.get(key)
            for key in ("durationday", "duration_days", "固定用药天数")
            if row.get(key) not in (None, "")
        ),
        None,
    )
    explicit_duration = parse_number(raw_explicit_duration)
    if explicit_duration is not None and explicit_duration <= 0:
        explicit_duration = None
    raw_per_purchase_unit_duration = next(
        (
            row.get(key)
            for key in (
                "duration_days_per_purchase_unit",
                "每购买单位用药天数",
                "每盒用药天数",
            )
            if row.get(key) not in (None, "")
        ),
        None,
    )
    per_purchase_unit_duration = parse_number(raw_per_purchase_unit_duration)
    if per_purchase_unit_duration is not None and per_purchase_unit_duration <= 0:
        per_purchase_unit_duration = None
    duration_purchase_unit = normalize_unit(
        row.get("用药天数购买单位")
        or row.get("duration_purchase_unit")
        or ("盒" if row.get("每盒用药天数") not in (None, "") else "")
        or infer_purchase_unit(name, spec)
    )

    invalid_code = 0
    dose_signature = _dose_signature(dose, unit)
    if dose_signature is None:
        invalid_code = STATUS_CATALOG_DOSE_INVALID
    elif times is None or times <= 0:
        invalid_code = STATUS_CATALOG_FREQUENCY_INVALID

    signature = ""
    if not invalid_code:
        signature = json.dumps(
            [dose_signature, _decimal_text(times), route],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return {
        "id": _text(row.get("药品id") or row.get("药品ID")),
        "n": name_key,
        "c": clinical_key(clinical),
        "p": _package_key(spec),
        "d": decimal_output(dose),
        "u": unit,
        "f": _text(raw_frequency),
        "t": decimal_output(times, 12),
        "r": route,
        "s": signature,
        "x": invalid_code,
        "q": list(qualitative),
        "e": decimal_output(explicit_duration),
        "ep": decimal_output(per_purchase_unit_duration),
        "eu": duration_purchase_unit if per_purchase_unit_duration is not None else "",
    }


@dataclass
class CatalogMatch:
    status_code: int
    regimen: Optional[Dict[str, Any]] = None
    candidate_ids: tuple[str, ...] = ()
    qualitative_tokens: tuple[str, ...] = ()
    explicit_duration_days: Optional[Decimal] = None
    duration_days_per_purchase_unit: Optional[Decimal] = None
    duration_purchase_unit: str = ""


class DrugCatalog:
    def __init__(self, entries: Iterable[Dict[str, Any]], load_status: int = 0):
        self.load_status = load_status
        self.by_name: Dict[str, list[Dict[str, Any]]] = {}
        for entry in entries:
            self.by_name.setdefault(_text(entry.get("n")), []).append(entry)

    @classmethod
    def load(cls, path: Path) -> "DrugCatalog":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls([], STATUS_CATALOG_UNAVAILABLE)
        if payload.get("schema_version") != CATALOG_SCHEMA_VERSION or not isinstance(payload.get("entries"), list):
            return cls([], STATUS_CATALOG_SCHEMA_INVALID)
        return cls(payload["entries"])

    def match(self, drug: Dict[str, Any]) -> CatalogMatch:
        if self.load_status:
            return CatalogMatch(self.load_status)
        name, spec = split_drug_name_specification(drug.get("drugName"), drug.get("packingSpec"))
        candidates = self.by_name.get(normalize_drug_name(name), [])
        if not candidates:
            return CatalogMatch(STATUS_CATALOG_NAME_NOT_FOUND)
        package_key = _package_key(spec)
        direct_entries = [
            entry
            for entry in candidates
            if decimal_value(entry.get("e")) is not None
            and decimal_value(entry.get("e")) > 0
            and _text(entry.get("p")) == package_key
        ]
        direct_values = {
            value
            for entry in direct_entries
            for value in [decimal_value(entry.get("e"))]
            if value is not None and value > 0
        }
        direct_ids = tuple(
            _text(entry.get("id"))
            for entry in direct_entries
            if _text(entry.get("id"))
        )
        if len(direct_values) == 1:
            return CatalogMatch(
                STATUS_CATALOG_SUCCESS,
                candidate_ids=direct_ids,
                explicit_duration_days=next(iter(direct_values)),
            )
        if len(direct_values) > 1:
            return CatalogMatch(
                STATUS_CATALOG_REGIMEN_CONFLICT,
                candidate_ids=direct_ids,
            )
        per_unit_entries = [
            entry
            for entry in candidates
            if decimal_value(entry.get("ep")) is not None
            and decimal_value(entry.get("ep")) > 0
            and _text(entry.get("eu"))
            and _text(entry.get("p")) == package_key
        ]
        per_unit_values = {
            (decimal_value(entry.get("ep")), normalize_unit(entry.get("eu")))
            for entry in per_unit_entries
        }
        per_unit_ids = tuple(
            _text(entry.get("id"))
            for entry in per_unit_entries
            if _text(entry.get("id"))
        )
        if len(per_unit_values) == 1:
            days, unit = next(iter(per_unit_values))
            return CatalogMatch(
                STATUS_CATALOG_SUCCESS,
                candidate_ids=per_unit_ids,
                duration_days_per_purchase_unit=days,
                duration_purchase_unit=unit,
            )
        if len(per_unit_values) > 1:
            return CatalogMatch(
                STATUS_CATALOG_REGIMEN_CONFLICT,
                candidate_ids=per_unit_ids,
            )
        key = clinical_key(clinical_specification(name, spec))
        if not key:
            return CatalogMatch(STATUS_CATALOG_CLINICAL_SPEC_MISMATCH)
        exact = [entry for entry in candidates if entry.get("c") == key]
        if not exact:
            return CatalogMatch(STATUS_CATALOG_CLINICAL_SPEC_MISMATCH)
        candidate_ids = tuple(_text(entry.get("id")) for entry in exact if _text(entry.get("id")))
        explicit_entries = [
            entry
            for entry in exact
            if decimal_value(entry.get("e")) is not None
            and decimal_value(entry.get("e")) > 0
            and _text(entry.get("p")) == package_key
        ]
        explicit_values = {
            value
            for entry in explicit_entries
            for value in [decimal_value(entry.get("e"))]
            if value is not None and value > 0
        }
        if explicit_values:
            if len(explicit_values) != 1:
                return CatalogMatch(
                    STATUS_CATALOG_REGIMEN_CONFLICT,
                    candidate_ids=candidate_ids,
                )
            return CatalogMatch(
                STATUS_CATALOG_SUCCESS,
                candidate_ids=candidate_ids,
                explicit_duration_days=next(iter(explicit_values)),
            )
        invalid = {int(entry.get("x") or 0) for entry in exact if int(entry.get("x") or 0)}
        if invalid:
            qualitative_sets = {
                tuple(
                    token
                    for token in QUALITATIVE_USAGE_TOKENS
                    if token in (entry.get("q") or [])
                )
                for entry in exact
            }
            qualitative_sets.discard(())
            qualitative = (
                next(iter(qualitative_sets))
                if len(qualitative_sets) == 1
                else ()
            )
            return CatalogMatch(
                min(invalid),
                candidate_ids=candidate_ids,
                qualitative_tokens=qualitative,
            )
        signatures = {_text(entry.get("s")) for entry in exact}
        if len(signatures) != 1:
            return CatalogMatch(STATUS_CATALOG_REGIMEN_CONFLICT, candidate_ids=candidate_ids)
        selected = exact[0]
        regimen = {
            "dose_per_time": selected.get("d"),
            "dose_unit": _text(selected.get("u")),
            "frequency_text": _text(selected.get("f")),
            "times_per_day": selected.get("t"),
            "administration_route": _text(selected.get("r")),
        }
        return CatalogMatch(STATUS_CATALOG_SUCCESS, regimen=regimen, candidate_ids=candidate_ids)

    def apply_if_needed(self, drug: Dict[str, Any]) -> Dict[str, Any]:
        match = self.match(drug)
        if match.explicit_duration_days is not None:
            explicit_days = match.explicit_duration_days
            drug["duration_days"] = int(explicit_days)
            drug["duration_days_exact"] = decimal_output(explicit_days)
            drug["duration_calculation_status"] = "success"
            drug["duration_calculation_reason"] = "catalog_explicit_duration_days"
            drug["duration_rule_code"] = "catalog_explicit_duration_days"
            drug["explicit_duration_days"] = decimal_output(explicit_days)
            drug["usage_source"] = "business_drug_catalog"
            drug["status_code"] = STATUS_CATALOG_SUCCESS
            return drug
        if match.duration_days_per_purchase_unit is not None:
            purchase_quantity = decimal_value(drug.get("purchase_quantity"))
            if purchase_quantity is None:
                purchase_quantity = decimal_value(drug.get("drugCount"))
            purchase_unit = normalize_unit(
                drug.get("purchase_unit")
                or infer_purchase_unit(drug.get("drugName"), drug.get("packingSpec"))
            )
            expected_unit = normalize_unit(match.duration_purchase_unit)
            if (
                purchase_quantity is None
                or purchase_quantity <= 0
                or not purchase_unit
                or purchase_unit != expected_unit
            ):
                drug["duration_days"] = None
                drug["duration_calculation_status"] = "needs_review"
                drug["duration_calculation_reason"] = "catalog_purchase_unit_mismatch"
                drug["status_code"] = STATUS_UNIT_CONVERSION_FAILED
                return drug
            duration = match.duration_days_per_purchase_unit * purchase_quantity
            drug["duration_days"] = int(duration)
            drug["duration_days_exact"] = decimal_output(duration)
            drug["duration_calculation_status"] = "success"
            drug["duration_calculation_reason"] = (
                "catalog_duration_days_per_purchase_unit"
            )
            drug["duration_rule_code"] = (
                "catalog_duration_days_per_purchase_unit"
            )
            drug["duration_days_per_purchase_unit"] = decimal_output(
                match.duration_days_per_purchase_unit
            )
            drug["duration_purchase_unit"] = expected_unit
            drug["usage_source"] = "business_drug_catalog"
            drug["status_code"] = STATUS_CATALOG_SUCCESS
            return drug
        dose = decimal_value(drug.get("dose_per_time"))
        frequency = decimal_value(drug.get("times_per_day"))
        if dose is not None and dose > 0 and frequency is not None and frequency > 0:
            drug["status_code"] = status_code_for_drug(drug)
            return drug

        source_qualitative = qualitative_usage_tokens(
            drug.get("usage_text"),
            drug.get("frequency_text"),
            drug.get("usage_method"),
        )
        if source_qualitative and match.qualitative_tokens:
            duration = calculate_duration(
                drug,
                {"explicit_duration_days": QUALITATIVE_DEFAULT_DURATION_DAYS},
            )
            drug.update(duration)
            drug["duration_days"] = QUALITATIVE_DEFAULT_DURATION_DAYS
            drug["duration_days_exact"] = QUALITATIVE_DEFAULT_DURATION_DAYS
            drug["duration_calculation_status"] = "success"
            drug["duration_calculation_reason"] = (
                "qualitative_usage_catalog_default_30_days"
            )
            drug["duration_rule_code"] = (
                "qualitative_usage_catalog_default_30_days"
            )
            drug["usage_source"] = "ocr_catalog_qualitative_default"
            drug["status_code"] = STATUS_QUALITATIVE_30_DAY_SUCCESS
            drug["qualitative_usage_tokens"] = list(source_qualitative)
            drug["catalog_qualitative_tokens"] = list(
                match.qualitative_tokens
            )
            return drug
        if match.status_code != STATUS_CATALOG_SUCCESS or not match.regimen:
            drug["duration_days"] = None
            drug["duration_calculation_status"] = "needs_review"
            drug["duration_calculation_reason"] = STATUS_LABELS[match.status_code]
            drug["status_code"] = match.status_code
            if match.candidate_ids:
                drug["review"] = {
                    "candidate_count": len(match.candidate_ids),
                    "candidate_ids": list(match.candidate_ids),
                }
            return drug

        regimen = match.regimen
        known_dose = _dose_signature(dose, drug.get("dose_unit")) if dose is not None and dose > 0 else None
        catalog_dose = _dose_signature(regimen.get("dose_per_time"), regimen.get("dose_unit"))
        known_route = normalize_route(drug.get("administration_route"))
        catalog_route = normalize_route(regimen.get("administration_route"))
        if known_dose and known_dose != catalog_dose:
            return self._catalog_conflict(drug, match)
        if frequency is not None and frequency > 0:
            catalog_frequency = decimal_value(regimen.get("times_per_day"))
            if catalog_frequency is None or abs(frequency - catalog_frequency) > Decimal("0.000001"):
                return self._catalog_conflict(drug, match)
        if known_route and catalog_route and known_route != catalog_route:
            return self._catalog_conflict(drug, match)

        original_source = _text(drug.get("usage_source"))
        if dose is None or dose <= 0:
            drug["dose_per_time"] = regimen["dose_per_time"]
            drug["dose_unit"] = regimen["dose_unit"]
        if frequency is None or frequency <= 0:
            drug["frequency_text"] = regimen["frequency_text"]
            drug["times_per_day"] = regimen["times_per_day"]
        if not known_route:
            drug["administration_route"] = regimen["administration_route"]
            drug["usage_method"] = regimen["administration_route"]

        usage = {
            "explicit_duration_days": drug.get("explicit_duration_days"),
            "dose_per_time": drug.get("dose_per_time"),
            "dose_unit": drug.get("dose_unit"),
            "times_per_day": drug.get("times_per_day"),
        }
        drug.update(calculate_duration(drug, usage))
        drug["usage_source"] = "ocr_catalog" if original_source else "business_drug_catalog"
        drug["status_code"] = (
            STATUS_CATALOG_SUCCESS
            if drug.get("duration_calculation_status") == "success"
            else status_code_for_drug(drug)
        )
        if drug["status_code"] >= 200:
            drug["review"] = {"candidate_count": len(match.candidate_ids), "candidate_ids": list(match.candidate_ids)}
        return drug

    @staticmethod
    def _catalog_conflict(drug: Dict[str, Any], match: CatalogMatch) -> Dict[str, Any]:
        drug["duration_days"] = None
        drug["duration_calculation_status"] = "needs_review"
        drug["duration_calculation_reason"] = STATUS_LABELS[STATUS_OCR_CATALOG_CONFLICT]
        drug["status_code"] = STATUS_OCR_CATALOG_CONFLICT
        drug["review"] = {
            "candidate_count": len(match.candidate_ids),
            "candidate_ids": list(match.candidate_ids),
        }
        return drug


def status_code_for_drug(drug: Dict[str, Any]) -> int:
    existing_status = int(drug.get("status_code") or 0)
    if existing_status in {
        STATUS_OCR_SUCCESS,
        STATUS_CATALOG_SUCCESS,
        STATUS_QUALITATIVE_30_DAY_SUCCESS,
    } or existing_status >= 300:
        return existing_status
    if drug.get("duration_calculation_status") == "success":
        return STATUS_CATALOG_SUCCESS if "catalog" in _text(drug.get("usage_source")) else STATUS_OCR_SUCCESS
    reason = _text(drug.get("duration_calculation_reason"))
    mapping = {
        "usage_not_found_in_image": STATUS_USAGE_NOT_FOUND,
        "multiple_usage_candidates_for_single_drug": STATUS_USAGE_NOT_FOUND,
        "ocr_runtime_error": STATUS_USAGE_NOT_FOUND,
        "dose_missing_or_unparsed": STATUS_DOSE_MISSING,
        "frequency_missing_or_unparsed": STATUS_FREQUENCY_MISSING,
        "package_quantity_missing_or_unparsed": STATUS_PACKAGE_UNPARSED,
        "container_volume_missing_for_unit_conversion": STATUS_PACKAGE_UNIT_UNRESOLVED,
        "strength_missing_for_unit_conversion": STATUS_PACKAGE_UNIT_UNRESOLVED,
        "dose_and_package_units_mismatch": STATUS_UNIT_CONVERSION_FAILED,
        "drug_form_and_ocr_unit_conflict": STATUS_DRUG_FORM_OCR_UNIT_CONFLICT,
        "ocr_confidence_below_threshold": STATUS_OCR_LOW_CONFIDENCE,
    }
    return mapping.get(reason, STATUS_PACKAGE_UNPARSED)


def compact_processing_result(drug: Dict[str, Any], index: int) -> Dict[str, Any]:
    code = int(drug.get("status_code") or status_code_for_drug(drug))
    result: Dict[str, Any] = {"drug_index": index, "status_code": code}
    review = drug.get("review")
    if code >= 200 and isinstance(review, dict) and review:
        result["review"] = review
    return result

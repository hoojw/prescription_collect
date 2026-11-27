#!/usr/bin/env python3
"""Local PaddleOCR extraction of prescription fields and medication duration.

The middle image area supplies medication usage. Separate top and upper-right
crops supply the issuing hospital, birth date and department.
"""

from __future__ import annotations

import math
import os
import re
import argparse
import difflib
import json
import datetime as dt
import time
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_OCR_CPU_THREADS = "3"
os.environ["PRESCRIPTION_OCR_THREADS"] = DEFAULT_OCR_CPU_THREADS
os.environ["PADDLE_PDX_CPU_NUM_THREADS"] = DEFAULT_OCR_CPU_THREADS
for _thread_env_name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[_thread_env_name] = "1"


NUMBER_CHARS = "零〇一二两三四五六七八九十半"
NUMBER_MAP = {
    "零": Decimal("0"),
    "〇": Decimal("0"),
    "一": Decimal("1"),
    "二": Decimal("2"),
    "两": Decimal("2"),
    "三": Decimal("3"),
    "四": Decimal("4"),
    "五": Decimal("5"),
    "六": Decimal("6"),
    "七": Decimal("7"),
    "八": Decimal("8"),
    "九": Decimal("9"),
    "十": Decimal("10"),
    "半": Decimal("0.5"),
}

UNIT_ALIASES = {
    "毫升": "ml",
    "ML": "ml",
    "mL": "ml",
    "ml": "ml",
    "克": "g",
    "G": "g",
    "g": "g",
    "毫克": "mg",
    "MG": "mg",
    "mg": "mg",
    "微克": "μg",
    "μG": "μg",
    "μg": "μg",
    "UG": "μg",
    "ug": "μg",
    "MCG": "μg",
    "mcg": "μg",
    "小袋": "袋",
    "小瓶": "瓶",
    "国际单位": "IU",
    "国际单位(IU)": "IU",
    "IU": "IU",
    "iu": "IU",
    "升": "L",
    "L": "L",
    "l": "L",
    "微升": "μl",
    "μL": "μl",
    "ul": "μl",
    "UL": "μl",
}

COUNT_UNITS = (
    "片", "粒", "袋", "支", "瓶", "丸", "贴", "枚", "管", "喷", "揿", "次",
    "滴", "包", "吸", "泡", "颗", "张", "份", "ml", "g", "mg", "μg", "IU", "万单位",
)
DISCRETE_DOSE_UNITS = {
    "片", "粒", "袋", "支", "瓶", "丸", "贴", "枚", "管", "喷", "揿", "次",
    "滴", "包", "吸", "泡", "颗", "张", "份",
}
# Only for inhalation products, these package and administration units all
# represent one discrete inhaled dose. They must not be treated as generally
# interchangeable for non-inhalation products.
INHALATION_DISCRETE_UNITS = {"泡", "吸", "揿"}
DOSE_UNITS_PATTERN = (
    r"国际单位\(IU\)|国际单位|万单位|小袋|小瓶|毫升|微升|微克|毫克|"
    r"mcg|MCG|ml|mL|ML|μg|μG|ug|UG|mg|MG|IU|iu|"
    r"片|粒|袋|支|瓶|丸|贴|枚|管|喷|揿|次|滴|包|吸|泡|颗|张|份|克|升|g|G"
)
PACKAGE_UNIT_PATTERN = DOSE_UNITS_PATTERN
PACK_MULTIPLIER_UNITS = ("板", "瓶", "盒", "袋", "支", "管", "罐", "包")

DOSAGE_OCR_CORRECTIONS = {
    "掀": "揿",
}


def normalize_text(text: Any) -> str:
    value = str(text or "")
    table = str.maketrans({"：": ":", "，": ",", "；": ";", "×": "*", "ｘ": "*", "X": "*"})
    value = value.translate(table)
    value = re.sub(r"\s+", "", value)
    return value


def normalize_dosage_ocr_text(text: Any) -> str:
    value = normalize_text(text)
    for wrong, correct in DOSAGE_OCR_CORRECTIONS.items():
        value = value.replace(wrong, correct)
    return value


def parse_number(value: Any) -> Optional[Decimal]:
    text = normalize_text(value)
    if not text:
        return None
    if text in NUMBER_MAP:
        return NUMBER_MAP[text]
    if text == "十一":
        return Decimal("11")
    if text.startswith("十") and len(text) == 2 and text[1] in NUMBER_MAP:
        return Decimal("10") + NUMBER_MAP[text[1]]
    if text.endswith("十") and len(text) == 2 and text[0] in NUMBER_MAP:
        return NUMBER_MAP[text[0]] * Decimal("10")
    if "十" in text and len(text) == 3 and text[0] in NUMBER_MAP and text[2] in NUMBER_MAP:
        return NUMBER_MAP[text[0]] * Decimal("10") + NUMBER_MAP[text[2]]
    if text in {"1/2", "0.5"}:
        return Decimal("0.5")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def normalize_unit(unit: Any) -> str:
    text = str(unit or "")
    return UNIT_ALIASES.get(text, text.lower())


def convert_compatible_quantity(value: Decimal, source_unit: str, target_unit: str) -> Optional[Decimal]:
    unit_families = (
        {"μg": Decimal("0.001"), "mg": Decimal("1"), "g": Decimal("1000")},
        {"μl": Decimal("0.001"), "ml": Decimal("1"), "L": Decimal("1000")},
        {"IU": Decimal("1"), "万单位": Decimal("10000")},
    )
    for factors in unit_families:
        if source_unit in factors and target_unit in factors:
            return value * factors[source_unit] / factors[target_unit]
    return None


def is_inhalation_discrete_unit_equivalence(
    drug_name: Any,
    dose_unit: str,
    package_unit: str,
) -> bool:
    """Whether inhalation-specific discrete units can be used 1:1.

    For products whose name identifies them as inhalation medicines, a package
    count in 泡、吸 or 揿 and an OCR dosage in one of those units are both a
    count of delivered inhaled doses. This deliberately does not apply to
    ordinary discrete dosage units or non-inhalation medicines.
    """
    return (
        "吸入" in normalize_text(drug_name)
        and dose_unit in INHALATION_DISCRETE_UNITS
        and package_unit in INHALATION_DISCRETE_UNITS
    )


def decimal_value(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def decimal_output(value: Optional[Decimal], places: int = 4) -> Optional[float]:
    if value is None:
        return None
    quantized = value.quantize(Decimal("1." + "0" * places)).normalize()
    return float(quantized)


def quantity_output(value: Optional[Decimal]) -> Optional[Any]:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return decimal_output(value)


def parse_usage_text(text: Any) -> Dict[str, Any]:
    raw = str(text or "").strip()
    normalized = normalize_dosage_ocr_text(raw)
    result: Dict[str, Any] = {
        "usage_text": raw,
        "usage_method": "",
        "administration_route": "",
        "dose_per_time": None,
        "dose_unit": "",
        "frequency_text": "",
        "times_per_day": None,
        "explicit_duration_days": None,
    }

    method_routes = (
        ("餐时口服", "口服"),
        ("餐前口服", "口服"),
        ("餐后口服", "口服"),
        ("临睡前口服", "口服"),
        ("睡前口服", "口服"),
        ("空腹口服", "口服"),
        ("舌下含服", "舌下含服"),
        ("阴道给药", "阴道给药"),
        ("直肠给药", "直肠给药"),
        ("口腔喷入", "口腔喷入"),
        ("鼻腔喷入", "鼻腔喷入"),
        ("雾化吸入", "雾化吸入"),
        ("皮下注射", "皮下注射"),
        ("肌肉注射", "肌肉注射"),
        ("静脉注射", "静脉注射"),
        ("静脉滴注", "静脉滴注"),
        ("吸入给药", "吸入"),
        ("局部外用", "外用"),
        ("喷鼻", "鼻腔喷入"),
        ("含漱", "含漱"),
        ("嚼服", "嚼服"),
        ("冲服", "冲服"),
        ("涂抹", "涂抹"),
        ("口服", "口服"),
        ("外用", "外用"),
        ("肌注", "肌注"),
        ("静滴", "静滴"),
        ("鼻饲", "鼻饲"),
        ("吸入", "吸入"),
        ("滴眼", "滴眼"),
        ("滴耳", "滴耳"),
        ("滴鼻", "滴鼻"),
    )
    for method, route in method_routes:
        if method in normalized:
            result["usage_method"] = method
            result["administration_route"] = route
            break

    number_token = rf"[0-9.]+|[{NUMBER_CHARS}]+|1/2"
    frequency_span: Optional[Tuple[int, int]] = None
    frequency_patterns = (
        (rf"(?:每)?({number_token})(?:日|天)({number_token})次", "period_days"),
        (rf"(?:每日|每天)({number_token})次", "daily"),
        (rf"隔日({number_token})次", "alternate_days"),
        (rf"每({number_token})小时(?:1|一)?次", "hourly"),
        (rf"(?:每)?({number_token})周({number_token})次", "period_weeks"),
        (rf"每周({number_token})次", "weekly"),
        (rf"每(?:隔)?({number_token})(?:日|天)(?:1|一)?次", "every_days"),
    )
    for pattern, kind in frequency_patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        if kind in {"period_days", "period_weeks"}:
            period = parse_number(match.group(1))
            occurrences = parse_number(match.group(2))
            if period is None or period <= 0 or occurrences is None or occurrences <= 0:
                continue
            divisor = period * (Decimal("7") if kind == "period_weeks" else Decimal("1"))
            per_day = occurrences / divisor
        elif kind == "hourly":
            number = parse_number(match.group(1))
            if number is None or number <= 0:
                continue
            per_day = Decimal("24") / number
        elif kind == "weekly":
            number = parse_number(match.group(1))
            if number is None or number <= 0:
                continue
            per_day = number / Decimal("7")
        elif kind == "every_days":
            number = parse_number(match.group(1))
            if number is None or number <= 0:
                continue
            per_day = Decimal("1") / number
        elif kind == "alternate_days":
            number = parse_number(match.group(1))
            if number is None or number <= 0:
                continue
            per_day = number / Decimal("2")
        else:
            number = parse_number(match.group(1))
            if number is None or number <= 0:
                continue
            per_day = number
        result["frequency_text"] = match.group(0)
        frequency_span = match.span()
        # Keep enough precision for fractional schedules such as 1/14 day.
        # Otherwise a mathematically exact 28-day course can become 27 after
        # the approved floor operation because 0.071429 is slightly high.
        result["times_per_day"] = decimal_output(per_day, 12)
        break

    if result["times_per_day"] is None:
        special_frequencies = (
            (("早晚各一次", "早晚各1次"), "早晚各一次", 2.0),
            (("隔日一次", "隔日1次"), "隔日一次", 0.5),
            (("每晚一次", "每晚1次"), "一日1次", 1.0),
            (("睡前一次", "睡前1次"), "睡前一次", 1.0),
        )
        for variants, frequency_text, times_per_day in special_frequencies:
            matched_variant = next(
                (variant for variant in variants if variant in normalized),
                "",
            )
            if not matched_variant:
                continue
            start = normalized.index(matched_variant)
            frequency_span = (start, start + len(matched_variant))
            result["frequency_text"] = frequency_text
            result["times_per_day"] = times_per_day
            break

    if result["times_per_day"] is None:
        if "必要时" in normalized:
            result["frequency_text"] = "必要时"
        elif "适量" in normalized:
            result["frequency_text"] = "适量"

    # OCR may omit punctuation and return text such as
    # ``一日一次一次2.0片``.  Remove the already parsed frequency span before
    # looking for the per-dose phrase, otherwise the first frequency ``一次``
    # can be misread as ``一次`` + ``一`` + ``次``.
    dose_search_text = normalized
    if frequency_span is not None:
        start, end = frequency_span
        dose_search_text = f"{normalized[:start]},{normalized[end:]}"
    dose_matches = list(
        re.finditer(
            rf"(?:每次|一次)({number_token})({DOSE_UNITS_PATTERN})",
            dose_search_text,
            re.IGNORECASE,
        )
    )
    if dose_matches:
        concrete_matches = [
            match
            for match in dose_matches
            if normalize_unit(match.group(2)) != "次"
        ]
        dose_match = (concrete_matches or dose_matches)[0]
        result["dose_per_time"] = decimal_output(parse_number(dose_match.group(1)))
        result["dose_unit"] = normalize_unit(dose_match.group(2))

    duration_match = re.search(rf"(?:用药|连用|疗程)({number_token})(天|日|周)", normalized)
    if duration_match:
        duration = parse_number(duration_match.group(1))
        if duration is not None:
            if duration_match.group(2) == "周":
                duration *= Decimal("7")
            result["explicit_duration_days"] = decimal_output(duration)
    return result


def strip_brand_prefix(value: Any) -> str:
    return re.sub(r"^\s*[\[【][^\]】]+[\]】]\s*", "", str(value or "")).strip()


def split_drug_name_specification(drug_name: Any, packing_spec: Any = "") -> Tuple[str, str]:
    name = strip_brand_prefix(drug_name)
    explicit_spec = str(packing_spec or "").strip()
    if explicit_spec:
        return name, explicit_spec
    patch_spec = re.search(
        r"[(（]?\s*[0-9]+(?:\.[0-9]+)?\s*(?:cm|mm)\s*[*×xX]\s*"
        r"[0-9]+(?:\.[0-9]+)?\s*(?:cm|mm)\s*[)）]?\s*[*×xX]\s*"
        r"[0-9]+(?:\.[0-9]+)?\s*贴(?:\s*[*×xX]\s*[0-9]+\s*(?:袋|板|包|盒))*"
        r"(?:\s*/\s*(?:盒|袋|板|包|罐|瓶|支|管))*\s*$",
        name,
        re.IGNORECASE,
    )
    if patch_spec:
        return name[: patch_spec.start()].strip(), patch_spec.group(0).strip()
    match = re.search(
        r"[0-9]+(?:\.[0-9]+)?\s*(?:%|μg|µg|ug|mcg|mg|g|ml|μl|l|kcal|微克|毫克|克|毫升|微升|升|片|粒|袋|支|瓶|贴|揿|喷|罐|盒|s|t).*$",
        name,
        re.IGNORECASE,
    )
    if not match:
        return name, ""
    start = match.start()
    if start > 0 and name[start - 1] in "(（":
        start -= 1
    return name[:start].strip(), name[start:].strip()


def infer_purchase_unit(drug_name: Any, packing_spec: Any = "") -> str:
    spec = normalize_text(packing_spec) or normalize_text(drug_name)
    matches = re.findall(r"/(?:小)?(盒|瓶|支|袋|板|管|罐|包)(?=/|$)", spec)
    if matches:
        return matches[-1]
    return ""


def canonical_drug_key(drug: Dict[str, Any]) -> str:
    drug_name = normalize_text(strip_brand_prefix(drug.get("drugName"))).lower()
    packing_spec = normalize_text(drug.get("packingSpec")).lower()
    return f"{drug_name}|{packing_spec}"


def merge_same_drugs(drug_infos: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    for index, raw_drug in enumerate(drug_infos):
        if not isinstance(raw_drug, dict):
            continue
        key = canonical_drug_key(raw_drug)
        if not key.strip("|"):
            key = f"__missing_name_{index}"
        count = decimal_value(raw_drug.get("drugCount")) or Decimal("0")
        source_count = quantity_output(count)
        if key not in by_key:
            drug = dict(raw_drug)
            drug.pop("drugPrice", None)
            drug.pop("drugTotalPrice", None)
            drug["drugCount"] = quantity_output(count)
            drug["source_drug_indices"] = [index]
            drug["source_drug_counts"] = [source_count]
            drug["merged_source_count"] = 1
            drug["merged_same_drug"] = False
            by_key[key] = drug
            merged.append(drug)
            continue
        drug = by_key[key]
        combined = (decimal_value(drug.get("drugCount")) or Decimal("0")) + count
        drug["drugCount"] = quantity_output(combined)
        drug["source_drug_indices"].append(index)
        drug["source_drug_counts"].append(source_count)
        drug["merged_source_count"] = len(drug["source_drug_indices"])
        drug["merged_same_drug"] = True
        if not drug.get("packingSpec") and raw_drug.get("packingSpec"):
            drug["packingSpec"] = raw_drug["packingSpec"]
    return merged


def build_upload_medicine_info(drug_infos: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for drug in drug_infos:
        if not isinstance(drug, dict):
            continue
        drug_name, specification = split_drug_name_specification(
            drug.get("drugName"), drug.get("packingSpec")
        )
        purchase_quantity = drug.get("purchase_quantity")
        if purchase_quantity is None:
            purchase_quantity = quantity_output(decimal_value(drug.get("drugCount")))
        purchase_unit = str(drug.get("purchase_unit") or infer_purchase_unit(drug.get("drugName"), drug.get("packingSpec")))
        usage_text = "|".join(
            str(drug.get(key) or "")
            for key in ("usage_text", "frequency_text", "usage_method")
        )
        usage_frequency = str(drug.get("frequency_text") or "")
        dosage_per_time = drug.get("dose_per_time")
        dosage_unit = str(drug.get("dose_unit") or "")
        if "必要时" in usage_text and not usage_frequency:
            usage_frequency = "必要时"
        if "适量" in usage_text:
            dosage_per_time = "每次"
            dosage_unit = "适量"
        output.append(
            {
                "drug_name": drug_name,
                "specification": specification,
                "total_quantity": purchase_quantity,
                "total_unit": purchase_unit,
                "usage_method": str(drug.get("usage_method") or drug.get("administration_route") or ""),
                "usage_frequency": usage_frequency,
                "dosage_per_time": dosage_per_time,
                "dosage_unit": dosage_unit,
                "duration_days": drug.get("duration_days"),
                "drcode": str(drug.get("drcode") or ""),
            }
        )
    return output


def parse_concentration_total_mass(specification: Any) -> Optional[Tuple[Decimal, str]]:
    """Return total active mass for concentration-volume package formats."""
    spec = normalize_text(specification)
    number = r"[0-9]+(?:\.[0-9]+)?"
    concentration_pattern = re.compile(
        rf"({number})(μg|微克|ug|mcg|mg|毫克|g|克)/(?:({number}))?(?:ml|毫升)",
        re.IGNORECASE,
    )
    for concentration in concentration_pattern.finditer(spec):
        mass = decimal_value(concentration.group(1))
        denominator = decimal_value(concentration.group(3)) or Decimal("1")
        if mass is None or denominator <= 0:
            continue

        volume = None
        after = spec[concentration.end() :]
        after_match = re.match(rf"\*({number})(?:ml|毫升)", after, re.IGNORECASE)
        if after_match:
            volume = decimal_value(after_match.group(1))
        if volume is None:
            before = spec[: concentration.start()]
            before_match = re.search(rf"({number})(?:ml|毫升)[(（]?$", before, re.IGNORECASE)
            if before_match:
                volume = decimal_value(before_match.group(1))
        if volume is not None and volume > 0:
            return mass * volume / denominator, normalize_unit(concentration.group(2))
    return None


def infer_count_unit_from_drug_name(drug_name: Any, packing_spec: Any = "") -> str:
    name, _ = split_drug_name_specification(drug_name, packing_spec)
    normalized = normalize_text(name)
    if re.search(r"(?:片|肠溶片|缓释片|控释片|分散片|咀嚼片|泡腾片)(?:[(（][^()（）]*[)）])?$", normalized):
        return "片"
    if re.search(r"(?:软胶囊|胶囊)(?:[(（][^()（）]*[)）])?$", normalized):
        return "粒"
    if re.search(r"(?:滴丸|丸)(?:[(（][^()（）]*[)）])?$", normalized):
        return "丸"
    return ""


def parse_package_quantity(drug_name: Any, packing_spec: Any, drug_count: Any) -> Dict[str, Any]:
    spec = normalize_text(packing_spec) or normalize_text(drug_name)
    purchase_count = decimal_value(drug_count)
    purchase_unit = infer_purchase_unit(drug_name, packing_spec)
    result: Dict[str, Any] = {
        "package_text": spec,
        "package_quantity_per_box": None,
        "package_quantity_unit": "",
        "package_count": quantity_output(purchase_count),
        "purchase_quantity": quantity_output(purchase_count),
        "purchase_unit": purchase_unit,
        "total_quantity": quantity_output(purchase_count),
        "total_unit": purchase_unit,
        "calculation_total_quantity": None,
        "calculation_total_unit": "",
        "unit_count_per_box": None,
        "inferred_count_unit": "",
        "per_unit_quantity": None,
        "per_unit_unit": "",
    }
    if not spec:
        return result

    # A plus sign usually represents a multi-component package (for example
    # aerosol plus insurance liquid). Do not silently treat only the last
    # component as the whole package.
    if re.search(
        r"[0-9]+(?:\.[0-9]+)?(?:μg|微克|ug|mcg|mg|毫克|g|克|ml|毫升)"
        r"\+[0-9]+(?:\.[0-9]+)?(?:μg|微克|ug|mcg|mg|毫克|g|克|ml|毫升)",
        spec,
        re.IGNORECASE,
    ):
        return result

    mass_ratio = re.search(
        r"(?:[0-9]+(?:\.[0-9]+)?%[(（]?)?"
        r"([0-9]+(?:\.[0-9]+)?)(μg|微克|ug|mcg|mg|毫克|g|克):"
        r"[0-9]+(?:\.[0-9]+)?(?:μg|微克|ug|mcg|mg|毫克|g|克)[)）]?$",
        spec,
        re.IGNORECASE,
    )
    if mass_ratio:
        quantity = decimal_value(mass_ratio.group(1))
        unit = normalize_unit(mass_ratio.group(2))
        result["package_quantity_per_box"] = decimal_output(quantity)
        result["package_quantity_unit"] = unit
        if purchase_count is not None and quantity is not None:
            result["calculation_total_quantity"] = decimal_output(quantity * purchase_count)
            result["calculation_total_unit"] = unit
        return result

    concentration_total = parse_concentration_total_mass(spec)
    if concentration_total is not None:
        quantity, unit = concentration_total
        result["package_quantity_per_box"] = decimal_output(quantity)
        result["package_quantity_unit"] = unit
        if purchase_count is not None:
            result["calculation_total_quantity"] = decimal_output(quantity * purchase_count)
            result["calculation_total_unit"] = unit
        return result

    # Container-labelled total strength, for example
    # "2.4ml:10mg/支*1支" = 10mg per syringe/container.
    container_mass = re.search(
        r"[0-9]+(?:\.[0-9]+)?(?:ml|毫升):"
        r"([0-9]+(?:\.[0-9]+)?)(μg|微克|ug|mcg|mg|毫克|g|克)"
        r"/(?:支|瓶)(?:\*([0-9]+)(?:支|瓶))?",
        spec,
        re.IGNORECASE,
    )
    if container_mass:
        mass = decimal_value(container_mass.group(1))
        container_count = decimal_value(container_mass.group(3)) or Decimal("1")
        unit = normalize_unit(container_mass.group(2))
        if mass is not None:
            quantity = mass * container_count
            result["package_quantity_per_box"] = decimal_output(quantity)
            result["package_quantity_unit"] = unit
            if purchase_count is not None:
                result["calculation_total_quantity"] = decimal_output(
                    quantity * purchase_count
                )
                result["calculation_total_unit"] = unit
            return result

    # Less common export form: "20*20ml/支/盒" means 20 containers, each 20ml.
    count_volume = re.search(
        r"([0-9]+)\*[0-9]+(?:\.[0-9]+)?(?:ml|毫升)/(支|瓶|袋)(?:/|$)",
        spec,
        re.IGNORECASE,
    )
    if count_volume:
        quantity = decimal_value(count_volume.group(1))
        unit = normalize_unit(count_volume.group(2))
        result["package_quantity_per_box"] = decimal_output(quantity)
        result["package_quantity_unit"] = unit
        if purchase_count is not None and quantity is not None:
            result["calculation_total_quantity"] = decimal_output(quantity * purchase_count)
            result["calculation_total_unit"] = unit
        return result

    shorthand_counts = list(re.finditer(r"([0-9]+)([sStT])", spec))
    if shorthand_counts:
        selected_shorthand = shorthand_counts[-1]
        shorthand = selected_shorthand.group(2).lower()
        inferred_unit = infer_count_unit_from_drug_name(drug_name, packing_spec)
        if shorthand == "t":
            unit = "片"
        elif inferred_unit:
            unit = inferred_unit
        else:
            return result
        quantity = decimal_value(selected_shorthand.group(1))
        tail = spec[selected_shorthand.end() :]
        for multiplier, multiplier_unit in re.findall(r"\*([0-9]+)(板|瓶|盒|袋|支|管|罐|包)", tail):
            if quantity is not None and multiplier_unit in PACK_MULTIPLIER_UNITS:
                quantity *= Decimal(multiplier)
        result["package_quantity_per_box"] = decimal_output(quantity)
        result["package_quantity_unit"] = unit
        result["unit_count_per_box"] = decimal_output(quantity)
        result["inferred_count_unit"] = unit
        if purchase_count is not None and quantity is not None:
            result["calculation_total_quantity"] = decimal_output(quantity * purchase_count)
            result["calculation_total_unit"] = unit
        return result

    # Nested discrete package such as "16丸*18袋/盒". The first unit is
    # the administration unit and the following container is a multiplier.
    nested_discrete = re.search(
        r"([0-9]+)(片|粒|丸|贴|揿|喷|吸|泡|滴|颗)"
        r"\*([0-9]+)(板|瓶|袋|支|管|罐|包)(?:/|$)",
        spec,
        re.IGNORECASE,
    )
    if nested_discrete:
        per_container = decimal_value(nested_discrete.group(1))
        container_count = decimal_value(nested_discrete.group(3))
        unit = normalize_unit(nested_discrete.group(2))
        if per_container is not None and container_count is not None:
            quantity = per_container * container_count
            result["package_quantity_per_box"] = decimal_output(quantity)
            result["package_quantity_unit"] = unit
            result["unit_count_per_box"] = decimal_output(quantity)
            result["inferred_count_unit"] = unit
            if purchase_count is not None:
                result["calculation_total_quantity"] = decimal_output(
                    quantity * purchase_count
                )
                result["calculation_total_unit"] = unit
            return result

    unlabelled_multiplier = re.search(
        r"([0-9]+(?:\.[0-9]+)?)(μg|微克|ug|mcg|mg|毫克|g|克|ml|毫升)"
        r"\*([0-9]+)(?:/(?:盒|瓶|袋|包))?$",
        spec,
        re.IGNORECASE,
    )
    if unlabelled_multiplier:
        per_unit_quantity = decimal_value(unlabelled_multiplier.group(1))
        per_unit_unit = normalize_unit(unlabelled_multiplier.group(2))
        unit_count = decimal_value(unlabelled_multiplier.group(3))
        if per_unit_quantity is None or unit_count is None:
            return result
        inferred_unit = infer_count_unit_from_drug_name(drug_name, packing_spec)
        result["unit_count_per_box"] = decimal_output(unit_count)
        result["inferred_count_unit"] = inferred_unit
        result["per_unit_quantity"] = decimal_output(per_unit_quantity)
        result["per_unit_unit"] = per_unit_unit
        if inferred_unit:
            quantity = unit_count
            unit = inferred_unit
        else:
            quantity = per_unit_quantity * unit_count
            unit = per_unit_unit
        result["package_quantity_per_box"] = decimal_output(quantity)
        result["package_quantity_unit"] = unit
        if purchase_count is not None:
            result["calculation_total_quantity"] = decimal_output(quantity * purchase_count)
            result["calculation_total_unit"] = unit
        return result

    # A labeled single-unit strength such as "70mg/片/盒" states the
    # strength per tablet and, by omitting any multiplier, one tablet per box.
    # Keep the calculation quantity in administration units so a weekly
    # tablet regimen can be divided by its actual tablet count.
    single_unit_strength = re.search(
        r"([0-9]+(?:\.[0-9]+)?)(μg|微克|ug|mcg|mg|毫克|g|克)"
        r"/(片|粒|丸|贴|枚|喷|揿|滴|吸|泡|颗|张|份)"
        r"/(?:盒|板|瓶|袋|包|支)$",
        spec,
        re.IGNORECASE,
    )
    if single_unit_strength:
        strength = decimal_value(single_unit_strength.group(1))
        strength_unit = normalize_unit(single_unit_strength.group(2))
        count_unit = normalize_unit(single_unit_strength.group(3))
        if strength is not None and strength > 0:
            result["package_quantity_per_box"] = 1
            result["package_quantity_unit"] = count_unit
            result["unit_count_per_box"] = 1
            result["inferred_count_unit"] = count_unit
            result["per_unit_quantity"] = decimal_output(strength)
            result["per_unit_unit"] = strength_unit
            if purchase_count is not None:
                result["calculation_total_quantity"] = decimal_output(purchase_count)
                result["calculation_total_unit"] = count_unit
            return result

    matches = list(re.finditer(rf"([0-9]+(?:\.[0-9]+)?)({PACKAGE_UNIT_PATTERN})", spec, re.IGNORECASE))
    if not matches:
        return result
    selected = matches[-1]
    quantity = decimal_value(selected.group(1))
    unit = normalize_unit(selected.group(2))
    if quantity is None:
        return result

    tail = spec[selected.end() :]
    for multiplier, multiplier_unit in re.findall(r"\*([0-9]+)(板|瓶|盒|袋|支|管|罐|包)", tail):
        if multiplier_unit in PACK_MULTIPLIER_UNITS:
            quantity *= Decimal(multiplier)

    count = purchase_count
    result["package_quantity_per_box"] = decimal_output(quantity)
    result["package_quantity_unit"] = unit
    if count is not None:
        result["calculation_total_quantity"] = decimal_output(quantity * count)
        result["calculation_total_unit"] = unit
    return result


def parse_strength_mg_per_unit(drug_name: Any, packing_spec: Any) -> Optional[Decimal]:
    spec = normalize_text(packing_spec) or normalize_text(drug_name)
    container_mass = re.search(
        r"[0-9]+(?:\.[0-9]+)?(?:ml|毫升):"
        r"([0-9]+(?:\.[0-9]+)?)(μg|微克|ug|mcg|mg|毫克|g|克)"
        r"/(?:支|瓶)",
        spec,
        re.IGNORECASE,
    )
    if container_mass:
        value = decimal_value(container_mass.group(1))
        if value is not None:
            unit = normalize_unit(container_mass.group(2))
            if unit == "g":
                return value * Decimal("1000")
            if unit == "μg":
                return value / Decimal("1000")
            return value
    single_unit_strength = re.search(
        r"([0-9]+(?:\.[0-9]+)?)(μg|微克|ug|mcg|mg|毫克|g|克)"
        r"/(?:片|粒|丸|贴|枚|喷|揿|滴|吸|泡|颗|张|份)"
        r"(?:/(?:盒|板|瓶|袋|包|支))?$",
        spec,
        re.IGNORECASE,
    )
    if single_unit_strength:
        value = decimal_value(single_unit_strength.group(1))
        if value is not None:
            unit = normalize_unit(single_unit_strength.group(2))
            if unit == "g":
                return value * Decimal("1000")
            if unit == "μg":
                return value / Decimal("1000")
            return value
    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)(μg|微克|ug|mcg|mg|毫克|g|克)\*(?:[0-9]+(?:\.[0-9]+)?)(?:片|粒|袋|支|瓶)",
        spec,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"([0-9]+(?:\.[0-9]+)?)(μg|微克|ug|mcg|mg|毫克|g|克)"
            r"\*[0-9]+(?:/(?:盒|瓶|袋|包))?$",
            spec,
            re.IGNORECASE,
        )
    if not match:
        return None
    value = decimal_value(match.group(1))
    if value is None:
        return None
    unit = normalize_unit(match.group(2))
    if unit == "g":
        return value * Decimal("1000")
    if unit == "μg":
        return value / Decimal("1000")
    return value


def parse_container_volume_ml(drug_name: Any, packing_spec: Any) -> Optional[Decimal]:
    spec = normalize_text(packing_spec) or normalize_text(drug_name)
    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)(?:ml|毫升)(?:/|\*)(?:瓶|袋|支)",
        spec,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"每(?:瓶|袋|支)(?:装)?([0-9]+(?:\.[0-9]+)?)(?:ml|毫升)",
            spec,
            re.IGNORECASE,
        )
    if not match:
        match = re.search(
            r"([0-9]+(?:\.[0-9]+)?)(?:ml|毫升)\*[0-9]+(?:/(?:盒|瓶|袋|包))?$",
            spec,
            re.IGNORECASE,
        )
    if not match:
        return None
    return decimal_value(match.group(1))


def calculate_duration(drug: Dict[str, Any], usage: Dict[str, Any]) -> Dict[str, Any]:
    package = parse_package_quantity(drug.get("drugName"), drug.get("packingSpec"), drug.get("drugCount"))
    result: Dict[str, Any] = dict(package)
    result.update(
        {
            "daily_quantity": None,
            "daily_quantity_unit": "",
            "duration_days_exact": None,
            "duration_days": None,
            "duration_calculation_status": "needs_review",
            "duration_calculation_reason": "",
        }
    )

    explicit = decimal_value(usage.get("explicit_duration_days"))
    if explicit is not None and explicit > 0:
        result["duration_days_exact"] = decimal_output(explicit)
        result["duration_days"] = int(math.ceil(float(explicit)))
        result["duration_calculation_status"] = "success"
        result["duration_calculation_reason"] = "explicit_duration_from_image"
        return result

    times_per_day = decimal_value(usage.get("times_per_day"))
    dose_per_time = decimal_value(usage.get("dose_per_time"))
    dose_unit = normalize_unit(usage.get("dose_unit"))
    total_quantity = decimal_value(package.get("calculation_total_quantity"))
    total_unit = normalize_unit(package.get("calculation_total_unit"))
    inferred_count_unit = normalize_unit(package.get("inferred_count_unit"))
    inhalation_unit_equivalence = is_inhalation_discrete_unit_equivalence(
        drug.get("drugName"), dose_unit, total_unit
    )

    if times_per_day is None or times_per_day <= 0:
        result["duration_calculation_reason"] = "frequency_missing_or_unparsed"
        return result
    if total_quantity is None or total_quantity <= 0 or not total_unit:
        result["duration_calculation_reason"] = "package_quantity_missing_or_unparsed"
        return result

    if total_unit == "次":
        daily_quantity = times_per_day
        daily_unit = "次"
    else:
        if dose_per_time is None or dose_per_time <= 0 or not dose_unit:
            result["duration_calculation_reason"] = "dose_missing_or_unparsed"
            return result
        if (
            inferred_count_unit
            and dose_unit in DISCRETE_DOSE_UNITS
            and dose_unit != inferred_count_unit
            and not inhalation_unit_equivalence
        ):
            result["duration_calculation_reason"] = "drug_form_and_ocr_unit_conflict"
            return result
        if dose_unit == total_unit:
            dose_in_total_unit = dose_per_time
        elif inhalation_unit_equivalence:
            dose_in_total_unit = dose_per_time
        elif (converted_quantity := convert_compatible_quantity(dose_per_time, dose_unit, total_unit)) is not None:
            dose_in_total_unit = converted_quantity
        elif dose_unit in {"mg", "g", "μg"} and total_unit in DISCRETE_DOSE_UNITS:
            strength_mg = parse_strength_mg_per_unit(drug.get("drugName"), drug.get("packingSpec"))
            if strength_mg is None or strength_mg <= 0:
                result["duration_calculation_reason"] = "strength_missing_for_unit_conversion"
                return result
            if dose_unit == "g":
                dose_mg = dose_per_time * Decimal("1000")
            elif dose_unit == "μg":
                dose_mg = dose_per_time / Decimal("1000")
            else:
                dose_mg = dose_per_time
            dose_in_total_unit = dose_mg / strength_mg
        elif dose_unit in DISCRETE_DOSE_UNITS and total_unit in {"mg", "g", "μg"}:
            strength_mg = parse_strength_mg_per_unit(drug.get("drugName"), drug.get("packingSpec"))
            if strength_mg is None or strength_mg <= 0:
                result["duration_calculation_reason"] = "strength_missing_for_unit_conversion"
                return result
            dose_mass_mg = dose_per_time * strength_mg
            target_mass = convert_compatible_quantity(dose_mass_mg, "mg", total_unit)
            if target_mass is None:
                result["duration_calculation_reason"] = "dose_and_package_units_mismatch"
                return result
            dose_in_total_unit = target_mass
        elif dose_unit in {"瓶", "袋", "支"} and total_unit == "ml":
            container_ml = parse_container_volume_ml(drug.get("drugName"), drug.get("packingSpec"))
            if container_ml is None or container_ml <= 0:
                result["duration_calculation_reason"] = "container_volume_missing_for_unit_conversion"
                return result
            dose_in_total_unit = dose_per_time * container_ml
        elif dose_unit == "ml" and total_unit in {"瓶", "袋", "支"}:
            container_ml = parse_container_volume_ml(drug.get("drugName"), drug.get("packingSpec"))
            if container_ml is None or container_ml <= 0:
                result["duration_calculation_reason"] = "container_volume_missing_for_unit_conversion"
                return result
            dose_in_total_unit = dose_per_time / container_ml
        else:
            result["duration_calculation_reason"] = "dose_and_package_units_mismatch"
            return result
        daily_quantity = dose_in_total_unit * times_per_day
        daily_unit = total_unit

    if daily_quantity <= 0:
        result["duration_calculation_reason"] = "daily_quantity_is_zero"
        return result
    exact = total_quantity / daily_quantity
    exact_output = decimal_output(exact)
    result["daily_quantity"] = decimal_output(daily_quantity)
    result["daily_quantity_unit"] = daily_unit
    result["duration_days_exact"] = exact_output
    result["duration_days"] = int(math.floor(float(exact_output)))
    result["duration_calculation_status"] = "success"
    result["duration_calculation_reason"] = "calculated_from_package_and_usage"
    return result


def _usage_candidate(text: str) -> bool:
    normalized = normalize_text(text)
    if "用法用量" in normalized:
        return True
    period_frequency = re.search(r"(?:每)?[0-9一二两三四五六七八九十]+[日天周][0-9一二两三四五六七八九十]+次", normalized)
    if period_frequency and any(method in normalized for method in ("口服", "鼻饲", "注射", "吸入", "外用")):
        return True
    if any(token in normalized for token in ("必要时", "适量")):
        has_method = any(
            method in normalized
            for method in ("口服", "鼻饲", "注射", "吸入", "外用")
        )
        has_dose = re.search(
            rf"(?:每次|一次)(?:[0-9.]+|[{NUMBER_CHARS}]+|1/2)({DOSE_UNITS_PATTERN})",
            normalized,
            re.IGNORECASE,
        )
        if has_method and has_dose:
            return True
    hints = ("每次", "一日", "1日", "日1次", "每日", "每天", "每周", "周1次", "每隔", "口服", "外用", "注射", "静滴", "早晚各")
    return sum(1 for hint in hints if hint in normalized) >= 2


def _merge_usage_candidates(
    groups: Sequence[Sequence[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Merge duplicate OCR detections without collapsing distinct drug rows."""
    selected: List[Dict[str, Any]] = []
    for group in groups:
        for source in group:
            candidate = dict(source)
            key = normalize_text(candidate.get("text"))
            if not key:
                continue
            y = float(candidate.get("y") or 0.0)
            height = max(1.0, float(candidate.get("height") or 16.0))
            duplicate_index: Optional[int] = None
            for index, current in enumerate(selected):
                if normalize_text(current.get("text")) != key:
                    continue
                current_y = float(current.get("y") or 0.0)
                current_height = max(
                    1.0, float(current.get("height") or 16.0)
                )
                overlap = max(
                    0.0,
                    min(y + height, current_y + current_height)
                    - max(y, current_y),
                )
                overlap_ratio = overlap / min(height, current_height)
                if overlap_ratio >= 0.5 or abs(y - current_y) <= 8.0:
                    duplicate_index = index
                    break
            if duplicate_index is None:
                selected.append(candidate)
            elif float(candidate.get("score") or 0.0) > float(
                selected[duplicate_index].get("score") or 0.0
            ):
                selected[duplicate_index] = candidate
    return sorted(selected, key=lambda item: float(item.get("y") or 0.0))


def _drug_name_similarity(drug: Dict[str, Any], ocr_text: Any) -> float:
    drug_name, _ = split_drug_name_specification(drug.get("drugName"), drug.get("packingSpec"))
    left = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", normalize_text(drug_name)).lower()
    right = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", normalize_text(ocr_text)).lower()
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right)) + 0.35
    return difflib.SequenceMatcher(None, left, right).ratio()


def _assign_usage_candidates(
    drugs: Sequence[Dict[str, Any]],
    candidates: Sequence[Dict[str, Any]],
    all_lines: Sequence[Dict[str, Any]],
) -> List[Tuple[Optional[Dict[str, Any]], str, str]]:
    if not drugs:
        return []
    if len(drugs) == 1:
        if len(candidates) == 1:
            return [(dict(candidates[0]), "single_drug_fast_path", "high")]
        if not candidates:
            return [(None, "single_drug_fast_path", "none")]
        return [(None, "multiple_usage_candidates_for_single_drug", "low")]

    scored_anchors: List[Tuple[float, int, int]] = []
    for drug_index, drug in enumerate(drugs):
        for line_index, line in enumerate(all_lines):
            if _usage_candidate(str(line.get("text") or "")):
                continue
            score = _drug_name_similarity(drug, line.get("text"))
            if score >= 0.55:
                scored_anchors.append((score, drug_index, line_index))

    anchors: Dict[int, Dict[str, Any]] = {}
    used_line_indexes = set()
    for score, drug_index, line_index in sorted(scored_anchors, reverse=True):
        if drug_index in anchors or line_index in used_line_indexes:
            continue
        anchor = dict(all_lines[line_index])
        anchor["match_score"] = score
        anchors[drug_index] = anchor
        used_line_indexes.add(line_index)

    assignments: List[Tuple[Optional[Dict[str, Any]], str, str]] = [
        (None, "drug_name_and_position", "none") for _ in drugs
    ]
    used_candidates = set()
    if anchors:
        sorted_anchor_positions = sorted(
            ((index, float(anchor.get("y", 0.0))) for index, anchor in anchors.items()),
            key=lambda item: item[1],
        )
        for position, (drug_index, anchor_y) in enumerate(sorted_anchor_positions):
            next_y = (
                sorted_anchor_positions[position + 1][1]
                if position + 1 < len(sorted_anchor_positions)
                else float("inf")
            )
            choices = [
                (candidate_index, candidate)
                for candidate_index, candidate in enumerate(candidates)
                if candidate_index not in used_candidates
                and float(candidate.get("y", 0.0)) >= anchor_y
                and float(candidate.get("y", 0.0)) < next_y
            ]
            if not choices:
                continue
            candidate_index, candidate = min(
                choices,
                key=lambda item: float(item[1].get("y", 0.0)) - anchor_y,
            )
            used_candidates.add(candidate_index)
            confidence = "high" if float(anchors[drug_index].get("match_score", 0.0)) >= 0.75 else "medium"
            assignments[drug_index] = (dict(candidate), "drug_name_and_position", confidence)

    if all(candidate is not None for candidate, _, _ in assignments):
        return assignments
    if len(candidates) == len(drugs):
        return [
            (dict(candidate), "vertical_order_fallback", "medium")
            for candidate in sorted(candidates, key=lambda item: float(item.get("y", 0.0)))
        ]
    return assignments


DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})\s*(?:年|[-‐‑‒–—―－/.])\s*"
    r"(?P<month>\d{1,2})\s*(?:月|[-‐‑‒–—―－/.])\s*(?P<day>\d{1,2})\s*日?"
)
AGE_PATTERN = re.compile(r"年龄\s*[:：]?\s*(\d{1,3})\s*岁?")
DEPARTMENT_LABEL_PATTERN = re.compile(r"(?:就诊|开方)?科室\s*[:：]?\s*")
HEADER_STOP_LABELS = re.compile(
    r"(?:姓名|患者|性别|年龄|出生(?:日期)?|生日|开具日期|处方日期|日期|诊断|电话)\s*[:：]?"
)
HOSPITAL_INSTITUTION_PATTERN = re.compile(
    r"(?:互联网医院|妇幼保健院|社区卫生服务中心|卫生服务中心|中心医院|医院|卫生院|门诊部|诊所|医务室)"
)
HOSPITAL_TITLE_NOISE_PATTERN = re.compile(
    r"(?:电子)?(?:普通|急诊|儿科|麻醉|精二|精一)?处方笺|(?:电子)?处方|普通|已使用"
)
HOSPITAL_TITLE_STANDALONE_NOISE = {"普通", "普", "通", "急诊", "急", "诊"}
HOSPITAL_SOURCE_INTERNAL = 1
HOSPITAL_SOURCE_EXTERNAL = 2
HOSPITAL_SOURCE_REVIEW = 3


def _header_rows(lines: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Combine OCR boxes that belong to one visual row."""
    boxes: List[Dict[str, Any]] = []
    for index, line in enumerate(lines):
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        y = float(line.get("y", index))
        height = max(1.0, float(line.get("height", 16.0)))
        boxes.append(
            {
                "text": text,
                "score": float(line.get("score", 0.0)),
                "x": float(line.get("x", index)),
                "y": y,
                "width": max(
                    1.0,
                    float(line.get("width", max(height, len(text) * height))),
                ),
                "height": height,
            }
        )
    boxes.sort(key=lambda item: (item["y"], item["x"]))
    rows: List[Dict[str, Any]] = []
    for box in boxes:
        center = box["y"] + box["height"] / 2.0
        target: Optional[Dict[str, Any]] = None
        for row in reversed(rows[-3:]):
            tolerance = max(8.0, min(30.0, max(row["height"], box["height"]) * 0.7))
            if abs(center - row["center"]) <= tolerance:
                target = row
                break
        if target is None:
            rows.append(
                {
                    "boxes": [box],
                    "center": center,
                    "height": box["height"],
                }
            )
        else:
            target["boxes"].append(box)
            target["center"] = sum(
                item["y"] + item["height"] / 2.0 for item in target["boxes"]
            ) / len(target["boxes"])
            target["height"] = max(target["height"], box["height"])

    result: List[Dict[str, Any]] = []
    for row in rows:
        ordered = sorted(row["boxes"], key=lambda item: item["x"])
        parts: List[str] = []
        segments: List[Dict[str, Any]] = []
        cursor = 0
        for item in ordered:
            if parts:
                cursor += 1
            start = cursor
            parts.append(item["text"])
            cursor += len(item["text"])
            segment = dict(item)
            segment["char_start"] = start
            segment["char_end"] = cursor
            segments.append(segment)
        result.append(
            {
                "text": " ".join(parts),
                "score": min(item["score"] for item in ordered),
                "y": min(item["y"] for item in ordered),
                "height": max(item["height"] for item in ordered),
                "segments": segments,
            }
        )
    return sorted(result, key=lambda item: item["y"])


def normalize_hospital_ocr_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = HOSPITAL_TITLE_NOISE_PATTERN.sub("", text)
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text)


def _hospital_title_blocks(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Join visually adjacent, similarly sized title rows before classification."""
    blocks: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        block_rows = [row]
        cursor = index - 1
        while cursor >= 0 and len(block_rows) < 3:
            previous = rows[cursor]
            current = block_rows[0]
            previous_height = max(1.0, float(previous.get("height") or 1.0))
            current_height = max(1.0, float(current.get("height") or 1.0))
            size_ratio = min(previous_height, current_height) / max(
                previous_height, current_height
            )
            vertical_gap = float(current.get("y") or 0.0) - (
                float(previous.get("y") or 0.0) + previous_height
            )
            if size_ratio < 0.65 or vertical_gap > max(
                20.0, max(previous_height, current_height) * 1.6
            ):
                break
            if HEADER_STOP_LABELS.search(str(previous.get("text") or "")):
                break
            block_rows.insert(0, previous)
            cursor -= 1

        raw_text = " ".join(
            part
            for item in block_rows
            for part in str(item.get("text") or "").strip().split()
            if part not in HOSPITAL_TITLE_STANDALONE_NOISE
        ).strip()
        normalized = normalize_hospital_ocr_name(raw_text)
        if len(normalized) < 4 or not HOSPITAL_INSTITUTION_PATTERN.search(normalized):
            continue
        blocks.append(
            {
                "text": raw_text,
                "normalized": normalized,
                "score": min(float(item.get("score") or 0.0) for item in block_rows),
                "height": max(float(item.get("height") or 0.0) for item in block_rows),
                "y": min(float(item.get("y") or 0.0) for item in block_rows),
                "line_count": len(block_rows),
            }
        )
    return blocks


def parse_hospital_fields(
    lines: Sequence[Dict[str, Any]],
    internal_hospital_name: str,
    min_confidence: float = 0.75,
    internal_hospital_aliases: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Classify the issuing hospital from the prescription title area."""
    candidates = _hospital_title_blocks(_header_rows(lines))

    if not candidates:
        return {
            "hospital_name_ocr": "",
            "hospital_source": "review",
            "hospital_source_code": HOSPITAL_SOURCE_REVIEW,
            "hospital_ocr_status": "not_found",
            "hospital_ocr_confidence": None,
            "hospital_ocr_text": "",
            "hospital_ocr_reason": "hospital_name_not_found",
            "hospital_ocr_lines_checked": len(lines),
        }

    selected = max(
        candidates,
        key=lambda item: (
            int(item["line_count"]),
            float(item["height"]),
            float(item["score"]),
            -float(item["y"]),
        ),
    )
    confidence = float(selected["score"])
    candidate = str(selected["normalized"])
    raw_text = str(selected["text"])
    internal_names = [internal_hospital_name, *(internal_hospital_aliases or [])]
    normalized_internal_names = list(
        dict.fromkeys(
            normalized
            for normalized in map(normalize_hospital_ocr_name, internal_names)
            if normalized
        )
    )
    similarities = [
        difflib.SequenceMatcher(None, candidate, internal).ratio()
        for internal in normalized_internal_names
    ]
    similarity = max(similarities, default=0.0)
    exact_internal = any(
        internal in candidate or candidate == internal
        for internal in normalized_internal_names
    )

    if confidence < float(min_confidence):
        source = "review"
        source_code = HOSPITAL_SOURCE_REVIEW
        reason = "hospital_ocr_confidence_below_threshold"
        status = "low_confidence"
    elif exact_internal:
        source = "internal"
        source_code = HOSPITAL_SOURCE_INTERNAL
        reason = "internal_hospital_matched"
        status = "success"
    elif normalized_internal_names and similarity >= 0.72:
        source = "review"
        source_code = HOSPITAL_SOURCE_REVIEW
        reason = "hospital_name_close_to_internal"
        status = "success"
    else:
        source = "external"
        source_code = HOSPITAL_SOURCE_EXTERNAL
        reason = "different_hospital_name"
        status = "success"

    return {
        "hospital_name_ocr": candidate,
        "hospital_source": source,
        "hospital_source_code": source_code,
        "hospital_ocr_status": status,
        "hospital_ocr_confidence": round(confidence, 6),
        "hospital_ocr_text": raw_text,
        "hospital_ocr_reason": reason,
        "hospital_ocr_similarity": round(similarity, 6),
        "hospital_ocr_lines_checked": len(lines),
        "hospital_ocr_title_line_count": int(selected["line_count"]),
    }


def _normalise_date(match: re.Match[str]) -> Optional[dt.date]:
    try:
        return dt.date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def _segments_for_span(row: Dict[str, Any], start: int, end: int) -> List[Dict[str, Any]]:
    return [
        segment
        for segment in row.get("segments") or []
        if int(segment["char_end"]) > start and int(segment["char_start"]) < end
    ]


def _segments_bbox(segments: Sequence[Dict[str, Any]]) -> Optional[List[float]]:
    if not segments:
        return None
    left = min(float(segment["x"]) for segment in segments)
    top = min(float(segment["y"]) for segment in segments)
    right = max(float(segment["x"]) + float(segment["width"]) for segment in segments)
    bottom = max(float(segment["y"]) + float(segment["height"]) for segment in segments)
    return [left, top, max(1.0, right - left), max(1.0, bottom - top)]


def _department_from_row(
    row: Dict[str, Any],
    label: re.Match[str],
) -> Tuple[str, Optional[float], Optional[List[float]]]:
    selected: List[Dict[str, Any]] = []
    pieces: List[str] = []
    previous_right: Optional[float] = None
    stopped = False
    for segment in row.get("segments") or []:
        if int(segment["char_end"]) <= label.end():
            continue
        relative_start = max(0, label.end() - int(segment["char_start"]))
        piece = str(segment["text"])[relative_start:]
        stop_match = HEADER_STOP_LABELS.search(piece)
        if stop_match:
            piece = piece[: stop_match.start()]
            stopped = True
        clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", piece)
        if not clean:
            if stopped:
                break
            previous_right = float(segment["x"]) + float(segment["width"])
            continue
        if previous_right is not None and selected:
            gap = float(segment["x"]) - previous_right
            if gap > max(24.0, float(segment["height"]) * 1.8):
                break
        if len("".join(pieces)) + len(clean) > 16:
            break
        pieces.append(clean)
        selected.append(segment)
        previous_right = float(segment["x"]) + float(segment["width"])
        if stopped or len("".join(pieces)) >= 16:
            break
    value = "".join(pieces)
    if not value or value in {"科室", "未知", "无"}:
        return "", None, None
    return (
        value,
        min(float(segment["score"]) for segment in selected),
        _segments_bbox(selected),
    )


def _field_result(
    value: str,
    confidence: Optional[float],
    raw_text: str,
    min_confidence: float,
) -> Dict[str, Any]:
    if not value or confidence is None:
        return {"value": "", "status": "not_found", "confidence": None, "ocr_text": ""}
    return {
        "value": value,
        "status": "success" if confidence >= min_confidence else "low_confidence",
        "confidence": round(confidence, 6),
        "ocr_text": raw_text,
    }


def parse_header_fields(
    lines: Sequence[Dict[str, Any]],
    min_confidence: float = 0.75,
    reference_year: Optional[int] = None,
) -> Dict[str, Any]:
    """Parse birth date and department from OCR boxes in the upper-right area."""
    year_now = int(reference_year or dt.date.today().year)
    rows = _header_rows(lines)

    department_value = ""
    department_score: Optional[float] = None
    department_text = ""
    department_bbox: Optional[List[float]] = None
    for row in rows:
        text = str(row["text"])
        label = DEPARTMENT_LABEL_PATTERN.search(text)
        if not label:
            continue
        value, score, bbox = _department_from_row(row, label)
        if value:
            department_value = value
            department_score = score
            department_text = text
            department_bbox = bbox
            break

    birth_candidates: List[Dict[str, Any]] = []
    birth_label_without_date: List[int] = []
    for row_index, row in enumerate(rows):
        text = str(row["text"])
        has_birth_label = bool(re.search(r"出生(?:日期)?|生日", text))
        age_match = AGE_PATTERN.search(text)
        has_issue_label = bool(re.search(r"开具日期|处方日期|审核日期", text))
        date_matches = list(DATE_PATTERN.finditer(text))
        if has_birth_label and not date_matches:
            birth_label_without_date.append(row_index)
        for date_match in date_matches:
            value = _normalise_date(date_match)
            if value is None or value.year > year_now or value.year < year_now - 120:
                continue
            priority = 0
            if has_birth_label:
                priority += 100
            if age_match:
                priority += 80
                age = int(age_match.group(1))
                if abs((year_now - value.year) - age) <= 1:
                    priority += 30
            if has_issue_label:
                priority -= 200
            if value.year >= year_now - 1 and not has_birth_label and not age_match:
                priority -= 100
            if priority > 0:
                date_segments = _segments_for_span(row, date_match.start(), date_match.end())
                date_score = (
                    min(float(segment["score"]) for segment in date_segments)
                    if date_segments
                    else float(row["score"])
                )
                birth_candidates.append(
                    {
                        "priority": priority,
                        "value": value,
                        "score": date_score,
                        "text": text,
                        "bbox": _segments_bbox(date_segments),
                    }
                )

    if not birth_candidates:
        for row_index in birth_label_without_date:
            if row_index + 1 >= len(rows):
                continue
            next_row = rows[row_index + 1]
            if float(next_row["y"]) - float(rows[row_index]["y"]) > max(
                35.0, float(rows[row_index]["height"]) * 2.0
            ):
                continue
            text = str(next_row["text"])
            if re.search(r"开具日期|处方日期|审核日期", text):
                continue
            match = DATE_PATTERN.search(text)
            value = _normalise_date(match) if match else None
            if value is not None and year_now - 120 <= value.year <= year_now:
                date_segments = _segments_for_span(next_row, match.start(), match.end())
                score = (
                    min(float(segment["score"]) for segment in date_segments)
                    if date_segments
                    else float(next_row["score"])
                )
                birth_candidates.append(
                    {
                        "priority": 90,
                        "value": value,
                        "score": score,
                        "text": f"{rows[row_index]['text']} {text}",
                        "bbox": _segments_bbox(date_segments),
                    }
                )

    birth_value = ""
    birth_score: Optional[float] = None
    birth_text = ""
    birth_bbox: Optional[List[float]] = None
    if birth_candidates:
        candidate = max(
            birth_candidates,
            key=lambda item: (item["priority"], item["score"]),
        )
        birth_value = candidate["value"].isoformat()
        birth_score = float(candidate["score"])
        birth_text = str(candidate["text"])
        birth_bbox = candidate["bbox"]

    birth = _field_result(birth_value, birth_score, birth_text, float(min_confidence))
    department = _field_result(
        department_value,
        department_score,
        department_text,
        float(min_confidence),
    )
    return {
        "birth_date": birth["value"],
        "birth_date_ocr_status": birth["status"],
        "birth_date_ocr_confidence": birth["confidence"],
        "birth_date_ocr_text": birth["ocr_text"],
        "department": department["value"],
        "department_ocr_status": department["status"],
        "department_ocr_confidence": department["confidence"],
        "department_ocr_text": department["ocr_text"],
        "header_ocr_lines_checked": len(lines),
        "field_source": "prescription_image_paddleocr",
        "_birth_date_bbox": birth_bbox,
        "_department_bbox": department_bbox,
    }


def parse_isolated_birth_date(
    lines: Sequence[Dict[str, Any]],
    expected_value: str,
    min_confidence: float = 0.75,
    reference_year: Optional[int] = None,
) -> Dict[str, Any]:
    """Parse a date-only retry crop after the age-row anchor was established."""
    year_now = int(reference_year or dt.date.today().year)
    candidates: List[Tuple[int, dt.date, float, str]] = []
    for row in _header_rows(lines):
        text = str(row["text"])
        for match in DATE_PATTERN.finditer(text):
            value = _normalise_date(match)
            if value is None or not year_now - 120 <= value.year <= year_now:
                continue
            segments = _segments_for_span(row, match.start(), match.end())
            score = (
                min(float(segment["score"]) for segment in segments)
                if segments
                else float(row["score"])
            )
            candidates.append((1 if value.isoformat() == expected_value else 0, value, score, text))
    if not candidates:
        return _field_result("", None, "", float(min_confidence))
    _, value, score, text = max(candidates, key=lambda item: (item[0], item[2]))
    return _field_result(value.isoformat(), score, text, float(min_confidence))


class PaddleUsageOCR:
    def __init__(
        self,
        min_confidence: float = 0.75,
        crop_top: float = 0.30,
        crop_bottom: float = 0.70,
        single_crop_top: float = 0.35,
        single_crop_bottom: float = 0.53,
        multi_crop_top: float = 0.35,
        multi_crop_bottom: float = 0.70,
        header_crop_left: float = 0.48,
        header_crop_right: float = 0.96,
        header_crop_top: float = 0.20,
        header_crop_bottom: float = 0.35,
        hospital_crop_left: float = 0.10,
        hospital_crop_right: float = 0.90,
        hospital_crop_top: float = 0.02,
        hospital_crop_bottom: float = 0.20,
        usage_preprocessing_mode: str = "blue_mask",
        usage_grayscale_threshold: Optional[int] = 210,
        header_grayscale_threshold: Optional[int] = 225,
        hospital_grayscale_threshold: Optional[int] = 140,
        cpu_threads: int = 3,
    ):
        self.cpu_threads = max(1, int(cpu_threads))
        os.environ["PADDLE_PDX_CPU_NUM_THREADS"] = str(self.cpu_threads)
        os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
        from paddleocr import PaddleOCR

        self.min_confidence = float(min_confidence)
        self.crop_top = max(0.0, min(float(crop_top), 0.95))
        self.crop_bottom = max(self.crop_top + 0.05, min(float(crop_bottom), 1.0))
        self.single_crop_top = max(0.0, min(float(single_crop_top), 0.95))
        self.single_crop_bottom = max(
            self.single_crop_top + 0.05, min(float(single_crop_bottom), 1.0)
        )
        self.multi_crop_top = max(0.0, min(float(multi_crop_top), 0.95))
        self.multi_crop_bottom = max(
            self.multi_crop_top + 0.05, min(float(multi_crop_bottom), 1.0)
        )
        self.header_crop_left = max(0.0, min(float(header_crop_left), 0.95))
        self.header_crop_right = max(
            self.header_crop_left + 0.05, min(float(header_crop_right), 1.0)
        )
        self.header_crop_top = max(0.0, min(float(header_crop_top), 0.90))
        self.header_crop_bottom = max(
            self.header_crop_top + 0.05, min(float(header_crop_bottom), 1.0)
        )
        self.hospital_crop_left = max(0.0, min(float(hospital_crop_left), 0.95))
        self.hospital_crop_right = max(
            self.hospital_crop_left + 0.05, min(float(hospital_crop_right), 1.0)
        )
        self.hospital_crop_top = max(0.0, min(float(hospital_crop_top), 0.90))
        self.hospital_crop_bottom = max(
            self.hospital_crop_top + 0.05, min(float(hospital_crop_bottom), 1.0)
        )
        self.usage_preprocessing_mode = self._normalise_usage_preprocessing_mode(
            usage_preprocessing_mode
        )
        self.usage_grayscale_threshold = self._normalise_threshold(
            usage_grayscale_threshold
        )
        self.header_grayscale_threshold = self._normalise_threshold(
            header_grayscale_threshold
        )
        self.hospital_grayscale_threshold = self._normalise_threshold(
            hospital_grayscale_threshold
        )
        self.model_load_seconds = 0.0
        self.inference_seconds = 0.0
        self.first_inference_seconds = 0.0
        self.predict_calls = 0
        model_load_started = time.perf_counter()
        try:
            self.engine = PaddleOCR(
                lang="ch",
                cpu_threads=self.cpu_threads,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        finally:
            self.model_load_seconds = time.perf_counter() - model_load_started

    @staticmethod
    def _result_lines(results: Sequence[Any]) -> List[Dict[str, Any]]:
        lines: List[Dict[str, Any]] = []
        for result in results:
            if not hasattr(result, "get"):
                continue
            texts = result.get("rec_texts")
            scores = result.get("rec_scores")
            polys = result.get("rec_polys")
            if texts is None:
                texts = []
            if scores is None:
                scores = []
            if polys is None:
                polys = result.get("dt_polys")
            if polys is None:
                polys = []
            for index, text in enumerate(texts):
                score = float(scores[index]) if index < len(scores) else 0.0
                x = 0.0
                y = float(index)
                width = max(16.0, len(str(text)) * 16.0)
                height = 16.0
                if index < len(polys):
                    poly = polys[index]
                    try:
                        xs = [float(point[0]) for point in poly]
                        ys = [float(point[1]) for point in poly]
                        x = min(xs)
                        y = min(ys)
                        width = max(1.0, max(xs) - min(xs))
                        height = max(1.0, max(ys) - min(ys))
                    except Exception:
                        pass
                lines.append(
                    {
                        "text": str(text),
                        "score": score,
                        "x": x,
                        "y": y,
                        "width": width,
                        "height": height,
                    }
                )
        return sorted(lines, key=lambda item: (item["y"], item["x"], item["text"]))

    def _predict(self, image: Any) -> List[Dict[str, Any]]:
        inference_started = time.perf_counter()
        self.predict_calls += 1
        try:
            results = list(self.engine.predict(image))
        finally:
            elapsed = time.perf_counter() - inference_started
            self.inference_seconds += elapsed
            if self.predict_calls == 1:
                self.first_inference_seconds = elapsed
        return self._result_lines(results)

    def timing_metrics(self, image_count: int = 0) -> Dict[str, Any]:
        images = max(0, int(image_count or 0))
        inference_seconds = max(0.0, float(self.inference_seconds))
        model_load_seconds = max(0.0, float(self.model_load_seconds))
        first_inference_seconds = max(
            0.0, float(getattr(self, "first_inference_seconds", 0.0))
        )
        try:
            blas_thread_limit = int(os.environ.get("OMP_NUM_THREADS") or 0)
        except ValueError:
            blas_thread_limit = 0
        return {
            "ocr_thread_limit": max(
                1, int(getattr(self, "cpu_threads", DEFAULT_OCR_CPU_THREADS))
            ),
            "ocr_blas_thread_limit": blas_thread_limit,
            "ocr_model_load_seconds": round(model_load_seconds, 6),
            "ocr_first_inference_seconds": round(first_inference_seconds, 6),
            "ocr_warmup_seconds": round(
                model_load_seconds + first_inference_seconds, 6
            ),
            "ocr_inference_seconds": round(inference_seconds, 6),
            "ocr_predict_calls": max(0, int(self.predict_calls)),
            "ocr_average_seconds": round(inference_seconds / images, 6) if images else 0.0,
        }

    @staticmethod
    def _normalise_threshold(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        return max(1, min(int(value), 254))

    @staticmethod
    def _normalise_usage_preprocessing_mode(value: Any) -> str:
        mode = str(value or "blue_mask").strip().lower()
        allowed = {"blue_mask", "grayscale", "original", "auto"}
        if mode not in allowed:
            raise ValueError(
                "usage_preprocessing_mode must be one of: "
                + ", ".join(sorted(allowed))
            )
        return mode

    @staticmethod
    def _apply_grayscale_threshold(image: Any, threshold: Optional[int]) -> Any:
        if threshold is None:
            return image
        import cv2

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, int(threshold), 255, cv2.THRESH_BINARY)
        return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)

    def _usage_crop_ratios(self, drug_count: int) -> Tuple[float, float, str]:
        if drug_count > 1:
            return self.multi_crop_top, self.multi_crop_bottom, "multi"
        return self.single_crop_top, self.single_crop_bottom, "single"

    def _read_candidates(
        self,
        image_path: Path,
        crop_top: Optional[float] = None,
        crop_bottom: Optional[float] = None,
        expected_count: int = 1,
        pass_name: str = "primary",
        preprocessing_mode: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]], Dict[str, Any]]:
        import cv2
        import numpy as np

        image = cv2.imread(str(image_path))
        if image is None:
            raise RuntimeError("cannot read prescription image")
        height = image.shape[0]
        top = self.crop_top if crop_top is None else float(crop_top)
        bottom = self.crop_bottom if crop_bottom is None else float(crop_bottom)
        crop_y1 = int(height * top)
        crop_y2 = int(height * bottom)
        original_crop = image[crop_y1:crop_y2, :]
        if original_crop.size == 0:
            raise RuntimeError("prescription usage crop is empty")

        def globalize_lines(
            source_lines: Sequence[Dict[str, Any]],
        ) -> List[Dict[str, Any]]:
            # OCR runs on a 2x crop.  Convert every vertical coordinate to the
            # same 2x full-image coordinate space before results from primary
            # and fallback crops are merged.
            y_offset = float(crop_y1 * 2)
            output: List[Dict[str, Any]] = []
            for source in source_lines:
                line = dict(source)
                line["y"] = float(line.get("y") or 0.0) + y_offset
                line["ocr_pass"] = str(pass_name or "primary")
                output.append(line)
            return output

        def clean_candidates(lines: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
            cleaned: List[Dict[str, Any]] = []
            for line in sorted(lines, key=lambda item: item["y"]):
                if not _usage_candidate(line["text"]):
                    continue
                text = str(line["text"]).strip()
                if "用法用量" in text:
                    text = re.sub(r"^.*?用法用量[:：]?", "", text).strip()
                key = normalize_text(text)
                if not key:
                    continue
                cleaned.append(
                    {
                        "text": text,
                        "score": float(line["score"]),
                        "x": float(line.get("x") or 0.0),
                        "y": float(line["y"]),
                        "width": float(line.get("width") or 0.0),
                        "height": float(line.get("height") or 16.0),
                        "ocr_pass": str(line.get("ocr_pass") or pass_name),
                    }
                )
            return cleaned

        def merge_candidates(
            groups: Sequence[Sequence[Dict[str, Any]]],
        ) -> List[Dict[str, Any]]:
            return _merge_usage_candidates(groups)

        resized = cv2.resize(
            original_crop,
            None,
            fx=2.0,
            fy=2.0,
            interpolation=cv2.INTER_CUBIC,
        )

        candidate_groups: List[List[Dict[str, Any]]] = []
        lines: List[Dict[str, Any]] = []
        mode = self._normalise_usage_preprocessing_mode(
            preprocessing_mode or self.usage_preprocessing_mode
        )
        preprocessing = {
            "mode": mode,
            "blue_mask_used": False,
            "grayscale_used": False,
            "original_anchor_ocr_used": False,
        }

        if mode in {"original", "auto"}:
            original_lines = globalize_lines(self._predict(resized))
            lines.extend(original_lines)
            candidate_groups.append(clean_candidates(original_lines))
            preprocessing["original_anchor_ocr_used"] = True

        if mode in {"grayscale", "auto"}:
            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
            if self.usage_grayscale_threshold is None:
                _, binary = cv2.threshold(
                    gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
                )
            else:
                _, binary = cv2.threshold(
                    gray, int(self.usage_grayscale_threshold), 255, cv2.THRESH_BINARY
                )
            gray_text = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
            gray_lines = globalize_lines(self._predict(gray_text))
            lines.extend(gray_lines)
            candidate_groups.append(clean_candidates(gray_lines))
            preprocessing["grayscale_used"] = True

        if mode in {"blue_mask", "auto"}:
            hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
            blue = cv2.inRange(
                hsv, np.array([80, 45, 35]), np.array([145, 255, 255])
            )
            blue_text = np.full_like(resized, 255)
            blue_text[blue > 0] = (0, 0, 0)
            blue_lines = globalize_lines(self._predict(blue_text))
            lines.extend(blue_lines)
            candidate_groups.append(clean_candidates(blue_lines))
            preprocessing["blue_mask_used"] = True

        if expected_count > 1 and mode == "blue_mask":
            original_lines = globalize_lines(self._predict(resized))
            lines = list(original_lines) + lines
            preprocessing["original_anchor_ocr_used"] = True

        candidates = merge_candidates(candidate_groups)
        raw_line_count = len(lines)
        lines = _merge_usage_candidates([lines])
        return candidates, raw_line_count, lines, preprocessing

    def extract_header_fields(self, image_path: Path) -> Dict[str, Any]:
        import cv2
        import numpy as np

        image = cv2.imread(str(image_path))
        if image is None:
            raise RuntimeError("cannot read prescription image")
        height, width = image.shape[:2]
        original_crop = image[
            int(height * self.header_crop_top) : int(height * self.header_crop_bottom),
            int(width * self.header_crop_left) : int(width * self.header_crop_right),
        ]
        if original_crop.size == 0:
            raise RuntimeError("upper-right prescription crop is empty")
        crop = self._apply_grayscale_threshold(
            original_crop,
            self.header_grayscale_threshold,
        )
        crop = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        lines = self._predict(crop)
        result = parse_header_fields(lines, self.min_confidence)

        if (
            result["birth_date_ocr_status"] != "success"
            or result["department_ocr_status"] != "success"
        ):
            original_crop = cv2.resize(
                original_crop,
                None,
                fx=2.0,
                fy=2.0,
                interpolation=cv2.INTER_CUBIC,
            )
            hsv = cv2.cvtColor(original_crop, cv2.COLOR_BGR2HSV)
            blue = cv2.inRange(hsv, np.array([80, 45, 35]), np.array([145, 255, 255]))
            blue_text = np.full_like(original_crop, 255)
            blue_text[blue > 0] = (0, 0, 0)
            blue_lines = self._predict(blue_text)
            fallback = parse_header_fields(blue_lines, self.min_confidence)
            for field in ("birth_date", "department"):
                status_key = f"{field}_ocr_status"
                rank = {"not_found": 0, "low_confidence": 1, "success": 2}
                fallback_confidence = float(fallback.get(f"{field}_ocr_confidence") or 0.0)
                result_confidence = float(result.get(f"{field}_ocr_confidence") or 0.0)
                if (
                    rank[fallback[status_key]] > rank[result[status_key]]
                    or (
                        rank[fallback[status_key]] == rank[result[status_key]]
                        and fallback_confidence > result_confidence
                    )
                ):
                    for suffix in ("", "_ocr_status", "_ocr_confidence", "_ocr_text"):
                        result[f"{field}{suffix}"] = fallback[f"{field}{suffix}"]
                    result[f"_{field}_bbox"] = fallback.get(f"_{field}_bbox")
            result["header_ocr_lines_checked"] += len(blue_lines)
            result["blue_fallback_used"] = True
        else:
            result["blue_fallback_used"] = False

        result["birth_date_local_retry_used"] = False
        birth_bbox = result.get("_birth_date_bbox")
        if (
            result.get("birth_date_ocr_status") != "success"
            and result.get("birth_date")
            and isinstance(birth_bbox, list)
            and len(birth_bbox) == 4
        ):
            left, top, box_width, box_height = [float(value) for value in birth_bbox]
            pad_x = max(8, int(box_height * 0.45))
            pad_y = max(6, int(box_height * 0.30))
            x1 = max(0, int(left) - pad_x)
            y1 = max(0, int(top) - pad_y)
            x2 = min(crop.shape[1], int(left + box_width) + pad_x)
            y2 = min(crop.shape[0], int(top + box_height) + pad_y)
            retry_crop = crop[y1:y2, x1:x2]
            if retry_crop.size:
                retry_crop = cv2.resize(
                    retry_crop,
                    None,
                    fx=2.0,
                    fy=2.0,
                    interpolation=cv2.INTER_CUBIC,
                )
                retry_lines = self._predict(retry_crop)
                retry = parse_isolated_birth_date(
                    retry_lines,
                    str(result["birth_date"]),
                    self.min_confidence,
                )
                result["header_ocr_lines_checked"] += len(retry_lines)
                result["birth_date_local_retry_used"] = True
                if (
                    retry["value"] == result["birth_date"]
                    and float(retry.get("confidence") or 0.0)
                    > float(result.get("birth_date_ocr_confidence") or 0.0)
                ):
                    result["birth_date_ocr_status"] = retry["status"]
                    result["birth_date_ocr_confidence"] = retry["confidence"]
                    result["birth_date_ocr_text"] = retry["ocr_text"]
        result.pop("_birth_date_bbox", None)
        result.pop("_department_bbox", None)
        return result

    def extract_hospital_fields(
        self,
        image_path: Path,
        internal_hospital_name: str,
        internal_hospital_aliases: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        import cv2

        image = cv2.imread(str(image_path))
        if image is None:
            raise RuntimeError("cannot read prescription image")
        height, width = image.shape[:2]
        crop = image[
            int(height * self.hospital_crop_top) : int(height * self.hospital_crop_bottom),
            int(width * self.hospital_crop_left) : int(width * self.hospital_crop_right),
        ]
        if crop.size == 0:
            raise RuntimeError("prescription hospital crop is empty")
        crop = self._apply_grayscale_threshold(
            crop,
            self.hospital_grayscale_threshold,
        )
        crop = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        lines = self._predict(crop)
        return parse_hospital_fields(
            lines,
            internal_hospital_name,
            self.min_confidence,
            internal_hospital_aliases,
        )

    def enrich(self, drug_infos: Sequence[Dict[str, Any]], image_path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        enriched = [dict(drug) for drug in drug_infos]
        crop_top, crop_bottom, crop_mode = self._usage_crop_ratios(len(enriched))
        candidates, lines_checked, all_lines, preprocessing = self._read_candidates(
            image_path,
            crop_top,
            crop_bottom,
            len(enriched),
            "primary",
        )
        assignments = _assign_usage_candidates(enriched, candidates, all_lines)
        primary_candidate_count = len(candidates)
        primary_line_count = len(all_lines)
        fallback_candidate_count = 0
        combined_candidate_count = len(candidates)
        result_reuse_used = False
        fallback_needed = (
            not candidates
            if len(enriched) <= 1
            else any(candidate is None for candidate, _, _ in assignments)
        )
        fallback_used = False
        if fallback_needed and (crop_top, crop_bottom) != (self.crop_top, self.crop_bottom):
            (
                fallback_candidates,
                fallback_lines_checked,
                fallback_lines,
                fallback_preprocessing,
            ) = self._read_candidates(
                image_path,
                self.crop_top,
                self.crop_bottom,
                len(enriched),
                "fallback",
            )
            fallback_candidate_count = len(fallback_candidates)
            combined_candidates = _merge_usage_candidates(
                [candidates, fallback_candidates]
            )
            combined_lines = _merge_usage_candidates([all_lines, fallback_lines])
            combined_candidate_count = len(combined_candidates)
            combined_assignments = _assign_usage_candidates(
                enriched,
                combined_candidates,
                combined_lines,
            )
            fallback_assignments = _assign_usage_candidates(
                enriched, fallback_candidates, fallback_lines
            )
            lines_checked += fallback_lines_checked

            primary_coverage = sum(item[0] is not None for item in assignments)
            combined_coverage = sum(
                item[0] is not None for item in combined_assignments
            )
            fallback_coverage = sum(
                item[0] is not None for item in fallback_assignments
            )
            if combined_coverage > primary_coverage:
                candidates = combined_candidates
                all_lines = combined_lines
                assignments = combined_assignments
                crop_top = min(crop_top, self.crop_top)
                crop_bottom = max(crop_bottom, self.crop_bottom)
                crop_mode = f"{crop_mode}_fallback_merged"
                result_reuse_used = bool(primary_candidate_count or primary_line_count)
            elif fallback_coverage > primary_coverage:
                candidates = fallback_candidates
                all_lines = fallback_lines
                assignments = fallback_assignments
                crop_top, crop_bottom = self.crop_top, self.crop_bottom
                crop_mode = f"{crop_mode}_fallback"
            preprocessing["blue_mask_used"] = bool(
                preprocessing["blue_mask_used"]
                or fallback_preprocessing["blue_mask_used"]
            )
            preprocessing["grayscale_used"] = bool(
                preprocessing.get("grayscale_used", False)
                or fallback_preprocessing.get("grayscale_used", False)
            )
            preprocessing["original_anchor_ocr_used"] = bool(
                preprocessing["original_anchor_ocr_used"]
                or fallback_preprocessing["original_anchor_ocr_used"]
            )
            fallback_used = True

        metrics: Dict[str, Any] = {
            "ocr_lines_checked": lines_checked,
            "ocr_unique_lines": len(all_lines),
            "usage_found": 0,
            "duration_success": 0,
            "needs_review": 0,
            "crop_mode": crop_mode,
            "crop_top_ratio": crop_top,
            "crop_bottom_ratio": crop_bottom,
            "fallback_used": fallback_used,
            "usage_preprocessing_mode": preprocessing["mode"],
            "usage_blue_mask_used": preprocessing["blue_mask_used"],
            "usage_grayscale_used": preprocessing.get("grayscale_used", False),
            "usage_original_anchor_ocr_used": preprocessing["original_anchor_ocr_used"],
            "primary_candidate_count": primary_candidate_count,
            "fallback_candidate_count": fallback_candidate_count,
            "combined_candidate_count": combined_candidate_count,
            "fallback_result_reuse_used": result_reuse_used,
            "usage_original_retry_used": False,
            "usage_original_retry_selected": 0,
            "usage_original_retry_candidate_count": 0,
            "usage_original_retry_candidates": [],
            "ocr_coordinate_scale": 2.0,
            "usage_candidates": [
                {
                    "text": str(candidate.get("text") or ""),
                    "score": round(float(candidate.get("score") or 0.0), 6),
                    "x": round(float(candidate.get("x") or 0.0), 2),
                    "y": round(float(candidate.get("y") or 0.0), 2),
                    "width": round(float(candidate.get("width") or 0.0), 2),
                    "height": round(float(candidate.get("height") or 0.0), 2),
                    "ocr_pass": str(candidate.get("ocr_pass") or ""),
                }
                for candidate in candidates
            ],
            "failure_counts": {
                "ocr_no_text": 0,
                "ocr_low_confidence": 0,
                "usage_not_found": 0,
                "usage_parse_failed": 0,
                "drug_usage_match_failed": 0,
                "duration_calculation_failed": 0,
            },
        }

        original_retry_assignments: Optional[
            List[Tuple[Optional[Dict[str, Any]], str, float]]
        ] = None

        for index, drug in enumerate(enriched):
            candidate, mapping_method, mapping_confidence = assignments[index]
            if candidate is None:
                if not all_lines:
                    failure_type = "ocr_no_text"
                elif not candidates:
                    failure_type = "usage_not_found"
                else:
                    failure_type = "drug_usage_match_failed"
                drug.update(
                    {
                        "usage_source": "prescription_image_paddleocr",
                        "usage_ocr_status": "not_found",
                        "usage_failure_type": failure_type,
                        "usage_mapping_method": mapping_method,
                        "usage_mapping_confidence": mapping_confidence,
                        "duration_calculation_status": "needs_review",
                        "duration_calculation_reason": (
                            "multiple_usage_candidates_for_single_drug"
                            if mapping_method == "multiple_usage_candidates_for_single_drug"
                            else "usage_not_found_in_image"
                        ),
                    }
                )
                metrics["failure_counts"][failure_type] += 1
                metrics["needs_review"] += 1
                continue
            usage = parse_usage_text(candidate["text"])
            confidence = float(candidate["score"])
            duration = calculate_duration(drug, usage)
            primary_candidate = candidate
            original_retry_candidate: Optional[Dict[str, Any]] = None
            original_retry_selected = False
            if (
                str(
                    getattr(
                        self,
                        "usage_preprocessing_mode",
                        preprocessing.get("mode") or "blue_mask",
                    )
                )
                == "blue_mask"
                and duration.get("duration_calculation_reason")
                == "dose_and_package_units_mismatch"
            ):
                if original_retry_assignments is None:
                    (
                        original_retry_candidates,
                        original_retry_lines_checked,
                        original_retry_lines,
                        _original_retry_preprocessing,
                    ) = self._read_candidates(
                        image_path,
                        crop_top,
                        crop_bottom,
                        len(enriched),
                        "original_retry",
                        preprocessing_mode="original",
                    )
                    original_retry_assignments = _assign_usage_candidates(
                        enriched,
                        original_retry_candidates,
                        original_retry_lines,
                    )
                    metrics["usage_original_retry_used"] = True
                    metrics["usage_original_retry_candidate_count"] = len(
                        original_retry_candidates
                    )
                    metrics["usage_original_retry_candidates"] = [
                        {
                            "text": str(item.get("text") or ""),
                            "score": round(float(item.get("score") or 0.0), 6),
                            "ocr_pass": str(item.get("ocr_pass") or ""),
                        }
                        for item in original_retry_candidates
                    ]
                    metrics["ocr_lines_checked"] += original_retry_lines_checked
                (
                    original_retry_candidate,
                    retry_mapping_method,
                    retry_mapping_confidence,
                ) = original_retry_assignments[index]
                if original_retry_candidate is not None:
                    retry_usage = parse_usage_text(original_retry_candidate["text"])
                    retry_confidence = float(original_retry_candidate["score"])
                    retry_duration = calculate_duration(drug, retry_usage)
                    if (
                        retry_confidence >= self.min_confidence
                        and retry_duration.get("duration_calculation_status") == "success"
                    ):
                        candidate = original_retry_candidate
                        usage = retry_usage
                        confidence = retry_confidence
                        duration = retry_duration
                        mapping_method = f"{retry_mapping_method}_original_retry"
                        mapping_confidence = retry_mapping_confidence
                        original_retry_selected = True
                        metrics["usage_original_retry_selected"] += 1
            status = "success" if confidence >= self.min_confidence else "low_confidence"
            if status == "low_confidence" and duration["duration_calculation_status"] == "success":
                duration["duration_calculation_status"] = "needs_review"
                duration["duration_calculation_reason"] = "ocr_confidence_below_threshold"
            if status == "low_confidence":
                failure_type = "ocr_low_confidence"
            elif duration["duration_calculation_status"] == "success":
                failure_type = ""
            elif duration.get("duration_calculation_reason") in {
                "frequency_missing_or_unparsed",
                "dose_missing_or_unparsed",
            }:
                failure_type = "usage_parse_failed"
            else:
                failure_type = "duration_calculation_failed"
            drug.update(usage)
            drug.update(duration)
            drug.update(
                {
                    "usage_source": "prescription_image_paddleocr",
                    "usage_ocr_status": status,
                    "usage_failure_type": failure_type,
                    "usage_ocr_confidence": round(confidence, 6),
                    "usage_mapping_method": mapping_method,
                    "usage_mapping_confidence": mapping_confidence,
                    "usage_ocr_primary_text": str(primary_candidate.get("text") or ""),
                    "usage_ocr_original_retry_used": original_retry_candidate is not None,
                    "usage_ocr_original_retry_text": (
                        str(original_retry_candidate.get("text") or "")
                        if original_retry_candidate is not None
                        else ""
                    ),
                    "usage_ocr_selected_pass": (
                        "original_retry" if original_retry_selected else "primary"
                    ),
                }
            )
            if failure_type:
                metrics["failure_counts"][failure_type] += 1
            metrics["usage_found"] += 1
            if drug["duration_calculation_status"] == "success":
                metrics["duration_success"] += 1
            else:
                metrics["needs_review"] += 1
        return enriched, metrics


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run local PaddleOCR usage extraction on one prescription image."
    )
    parser.add_argument("--image", required=True)
    parser.add_argument("--drug-name", required=True)
    parser.add_argument("--drug-count", type=float, required=True)
    parser.add_argument("--packing-spec", default="")
    parser.add_argument("--min-confidence", type=float, default=0.75)
    args = parser.parse_args()

    engine = PaddleUsageOCR(min_confidence=args.min_confidence)
    drugs, metrics = engine.enrich(
        [
            {
                "drugName": args.drug_name,
                "drugCount": args.drug_count,
                "packingSpec": args.packing_spec,
            }
        ],
        Path(args.image).expanduser().resolve(),
    )
    header = engine.extract_header_fields(Path(args.image).expanduser().resolve())
    print(
        json.dumps(
            {"drug": drugs[0], "patient_fields": header, "metrics": metrics},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

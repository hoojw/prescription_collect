"""Validate fields supplied by platform APIs before OCR and delivery.

This module deliberately checks only fields whose source is the platform
response. OCR-derived fields and optional delivery fields remain governed by
their existing processing rules.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from prescription_usage_ocr import split_drug_name_specification

REASON = "platform_required_missing"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _source(field: str, source: str) -> dict[str, str]:
    return {"field": field, "source": source}


def _meituan_diagnosis_name(record: dict[str, Any], patient: dict[str, Any]) -> Any:
    """Use the same Meituan diagnosis fallbacks as interface delivery."""
    illness = patient.get("illnessDetail") or []
    if isinstance(illness, dict):
        illness = [illness]
    if isinstance(illness, list):
        for item in illness:
            if isinstance(item, dict) and not _missing(item.get("icdName")):
                return item.get("icdName")
    return record.get("icd_name")


def _platform_drug_specification(drug: dict[str, Any], platform: str) -> Any:
    explicit = drug.get("packingSpec") or drug.get("packing_spec")
    if not _missing(explicit) or platform != "MEITUAN":
        return explicit
    # Some Meituan stores return packingSpec=null while retaining the complete
    # platform specification at the end of drugName. Reuse the existing parser
    # instead of treating that platform-provided value as absent.
    _, derived = split_drug_name_specification(
        drug.get("drugName") or drug.get("drug_name"),
        "",
    )
    return derived


def validate_platform_required_fields(
    record: dict[str, Any], platform: str
) -> tuple[list[str], list[dict[str, str]]]:
    """Return missing canonical fields and their platform source locations."""
    platform_name = _text(platform).upper()
    missing: list[str] = []
    sources: list[dict[str, str]] = []

    def require(field: str, value: Any, source: str) -> None:
        if _missing(value):
            missing.append(field)
            sources.append(_source(field, source))

    require("order_view_id", record.get("order_view_id"), "platform.order_id")
    require("prescription_id", record.get("prescription_id"), "platform.prescription_id")
    require(
        "prescription_time",
        record.get("prescription_time"),
        "platform.prescription_time",
    )
    require("image_url", record.get("image_url"), "platform.prescription_image")
    require("total_fee", record.get("total_fee"), "platform.order_amount")

    patient = record.get("patient_info") or {}
    if not isinstance(patient, dict):
        patient = {}
    require(
        "patient_info.name",
        patient.get("name") or patient.get("medicalUserName"),
        "platform.patient.name|medicalUserName",
    )
    require("patient_info.gender", patient.get("gender"), "platform.patient.gender")
    require("patient_info.age", patient.get("age"), "platform.patient.age")

    require(
        "diagnosis.disease_name",
        _meituan_diagnosis_name(record, patient)
        if platform_name == "MEITUAN"
        else record.get("icd_name"),
        "platform.diagnosis.name|patient.illnessDetail[].icdName"
        if platform_name == "MEITUAN"
        else "platform.diagnosis.name",
    )

    drugs = record.get("drug_infos") or []
    if isinstance(drugs, dict):
        drugs = [drugs]
    if not isinstance(drugs, list) or not drugs:
        missing.append("drug_infos")
        sources.append(_source("drug_infos", "platform.drug_list"))
    else:
        for index, drug in enumerate(drugs):
            if not isinstance(drug, dict):
                missing.append(f"drug_infos[{index}]")
                sources.append(_source(f"drug_infos[{index}]", "platform.drug_list"))
                continue
            prefix = f"drug_infos[{index}]"
            require(
                f"{prefix}.drugName",
                drug.get("drugName") or drug.get("drug_name"),
                f"platform.drug_list[{index}].name",
            )
            require(
                f"{prefix}.packingSpec",
                _platform_drug_specification(drug, platform_name),
                f"platform.drug_list[{index}].specification|name_suffix"
                if platform_name == "MEITUAN"
                else f"platform.drug_list[{index}].specification",
            )
            quantity = drug.get("drugCount")
            if quantity is None:
                quantity = drug.get("purchase_quantity")
            try:
                invalid_quantity = float(quantity) <= 0
            except (TypeError, ValueError):
                invalid_quantity = True
            if invalid_quantity:
                missing.append(f"{prefix}.drugCount")
                sources.append(
                    _source(f"{prefix}.drugCount", f"platform.drug_list[{index}].quantity")
                )

    # The JD SKU is directly platform-provided. Meituan drcode is resolved by
    # the separate product-list matcher and keeps its existing review rules.
    if platform_name == "JINGDONG" and isinstance(drugs, list):
        for index, drug in enumerate(drugs):
            if isinstance(drug, dict) and _missing(
                drug.get("jd_sku_id") or drug.get("drcode")
            ):
                field = f"drug_infos[{index}].drcode"
                missing.append(field)
                sources.append(_source(field, f"platform.drug_list[{index}].sku_id"))

    return missing, sources


def attach_platform_required_review(
    record: dict[str, Any],
    platform: str,
    missing_fields: list[str],
    missing_sources: list[dict[str, str]],
) -> dict[str, Any]:
    review = dict(record)
    review["review_type"] = "platform_required_field"
    review["platform"] = _text(platform).upper()
    review["need_review_reasons"] = [REASON]
    review["platform_missing_fields"] = list(dict.fromkeys(missing_fields))
    review["platform_missing_sources"] = missing_sources
    review["upload_eligible"] = False
    return review


def missing_field_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for record in records:
        counter.update(str(field) for field in record.get("platform_missing_fields") or [])
    return dict(sorted(counter.items()))

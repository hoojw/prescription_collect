#!/usr/bin/env python3
"""Offline, privacy-safe duration validation for historical exports and mocks."""

from __future__ import annotations

import argparse
import collections
import json
import zipfile
from pathlib import Path
from typing import Any, Iterable

from drug_catalog import DrugCatalog, catalog_entry_from_row, status_code_for_drug
from prescription_usage_ocr import calculate_duration, parse_usage_text


def _records(payload: Any) -> Iterable[dict[str, Any]]:
    roots = payload if isinstance(payload, list) else [payload]
    for root in roots:
        if not isinstance(root, dict):
            continue
        data = root.get("data_list")
        if isinstance(data, list):
            yield from (item for item in data if isinstance(item, dict))


def _history_drug(medicine: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    method = str(medicine.get("usage_method") or "")
    frequency = str(medicine.get("usage_frequency") or "")
    dose = medicine.get("dosage_per_time")
    unit = str(medicine.get("dosage_unit") or "")
    usage = parse_usage_text(f"{method} {frequency} 每次{dose or ''}{unit}")
    drug = {
        "drugName": str(medicine.get("drug_name") or ""),
        "packingSpec": str(medicine.get("specification") or ""),
        "drugCount": medicine.get("total_quantity"),
        "usage_method": method,
        "administration_route": usage.get("administration_route"),
        "frequency_text": frequency,
        "times_per_day": usage.get("times_per_day"),
        "dose_per_time": usage.get("dose_per_time"),
        "dose_unit": usage.get("dose_unit"),
        "usage_source": "historical_structured_export",
    }
    drug.update(calculate_duration(drug, usage))
    return drug, usage


def history_validation(zip_path: Path, catalog: DrugCatalog) -> dict[str, Any]:
    status_counts: collections.Counter[int] = collections.Counter()
    reason_counts: collections.Counter[str] = collections.Counter()
    files = records = medicines = 0
    success = catalog_success = 0
    review_examples: list[dict[str, Any]] = []
    with zipfile.ZipFile(zip_path) as archive:
        members = [
            name
            for name in archive.namelist()
            if name.lower().endswith(".json") and "/._" not in name
        ]
        for name in members:
            try:
                payload = json.loads(archive.read(name).decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            files += 1
            for record in _records(payload):
                records += 1
                for medicine in record.get("medicine_info") or []:
                    if not isinstance(medicine, dict):
                        continue
                    medicines += 1
                    drug, _ = _history_drug(medicine)
                    catalog.apply_if_needed(drug)
                    code = int(drug.get("status_code") or status_code_for_drug(drug))
                    status_counts[code] += 1
                    if drug.get("duration_calculation_status") == "success":
                        success += 1
                        if code == 110:
                            catalog_success += 1
                    else:
                        reason = str(drug.get("duration_calculation_reason") or "unknown")
                        reason_counts[reason] += 1
                        if len(review_examples) < 20:
                            review_examples.append(
                                {
                                    "drug_name": drug["drugName"],
                                    "specification": drug["packingSpec"],
                                    "status_code": code,
                                    "reason": reason,
                                }
                            )
    return {
        "files": files,
        "records": records,
        "medicines": medicines,
        "duration_success": success,
        "duration_review": medicines - success,
        "catalog_success": catalog_success,
        "status_counts": {str(key): value for key, value in sorted(status_counts.items())},
        "review_reason_counts": dict(reason_counts.most_common()),
        "review_examples_no_patient_data": review_examples,
    }


def mock_validation() -> list[dict[str, Any]]:
    def row(drug_id: str, name: str, spec: str, dose: Any, unit: str, freq: str, route: str, form: str):
        return {
            "药品id": drug_id,
            "药品名称": name,
            "药品规格": spec,
            "推荐用量": dose,
            "用量单位": unit,
            "常用频次": freq,
            "给药途径": route,
            "剂型": form,
        }

    tablet = catalog_entry_from_row(
        row("mock-1", "业务测试药片", "0.5g*20片/盒", 1, "片", "一天两次", "口服", "片剂")
    )
    injection = catalog_entry_from_row(
        row("mock-2", "业务测试注射液", "2.4ml:20mg/支/盒", 20, "mg", "一天一次", "静脉注射", "注射剂")
    )
    conflict_a = catalog_entry_from_row(
        row("mock-3a", "冲突测试药片", "10mg*10片/盒", 1, "片", "一天一次", "口服", "片剂")
    )
    conflict_b = catalog_entry_from_row(
        row("mock-3b", "冲突测试药片", "10mg*10片/盒", 2, "片", "一天一次", "口服", "片剂")
    )
    catalog = DrugCatalog([tablet, injection, conflict_a, conflict_b])
    cases = [
        {
            "case": "missing_frequency_catalog_success",
            "drug": {
                "drugName": "业务测试药片0.5g*20片/盒",
                "drugCount": 1,
                "dose_per_time": 1,
                "dose_unit": "片",
                "usage_source": "prescription_image_paddleocr",
            },
        },
        {
            "case": "missing_dose_catalog_success",
            "drug": {
                "drugName": "业务测试药片0.5g*20片/盒",
                "drugCount": 1,
                "times_per_day": 2,
                "administration_route": "口服",
                "usage_source": "prescription_image_paddleocr",
            },
        },
        {
            "case": "mass_volume_mismatch_review",
            "drug": {"drugName": "业务测试注射液1.2ml:20mg/支/盒", "drugCount": 1},
        },
        {
            "case": "regimen_conflict_review",
            "drug": {"drugName": "冲突测试药片10mg*10片/盒", "drugCount": 1},
        },
        {
            "case": "unlabelled_pack_suffix_success",
            "drug": {
                "drugName": "业务测试药片0.5g*20",
                "drugCount": 1,
                "dose_per_time": 1,
                "dose_unit": "片",
                "times_per_day": 2,
                "duration_calculation_status": "success",
            },
        },
    ]
    output = []
    for case in cases:
        drug = dict(case["drug"])
        if drug.get("duration_calculation_status") == "success":
            usage = {
                "dose_per_time": drug.get("dose_per_time"),
                "dose_unit": drug.get("dose_unit"),
                "times_per_day": drug.get("times_per_day"),
            }
            drug.update(calculate_duration(drug, usage))
        catalog.apply_if_needed(drug)
        output.append(
            {
                "case": case["case"],
                "status_code": int(drug.get("status_code") or status_code_for_drug(drug)),
                "duration_days": drug.get("duration_days"),
                "reason": drug.get("duration_calculation_reason"),
            }
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-zip", required=True)
    parser.add_argument("--catalog-index", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    catalog = DrugCatalog.load(Path(args.catalog_index))
    report = {
        "history": history_validation(Path(args.history_zip), catalog),
        "business_catalog_mocks": mock_validation(),
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **report["history"]}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

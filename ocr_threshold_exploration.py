#!/usr/bin/env python3
"""Compare grayscale thresholds for header and usage OCR on saved prescriptions."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
from collections import Counter, defaultdict
from statistics import mean

from prescription_usage_ocr import PaddleUsageOCR


def age_number(value: object) -> int | None:
    match = re.search(r"\d{1,3}", str(value or ""))
    return int(match.group()) if match else None


def raw_drugs(record: dict) -> list[dict]:
    output = []
    for medicine in record.get("medicine_info") or []:
        if not isinstance(medicine, dict):
            continue
        output.append(
            {
                "drugName": medicine.get("drug_name") or "",
                "packingSpec": medicine.get("specification") or "",
                "drugCount": medicine.get("total_quantity"),
                "purchase_unit": medicine.get("total_unit") or "",
            }
        )
    return output


def modal_matches(values_by_mode: dict[str, dict[str, str]]) -> dict[str, int]:
    matches = Counter()
    image_names = {name for values in values_by_mode.values() for name in values}
    for image_name in image_names:
        values = [
            values_by_mode[mode].get(image_name, "")
            for mode in values_by_mode
            if values_by_mode[mode].get(image_name, "")
        ]
        if not values:
            continue
        modal = Counter(values).most_common(1)[0][0]
        for mode, mode_values in values_by_mode.items():
            if mode_values.get(image_name) == modal:
                matches[mode] += 1
    return dict(matches)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_dir", type=pathlib.Path)
    parser.add_argument("prescriptions", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--thresholds", default="original,100,120,140,160,180,200,225")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    modes: list[tuple[str, int | None]] = []
    for value in args.thresholds.split(","):
        value = value.strip().lower()
        modes.append((value, None if value == "original" else int(value)))

    records = [
        json.loads(line)
        for line in args.prescriptions.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    records = [record for record in records if record.get("image_file")][: args.limit]
    engine = PaddleUsageOCR(
        min_confidence=0.75,
        hospital_grayscale_threshold=140,
    )

    header_stats: dict[str, dict] = {}
    header_birth_values: dict[str, dict[str, str]] = defaultdict(dict)
    header_department_values: dict[str, dict[str, str]] = defaultdict(dict)
    usage_stats: dict[str, dict] = {}
    usage_values: dict[str, dict[str, str]] = defaultdict(dict)

    for mode, threshold in modes:
        engine.header_grayscale_threshold = threshold
        birth_confidences = []
        department_confidences = []
        header = Counter()
        for record in records:
            image_name = pathlib.Path(str(record["image_file"])).name
            image_path = args.image_dir / image_name
            try:
                result = engine.extract_header_fields(image_path)
            except Exception:
                header["errors"] += 1
                continue
            birth = str(result.get("birth_date") or "")
            department = str(result.get("department") or "")
            birth_success = result.get("birth_date_ocr_status") == "success"
            department_success = result.get("department_ocr_status") == "success"
            header["birth_success"] += int(birth_success)
            header["department_success"] += int(department_success)
            header["both_success"] += int(birth_success and department_success)
            header["blue_fallback_used"] += int(bool(result.get("blue_fallback_used")))
            if birth_success:
                birth_confidences.append(float(result.get("birth_date_ocr_confidence") or 0.0))
                header_birth_values[mode][image_name] = birth
                age = age_number((record.get("patient_info") or {}).get("age"))
                reference_year_match = re.match(r"(20\d{2})", str(record.get("prescription_time") or ""))
                if age is not None and reference_year_match and re.match(r"(19|20)\d{2}", birth):
                    reference_year = int(reference_year_match.group(1))
                    birth_year = int(birth[:4])
                    header["birth_age_consistent"] += int(
                        abs((reference_year - birth_year) - age) <= 1
                    )
            if department_success:
                department_confidences.append(
                    float(result.get("department_ocr_confidence") or 0.0)
                )
                header_department_values[mode][image_name] = department
        header_stats[mode] = {
            "images": len(records),
            **header,
            "birth_confidence_mean": round(mean(birth_confidences), 6)
            if birth_confidences
            else 0.0,
            "department_confidence_mean": round(mean(department_confidences), 6)
            if department_confidences
            else 0.0,
        }

        # Usage OCR is fixed to the blue-mask pipeline; thresholds apply to header OCR only.
        usage = Counter()
        usage_confidences = []
        for record in records:
            image_name = pathlib.Path(str(record["image_file"])).name
            image_path = args.image_dir / image_name
            drugs = raw_drugs(record)
            if not drugs:
                usage["images_without_drugs"] += 1
                continue
            try:
                enriched, metrics = engine.enrich(drugs, image_path)
            except Exception:
                usage["errors"] += 1
                continue
            usage["images"] += 1
            usage["drugs"] += len(enriched)
            usage["usage_found"] += int(metrics.get("usage_found") or 0)
            usage["duration_success"] += int(metrics.get("duration_success") or 0)
            usage["needs_review"] += int(metrics.get("needs_review") or 0)
            usage["fallback_used"] += int(bool(metrics.get("fallback_used")))
            signatures = []
            for drug in enriched:
                if drug.get("usage_ocr_status") == "success":
                    usage_confidences.append(float(drug.get("usage_ocr_confidence") or 0.0))
                dose_frequency = bool(
                    drug.get("dose_per_time") is not None
                    and drug.get("dose_unit")
                    and drug.get("frequency_text")
                )
                usage["dose_frequency_parsed"] += int(dose_frequency)
                signatures.append(
                    "|".join(
                        str(value or "")
                        for value in (
                            drug.get("usage_method"),
                            drug.get("frequency_text"),
                            drug.get("dose_per_time"),
                            drug.get("dose_unit"),
                        )
                    )
                )
            usage_values[mode][image_name] = "||".join(signatures)
        usage_stats[mode] = {
            **usage,
            "usage_confidence_mean": round(mean(usage_confidences), 6)
            if usage_confidences
            else 0.0,
        }

    birth_modal = modal_matches(header_birth_values)
    department_modal = modal_matches(header_department_values)
    usage_modal = modal_matches(usage_values)
    for mode, _ in modes:
        header_stats[mode]["birth_modal_matches"] = birth_modal.get(mode, 0)
        header_stats[mode]["department_modal_matches"] = department_modal.get(mode, 0)
        usage_stats[mode]["usage_modal_matches"] = usage_modal.get(mode, 0)

    selected_header = max(
        (mode for mode, _ in modes),
        key=lambda mode: (
            header_stats[mode].get("birth_age_consistent", 0),
            header_stats[mode].get("birth_modal_matches", 0)
            + header_stats[mode].get("department_modal_matches", 0),
            header_stats[mode].get("both_success", 0),
            header_stats[mode].get("birth_confidence_mean", 0.0)
            + header_stats[mode].get("department_confidence_mean", 0.0),
            -header_stats[mode].get("blue_fallback_used", 0),
        ),
    )
    selected_usage = max(
        (mode for mode, _ in modes),
        key=lambda mode: (
            usage_stats[mode].get("usage_modal_matches", 0),
            usage_stats[mode].get("dose_frequency_parsed", 0),
            usage_stats[mode].get("duration_success", 0),
            usage_stats[mode].get("usage_found", 0),
            usage_stats[mode].get("usage_confidence_mean", 0.0),
            -usage_stats[mode].get("fallback_used", 0),
        ),
    )
    output = {
        "sample_size": len(records),
        "modes": [mode for mode, _ in modes],
        "header": header_stats,
        "usage": usage_stats,
        "selected_header_mode": selected_header,
        "selected_usage_mode": selected_usage,
        "selection_note": "Selection uses age consistency, cross-mode consensus, parse completeness, confidence and fallback count; final candidates require full-sample validation.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    print(json.dumps(output, ensure_ascii=False))
    print(f"OUTPUT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

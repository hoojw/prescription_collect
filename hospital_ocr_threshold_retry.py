#!/usr/bin/env python3
"""Retry hospital-title OCR on not-found images after grayscale thresholding."""

from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter

import cv2

from prescription_usage_ocr import PaddleUsageOCR, parse_hospital_fields


def threshold_lines(
    engine: PaddleUsageOCR,
    image_path: pathlib.Path,
    threshold: int,
) -> list[dict]:
    image = cv2.imread(str(image_path))
    if image is None:
        raise RuntimeError("cannot read image")
    height, width = image.shape[:2]
    crop = image[
        int(height * engine.hospital_crop_top) : int(height * engine.hospital_crop_bottom),
        int(width * engine.hospital_crop_left) : int(width * engine.hospital_crop_right),
    ]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    binary = cv2.resize(binary, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    binary_bgr = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    return engine._predict(binary_bgr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_dir", type=pathlib.Path)
    parser.add_argument("original_results", type=pathlib.Path)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--hospital", required=True)
    parser.add_argument("--thresholds", default="140,160,180,200")
    args = parser.parse_args()

    thresholds = [int(value) for value in args.thresholds.split(",") if value.strip()]
    original = [
        json.loads(line)
        for line in args.original_results.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    targets = [row for row in original if int(row.get("hospital_source_code") or 3) == 3]
    engine = PaddleUsageOCR(
        min_confidence=0.75,
        hospital_crop_left=0.10,
        hospital_crop_right=0.90,
        hospital_crop_top=0.02,
        hospital_crop_bottom=0.20,
    )

    results = []
    for target in targets:
        image_path = args.image_dir / str(target["image_file"])
        attempts = []
        selected = None
        for threshold in thresholds:
            try:
                lines = threshold_lines(engine, image_path, threshold)
                classification = parse_hospital_fields(lines, args.hospital, 0.75)
                attempt = {
                    "threshold": threshold,
                    "ocr_lines": [
                        {"text": row.get("text"), "score": row.get("score")}
                        for row in lines
                    ],
                    "classification": classification,
                }
            except Exception as exc:
                attempt = {
                    "threshold": threshold,
                    "ocr_lines": [],
                    "classification": {
                        "hospital_source_code": 3,
                        "hospital_ocr_status": "error",
                        "hospital_ocr_reason": "ocr_runtime_error",
                        "error": type(exc).__name__,
                        "error_message": str(exc),
                    },
                }
            attempts.append(attempt)
            if int(attempt["classification"].get("hospital_source_code") or 3) in {1, 2}:
                selected = attempt
                break
        if selected is None:
            selected = attempts[-1]
        results.append(
            {
                "image_file": target["image_file"],
                "selected_threshold": selected["threshold"],
                "classification": selected["classification"],
                "attempts": attempts,
            }
        )

    source_counts = Counter(
        int(row["classification"].get("hospital_source_code") or 3) for row in results
    )
    threshold_counts = Counter(
        int(row["selected_threshold"])
        for row in results
        if int(row["classification"].get("hospital_source_code") or 3) in {1, 2}
    )
    name_counts = Counter(
        str(row["classification"].get("hospital_name_ocr") or "")
        for row in results
        if row["classification"].get("hospital_name_ocr")
    )
    reason_counts = Counter(
        str(row["classification"].get("hospital_ocr_reason") or "") for row in results
    )
    summary = {
        "retry_total": len(results),
        "recovered": source_counts[1] + source_counts[2],
        "recovered_internal": source_counts[1],
        "recovered_external": source_counts[2],
        "still_needs_review": source_counts[3],
        "successful_threshold_counts": dict(sorted(threshold_counts.items())),
        "recognized_hospital_counts": dict(name_counts.most_common()),
        "final_reason_counts": dict(reason_counts.most_common()),
        "thresholds_tested": thresholds,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results_path = args.output_dir / "hospital_ocr_threshold_retry_results.jsonl"
    results_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results),
        encoding="utf-8",
    )
    results_path.chmod(0o600)
    summary_path = args.output_dir / "hospital_ocr_threshold_retry_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary_path.chmod(0o600)
    print(json.dumps(summary, ensure_ascii=False))
    print(f"RESULTS_PATH={results_path}")
    print(f"SUMMARY_PATH={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

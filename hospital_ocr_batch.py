#!/usr/bin/env python3
"""Run only prescription-title hospital OCR against an existing image directory."""

from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter

from prescription_usage_ocr import PaddleUsageOCR


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_dir", type=pathlib.Path)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--hospital", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument("--crop-left", type=float, default=0.10)
    parser.add_argument("--crop-right", type=float, default=0.90)
    parser.add_argument("--crop-top", type=float, default=0.02)
    parser.add_argument("--crop-bottom", type=float, default=0.20)
    args = parser.parse_args()

    images = sorted(args.image_dir.glob("*.png"))
    if args.limit > 0:
        images = images[: args.limit]
    if not images:
        raise SystemExit(f"no PNG images found in {args.image_dir}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    engine = PaddleUsageOCR(
        min_confidence=args.min_confidence,
        hospital_crop_left=args.crop_left,
        hospital_crop_right=args.crop_right,
        hospital_crop_top=args.crop_top,
        hospital_crop_bottom=args.crop_bottom,
    )

    results = []
    for image_path in images:
        try:
            result = engine.extract_hospital_fields(image_path, args.hospital)
        except Exception as exc:
            result = {
                "hospital_source": "review",
                "hospital_source_code": 3,
                "hospital_ocr_status": "error",
                "hospital_ocr_reason": "ocr_runtime_error",
                "error": type(exc).__name__,
            }
        results.append({"image_file": image_path.name, **result})

    source_counts = Counter(int(row.get("hospital_source_code") or 3) for row in results)
    reason_counts = Counter(str(row.get("hospital_ocr_reason") or "") for row in results)
    name_counts = Counter(
        str(row.get("hospital_name_ocr") or "")
        for row in results
        if row.get("hospital_name_ocr")
    )
    summary = {
        "images_total": len(results),
        "internal": source_counts[1],
        "external": source_counts[2],
        "needs_review": source_counts[3],
        "reason_counts": dict(reason_counts.most_common()),
        "recognized_hospital_counts": dict(name_counts.most_common()),
        "internal_hospital_name": args.hospital,
        "crop": {
            "left": args.crop_left,
            "right": args.crop_right,
            "top": args.crop_top,
            "bottom": args.crop_bottom,
        },
        "min_confidence": args.min_confidence,
    }

    results_path = args.output_dir / "hospital_ocr_only_results.jsonl"
    results_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results),
        encoding="utf-8",
    )
    results_path.chmod(0o600)
    summary_path = args.output_dir / "hospital_ocr_only_summary.json"
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

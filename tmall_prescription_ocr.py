#!/usr/bin/env python3
"""Tmall-specific OCR facade built on the shared PaddleOCR engine.

The crop ratios and preprocessing reproduce the Tmall prototype agreed in
early August 2026: original-colour OCR only, a fixed diagnosis band, and
different usage bands for single- and multi-drug prescriptions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

from prescription_usage_ocr import PaddleUsageOCR


DIAGNOSIS_LABEL_RE = re.compile(r"(?:临床)?诊断\s*[:：]?\s*(.*)")
DIAGNOSIS_STOP_RE = re.compile(
    r"^(?:Rp|R[pP]|用法用量|药品|补充说明|肝功能异常|肾功能异常|过敏史)\s*[:：]?"
)
AGE_LABEL_RE = re.compile(r"年龄\s*[:：]?\s*(\d{1,3})\s*岁?")
AGE_VALUE_RE = re.compile(r"^(\d{1,3})\s*岁$")


def _same_row_candidates(
    lines: Sequence[Dict[str, Any]],
    index: int,
) -> list[Dict[str, Any]]:
    anchor = lines[index]
    anchor_center = float(anchor.get("y") or 0.0) + float(anchor.get("height") or 0.0) / 2
    anchor_right = float(anchor.get("x") or 0.0) + float(anchor.get("width") or 0.0)
    tolerance = max(12.0, float(anchor.get("height") or 16.0) * 0.9)
    candidates = []
    for line in lines:
        center = float(line.get("y") or 0.0) + float(line.get("height") or 0.0) / 2
        if float(line.get("x") or 0.0) < anchor_right - 4:
            continue
        if abs(center - anchor_center) <= tolerance:
            candidates.append(line)
    return sorted(candidates, key=lambda item: float(item.get("x") or 0.0))


def parse_tmall_header_lines(
    lines: Sequence[Dict[str, Any]],
    min_confidence: float = 0.75,
) -> Dict[str, Any]:
    eligible = [
        line
        for line in lines
        if str(line.get("text") or "").strip()
        and float(line.get("score") or 0.0) >= float(min_confidence)
    ]
    diagnosis = ""
    diagnosis_scores: list[float] = []
    age = ""
    age_scores: list[float] = []
    for index, line in enumerate(eligible):
        value = str(line.get("text") or "").strip()
        if not age:
            age_match = AGE_LABEL_RE.search(value)
            if age_match and 0 <= int(age_match.group(1)) <= 120:
                age = str(int(age_match.group(1)))
                age_scores.append(float(line.get("score") or 0.0))
            elif re.search(r"年龄\s*[:：]?\s*$", value):
                for candidate in _same_row_candidates(eligible, index):
                    match = AGE_VALUE_RE.fullmatch(str(candidate.get("text") or "").strip())
                    if match and 0 <= int(match.group(1)) <= 120:
                        age = str(int(match.group(1)))
                        age_scores.extend(
                            [float(line.get("score") or 0.0), float(candidate.get("score") or 0.0)]
                        )
                        break

        if diagnosis:
            continue
        diagnosis_match = DIAGNOSIS_LABEL_RE.search(value)
        if not diagnosis_match:
            continue
        diagnosis = TmallPrescriptionOCR._clean_diagnosis(diagnosis_match.group(1))
        diagnosis_scores.append(float(line.get("score") or 0.0))
        if not diagnosis:
            for candidate in _same_row_candidates(eligible, index):
                candidate_text = TmallPrescriptionOCR._clean_diagnosis(candidate.get("text"))
                if candidate_text and not DIAGNOSIS_STOP_RE.search(candidate_text):
                    diagnosis = candidate_text
                    diagnosis_scores.append(float(candidate.get("score") or 0.0))
                    break

    diagnosis_confidence = min(diagnosis_scores) if diagnosis_scores else 0.0
    age_confidence = min(age_scores) if age_scores else 0.0
    return {
        "disease_name": diagnosis,
        "disease_code": "",
        "diagnosis_ocr_status": "success" if diagnosis else "not_found",
        "diagnosis_ocr_confidence": round(diagnosis_confidence, 6),
        "age": age,
        "age_ocr_status": "success" if age else "not_found",
        "age_ocr_confidence": round(age_confidence, 6),
        "header_ocr_lines_checked": len(lines),
    }


class TmallPrescriptionOCR:
    """Run Tmall diagnosis and usage OCR with one shared model instance."""

    def __init__(
        self,
        *,
        min_confidence: float = 0.75,
        cpu_threads: int = 3,
        diagnosis_crop_top: float = 0.12,
        diagnosis_crop_bottom: float = 0.335,
        single_usage_crop_top: float = 0.35,
        single_usage_crop_bottom: float = 0.62,
        multi_usage_crop_top: float = 0.35,
        multi_usage_crop_bottom: float = 0.70,
    ) -> None:
        self.min_confidence = float(min_confidence)
        self.diagnosis_crop_top = float(diagnosis_crop_top)
        self.diagnosis_crop_bottom = float(diagnosis_crop_bottom)
        self.engine = PaddleUsageOCR(
            min_confidence=min_confidence,
            crop_top=single_usage_crop_top,
            crop_bottom=multi_usage_crop_bottom,
            single_crop_top=single_usage_crop_top,
            single_crop_bottom=single_usage_crop_bottom,
            multi_crop_top=multi_usage_crop_top,
            multi_crop_bottom=multi_usage_crop_bottom,
            usage_preprocessing_mode="original",
            usage_grayscale_threshold=None,
            cpu_threads=cpu_threads,
        )

    @staticmethod
    def _clean_diagnosis(value: Any) -> str:
        text = str(value or "").strip()
        text = re.sub(r"^[\s:：,，;；]+", "", text)
        text = re.sub(r"[\s,，;；。]+$", "", text)
        return text

    def recognize_diagnosis(self, image_path: Path) -> Dict[str, Any]:
        import cv2

        path = Path(image_path)
        image = cv2.imread(str(path))
        if image is None:
            raise RuntimeError("cannot read Tmall prescription image")
        height = image.shape[0]
        y1 = int(height * self.diagnosis_crop_top)
        y2 = int(height * self.diagnosis_crop_bottom)
        crop = image[y1:y2, :]
        if crop.size == 0:
            raise RuntimeError("Tmall diagnosis crop is empty")
        crop = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        lines = self.engine._predict(crop)
        result = parse_tmall_header_lines(lines, self.min_confidence)
        result.update({
            "diagnosis_ocr_lines_checked": len(lines),
            "diagnosis_crop_top_ratio": self.diagnosis_crop_top,
            "diagnosis_crop_bottom_ratio": self.diagnosis_crop_bottom,
            "preprocessing_mode": "original",
        })
        return result

    def enrich_usage(
        self,
        drug_infos: Sequence[Dict[str, Any]],
        image_path: Path,
    ) -> Tuple[list[Dict[str, Any]], Dict[str, Any]]:
        return self.engine.enrich(drug_infos, Path(image_path))

    def timing_metrics(self, image_count: int = 0) -> Dict[str, Any]:
        return self.engine.timing_metrics(image_count)

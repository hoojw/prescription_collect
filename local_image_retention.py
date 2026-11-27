"""Conservative cleanup of local prescription images from completed runs."""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any

from prescription_delivery import review_reasons_for_record
from run_directory_layout import iter_run_dirs


@dataclass
class ImageCleanupResult:
    deleted: int = 0
    retained: int = 0
    errors: int = 0


def _record_image_path(
    run_dir: pathlib.Path, image_dir: pathlib.Path, record: dict[str, Any]
) -> pathlib.Path | None:
    raw = str(record.get("image_file") or "").strip()
    if not raw:
        return None
    candidate = (run_dir / raw).resolve()
    image_root = image_dir.resolve()
    if candidate == image_root or image_root not in candidate.parents:
        return None
    return candidate


def cleanup_local_images(
    runs_dir: pathlib.Path, image_retention_runs: int = 0
) -> ImageCleanupResult:
    """Remove only safely-deliverable images outside the newest retained runs."""
    result = ImageCleanupResult()
    if image_retention_runs <= 0 or not runs_dir.is_dir():
        return result

    for run_dir in iter_run_dirs(runs_dir)[image_retention_runs:]:
        image_dir = run_dir / "images"
        records_path = run_dir / "prescriptions.jsonl"
        if not image_dir.is_dir():
            continue
        lines = (
            records_path.read_text(encoding="utf-8").splitlines()
            if records_path.is_file()
            else []
        )
        removable: set[pathlib.Path] = set()
        protected: set[pathlib.Path] = set()
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            image_path = _record_image_path(run_dir, image_dir, record)
            if image_path is None:
                continue
            if record.get("cos_status") == "success" and not review_reasons_for_record(record):
                removable.add(image_path)
            else:
                protected.add(image_path)

        for image_path in sorted(removable - protected):
            try:
                if image_path.is_file():
                    image_path.unlink()
                    result.deleted += 1
            except OSError:
                result.errors += 1
        result.retained += sum(1 for path in protected if path.is_file())
    return result

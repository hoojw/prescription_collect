"""Helpers for the date-grouped prescription run directory layout.

New runs live at ``runs/YYYYMMDD/YYYYMMDD_HHMMSS``.  The readers deliberately
also accept the historical flat ``runs/<run_id>`` layout so retention can be
enabled without migrating existing evidence files.
"""

from __future__ import annotations

import pathlib
import re


DATE_DIRECTORY_RE = re.compile(r"^\d{8}$")
RUN_DIRECTORY_RE = re.compile(r"^\d{8}_\d{6}$")


def iter_run_dirs(runs_dir: pathlib.Path) -> list[pathlib.Path]:
    """Return real run directories from both current and historical layouts."""
    if not runs_dir.is_dir():
        return []

    result: list[pathlib.Path] = []
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        if DATE_DIRECTORY_RE.fullmatch(child.name):
            result.extend(
                grandchild
                for grandchild in child.iterdir()
                if grandchild.is_dir() and RUN_DIRECTORY_RE.fullmatch(grandchild.name)
            )
        else:
            # Treat non-date direct children as legacy run directories.  This
            # keeps existing retention behaviour and supports older batches.
            result.append(child)
    return sorted(
        result,
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )


def remove_empty_date_dirs(runs_dir: pathlib.Path) -> None:
    """Remove empty date grouping directories left after run retention."""
    if not runs_dir.is_dir():
        return
    for child in runs_dir.iterdir():
        if not child.is_dir() or not DATE_DIRECTORY_RE.fullmatch(child.name):
            continue
        try:
            child.rmdir()
        except OSError:
            pass

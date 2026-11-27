#!/usr/bin/env python3
"""Stable, low-noise progress messages shared by platform collectors."""

from __future__ import annotations

from collections.abc import Callable


def report_store_progress(
    logger: Callable[[str], None],
    *,
    store: str,
    processed: int,
    total: int,
    saved: int,
    review: int,
    errors: int,
    force: bool = False,
    interval: int = 10,
) -> bool:
    """Emit progress initially, every interval, and when explicitly forced."""
    safe_total = max(0, int(total))
    safe_processed = max(0, min(int(processed), safe_total))
    if not force and safe_processed != 0 and safe_processed % max(1, interval) != 0:
        return False
    percent = 100.0 if safe_total == 0 and force else (
        (safe_processed / safe_total * 100.0) if safe_total else 0.0
    )
    logger(
        f"progress store={store} processed={safe_processed}/{safe_total} "
        f"percent={percent:.1f} saved={max(0, int(saved))} "
        f"review={max(0, int(review))} errors={max(0, int(errors))}"
    )
    return True


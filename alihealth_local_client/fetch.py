"""Fetch Alihealth data from Qimen and apply the local Tmall product index."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from .client import QimenAlihealthClient, QimenClientError, service_data
from .config import ConfigurationError, Settings, load_settings
from .product_index import (
    MATCH_INDEX_UNAVAILABLE,
    ProductIndexError,
    TmallProductIndex,
    apply_product_index,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class FetchSummary:
    run_id: str
    date_range: str
    hospital_id: str
    pages_scanned: int = 0
    rx_ids: int = 0
    duplicate_rx_ids: int = 0
    details_ok: int = 0
    detail_errors: int = 0
    catalog_success: int = 0
    catalog_needs_review: int = 0
    search_elapsed_ms: int = 0
    detail_elapsed_ms: int = 0
    run_elapsed_seconds: float = 0.0
    truncated_by_max_pages: bool = False
    status: str = "running"
    error: str = ""
    search_pages_file: str = "search_pages.jsonl"
    details_file: str = "details.jsonl"
    detail_errors_file: str = "detail_errors.jsonl"
    catalog_processed_file: str = "catalog_processed.jsonl"
    need_review_records: int = 0
    need_review_file: str = "need_review.json"


def parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid date: {value}; expected YYYY-MM-DD") from exc


def resolve_date_range(
    settings: Settings,
    start_text: str | None,
    end_text: str | None,
) -> tuple[dt.date, dt.date]:
    if bool(start_text) != bool(end_text):
        raise ValueError("--start and --end must be provided together")
    if start_text and end_text:
        start_date = parse_date(start_text)
        end_date = parse_date(end_text)
    else:
        end_date = dt.datetime.now(CHINA_TZ).date()
        start_date = end_date - dt.timedelta(days=settings.lookback_days - 1)
    if end_date < start_date:
        raise ValueError("end date must be on or after start date")
    span_days = (end_date - start_date).days + 1
    if span_days > settings.max_search_span_days:
        raise ValueError(
            f"date range exceeds configured {settings.max_search_span_days}-day maximum"
        )
    return start_date, end_date


def date_range_milliseconds(start_date: dt.date, end_date: dt.date) -> tuple[int, int]:
    start = dt.datetime.combine(start_date, dt.time.min, tzinfo=CHINA_TZ)
    exclusive_end = dt.datetime.combine(
        end_date + dt.timedelta(days=1),
        dt.time.min,
        tzinfo=CHINA_TZ,
    )
    return int(start.timestamp() * 1000), int(exclusive_end.timestamp() * 1000) - 1


def _open_private_jsonl(path: pathlib.Path) -> TextIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")


def _write_line(handle: TextIO, value: dict[str, Any]) -> None:
    handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    handle.flush()


def _write_summary(path: pathlib.Path, summary: FetchSummary) -> None:
    temporary = path.with_suffix(".json.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(asdict(summary), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def _write_private_json(path: pathlib.Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def _rx_numbers(data: dict[str, Any]) -> list[str]:
    values = data.get("rx_nos") or []
    if isinstance(values, dict):
        values = values.get("string") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise QimenClientError("invalid-top-response", "rx_nos must be a list")
    return [str(value).strip().strip('"') for value in values if str(value).strip().strip('"')]


def fetch(
    settings: Settings,
    *,
    start_date: dt.date,
    end_date: dt.date,
    limit: int = 0,
    client: QimenAlihealthClient | None = None,
    product_index: TmallProductIndex | None = None,
) -> tuple[pathlib.Path, FetchSummary]:
    if limit < 0:
        raise ValueError("limit must be >= 0")
    qimen = client or QimenAlihealthClient(settings)
    catalog = product_index or TmallProductIndex.load(settings.product_index_path)
    if catalog.load_status == MATCH_INDEX_UNAVAILABLE:
        raise ProductIndexError(
            f"product index is unavailable or invalid: {settings.product_index_path}"
        )
    run_id = dt.datetime.now(CHINA_TZ).strftime("%Y%m%d_%H%M%S_%f")
    run_dir = settings.output_dir / "runs" / run_id
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        os.chmod(run_dir, 0o700)
    except OSError:
        pass
    summary = FetchSummary(
        run_id=run_id,
        date_range=f"{start_date.isoformat()}..{end_date.isoformat()}",
        hospital_id=settings.hospital_id,
    )
    summary_path = run_dir / "summary.json"
    started = time.monotonic()
    start_ms, end_ms = date_range_milliseconds(start_date, end_date)
    rx_numbers: list[str] = []
    seen: set[str] = set()
    need_review_records: list[dict[str, Any]] = []

    try:
        with (
            _open_private_jsonl(run_dir / summary.search_pages_file) as search_file,
            _open_private_jsonl(run_dir / summary.details_file) as detail_file,
            _open_private_jsonl(run_dir / summary.detail_errors_file) as error_file,
            _open_private_jsonl(
                run_dir / summary.catalog_processed_file
            ) as catalog_file,
        ):
            for page_no in range(1, settings.max_pages + 1):
                result = qimen.search(
                    {
                        "hospital_id": settings.hospital_id,
                        "start_time": start_ms,
                        "end_time": end_ms,
                        "page_no": page_no,
                        "page_size": settings.page_size,
                    }
                )
                data = service_data(result.payload, "search")
                page_rx_numbers = _rx_numbers(data)
                _write_line(
                    search_file,
                    {
                        "page_no": page_no,
                        "elapsed_ms": result.elapsed_ms,
                        "response": result.payload,
                    },
                )
                summary.pages_scanned += 1
                summary.search_elapsed_ms += result.elapsed_ms
                for rx_no in page_rx_numbers:
                    if rx_no in seen:
                        summary.duplicate_rx_ids += 1
                        continue
                    seen.add(rx_no)
                    rx_numbers.append(rx_no)
                    if limit and len(rx_numbers) >= limit:
                        break
                total_value = data.get("total")
                try:
                    total = int(total_value) if total_value is not None else None
                except (TypeError, ValueError):
                    total = None
                print(
                    f"SEARCH_PAGE page={page_no} received={len(page_rx_numbers)} "
                    f"unique={len(rx_numbers)} total={total if total is not None else 'unknown'}",
                    flush=True,
                )
                if limit and len(rx_numbers) >= limit:
                    break
                if not page_rx_numbers:
                    break
                if total is not None and page_no * settings.page_size >= total:
                    break
                if len(page_rx_numbers) < settings.page_size:
                    break
            else:
                summary.truncated_by_max_pages = True

            summary.rx_ids = len(rx_numbers)
            for index, rx_no in enumerate(rx_numbers, start=1):
                try:
                    result = qimen.detail(
                        {"hospital_id": settings.hospital_id, "rx_no": rx_no}
                    )
                    detail_data = service_data(result.payload, "detail")
                    _write_line(
                        detail_file,
                        {
                            "hospital_id": settings.hospital_id,
                            "rx_no": rx_no,
                            "elapsed_ms": result.elapsed_ms,
                            "response": result.payload,
                        },
                    )
                    summary.details_ok += 1
                    summary.detail_elapsed_ms += result.elapsed_ms
                    catalog_result = apply_product_index(detail_data, catalog)
                    catalog_record = {
                        "hospital_id": settings.hospital_id,
                        "rx_no": rx_no,
                        "detail": detail_data,
                        "catalog": catalog_result,
                    }
                    if catalog_result["status"] == "success":
                        summary.catalog_success += 1
                        _write_line(catalog_file, catalog_record)
                    else:
                        summary.catalog_needs_review += 1
                        need_review_records.append(catalog_record)
                except QimenClientError as exc:
                    summary.detail_errors += 1
                    _write_line(
                        error_file,
                        {
                            "hospital_id": settings.hospital_id,
                            "rx_no": rx_no,
                            "error_code": exc.code,
                            "error": str(exc),
                            "retryable": exc.retryable,
                        },
                    )
                if index == 1 or index % 10 == 0 or index == len(rx_numbers):
                    print(
                        f"DETAIL_PROGRESS completed={index} total={len(rx_numbers)} "
                        f"ok={summary.details_ok} errors={summary.detail_errors} "
                        f"catalog_ok={summary.catalog_success} "
                        f"catalog_review={summary.catalog_needs_review}",
                        flush=True,
                    )

        summary.status = (
            "needs_review"
            if (
                summary.detail_errors
                or summary.catalog_needs_review
                or summary.truncated_by_max_pages
            )
            else "success"
        )
    except Exception as exc:
        summary.status = "failed"
        summary.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        summary.need_review_records = len(need_review_records)
        _write_private_json(run_dir / summary.need_review_file, need_review_records)
        summary.run_elapsed_seconds = round(time.monotonic() - started, 3)
        _write_summary(summary_path, summary)
        print(f"SUMMARY_PATH={summary_path}", flush=True)
    return run_dir, summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch raw Alihealth prescriptions through Qimen")
    parser.add_argument("--config", required=True)
    parser.add_argument("--start", help="inclusive start date YYYY-MM-DD")
    parser.add_argument("--end", help="inclusive end date YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=0, help="maximum detail records, 0 is unlimited")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        settings = load_settings(args.config)
        start_date, end_date = resolve_date_range(settings, args.start, args.end)
        _, summary = fetch(
            settings,
            start_date=start_date,
            end_date=end_date,
            limit=args.limit,
        )
    except (
        ConfigurationError,
        ProductIndexError,
        QimenClientError,
        ValueError,
        OSError,
    ) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"completed status={summary.status} pages={summary.pages_scanned} "
        f"rx_ids={summary.rx_ids} details_ok={summary.details_ok} "
        f"detail_errors={summary.detail_errors} "
        f"catalog_success={summary.catalog_success} "
        f"catalog_review={summary.catalog_needs_review} "
        f"elapsed_seconds={summary.run_elapsed_seconds}",
        flush=True,
    )
    return 2 if summary.status == "needs_review" else 0


if __name__ == "__main__":
    raise SystemExit(main())

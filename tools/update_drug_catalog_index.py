#!/usr/bin/env python3
"""Merge normalized maintenance rows into the business drug catalog index."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from drug_catalog import CATALOG_SCHEMA_VERSION, catalog_entry_from_row


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) for row in rows
    ):
        raise ValueError("maintenance rows JSON must be a list or {rows: [...]}")
    return rows


def merge_index(
    base_payload: dict[str, Any],
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, int]]:
    if base_payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError("base catalog schema_version is unsupported")
    base_entries = base_payload.get("entries")
    if not isinstance(base_entries, list):
        raise ValueError("base catalog entries must be a list")

    updates = [catalog_entry_from_row(row) for row in rows]
    if any(not str(entry.get("id") or "").strip() for entry in updates):
        raise ValueError("every maintenance row must contain a drug ID")
    update_by_id = {str(entry["id"]): entry for entry in updates}
    if len(update_by_id) != len(updates):
        raise ValueError("maintenance rows contain duplicate drug IDs")

    retained = [
        entry
        for entry in base_entries
        if isinstance(entry, dict)
        and str(entry.get("id") or "") not in update_by_id
    ]
    replaced = len(base_entries) - len(retained)
    merged_entries = retained + [
        update_by_id[key] for key in sorted(update_by_id)
    ]
    output = dict(base_payload)
    output.update(
        {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "updated_at": dt.datetime.now().astimezone().isoformat(),
            "entry_count": len(merged_entries),
            "entries": merged_entries,
        }
    )
    return output, {
        "base_entries": len(base_entries),
        "maintenance_rows": len(rows),
        "replaced": replaced,
        "added": len(updates) - replaced,
        "output_entries": len(merged_entries),
    }


def atomic_write(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge maintenance rows JSON into drug_catalog_index.json"
    )
    parser.add_argument("--base-index", required=True, type=pathlib.Path)
    parser.add_argument("--rows-json", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    base = load_json(args.base_index)
    if not isinstance(base, dict):
        raise ValueError("base catalog index must be a JSON object")
    output, summary = merge_index(base, load_rows(args.rows_json))
    atomic_write(args.output, output)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Inspect JD prescription response structure without printing patient values."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


PROJECT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import server_jd_prescription_collect as jd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=pathlib.Path, required=True)
    args = parser.parse_args()

    config = jd.load_config(args.config)
    with jd.CdpPage(int(config["chrome_port"]), jd.HOME_URL) as tab:
        api = jd.JdPrescriptionApi(
            jd.JdBrowserTransport(
                tab,
                int(config.get("request_retries") or 3),
            )
        )
        page = api.list_page(
            "",
            "",
            1,
            1,
            str(config.get("rx_status_type") or "2"),
        )
        rx_ids = page.get("rx_ids") or []
        if not rx_ids:
            raise RuntimeError("no detail sample available")
        vo = api.detail(str(rx_ids[0])).raw

    ext = vo.get("rxExtendsInfo") or {}
    drugs = vo.get("rxSkuList") or []
    if isinstance(drugs, dict):
        drugs = [drugs]
    selection = jd.select_delivery_prescription(
        ext,
        str(config.get("internal_hospital_name") or ""),
    )

    indexed_fields = {}
    for field in (
        "rxNo",
        "hospitalName",
        "doctorName",
        "pharmacistName",
        "rxPicUrlList",
    ):
        values = jd.split_indexed_values(ext.get(field), preserve_empty=True)
        indexed_fields[field] = {
            "count": len(values),
            "empty_indexes": [i for i, value in enumerate(values) if not value],
        }

    output = {
        "list_total_pages": page.get("total_pages"),
        "detail_top_level_fields": sorted(vo.keys()),
        "detail_extend_fields": sorted(ext.keys()),
        "drug_count": len(drugs),
        "drug_fields": (
            sorted(drugs[0].keys())
            if drugs and isinstance(drugs[0], dict)
            else []
        ),
        "indexed_fields": indexed_fields,
        "selection": {
            "status": selection.get("status"),
            "reason": selection.get("reason"),
            "prescription_count": selection.get("prescription_count"),
            "selected_index": selection.get("index"),
            "image_source": selection.get("image_source"),
        },
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

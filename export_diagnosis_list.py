#!/usr/bin/env python3
"""Export unique diagnosis names/codes from uploaded prescription JSON files."""

from __future__ import annotations

import argparse
import glob
import json
import pathlib
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse
from xml.sax.saxutils import escape


PRODUCTION_API_HOST = "api.yizhujk.com"
DEFAULT_CONFIG_GLOB = "/etc/prescription-collector/stores/*.json"
DEFAULT_OUTPUT = "diagnosis_list.xlsx"


@dataclass(frozen=True)
class UploadJsonSource:
    path: pathlib.Path
    store_key: str = ""
    run_id: str = ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _store_key_from_output_dir(output_dir: pathlib.Path) -> str:
    return output_dir.name


def _run_id_from_run_dir(run_dir: pathlib.Path) -> str:
    return run_dir.name


def _is_production_api(config: dict[str, Any], production_host: str) -> bool:
    api_url = _text(config.get("api_url"))
    host = urlparse(api_url).hostname or ""
    return bool(config.get("api_submit_enabled", False)) and host == production_host


def discover_sources_from_configs(
    config_glob: str,
    production_host: str = PRODUCTION_API_HOST,
    require_success: bool = True,
) -> list[UploadJsonSource]:
    sources: list[UploadJsonSource] = []
    for config_name in sorted(glob.glob(config_glob)):
        config_path = pathlib.Path(config_name)
        config = _load_json(config_path)
        if not isinstance(config, dict) or not _is_production_api(config, production_host):
            continue
        output_dir_text = _text(config.get("output_dir"))
        if not output_dir_text:
            continue
        output_dir = pathlib.Path(output_dir_text)
        runs_dir = output_dir / "runs"
        if not runs_dir.is_dir():
            continue
        store_key = _store_key_from_output_dir(output_dir)
        for summary_path in sorted(runs_dir.glob("*/summary.json")):
            run_dir = summary_path.parent
            summary = _load_json(summary_path)
            if require_success and _text(summary.get("api_status")) != "success":
                continue
            upload_file = _text(summary.get("upload_json_file"))
            upload_path = (
                output_dir / pathlib.PurePosixPath(upload_file)
                if upload_file
                else run_dir / "prescription_upload.json"
            )
            if upload_path.is_file():
                sources.append(
                    UploadJsonSource(
                        path=upload_path,
                        store_key=store_key,
                        run_id=_run_id_from_run_dir(run_dir),
                    )
                )
    return sources


def discover_sources_from_root(
    root: pathlib.Path,
    require_success: bool = True,
) -> list[UploadJsonSource]:
    sources: list[UploadJsonSource] = []
    for summary_path in sorted(root.glob("*/runs/*/summary.json")):
        output_dir = summary_path.parents[2]
        run_dir = summary_path.parent
        summary = _load_json(summary_path)
        if require_success and _text(summary.get("api_status")) != "success":
            continue
        upload_file = _text(summary.get("upload_json_file"))
        upload_path = (
            output_dir / pathlib.PurePosixPath(upload_file)
            if upload_file
            else run_dir / "prescription_upload.json"
        )
        if upload_path.is_file():
            sources.append(
                UploadJsonSource(
                    path=upload_path,
                    store_key=_store_key_from_output_dir(output_dir),
                    run_id=_run_id_from_run_dir(run_dir),
                )
            )
    return sources


def collect_diagnoses(sources: Iterable[UploadJsonSource]) -> list[tuple[str, str]]:
    diagnoses: set[tuple[str, str]] = set()
    for source in sources:
        payload = _load_json(source.path)
        if not isinstance(payload, list):
            continue
        for hospital_entry in payload:
            if not isinstance(hospital_entry, dict):
                continue
            data_list = hospital_entry.get("data_list") or []
            if not isinstance(data_list, list):
                continue
            for prescription in data_list:
                if not isinstance(prescription, dict):
                    continue
                diagnosis = prescription.get("diagnosis") or {}
                if not isinstance(diagnosis, dict):
                    continue
                name = _text(diagnosis.get("disease_name"))
                code = _text(diagnosis.get("disease_code"))
                if name or code:
                    diagnoses.add((name, code))
    return sorted(diagnoses, key=lambda item: (item[0], item[1]))


def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _sheet_xml(rows: list[list[str]]) -> str:
    sheet_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for col_index, value in enumerate(row):
            ref = f"{_column_name(col_index)}{row_index}"
            cells.append(
                f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            )
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    dimension = f"A1:B{max(1, len(rows))}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="{dimension}"/>'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        '<cols><col min="1" max="1" width="32" customWidth="1"/>'
        '<col min="2" max="2" width="20" customWidth="1"/></cols>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        '</worksheet>'
    )


def write_xlsx(path: pathlib.Path, diagnoses: list[tuple[str, str]]) -> None:
    rows = [["disease_name", "disease_code"]]
    rows.extend([[name, code] for name, code in diagnoses])
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="diagnosis" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rows))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export unique diagnosis name/code pairs from uploaded prescription_upload.json files."
    )
    parser.add_argument(
        "--config-glob",
        default=DEFAULT_CONFIG_GLOB,
        help=f"Store config glob used to discover production uploads. Default: {DEFAULT_CONFIG_GLOB}",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        help="Alternative stores root, for example /var/lib/prescription-collector/stores.",
    )
    parser.add_argument(
        "--input",
        action="append",
        type=pathlib.Path,
        default=[],
        help="Specific prescription_upload.json file. Can be repeated.",
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=pathlib.Path(DEFAULT_OUTPUT),
        help=f"Output .xlsx path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--production-host",
        default=PRODUCTION_API_HOST,
        help=f"Host marker for production API configs. Default: {PRODUCTION_API_HOST}",
    )
    parser.add_argument(
        "--include-non-success",
        action="store_true",
        help="Include JSON files whose summary api_status is not success.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_success = not args.include_non_success
    sources = [UploadJsonSource(path=path) for path in args.input]
    if args.root:
        sources.extend(discover_sources_from_root(args.root, require_success=require_success))
    if not args.input and not args.root:
        sources.extend(
            discover_sources_from_configs(
                args.config_glob,
                production_host=args.production_host,
                require_success=require_success,
            )
        )
    sources = [source for source in sources if source.path.is_file()]
    diagnoses = collect_diagnoses(sources)
    write_xlsx(args.output, diagnoses)
    print(
        f"wrote {len(diagnoses)} unique diagnoses from {len(sources)} upload JSON files to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

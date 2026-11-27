#!/usr/bin/env python3
"""Maintain diagnosis-name references and report unmaintained upload diagnoses."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


DEFAULT_DB_PATH = pathlib.Path(
    "/var/lib/prescription-collector/diagnosis_maintenance.sqlite3"
)
DEFAULT_OUTPUT_DIR = pathlib.Path("/var/lib/prescription-collector/diagnos")
DIAGNOSIS_NAME_HEADERS = {
    "diagnosis",
    "diagnosis_name",
    "disease",
    "disease_name",
    "病名",
    "疾病名称",
    "诊断",
    "诊断名称",
}
XLSX_NS = {
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


@dataclass(frozen=True)
class DiagnosisItem:
    disease_name: str
    disease_code: str = ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_diagnosis_name(value: Any) -> str:
    return " ".join(_text(value).split())


def split_diagnosis_name(value: Any) -> list[str]:
    """Split only on English commas. Chinese commas remain part of a diagnosis."""
    names: list[str] = []
    seen: set[str] = set()
    for part in _text(value).split(","):
        name = normalize_diagnosis_name(part)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def ensure_database(db_path: pathlib.Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS maintained_diagnosis (
                diagnosis_name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def load_maintained_names(db_path: pathlib.Path) -> set[str]:
    if not db_path.is_file():
        return set()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT diagnosis_name FROM maintained_diagnosis"
        ).fetchall()
    finally:
        conn.close()
    return {normalize_diagnosis_name(row[0]) for row in rows if normalize_diagnosis_name(row[0])}


def replace_maintained_names(
    db_path: pathlib.Path,
    names: Iterable[str],
    source_file: str = "",
) -> int:
    ensure_database(db_path)
    normalized = sorted(
        {normalize_diagnosis_name(name) for name in names if normalize_diagnosis_name(name)}
    )
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM maintained_diagnosis")
        conn.executemany(
            """
            INSERT INTO maintained_diagnosis (diagnosis_name, created_at)
            VALUES (?, ?)
            """,
            [(name, now) for name in normalized],
        )
        conn.execute(
            """
            INSERT INTO metadata (key, value)
            VALUES ('source_file', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (source_file,),
        )
        conn.execute(
            """
            INSERT INTO metadata (key, value)
            VALUES ('updated_at', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()
    return len(normalized)


def append_maintained_names(
    db_path: pathlib.Path,
    names: Iterable[str],
    source_file: str = "",
) -> int:
    ensure_database(db_path)
    normalized = sorted(
        {normalize_diagnosis_name(name) for name in names if normalize_diagnosis_name(name)}
    )
    if not normalized:
        return 0
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO maintained_diagnosis (diagnosis_name, created_at)
            VALUES (?, ?)
            """,
            [(name, now) for name in normalized],
        )
        added = conn.total_changes - before
        conn.execute(
            """
            INSERT INTO metadata (key, value)
            VALUES ('last_append_source_file', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (source_file,),
        )
        conn.execute(
            """
            INSERT INTO metadata (key, value)
            VALUES ('last_append_at', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()
    return added


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join((text.text or "") for text in item.findall(".//x:t", XLSX_NS))
        for item in root.findall("x:si", XLSX_NS)
    ]


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    first_sheet = workbook.find(".//x:sheet", XLSX_NS)
    if first_sheet is None:
        raise ValueError("workbook has no sheets")
    rel_id = first_sheet.attrib.get(
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    )
    if not rel_id:
        return "xl/worksheets/sheet1.xml"
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("rel:Relationship", XLSX_NS):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target") or "worksheets/sheet1.xml"
            return "xl/" + target.lstrip("/")
    return "xl/worksheets/sheet1.xml"


def read_xlsx_rows(path: pathlib.Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as archive:
        shared = _xlsx_shared_strings(archive)
        sheet_path = _first_sheet_path(archive)
        root = ET.fromstring(archive.read(sheet_path))
    rows: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", XLSX_NS):
        values: list[str] = []
        for cell in row.findall("x:c", XLSX_NS):
            cell_type = cell.attrib.get("t")
            value = cell.find("x:v", XLSX_NS)
            inline = cell.find("x:is/x:t", XLSX_NS)
            if cell_type == "s" and value is not None:
                values.append(shared[int(value.text or "0")])
            elif cell_type == "inlineStr" and inline is not None:
                values.append(inline.text or "")
            elif value is not None:
                values.append(value.text or "")
            else:
                values.append("")
        rows.append(values)
    return rows


def extract_names_from_xlsx(path: pathlib.Path, column_name: str = "") -> list[str]:
    rows = read_xlsx_rows(path)
    rows = [row for row in rows if any(_text(cell) for cell in row)]
    if not rows:
        return []
    header = [normalize_diagnosis_name(cell) for cell in rows[0]]
    target = normalize_diagnosis_name(column_name)
    column_index = 0
    if target:
        if target not in header:
            raise ValueError(f"column not found: {column_name}")
        column_index = header.index(target)
        data_rows = rows[1:]
    else:
        matched = [
            index
            for index, name in enumerate(header)
            if name.lower() in DIAGNOSIS_NAME_HEADERS or name in DIAGNOSIS_NAME_HEADERS
        ]
        if matched:
            column_index = matched[0]
            data_rows = rows[1:]
        else:
            column_index = 0
            data_rows = rows
    names: list[str] = []
    for row in data_rows:
        if column_index < len(row):
            name = normalize_diagnosis_name(row[column_index])
            if name:
                names.append(name)
    return names


def diagnoses_from_payload(payload: Any) -> list[DiagnosisItem]:
    items: list[DiagnosisItem] = []
    if not isinstance(payload, list):
        return items
    seen: set[tuple[str, str]] = set()
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
            disease_code = _text(diagnosis.get("disease_code"))
            for name in split_diagnosis_name(diagnosis.get("disease_name")):
                key = (name, disease_code)
                if key not in seen:
                    seen.add(key)
                    items.append(DiagnosisItem(name, disease_code))
    return items


def unmaintained_diagnoses(
    payload: Any,
    maintained_names: set[str],
) -> list[DiagnosisItem]:
    maintained = {normalize_diagnosis_name(name) for name in maintained_names}
    items = [
        item
        for item in diagnoses_from_payload(payload)
        if normalize_diagnosis_name(item.disease_name) not in maintained
    ]
    return sorted(items, key=lambda item: (item.disease_name, item.disease_code))


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
    last_col = _column_name(max(0, len(rows[0]) - 1))
    dimension = f"A1:{last_col}{max(1, len(rows))}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="{dimension}"/>'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        '<cols><col min="1" max="1" width="36" customWidth="1"/>'
        '<col min="2" max="2" width="20" customWidth="1"/></cols>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        '</worksheet>'
    )


def write_unmaintained_xlsx(
    path: pathlib.Path,
    diagnoses: Iterable[DiagnosisItem],
) -> None:
    rows = [["disease_name", "disease_code"]]
    rows.extend([[item.disease_name, item.disease_code] for item in diagnoses])
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
            "</Types>",
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
            '<sheets><sheet name="unmaintained" sheetId="1" r:id="rId1"/></sheets>'
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


def write_unmaintained_report(
    upload_json_path: pathlib.Path,
    output_dir: pathlib.Path = DEFAULT_OUTPUT_DIR,
    db_path: pathlib.Path = DEFAULT_DB_PATH,
    store_key: str = "",
    run_id: str = "",
) -> tuple[pathlib.Path, int, int]:
    if not db_path.is_file():
        raise FileNotFoundError(f"diagnosis maintenance database not found: {db_path}")
    payload = json.loads(upload_json_path.read_text(encoding="utf-8"))
    maintained_names = load_maintained_names(db_path)
    report = unmaintained_diagnoses(payload, maintained_names)
    stamp = run_id or dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    output_name = f"{stamp}_unmaintained_diagnosis.xlsx"
    output_path = output_dir / output_name
    write_unmaintained_xlsx(output_path, report)
    added = append_maintained_names(
        db_path,
        [item.disease_name for item in report],
        str(output_path),
    )
    return output_path, len(report), added


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import maintained diagnosis names and report unmaintained upload diagnoses."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-xlsx")
    import_parser.add_argument("xlsx", type=pathlib.Path)
    import_parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB_PATH)
    import_parser.add_argument("--column", default="")

    append_parser = subparsers.add_parser("append-xlsx")
    append_parser.add_argument("xlsx", type=pathlib.Path)
    append_parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB_PATH)
    append_parser.add_argument("--column", default="")

    check_parser = subparsers.add_parser("check-upload")
    check_parser.add_argument("upload_json", type=pathlib.Path)
    check_parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB_PATH)
    check_parser.add_argument("--output-dir", type=pathlib.Path, default=DEFAULT_OUTPUT_DIR)
    check_parser.add_argument("--store-key", default="")
    check_parser.add_argument("--run-id", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "import-xlsx":
        names = extract_names_from_xlsx(args.xlsx, args.column)
        count = replace_maintained_names(args.db, names, str(args.xlsx))
        print(f"imported {count} maintained diagnosis names into {args.db}")
        return 0
    if args.command == "append-xlsx":
        names = extract_names_from_xlsx(args.xlsx, args.column)
        count = append_maintained_names(args.db, names, str(args.xlsx))
        print(f"appended {count} maintained diagnosis names into {args.db}")
        return 0
    if args.command == "check-upload":
        output_path, count, added = write_unmaintained_report(
            args.upload_json,
            output_dir=args.output_dir,
            db_path=args.db,
            store_key=args.store_key,
            run_id=args.run_id,
        )
        print(
            f"wrote {count} unmaintained diagnoses to {output_path}; "
            f"appended {added} to {args.db}"
        )
        return 0
    raise ValueError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

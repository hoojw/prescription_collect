#!/usr/bin/env python3
"""Build and submit the target prescription import JSON."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import requests


SUCCESS_PROCESSING_STATUS_CODES = frozenset({100, 110, 120})
SUCCESS_DRCODE_STATUS_CODES = frozenset({1, 2, 3, 4})
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ApiSubmitResult:
    success: bool
    http_status: int
    err_code: Any
    message: str
    response: Any

    @property
    def outcome(self) -> str:
        if self.success:
            return "success"
        if 200 <= self.http_status < 300:
            return "uncertain_response"
        return "failed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "http_status": self.http_status,
            "err_code": self.err_code,
            "message": self.message,
            "response": self.response,
        }


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _diagnosis_has_name(value: Any) -> bool:
    items = value if isinstance(value, list) else [value]
    return any(
        isinstance(item, dict) and bool(_text(item.get("disease_name") or item.get("icdName")))
        for item in items
    )


def _record_has_diagnosis_name(record: dict[str, Any]) -> bool:
    patient = record.get("patient_info") or {}
    if not isinstance(patient, dict):
        patient = {}
    illness = patient.get("illnessDetail") or []
    if isinstance(illness, dict):
        illness = [illness]
    if any(
        isinstance(item, dict) and bool(_text(item.get("icdName")))
        for item in illness
    ):
        return True
    return bool(_text(record.get("icd_name")))


def medicine_review_reasons(medicine_info: Any) -> list[str]:
    """Reject medicine items containing fields the receiving API treats as empty."""
    if not isinstance(medicine_info, list) or not medicine_info:
        return ["medicine_info_missing"]
    required_fields = (
        "drug_name",
        "specification",
        "total_unit",
        "drcode",
        "usage_method",
        "usage_frequency",
        "dosage_per_time",
        "dosage_unit",
    )
    reasons: list[str] = []
    for index, medicine in enumerate(medicine_info):
        if not isinstance(medicine, dict):
            reasons.append(f"drug_{index}_medicine_invalid")
            continue
        for field in required_fields:
            if not _text(medicine.get(field)):
                reasons.append(f"drug_{index}_{field}_missing")
    return reasons


def review_reasons_for_record(record: dict[str, Any]) -> list[str]:
    """Return stable reasons that keep a prescription out of interface delivery."""
    reasons: list[str] = []
    processing = record.get("processing")
    if not isinstance(processing, list) or not processing:
        reasons.append("processing_status_missing")
    else:
        for index, item in enumerate(processing):
            if not isinstance(item, dict):
                reasons.append(f"drug_{index}_processing_invalid")
                continue
            try:
                code = int(item.get("status_code") or 0)
            except (TypeError, ValueError):
                code = 0
            if code not in SUCCESS_PROCESSING_STATUS_CODES:
                drug_index = item.get("drug_index", index)
                reasons.append(f"drug_{drug_index}_status_{code}")
            if "drcode_status" in item:
                try:
                    drcode_status = int(item.get("drcode_status") or 0)
                except (TypeError, ValueError):
                    drcode_status = 0
                if drcode_status not in SUCCESS_DRCODE_STATUS_CODES:
                    drug_index = item.get("drug_index", index)
                    reasons.append(
                        f"drug_{drug_index}_drcode_status_{drcode_status}"
                    )

    reasons.extend(medicine_review_reasons(record.get("medicine_info")))
    if not _text(record.get("pharmacist_name")):
        reasons.append("audit_missing")
    if not _record_has_diagnosis_name(record):
        reasons.append("diagnosis_missing")

    try:
        hospital_source_code = int(record.get("hospital_source_code") or 0)
    except (TypeError, ValueError):
        hospital_source_code = 0
    if hospital_source_code == 3:
        reasons.append("hospital_source_needs_review")
    return reasons


def record_needs_review(record: dict[str, Any]) -> bool:
    return bool(review_reasons_for_record(record))


def _gender(value: Any) -> str:
    normalized = _text(value).lower()
    if normalized in {"1", "男", "male", "m"}:
        return "男"
    if normalized in {"2", "女", "female", "f"}:
        return "女"
    return _text(value)


def _first_value(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return ""


def _date_from_record(record: dict[str, Any]) -> str:
    value = _text(record.get("prescription_time"))
    if not value:
        value = _text(record.get("collected_at"))
    # The upload API expects a timestamp string without the ISO timezone suffix.
    # Both collectors store prescription_time as ISO-like text, so retain the
    # complete second-level time instead of reducing it to a calendar date.
    return value.replace("T", " ")[:19]


def format_collect_time(value: Any) -> str:
    """Render an ISO timestamp as an unambiguous Shanghai wall-clock time."""
    source = _text(value)
    if not source:
        return ""
    normalized = source[:-1] + "+00:00" if source.endswith(("Z", "z")) else source
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return source.replace("T", " ")[:19]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(SHANGHAI_TZ)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _diagnosis(record: dict[str, Any], patient: dict[str, Any]) -> dict[str, str]:
    illness = patient.get("illnessDetail") or []
    if isinstance(illness, dict):
        illness = [illness]
    first = next((item for item in illness if isinstance(item, dict)), {})
    return {
        "disease_name": _text(first.get("icdName") or record.get("icd_name")),
        "disease_code": _text(first.get("icdCode")),
    }


def build_interface_record(
    record: dict[str, Any],
    hospital_name: str,
    source: str = "美团",
    default_prescription_type: str = "普通",
    default_validity: str = "该处方有效期为3天，若病情变化，请及时就医",
) -> dict[str, Any]:
    patient = record.get("patient_info") or {}
    if not isinstance(patient, dict):
        patient = {}
    cos_url = _text(record.get("image_file_url"))
    if not cos_url:
        raise ValueError(f"prescription {record.get('prescription_id') or ''} has no COS image_file_url")
    medicine = record.get("medicine_info") or []
    if not isinstance(medicine, list) or not medicine:
        raise ValueError(f"prescription {record.get('prescription_id') or ''} has no medicine_info")
    birth_date = _first_value(
        patient,
        "birth_date",
        "birthDate",
        "birthday",
        "medicalUserBirthday",
    )
    department = _first_value(patient, "department", "departmentName") or record.get("department_name")
    prescription_id = _text(record.get("prescription_id"))
    record_hospital_name = _text(record.get("hospital_name") or hospital_name)
    doctor = "" if record_hospital_name == "外部医院" else _text(record.get("physician_name"))
    diagnosis = _diagnosis(record, patient)
    audit = _text(record.get("pharmacist_name"))
    if not audit:
        raise ValueError(
            f"prescription {prescription_id} has required field missing: audit"
        )
    if not _diagnosis_has_name(diagnosis):
        raise ValueError(
            f"prescription {prescription_id} has required field missing: diagnosis"
        )
    return {
        "hospital_name": record_hospital_name,
        "prescription_number": prescription_id,
        "prescription_type": _text(record.get("prescription_type") or default_prescription_type),
        "date": _date_from_record(record),
        "patient_info": {
            "name": _text(_first_value(patient, "name", "medicalUserName")),
            "gender": _gender(_first_value(patient, "gender", "sex")),
            "age": _text(patient.get("age")),
            "birth_date": _text(birth_date),
            "fee_type": _text(_first_value(patient, "fee_type", "feeType")),
            "department": _text(department),
        },
        "diagnosis": diagnosis,
        "medicine_info": medicine,
        "total_fee": record.get("total_fee"),
        "supplementary_info": _text(record.get("disposal_opinions") or record.get("dosage")),
        "doctor": doctor,
        "audit": audit,
        "pharmacy_audit": _text(record.get("hospital_pharmacist_name")),
        "prescription_validity": _text(record.get("prescription_validity") or default_validity),
        "source": source,
        "order_view_id": _text(record.get("order_view_id")),
        "collect_time": format_collect_time(record.get("collected_at")),
        "image_file": pathlib.Path(_text(record.get("image_file")) or f"{prescription_id}.png").name,
        "image_file_url": cos_url,
    }


def build_interface_payload(
    records: Iterable[dict[str, Any]],
    hospital_name: str,
    source: str = "美团",
) -> list[dict[str, Any]]:
    data_list = [build_interface_record(record, hospital_name, source) for record in records]
    return [{"hospital": hospital_name, "data_list": data_list}]


def write_interface_json(path: pathlib.Path, payload: Any) -> None:
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(target)


def submit_json_file(
    json_path: pathlib.Path,
    api_url: str,
    platform: str,
    app_code: str,
    timeout_seconds: int = 300,
    session: Optional[Any] = None,
) -> ApiSubmitResult:
    client = session or requests
    path = pathlib.Path(json_path)
    with path.open("rb") as handle:
        response = client.post(
            api_url,
            data={"platform": platform, "appCode": app_code},
            files={"file": (path.name, handle, "application/json")},
            headers={"Accept": "*/*", "User-Agent": "prescription-collector/1.0"},
            timeout=(10, max(30, int(timeout_seconds))),
        )
    try:
        body: Any = response.json()
    except Exception:
        body = {"raw_text": response.text[:2000]}
    business_success = isinstance(body, dict) and body.get("success") is True
    message = ""
    err_code: Any = None
    if isinstance(body, dict):
        message = _text(body.get("errMsg") or body.get("message"))
        err_code = body.get("errCode")
    if not message:
        message = "success" if response.ok and business_success else f"HTTP {response.status_code}"
    return ApiSubmitResult(
        success=bool(response.ok and business_success),
        http_status=int(response.status_code),
        err_code=err_code,
        message=message,
        response=body,
    )


def response_snapshot(result: ApiSubmitResult) -> dict[str, Any]:
    return {
        "recorded_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        **result.as_dict(),
    }

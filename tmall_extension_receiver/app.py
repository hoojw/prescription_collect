"""HTTP-neutral validation for raw Tmall extension batches."""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .store import BatchConflictError, BatchStore


BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


class RequestError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class TmallBatchApplication:
    def __init__(self, settings: Settings, store: BatchStore | None = None) -> None:
        self.settings = settings
        self.store = store or BatchStore(settings.database_path, settings.output_dir)

    @staticmethod
    def _json(payload: dict[str, Any], status: int = 200) -> HttpResult:
        return HttpResult(
            status=status,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def _authorized(self, headers: dict[str, str]) -> bool:
        supplied = headers.get("authorization", "")
        expected = f"Bearer {self.settings.bearer_token}"
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _parse_batch(self, raw_body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError("invalid_json", "request body must be a UTF-8 JSON object") from exc
        if not isinstance(payload, dict):
            raise RequestError("invalid_batch", "batch root must be an object")
        if str(payload.get("platform") or "").upper() != "TIANMAO":
            raise RequestError("invalid_platform", "platform must be TIANMAO")
        batch_id = str(payload.get("batch_id") or "").strip()
        if not BATCH_ID_RE.fullmatch(batch_id):
            raise RequestError("invalid_batch_id", "batch_id format is invalid")
        if payload.get("validation_stage") != "server_after_ingest":
            raise RequestError(
                "invalid_validation_stage",
                "validation_stage must be server_after_ingest",
            )
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise RequestError("invalid_records", "records must be a non-empty array")
        if len(records) > self.settings.max_records_per_batch:
            raise RequestError("too_many_records", "records exceeds the configured maximum", 413)
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise RequestError("invalid_record", f"records[{index}] must be an object")
            if not str(record.get("prescription_id") or "").strip():
                raise RequestError(
                    "invalid_record",
                    f"records[{index}].prescription_id is required",
                )
        return payload

    def handle(
        self,
        http_method: str,
        path: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> HttpResult:
        if http_method == "OPTIONS" and path == self.settings.ingest_path:
            return self._json({"status": "ok"})
        if path == "/healthz":
            if http_method != "GET":
                return self._json({"status": "method_not_allowed"}, 405)
            return self._json({"status": "ok"})
        if path != self.settings.ingest_path:
            return self._json({"status": "not_found"}, 404)
        if http_method != "POST":
            return self._json({"status": "method_not_allowed"}, 405)
        if not self._authorized(headers):
            return self._json({"status": "rejected", "code": "unauthorized"}, 401)
        if len(raw_body) > self.settings.max_body_bytes:
            return self._json({"status": "rejected", "code": "request_too_large"}, 413)
        if "application/json" not in headers.get("content-type", "").lower():
            return self._json({"status": "rejected", "code": "unsupported_content_type"}, 415)
        try:
            payload = self._parse_batch(raw_body)
            stored = self.store.save(payload)
            return self._json(
                {
                    "status": "duplicate" if stored.duplicate else "accepted",
                    "batch_id": stored.batch_id,
                    "records": stored.record_count,
                    "queue_status": "queued",
                }
            )
        except RequestError as exc:
            return self._json({"status": "rejected", "code": exc.code}, exc.status)
        except BatchConflictError:
            return self._json({"status": "rejected", "code": "batch_id_conflict"}, 409)
        except Exception:
            return self._json({"status": "error", "code": "storage_error"}, 500)


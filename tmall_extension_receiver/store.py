"""Private and idempotent storage for raw Tmall extension batches."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


class BatchConflictError(RuntimeError):
    """The same batch id was reused with different content."""


@dataclass(frozen=True)
class StoredBatch:
    batch_id: str
    duplicate: bool
    record_count: int
    payload_sha256: str
    queue_path: pathlib.Path


class BatchStore:
    def __init__(self, database_path: pathlib.Path, output_dir: pathlib.Path) -> None:
        self.database_path = database_path
        self.output_dir = output_dir
        self.queue_dir = output_dir / "inbox"
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.queue_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in {self.database_path.parent, self.output_dir, self.queue_dir}:
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tmall_extension_batches (
                    batch_id TEXT PRIMARY KEY,
                    received_at INTEGER NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload_path TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tmall_extension_batches_status "
                "ON tmall_extension_batches(status, received_at)"
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _serialized(payload: dict[str, Any]) -> bytes:
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def save(self, payload: dict[str, Any]) -> StoredBatch:
        raw = self._serialized(payload)
        digest = hashlib.sha256(raw).hexdigest()
        batch_id = str(payload["batch_id"])
        record_count = len(payload["records"])
        queue_path = self.queue_dir / f"{batch_id}.json"
        temporary = self.queue_dir / f".{batch_id}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT payload_sha256, record_count, payload_path "
                    "FROM tmall_extension_batches WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if row is not None:
                    connection.rollback()
                    if str(row[0]) != digest:
                        raise BatchConflictError(
                            "batch_id already exists with different content"
                        )
                    return StoredBatch(
                        batch_id=batch_id,
                        duplicate=True,
                        record_count=int(row[1]),
                        payload_sha256=digest,
                        queue_path=pathlib.Path(str(row[2])),
                    )
                os.replace(temporary, queue_path)
                connection.execute(
                    """
                    INSERT INTO tmall_extension_batches (
                        batch_id, received_at, payload_sha256, record_count,
                        status, payload_path
                    ) VALUES (?, ?, ?, ?, 'queued', ?)
                    """,
                    (batch_id, int(time.time()), digest, record_count, str(queue_path)),
                )
                connection.commit()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return StoredBatch(
            batch_id=batch_id,
            duplicate=False,
            record_count=record_count,
            payload_sha256=digest,
            queue_path=queue_path,
        )


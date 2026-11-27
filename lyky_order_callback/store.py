"""Durable, idempotent storage for raw orders without logging personal fields."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StoredOrder:
    event_id: int
    duplicate: bool


class OrderStore:
    def __init__(self, database_path: pathlib.Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS original_order_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        received_at INTEGER NOT NULL,
                        app_id TEXT NOT NULL,
                        source_order_id TEXT NOT NULL,
                        out_trade_no TEXT NOT NULL,
                        store_id TEXT NOT NULL,
                        platform INTEGER,
                        payload_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        schema_warnings_json TEXT NOT NULL,
                        UNIQUE(app_id, source_order_id, payload_sha256)
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_original_order_events_trade "
                    "ON original_order_events(out_trade_no, received_at)"
                )
            finally:
                cursor.close()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            yield connection
        finally:
            connection.close()

    def save(self, app_id: str, body: dict[str, Any], warnings: list[str]) -> StoredOrder:
        payload_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        source_id = str(body["id"])
        out_trade_no = str(body["out_trade_no"])
        store_id = str(body["store_id"])
        platform_value = body.get("platform")
        try:
            platform = int(platform_value) if str(platform_value or "").strip() else None
        except (TypeError, ValueError):
            platform = None
        now = int(time.time())

        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO original_order_events (
                    received_at, app_id, source_order_id, out_trade_no, store_id,
                    platform, payload_sha256, payload_json, schema_warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    app_id,
                    source_id,
                    out_trade_no,
                    store_id,
                    platform,
                    payload_hash,
                    payload_json,
                    json.dumps(warnings, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            duplicate = cursor.rowcount == 0
            cursor.close()
            cursor = connection.execute(
                    """
                    SELECT event_id FROM original_order_events
                    WHERE app_id=? AND source_order_id=? AND payload_sha256=?
                    """,
                    (app_id, source_id, payload_hash),
                )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
        if row is None:
            raise RuntimeError("order insert did not produce a durable row")
        return StoredOrder(event_id=int(row[0]), duplicate=duplicate)

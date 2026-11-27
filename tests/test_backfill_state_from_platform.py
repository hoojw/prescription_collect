import hashlib
import pathlib
import sqlite3
import tempfile
import unittest

from backfill_state_from_platform import (
    insert_state_rows,
    prescription_key,
    select_missing_records,
)
from server_jd_prescription_collect import StateStore


class BackfillStateTests(unittest.TestCase):
    def test_prescription_key_matches_platform_rules(self):
        self.assertEqual(
            prescription_key("JINGDONG", "136186144", "RX-1"),
            hashlib.sha256(b"JINGDONG:136186144:RX-1").hexdigest(),
        )
        self.assertEqual(
            prescription_key("MEITUAN", "32581900", "RX-2"),
            hashlib.sha256(b"RX-2").hexdigest(),
        )

    def test_insert_state_rows_is_idempotent_and_does_not_claim_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = pathlib.Path(tmp) / "state.sqlite3"
            StateStore(state_path).close()
            record = {
                "prescription_key": prescription_key(
                    "JINGDONG", "136186144", "RX-1"
                ),
                "prescription_id": "RX-1",
                "order_view_id": "ORDER-1",
            }

            self.assertEqual(insert_state_rows(state_path, [record], "run-1"), (1, 0))
            self.assertEqual(insert_state_rows(state_path, [record], "run-2"), (0, 1))

            connection = sqlite3.connect(state_path)
            try:
                row = connection.execute(
                    """
                    SELECT cos_status, api_status, api_message, upload_attempts
                    FROM prescriptions WHERE prescription_key = ?
                    """,
                    (record["prescription_key"],),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(
                row,
                ("not_requested", "not_requested", "platform_id_backfill", 0),
            )

    def test_select_missing_records_filters_state_and_batch_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = pathlib.Path(tmp) / "state.sqlite3"
            StateStore(state_path).close()
            first = {
                "prescription_key": prescription_key("JINGDONG", "1", "RX-1"),
                "prescription_id": "RX-1",
                "order_view_id": "ORDER-1",
            }
            second = {
                "prescription_key": prescription_key("JINGDONG", "1", "RX-2"),
                "prescription_id": "RX-2",
                "order_view_id": "ORDER-2",
            }
            insert_state_rows(state_path, [first], "run-1")

            missing, existing, duplicates = select_missing_records(
                state_path, [first, second, second]
            )

            self.assertEqual(missing, [second])
            self.assertEqual(existing, 1)
            self.assertEqual(duplicates, 1)


if __name__ == "__main__":
    unittest.main()

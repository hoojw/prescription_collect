import json
import tempfile
import unittest
from pathlib import Path

from multi_prescription_queue import append_multi_prescription_order


class MultiPrescriptionQueueTests(unittest.TestCase):
    def test_queue_is_single_file_and_deduplicates_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "multi_prescription_orders.jsonl"
            first = append_multi_prescription_order(
                platform="MEITUAN",
                store_id="32187777",
                store_name="测试店铺",
                order_id="order-1",
                prescription_count=2,
                prescription_ids=["rx-1", "rx-2", "rx-1"],
                queue_path=path,
            )
            second = append_multi_prescription_order(
                platform="MEITUAN",
                store_id="32187777",
                store_name="测试店铺",
                order_id="order-1",
                prescription_count=2,
                prescription_ids=["rx-1", "rx-2"],
                queue_path=path,
            )
            self.assertTrue(first)
            self.assertFalse(second)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["queue_key"], "MEITUAN:32187777:order-1")
            self.assertEqual(rows[0]["prescription_ids"], ["rx-1", "rx-2"])
            self.assertEqual(rows[0]["status"], "pending")

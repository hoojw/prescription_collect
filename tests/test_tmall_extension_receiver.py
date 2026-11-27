import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tmall_extension_receiver.app import TmallBatchApplication
from tmall_extension_receiver.config import Settings
from tmall_extension_receiver.store import BatchStore


TOKEN = "test-token-that-is-longer-than-thirty-two-characters"


def sample_batch(batch_id="20260818_120000", *, doctor=""):
    return {
        "schema_version": 2,
        "platform": "TIANMAO",
        "batch_id": batch_id,
        "validation_stage": "server_after_ingest",
        "records": [
            {
                "prescription_id": "rx-1",
                "order_id": "1234567890123456789",
                "doctor": doctor,
                "image_url": "https://example.invalid/rx-1.jpg",
                "order": {
                    "total_fee": None,
                    "products": [{"drug_name": "测试药品", "drcode": "", "quantity": None}],
                },
            }
        ],
    }


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.settings = Settings(
            listen_host="127.0.0.1",
            listen_port=8095,
            ingest_path="/internal/tmall/prescription-batches",
            output_dir=root / "data",
            database_path=root / "data" / "state.sqlite3",
            bearer_token=TOKEN,
            max_body_bytes=1024 * 1024,
            max_records_per_batch=100,
        )
        self.app = TmallBatchApplication(
            self.settings,
            BatchStore(self.settings.database_path, self.settings.output_dir),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def post(self, payload, *, token=TOKEN):
        return self.app.handle(
            "POST",
            self.settings.ingest_path,
            {
                "authorization": f"Bearer {token}",
                "content-type": "application/json;charset=UTF-8",
            },
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def decoded(self, result):
        return json.loads(result.body.decode("utf-8"))

    def test_missing_business_fields_are_queued_without_browser_review(self):
        result = self.post(sample_batch())
        self.assertEqual(result.status, 200)
        self.assertEqual(self.decoded(result)["status"], "accepted")
        queue_path = self.settings.output_dir / "inbox" / "20260818_120000.json"
        stored = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertIsNone(stored["records"][0]["order"]["total_fee"])
        self.assertEqual(stored["records"][0]["order"]["products"][0]["drcode"], "")

    def test_same_batch_and_content_is_idempotent(self):
        self.assertEqual(self.decoded(self.post(sample_batch()))["status"], "accepted")
        duplicate = self.post(sample_batch())
        self.assertEqual(duplicate.status, 200)
        self.assertEqual(self.decoded(duplicate)["status"], "duplicate")

    def test_same_batch_id_with_different_content_is_rejected(self):
        self.post(sample_batch())
        conflict = self.post(sample_batch(doctor="different"))
        self.assertEqual(conflict.status, 409)
        self.assertEqual(self.decoded(conflict)["code"], "batch_id_conflict")

    def test_invalid_token_is_rejected_without_storing(self):
        result = self.post(sample_batch(), token="wrong")
        self.assertEqual(result.status, 401)
        self.assertEqual(self.decoded(result)["code"], "unauthorized")
        self.assertFalse((self.settings.output_dir / "inbox" / "20260818_120000.json").exists())

    def test_empty_records_are_rejected(self):
        payload = sample_batch()
        payload["records"] = []
        result = self.post(payload)
        self.assertEqual(result.status, 400)
        self.assertEqual(self.decoded(result)["code"], "invalid_records")


if __name__ == "__main__":
    unittest.main()


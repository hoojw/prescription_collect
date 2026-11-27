import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from cos_image_upload import (
    CosSettings,
    build_scoped_folder_prefix,
    normalize_business_date,
    upload_image,
)


class FakeCosClient:
    def __init__(self):
        self.put_key = ""
        self.head_key = ""

    def put_object(self, **kwargs):
        self.put_key = kwargs["Key"]
        return {"ETag": '"test-etag"'}

    def head_object(self, **kwargs):
        self.head_key = kwargs["Key"]
        return {}


class CosImageUploadTests(unittest.TestCase):
    def test_normalize_business_date_accepts_prescription_timestamp(self):
        self.assertEqual(
            normalize_business_date("2026-07-21 23:53:09"),
            "2026-07-21",
        )

    def test_normalize_business_date_accepts_chinese_date(self):
        self.assertEqual(normalize_business_date("2026年7月2日"), "2026-07-02")

    def test_scoped_folder_is_platform_store_and_date(self):
        self.assertEqual(
            build_scoped_folder_prefix(
                "/files", "JINGDONG", "136186144", "2026-07-21T12:00:00+08:00"
            ),
            "files/jingdong/136186144/2026-07-21",
        )

    def test_scoped_folder_rejects_missing_date(self):
        with self.assertRaisesRegex(ValueError, "business date"):
            build_scoped_folder_prefix("/files", "MEITUAN", "32581900", "")

    def test_upload_uses_scoped_object_key(self):
        with tempfile.TemporaryDirectory() as temp:
            image = pathlib.Path(temp) / "rx.png"
            image.write_bytes(b"test-image")
            client = FakeCosClient()
            result = upload_image(
                image,
                "RX-1",
                settings=CosSettings(
                    base_url="https://example.test",
                    access_key="id",
                    secret_key="key",
                    region="region",
                    bucket="bucket",
                    folder_prefix="/files",
                ),
                client=client,
                platform="MEITUAN",
                store_id="32581900",
                business_date="2026-07-21 10:00:00",
            )
            expected = "files/meituan/32581900/2026-07-21/RX-1.png"
            self.assertEqual(client.put_key, expected)
            self.assertEqual(client.head_key, expected)
            self.assertEqual(result["object_key"], expected)


if __name__ == "__main__":
    unittest.main()

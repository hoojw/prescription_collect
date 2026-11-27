import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collect_store_fleet import (
    format_fleet_summary,
    load_enabled_stores,
    start_browser,
    stop_browser,
)


class StoreFleetTests(unittest.TestCase):
    def test_login_summary_is_human_readable(self):
        output = format_fleet_summary(
            {
                "stores": 2,
                "success": 1,
                "failed": 1,
                "elapsed_seconds": 12.248,
                "ocr_thread_limit": 3,
                "ocr_blas_thread_limit": 1,
                "results": [
                    {
                        "key": "mt-1",
                        "name": "保民",
                        "platform": "MEITUAN",
                        "elapsed_seconds": 3.911,
                        "exit_code": 0,
                        "error": "",
                    },
                    {
                        "key": "jd-2",
                        "name": "南无",
                        "platform": "JINGDONG",
                        "elapsed_seconds": 2.726,
                        "exit_code": 2,
                        "error": "",
                    },
                ],
            },
            check_login=True,
        )
        self.assertIn("店铺登录预检结果", output)
        self.assertIn("1. 保民（美团）", output)
        self.assertIn("状态：登录正常", output)
        self.assertIn("2. 南无（京东）", output)
        self.assertIn("状态：需要重新登录或核对店铺", output)
        self.assertIn("FLEET_SUMMARY stores=2 success=1 failed=1", output)
        self.assertNotIn('"results"', output)

    @staticmethod
    def write_config(path: pathlib.Path, port: int) -> None:
        path.write_text(json.dumps({"chrome_port": port}), encoding="utf-8")

    def test_registry_loads_only_enabled_stores(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = root / "store-a.json"
            self.write_config(config, 9333)
            registry = root / "stores.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "stores": [
                            {
                                "key": "store-a",
                                "enabled": True,
                                "browser_service": "meituan-store-chrome@store-a.service",
                                "config": str(config),
                            },
                            {"key": "future", "enabled": False, "config": "missing.json"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stores = load_enabled_stores(registry)
            self.assertEqual(
                stores,
                [
                    {
                        "key": "store-a",
                        "name": "store-a",
                        "platform": "MEITUAN",
                        "config": str(config.resolve()),
                        "browser_service": "meituan-store-chrome@store-a.service",
                        "chrome_port": "9333",
                    }
                ],
            )

    def test_registry_preserves_jingdong_platform(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = root / "jd.json"
            self.write_config(config, 9433)
            registry = root / "stores.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "stores": [
                            {
                                "key": "jd-136186144",
                                "platform": "jingdong",
                                "enabled": True,
                                "browser_service": "jingdong-store-chrome@jd-136186144.service",
                                "config": str(config),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stores = load_enabled_stores(registry)
            self.assertEqual(stores[0]["platform"], "JINGDONG")

    def test_registry_preserves_friendly_store_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = root / "mt.json"
            self.write_config(config, 9341)
            registry = root / "stores.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "stores": [
                            {
                                "key": "mt-32187777",
                                "name": "南无",
                                "platform": "MEITUAN",
                                "enabled": True,
                                "browser_service": "meituan-store-chrome@store-2.service",
                                "config": str(config),
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stores = load_enabled_stores(registry)
            self.assertEqual(stores[0]["name"], "南无")

    def test_registry_rejects_unknown_platform(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = root / "store.json"
            self.write_config(config, 9555)
            registry = root / "stores.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "stores": [
                            {
                                "key": "bad",
                                "platform": "OTHER",
                                "enabled": True,
                                "browser_service": "other@bad.service",
                                "config": str(config),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsupported enabled store platform"):
                load_enabled_stores(registry)

    def test_registry_rejects_duplicate_enabled_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = root / "store.json"
            self.write_config(config, 9333)
            registry = root / "stores.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "stores": [
                            {
                                "key": "a",
                                "enabled": True,
                                "browser_service": "meituan-store-chrome@a.service",
                                "config": str(config),
                            },
                            {
                                "key": "b",
                                "enabled": True,
                                "browser_service": "meituan-store-chrome@b.service",
                                "config": str(config),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate enabled store entry"):
                load_enabled_stores(registry)

    def test_registry_rejects_wrong_platform_browser_service(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = root / "store.json"
            self.write_config(config, 9333)
            registry = root / "stores.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "stores": [
                            {
                                "key": "bad",
                                "platform": "MEITUAN",
                                "enabled": True,
                                "browser_service": "jingdong-store-chrome@bad.service",
                                "config": str(config),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid browser_service"):
                load_enabled_stores(registry)

    @mock.patch("collect_store_fleet.wait_for_port")
    @mock.patch("collect_store_fleet.systemctl")
    def test_start_browser_restarts_then_waits_for_ready(self, systemctl_mock, wait_mock):
        systemctl_mock.return_value = mock.Mock(returncode=0, stderr="", stdout="")
        start_browser("meituan-store-chrome@store-a.service", 9333, 12)
        self.assertEqual(
            systemctl_mock.call_args_list,
            [
                mock.call("stop", "meituan-store-chrome@store-a.service"),
                mock.call("start", "meituan-store-chrome@store-a.service"),
            ],
        )
        self.assertEqual(
            wait_mock.call_args_list,
            [
                mock.call(9333, open_state=False, timeout=12),
                mock.call(9333, open_state=True, timeout=12),
            ],
        )

    @mock.patch("collect_store_fleet.wait_for_port")
    @mock.patch("collect_store_fleet.systemctl")
    def test_stop_browser_waits_for_closed_port(self, systemctl_mock, wait_mock):
        systemctl_mock.return_value = mock.Mock(returncode=0, stderr="", stdout="")
        stop_browser("jingdong-store-chrome@store-a.service", 9433, 8)
        systemctl_mock.assert_called_once_with(
            "stop", "jingdong-store-chrome@store-a.service"
        )
        wait_mock.assert_called_once_with(9433, open_state=False, timeout=8)


if __name__ == "__main__":
    unittest.main()

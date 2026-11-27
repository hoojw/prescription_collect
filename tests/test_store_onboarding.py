import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from store_onboarding import (
    add_disabled_registry_entry,
    add_discovered_instance,
    build_draft_config,
    choose_meituan_instance,
)


class StoreOnboardingTests(unittest.TestCase):
    def test_meituan_uses_next_store_number(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            browsers = root / "browsers"
            profiles = root / "profiles"
            browsers.mkdir()
            profiles.mkdir()
            (browsers / "store-1.env").write_text("", encoding="utf-8")
            (profiles / "store-2").mkdir()
            (browsers / "store-4.env").write_text("", encoding="utf-8")
            self.assertEqual(choose_meituan_instance(browsers, profiles), "store-3")

    def test_discovered_instance_has_no_collector_capability_by_default(self):
        registry = {"schema_version": 1, "instances": []}
        add_discovered_instance(
            registry,
            store_key="mt-32581900",
            platform="MEITUAN",
            identity={"current_poi_id": "32581900"},
            state={
                "instance": "onboard-mt-20260907120000",
                "chrome_port": 9350,
                "chrome_profile": "/opt/meituan-chrome-profiles/onboard-mt-20260907120000",
            },
        )
        entry = registry["instances"][0]
        self.assertFalse(entry["confirmed"])
        self.assertEqual(entry["capabilities"], [])

    def test_meituan_draft_is_safe_and_uses_discovered_identity(self):
        key, config = build_draft_config(
            "MEITUAN",
            {"current_poi_id": "32581900"},
            9350,
            {
                "expected_poi_id": "REPLACE_MEITUAN_POI_ID",
                "output_dir": "/data/mt-REPLACE_MEITUAN_POI_ID",
                "cos_upload_enabled": True,
                "api_submit_enabled": True,
            },
        )
        self.assertEqual(key, "mt-32581900")
        self.assertEqual(config["expected_poi_id"], "32581900")
        self.assertEqual(config["chrome_port"], 9350)
        self.assertFalse(config["cos_upload_enabled"])
        self.assertFalse(config["api_submit_enabled"])

    def test_jd_draft_records_exact_shop_name(self):
        key, config = build_draft_config(
            "JINGDONG",
            {"current_vender_id": "136186144", "current_shop_name": "测试京东店"},
            9450,
            {
                "expected_vender_id": "REPLACE_JD_VENDER_ID",
                "expected_shop_name": "REPLACE_JD_EXACT_SHOP_NAME",
            },
        )
        self.assertEqual(key, "jd-136186144")
        self.assertEqual(config["expected_shop_name"], "测试京东店")

    def test_registry_entry_is_disabled_until_manual_confirmation(self):
        registry = {"schema_version": 1, "stores": []}
        add_disabled_registry_entry(
            registry,
            store_key="mt-32581900",
            platform="MEITUAN",
            name="测试店",
            config_path=pathlib.Path("/etc/stores/mt-32581900.json"),
            browser_service="meituan-store-chrome@onboard-mt-1.service",
        )
        self.assertFalse(registry["stores"][0]["enabled"])

    def test_duplicate_registry_identity_is_rejected(self):
        registry = {"stores": [{"key": "mt-32581900"}]}
        with self.assertRaisesRegex(ValueError, "already exists"):
            add_disabled_registry_entry(
                registry,
                store_key="mt-32581900",
                platform="MEITUAN",
                name="测试店",
                config_path=pathlib.Path("/etc/stores/mt-32581900.json"),
                browser_service="meituan-store-chrome@onboard-mt-1.service",
            )


if __name__ == "__main__":
    unittest.main()

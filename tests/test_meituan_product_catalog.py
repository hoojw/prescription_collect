import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import meituan_product_catalog as catalog


class MeituanProductCatalogTests(unittest.TestCase):
    def test_entries_include_each_platform_sku(self):
        entries = catalog.entries_from_products(
            [
                {
                    "name": "[品牌]测试片 10mg*10片/盒",
                    "upcCode": "top-upc",
                    "wmProductSkus": [
                        {"id": 101, "upcCode": "upc-1", "spec": "10mg*10片/盒"},
                        {"id": 102, "upcCode": "upc-2", "spec": "10mg*20片/盒"},
                    ],
                }
            ],
            "32581900",
        )
        self.assertEqual([entry["s"] for entry in entries], ["101", "102"])
        self.assertEqual([entry["u"] for entry in entries], ["upc-1", "upc-2"])
        self.assertTrue(all(entry["p"] == "32581900" for entry in entries))

    def test_complete_catalog_uses_total_count(self):
        responses = [
            {
                "pageNum": 1,
                "pageSize": 2,
                "totalCount": 3,
                "productList": [{"skuId": 1}, {"skuId": 2}],
            },
            {
                "pageNum": 2,
                "pageSize": 2,
                "totalCount": 3,
                "productList": [{"skuId": 3}],
            },
        ]
        with mock.patch.object(catalog, "fetch_product_page", side_effect=responses):
            products, pages = catalog.fetch_complete_catalog(
                object(), "32581900", page_size=2
            )
        self.assertEqual(len(products), 3)
        self.assertEqual(pages, 2)

    def test_incomplete_catalog_is_rejected(self):
        response = {
            "pageNum": 1,
            "pageSize": 20,
            "totalCount": 2,
            "productList": [{"skuId": 1}],
        }
        with mock.patch.object(catalog, "fetch_product_page", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                catalog.fetch_complete_catalog(
                    object(), "32581900", page_size=20, max_pages=1
                )

    def test_duplicate_product_across_pages_is_rejected(self):
        responses = [
            {
                "pageNum": 1,
                "pageSize": 1,
                "totalCount": 2,
                "productList": [{"id": 100, "skuId": 1}],
            },
            {
                "pageNum": 2,
                "pageSize": 1,
                "totalCount": 2,
                "productList": [{"id": 100, "skuId": 1}],
            },
        ]
        with mock.patch.object(catalog, "fetch_product_page", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                catalog.fetch_complete_catalog(
                    object(), "32581900", page_size=1
                )

    def test_refresh_writes_store_bound_index_atomically(self):
        products = [
            {
                "name": "测试片 10mg*10片/盒",
                "wmProductSkus": [
                    {"id": 101, "upcCode": "upc-1", "spec": "10mg*10片/盒"}
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "catalog" / "medicine_sku_index.json"
            with mock.patch.object(
                catalog,
                "fetch_complete_catalog",
                return_value=(products, 1),
            ):
                result = catalog.refresh_sku_index(
                    object(), "32581900", path, retries=1
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result.status, "success")
            self.assertEqual(payload["poi_id"], "32581900")
            self.assertEqual(payload["product_count"], 1)
            self.assertEqual(payload["entry_count"], 1)
            self.assertEqual(result.index.match({"upcCode": "upc-1"}, "32581900").drcode, "101")

    def test_refresh_failure_uses_only_fresh_matching_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "medicine_sku_index.json"
            catalog._atomic_write_index(
                path,
                "32581900",
                1,
                [
                    {
                        "p": "32581900",
                        "s": "101",
                        "u": "upc-1",
                        "t": "测试片",
                        "n": "测试片",
                        "c": "10片",
                    }
                ],
            )
            with mock.patch.object(
                catalog,
                "fetch_complete_catalog",
                side_effect=RuntimeError("temporary"),
            ):
                result = catalog.refresh_sku_index(
                    object(),
                    "32581900",
                    path,
                    cache_ttl_hours=72,
                    retries=1,
                )
            self.assertEqual(result.status, "fallback_cache")
            self.assertIn("temporary", result.error)


if __name__ == "__main__":
    unittest.main()

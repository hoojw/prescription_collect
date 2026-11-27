import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alihealth_local_client.product_index import (
    MATCH_AMBIGUOUS,
    MATCH_EXACT,
    TmallProductIndex,
    apply_product_index,
    build_product_index,
)


def row(
    item_id,
    sku_id,
    sku_outer_id,
    sku_price,
    box_count,
    *,
    title="[测试牌]厄贝沙坦片75mg*7片/盒",
    item_price="8.00",
):
    return {
        "id": item_id,
        "title": title,
        "price": item_price,
        "sku.skuId": sku_id,
        "sku.skuPrice": sku_price,
        "sku.skuOuterId": sku_outer_id,
        "_extra_values": [f"套餐类型:{box_count}盒装;"],
        "_source_file": "test.xlsx",
        "_source_row": 4,
    }


class BuildTests(unittest.TestCase):
    def test_single_box_merchant_code_and_sku_price_are_selected(self):
        payload = build_product_index(
            [
                row("item-1", "sku-1", "merchant-code", "63.00", 1, item_price="189.00"),
                row("item-1", "sku-3", "merchant-code*3", "189.00", 3, item_price="189.00"),
            ],
            expected_products=1,
        )
        product = payload["products"][0]
        self.assertEqual(product["drcode"], "merchant-code")
        self.assertEqual(product["platform_sku_id"], "sku-1")
        self.assertEqual(product["single_box_price"], "63.00")
        self.assertEqual(payload["summary"]["item_price_mismatches"], 1)

    def test_duplicate_match_keys_are_reported_in_index_audit(self):
        payload = build_product_index(
            [
                row("item-1", "sku-1", "code-a", "8.00", 1),
                row("item-2", "sku-2", "code-b", "9.00", 1, item_price="9.00"),
            ],
            expected_products=2,
        )
        self.assertGreater(payload["summary"]["duplicate_full_match_keys"], 0)
        self.assertEqual(
            payload["summary"]["products_affected_by_full_key_conflicts"],
            2,
        )
        self.assertEqual(
            payload["match_key_conflicts"][0]["item_ids"],
            ["item-1", "item-2"],
        )


class MatchAndFeeTests(unittest.TestCase):
    def setUp(self):
        payload = build_product_index(
            [
                row("item-1", "sku-1", "code-a", "8.00", 1),
                row("item-1", "sku-5", "code-a*5", "40.00", 5),
                row(
                    "item-2",
                    "sku-2",
                    "code-b",
                    "12.50",
                    1,
                    title="[测试牌]氯沙坦钾片50mg*14片/盒",
                    item_price="12.50",
                ),
            ],
            expected_products=2,
        )
        self.index = TmallProductIndex(payload["products"])

    def test_name_and_spec_match_ignores_brand_prefix(self):
        match = self.index.match(
            {"drug_name": "厄贝沙坦片", "spec": "75mg*7片/盒"}
        )
        self.assertEqual(match.status, MATCH_EXACT)
        self.assertEqual(match.entry["drcode"], "code-a")

    def test_total_fee_is_sum_of_single_box_price_times_box_quantity(self):
        result = apply_product_index(
            {
                "drug_list": {
                    "drug_dto": [
                        {"drug_name": "厄贝沙坦片", "spec": "75mg*7片/盒", "total": "3"},
                        {"drug_name": "氯沙坦钾片", "spec": "50mg*14片/盒", "total": "2"},
                    ]
                }
            },
            self.index,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_fee"], 49.0)
        self.assertEqual([item["drcode"] for item in result["drugs"]], ["code-a", "code-b"])

    def test_invalid_box_quantity_enters_review(self):
        result = apply_product_index(
            {
                "drug_list": {
                    "drug_dto": {
                        "drug_name": "厄贝沙坦片",
                        "spec": "75mg*7片/盒",
                        "total": "适量",
                    }
                }
            },
            self.index,
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("box_quantity_invalid", result["reasons"])
        self.assertIsNone(result["total_fee"])

    def test_duplicate_name_and_spec_is_ambiguous(self):
        payload = build_product_index(
            [
                row("item-1", "sku-1", "code-a", "8.00", 1),
                row("item-2", "sku-2", "code-b", "9.00", 1, item_price="9.00"),
            ],
            expected_products=2,
        )
        index = TmallProductIndex(payload["products"])
        match = index.match({"drug_name": "厄贝沙坦片", "spec": "75mg*7片/盒"})
        self.assertEqual(match.status, MATCH_AMBIGUOUS)
        self.assertIsNone(match.entry)


if __name__ == "__main__":
    unittest.main()

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import server_jd_prescription_collect as collector


class JdCollectorTests(unittest.TestCase):
    def test_store_identity_can_be_discovered_before_expected_config_exists(self):
        vender, shop = collector.extract_store_identity(
            {
                "venderId": "136186144",
                "rxExtendsInfo": {"shopName": "测试京东店"},
            }
        )
        self.assertEqual(vender, "136186144")
        self.assertEqual(shop, "测试京东店")

    def test_store_discovery_rejects_incomplete_identity(self):
        with self.assertRaisesRegex(RuntimeError, "missing venderId or shopName"):
            collector.extract_store_identity({"venderId": "136186144"})

    def test_multiple_prescriptions_are_skipped_even_with_one_internal(self):
        result = collector.select_delivery_prescription(
            {
                "rxNo": "p1,p2",
                "hospitalName": "内部医院,外部医院",
                "doctorName": "医生甲,医生乙",
                "pharmacistName": "药师甲,药师乙",
                "rxPicUrlList": "https://example.invalid/1.png,https://example.invalid/2.png",
            },
            "内部医院",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "multiple_prescriptions_in_order")
        self.assertEqual(result["prescription_count"], 2)
        self.assertEqual(result["prescription_ids"], ["p1", "p2"])

    def test_multiple_prescriptions_remain_skipped_after_detail_normalization(self):
        result = collector.normalize_detail_record(
            {
                "rxId": "rx-detail-1",
                "orderId": "order-1",
                "venderId": "136186144",
                "shopName": "测试京东店",
                "rxExtendsInfo": {"rxNo": "p1,p2"},
            },
            "136186144",
            "测试京东店",
            "内部医院",
            "内部医院",
            "自费",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["order_id"], "order-1")
        self.assertEqual(result["prescription_ids"], ["p1", "p2"])

    def test_single_prescription_remains_upload_eligible(self):
        result = collector.select_delivery_prescription(
            {
                "rxNo": "p1",
                "hospitalName": "内部医院",
                "doctorName": "医生甲",
                "pharmacistName": "药师甲",
                "rxPicUrlList": "https://example.invalid/1.png",
            },
            "内部医院",
        )
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["rx_no"], "p1")

    def test_multiple_distinct_drugs_are_kept_for_ocr_matching(self):
        drugs = collector.merge_jd_drugs(
            [
                {"skuId": "sku1", "skuName": "药品甲 10mg*10片/盒", "buyNum": 1},
                {"skuId": "sku2", "skuName": "药品乙 20mg*20片/盒", "buyNum": 2},
            ]
        )
        self.assertEqual(len(drugs), 2)
        self.assertEqual([drug["drcode"] for drug in drugs], ["sku1", "sku2"])

    def test_same_jd_drug_lines_are_merged_before_ocr_matching(self):
        drugs = collector.merge_jd_drugs(
            [
                {"skuId": "sku1", "skuName": "药品甲 10mg*10片/盒", "buyNum": 1},
                {"skuId": "sku1", "skuName": "药品甲 10mg*10片/盒", "buyNum": 2},
            ]
        )
        self.assertEqual(len(drugs), 1)
        self.assertEqual(drugs[0]["jd_purchase_quantity"], 3)

    def test_jd_expiry_suffix_is_removed_without_rewriting_specification(self):
        drug = collector.normalize_jd_drug(
            {
                "skuId": "sku-inhaler",
                "skuName": (
                    "沙美特罗替卡松吸入粉雾剂 "
                    "50μg:250μg*60泡【27年4月到期】"
                ),
                "buyNum": 1,
            }
        )
        self.assertEqual(drug["drugName"], "沙美特罗替卡松吸入粉雾剂")
        self.assertEqual(drug["packingSpec"], "50μg:250μg*60泡")
        self.assertEqual(drug["drugCount"], 1)
        self.assertIn("【27年4月到期】", drug["source_drug_name"])

    def test_internal_company_name_is_not_exposed_as_output_hospital_name(self):
        result = collector.normalize_detail_record(
            {
                "venderId": "136186144",
                "orderId": "order-1",
                "rxId": "rx-1",
                "orderSum": 10,
                "rxSkuList": [
                    {
                        "skuId": "sku-1",
                        "skuName": "药品甲 10mg*10片/盒",
                        "buyNum": 1,
                    }
                ],
                "rxExtendsInfo": {
                    "shopName": "测试店铺",
                    "rxNo": "prescription-1",
                    "hospitalName": "天津北辰保民医院有限公司",
                    "doctorName": "内部医生",
                    "pharmacistName": "内部药师",
                    "rxPicUrlList": "https://example.invalid/1.png",
                },
            },
            "136186144",
            "测试店铺",
            "天津北辰保民医院有限公司",
            "天津北辰保民互联网医院",
            "自费",
        )
        self.assertEqual(result["status"], "selected")
        self.assertEqual(
            result["record"]["source_hospital_name"],
            "天津北辰保民医院有限公司",
        )
        self.assertEqual(
            result["record"]["hospital_name"],
            "天津北辰保民互联网医院",
        )

    def test_detail_field_priorities_follow_approved_order(self):
        store_time = 1_774_236_000_000
        result = collector.normalize_detail_record(
            {
                "venderId": "136186144",
                "orderId": "order-1",
                "rxId": "rx-1",
                "skuSum": 100,
                "orderSum": 80,
                "orderTime": store_time - 60_000,
                "rxSkuList": [
                    {
                        "skuId": "sku-effective",
                        "skuName": "药品甲 10mg*10片/盒",
                        "buyNum": 1,
                    }
                ],
                "originRxSkuList": [
                    {
                        "skuId": "sku-original",
                        "skuName": "药品乙 20mg*20片/盒",
                        "buyNum": 2,
                    }
                ],
                "rxExtendsInfo": {
                    "shopName": "测试店铺",
                    "rxNo": "prescription-1",
                    "hospitalName": "内部医院",
                    "doctorName": "内部医生",
                    "pharmacistName": "内部药师",
                    "rxPicUrlList": "https://example.invalid/1.png",
                    "storeTime": store_time,
                    "created": store_time - 30_000,
                },
            },
            "136186144",
            "测试店铺",
            "内部医院",
            "内部医院",
            "自费",
        )
        record = result["record"]
        self.assertEqual(record["total_fee"], 100.0)
        self.assertEqual(
            record["prescription_time"],
            collector.timestamp_to_iso(store_time),
        )
        self.assertEqual(
            [drug["drcode"] for drug in record["drug_infos"]],
            ["sku-effective"],
        )

    def test_malformed_effective_drug_does_not_fallback_per_item(self):
        result = collector.normalize_detail_record(
            {
                "venderId": "136186144",
                "orderId": "order-1",
                "rxId": "rx-1",
                "rxSkuList": [
                    {
                        "skuId": "sku-effective",
                        "skuName": "药品甲 10mg*10片/盒",
                        "buyNum": None,
                    }
                ],
                "originRxSkuList": [
                    {
                        "skuId": "sku-original",
                        "skuName": "药品乙 20mg*20片/盒",
                        "buyNum": 2,
                    }
                ],
                "rxExtendsInfo": {
                    "shopName": "测试店铺",
                    "rxNo": "prescription-1",
                    "hospitalName": "内部医院",
                    "rxPicUrlList": "https://example.invalid/1.png",
                },
            },
            "136186144",
            "测试店铺",
            "内部医院",
            "内部医院",
            "自费",
        )
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["reason"], "rx_sku_item_missing_required_field")


if __name__ == "__main__":
    unittest.main()

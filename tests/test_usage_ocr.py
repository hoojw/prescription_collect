import unittest
from decimal import Decimal
from pathlib import Path

from prescription_usage_ocr import (
    PaddleUsageOCR,
    _assign_usage_candidates,
    _merge_usage_candidates,
    _usage_candidate,
    build_upload_medicine_info,
    calculate_duration,
    merge_same_drugs,
    parse_header_fields,
    parse_hospital_fields,
    parse_isolated_birth_date,
    parse_package_quantity,
    parse_strength_mg_per_unit,
    parse_usage_text,
    split_drug_name_specification,
)


class UsageParsingTests(unittest.TestCase):
    def test_ocr_timing_metrics_report_predict_calls_and_average(self):
        class FakeEngine:
            def predict(self, image):
                return []

        engine = object.__new__(PaddleUsageOCR)
        engine.engine = FakeEngine()
        engine.model_load_seconds = 1.25
        engine.inference_seconds = 0.0
        engine.first_inference_seconds = 0.0
        engine.predict_calls = 0
        self.assertEqual(engine._predict(object()), [])
        metrics = engine.timing_metrics(image_count=2)
        self.assertEqual(metrics["ocr_thread_limit"], 3)
        self.assertEqual(metrics["ocr_blas_thread_limit"], 1)
        self.assertEqual(metrics["ocr_model_load_seconds"], 1.25)
        self.assertEqual(metrics["ocr_predict_calls"], 1)
        self.assertGreaterEqual(metrics["ocr_first_inference_seconds"], 0.0)
        self.assertEqual(
            metrics["ocr_warmup_seconds"],
            round(
                metrics["ocr_model_load_seconds"]
                + metrics["ocr_first_inference_seconds"],
                6,
            ),
        )
        self.assertGreaterEqual(metrics["ocr_inference_seconds"], 0.0)
        # The total and per-image values are each rounded independently; a
        # sub-microsecond fake inference can therefore differ by one unit in
        # the sixth decimal place after division.
        self.assertAlmostEqual(
            metrics["ocr_average_seconds"],
            metrics["ocr_inference_seconds"] / 2,
            places=5,
        )

    def test_qualitative_usage_is_serialized_as_non_empty_fields(self):
        appropriate = build_upload_medicine_info([{
            "drugName": "吡美莫司乳膏",
            "packingSpec": "1%*15g/支/盒",
            "drugCount": 1,
            "usage_text": "外用，一日2次，每次适量",
            "usage_method": "外用",
            "frequency_text": "一日2次",
        }])[0]
        self.assertEqual(appropriate["dosage_per_time"], "每次")
        self.assertEqual(appropriate["dosage_unit"], "适量")

        as_needed = build_upload_medicine_info([{
            "drugName": "开塞露",
            "packingSpec": "20ml*6支/盒",
            "drugCount": 1,
            "usage_text": "必要时，每次1支",
            "dose_per_time": 1,
            "dose_unit": "支",
        }])[0]
        self.assertEqual(as_needed["usage_frequency"], "必要时")
        self.assertEqual(as_needed["dosage_per_time"], 1)
        self.assertEqual(as_needed["dosage_unit"], "支")

    def test_usage_candidate_merge_preserves_identical_text_on_distinct_rows(self):
        candidates = _merge_usage_candidates(
            [
                [
                    {"text": "口服一日1次每次1片", "score": 0.99, "y": 100, "height": 24},
                    {"text": "口服一日1次每次1片", "score": 0.98, "y": 192, "height": 24},
                ]
            ]
        )
        self.assertEqual(len(candidates), 2)
        self.assertEqual([item["y"] for item in candidates], [100, 192])

    def test_usage_candidate_merge_collapses_same_physical_line(self):
        candidates = _merge_usage_candidates(
            [
                [{"text": "口服一日1次每次1片", "score": 0.91, "y": 100, "height": 24}],
                [{"text": "口服一日1次每次1片", "score": 0.99, "y": 103, "height": 24}],
            ]
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["score"], 0.99)

    def test_grayscale_threshold_values_are_clamped(self):
        self.assertIsNone(PaddleUsageOCR._normalise_threshold(None))
        self.assertEqual(PaddleUsageOCR._normalise_threshold(0), 1)
        self.assertEqual(PaddleUsageOCR._normalise_threshold(120), 120)
        self.assertEqual(PaddleUsageOCR._normalise_threshold(300), 254)

    def test_usage_preprocessing_mode_defaults_to_blue_mask(self):
        self.assertEqual(
            PaddleUsageOCR._normalise_usage_preprocessing_mode(None), "blue_mask"
        )
        self.assertEqual(
            PaddleUsageOCR._normalise_usage_preprocessing_mode("BLUE_MASK"),
            "blue_mask",
        )
        self.assertEqual(
            PaddleUsageOCR._normalise_usage_preprocessing_mode("grayscale"),
            "grayscale",
        )
        with self.assertRaisesRegex(ValueError, "usage_preprocessing_mode"):
            PaddleUsageOCR._normalise_usage_preprocessing_mode("grayscale_threshold")

    def test_hospital_title_uses_store_specific_internal_name(self):
        internal_name = "天津津南南无互联网医院"
        internal = parse_hospital_fields(
            [
                {
                    "text": f"{internal_name} 处方笺",
                    "score": 0.99,
                    "x": 100,
                    "y": 20,
                    "width": 360,
                    "height": 36,
                }
            ],
            internal_name,
        )
        self.assertEqual(internal["hospital_source_code"], 1)
        self.assertEqual(internal["hospital_source"], "internal")
        self.assertEqual(internal["hospital_name_ocr"], internal_name)

        external = parse_hospital_fields(
            [
                {
                    "text": "天津医科大学总医院 处方笺",
                    "score": 0.99,
                    "x": 100,
                    "y": 20,
                    "width": 360,
                    "height": 36,
                }
            ],
            internal_name,
        )
        self.assertEqual(external["hospital_source_code"], 2)
        self.assertEqual(external["hospital_source"], "external")

    def test_hospital_title_accepts_explicit_store_alias_only(self):
        internal_name = "天津津南南无互联网医院"
        image_title = "天津津南南无门诊互联网医院"
        row = {
            "text": f"{image_title} 处方笺",
            "score": 0.99,
            "x": 100,
            "y": 20,
            "width": 360,
            "height": 36,
        }
        without_alias = parse_hospital_fields([row], internal_name)
        self.assertEqual(without_alias["hospital_source_code"], 3)
        self.assertEqual(without_alias["hospital_ocr_reason"], "hospital_name_close_to_internal")

        with_alias = parse_hospital_fields(
            [row],
            internal_name,
            internal_hospital_aliases=[image_title],
        )
        self.assertEqual(with_alias["hospital_source_code"], 1)
        self.assertEqual(with_alias["hospital_source"], "internal")

        other_store = parse_hospital_fields(
            [
                {
                    **row,
                    "text": "天津北辰保民互联网医院 处方笺",
                }
            ],
            internal_name,
            internal_hospital_aliases=[image_title],
        )
        self.assertEqual(other_store["hospital_source_code"], 2)

    def test_hospital_title_classifies_internal_external_and_review(self):
        internal = parse_hospital_fields(
            [
                {
                    "text": "天津北辰保民互联网医院 已使用",
                    "score": 0.99,
                    "x": 100,
                    "y": 20,
                    "width": 300,
                    "height": 36,
                }
            ],
            "天津北辰保民互联网医院",
        )
        self.assertEqual(internal["hospital_source_code"], 1)
        self.assertEqual(internal["hospital_source"], "internal")
        self.assertEqual(internal["hospital_name_ocr"], "天津北辰保民互联网医院")

        external = parse_hospital_fields(
            [
                {
                    "text": "天津医科大学总医院 处方笺",
                    "score": 0.98,
                    "x": 100,
                    "y": 20,
                    "width": 300,
                    "height": 36,
                }
            ],
            "天津北辰保民互联网医院",
        )
        self.assertEqual(external["hospital_source_code"], 2)
        self.assertEqual(external["hospital_name_ocr"], "天津医科大学总医院")

        review = parse_hospital_fields(
            [
                {
                    "text": "天津北辰保氏互联网医院",
                    "score": 0.99,
                    "x": 100,
                    "y": 20,
                    "width": 300,
                    "height": 36,
                }
            ],
            "天津北辰保民互联网医院",
        )
        self.assertEqual(review["hospital_source_code"], 3)
        self.assertEqual(review["hospital_ocr_reason"], "hospital_name_close_to_internal")

    def test_hospital_title_joins_adjacent_lines_of_same_font_size(self):
        result = parse_hospital_fields(
            [
                {
                    "text": "中山国丹中医院银叶",
                    "score": 0.99,
                    "x": 170,
                    "y": 24,
                    "width": 310,
                    "height": 34,
                },
                {
                    "text": "普通",
                    "score": 0.95,
                    "x": 718,
                    "y": 25,
                    "width": 78,
                    "height": 33,
                },
                {
                    "text": "互联网医院处方笺",
                    "score": 0.98,
                    "x": 248,
                    "y": 68,
                    "width": 260,
                    "height": 34,
                },
            ],
            "天津市河北区和安门诊部互联网医院",
        )

        self.assertEqual(result["hospital_source_code"], 2)
        self.assertEqual(result["hospital_name_ocr"], "中山国丹中医院银叶互联网医院")
        self.assertEqual(result["hospital_ocr_title_line_count"], 2)

    def test_usage_crop_ratios_start_at_thirty_five_percent(self):
        engine = object.__new__(PaddleUsageOCR)
        engine.single_crop_top = 0.35
        engine.single_crop_bottom = 0.53
        engine.multi_crop_top = 0.35
        engine.multi_crop_bottom = 0.70
        self.assertEqual(engine._usage_crop_ratios(1), (0.35, 0.53, "single"))
        self.assertEqual(engine._usage_crop_ratios(2), (0.35, 0.70, "multi"))

    def test_header_fields_use_age_date_and_ignore_issue_date(self):
        result = parse_header_fields(
            [
                {"text": "科室：内科", "score": 0.99, "x": 20, "y": 10, "height": 18},
                {"text": "使", "score": 0.20, "x": 180, "y": 11, "height": 18},
                {"text": "日", "score": 0.30, "x": 0, "y": 40, "height": 18},
                {"text": "年龄：19岁", "score": 0.98, "x": 20, "y": 40, "height": 18},
                {"text": "2007-03-02", "score": 0.97, "x": 150, "y": 41, "height": 18},
                {"text": "使", "score": 0.25, "x": 360, "y": 41, "height": 18},
                {"text": "开具日期：2026年7月16日", "score": 0.99, "x": 20, "y": 72, "height": 18},
            ],
            reference_year=2026,
        )
        self.assertEqual(result["birth_date"], "2007-03-02")
        self.assertEqual(result["birth_date_ocr_status"], "success")
        self.assertEqual(result["birth_date_ocr_confidence"], 0.97)
        self.assertEqual(result["department"], "内科")
        self.assertEqual(result["department_ocr_status"], "success")
        self.assertEqual(result["department_ocr_confidence"], 0.99)

    def test_header_fields_support_chinese_date_and_split_department(self):
        result = parse_header_fields(
            [
                {"text": "科室：", "score": 0.96, "x": 20, "y": 10, "height": 18},
                {"text": "心血管内科", "score": 0.95, "x": 100, "y": 11, "height": 18},
                {"text": "生日：2007年3月2日", "score": 0.94, "x": 20, "y": 40, "height": 18},
            ],
            reference_year=2026,
        )
        self.assertEqual(result["department"], "心血管内科")
        self.assertEqual(result["birth_date"], "2007-03-02")

    def test_department_merges_multiple_close_boxes_up_to_sixteen_characters(self):
        result = parse_header_fields(
            [
                {"text": "科室：", "score": 0.99, "x": 20, "y": 10, "width": 55, "height": 18},
                {"text": "内分泌与", "score": 0.96, "x": 82, "y": 10, "width": 75, "height": 18},
                {"text": "代谢病科", "score": 0.95, "x": 162, "y": 10, "width": 75, "height": 18},
                {"text": "使", "score": 0.20, "x": 340, "y": 10, "width": 18, "height": 18},
            ],
            reference_year=2026,
        )
        self.assertEqual(result["department"], "内分泌与代谢病科")
        self.assertEqual(result["department_ocr_confidence"], 0.95)

    def test_isolated_birth_date_supports_long_dash(self):
        result = parse_isolated_birth_date(
            [
                {
                    "text": "1954—04—29",
                    "score": 0.98,
                    "x": 0,
                    "y": 0,
                    "width": 120,
                    "height": 20,
                }
            ],
            "1954-04-29",
            reference_year=2026,
        )
        self.assertEqual(result["value"], "1954-04-29")
        self.assertEqual(result["status"], "success")

    def test_header_low_confidence_is_retained_for_review(self):
        result = parse_header_fields(
            [
                {"text": "科室：内科", "score": 0.60, "x": 20, "y": 10, "height": 18},
                {"text": "出生日期：2007/3/2", "score": 0.65, "x": 20, "y": 40, "height": 18},
            ],
            reference_year=2026,
        )
        self.assertEqual(result["department_ocr_status"], "low_confidence")
        self.assertEqual(result["birth_date_ocr_status"], "low_confidence")
        self.assertEqual(result["birth_date"], "2007-03-02")

    def test_issue_date_alone_is_not_treated_as_birth_date(self):
        result = parse_header_fields(
            [
                {"text": "开具日期：2026-07-16", "score": 0.99, "x": 20, "y": 10, "height": 18},
            ],
            reference_year=2026,
        )
        self.assertEqual(result["birth_date_ocr_status"], "not_found")
        self.assertEqual(result["department_ocr_status"], "not_found")

    def test_daily_tablet_usage(self):
        usage = parse_usage_text("口服 一日1次 每次2片")
        self.assertEqual(usage["administration_route"], "口服")
        self.assertEqual(usage["dose_per_time"], 2.0)
        self.assertEqual(usage["dose_unit"], "片")
        self.assertEqual(usage["times_per_day"], 1.0)

    def test_frequency_once_is_not_reused_as_dose_when_ocr_drops_commas(self):
        usage = parse_usage_text("口服一日一次一次2.0片")
        self.assertEqual(usage["frequency_text"], "一日一次")
        self.assertEqual(usage["times_per_day"], 1.0)
        self.assertEqual(usage["dose_per_time"], 2.0)
        self.assertEqual(usage["dose_unit"], "片")

        drug = {"drugName": "阿立哌唑片5mg*10片", "drugCount": 3}
        duration = calculate_duration(drug, usage)
        self.assertEqual(duration["duration_days"], 15)
        self.assertEqual(duration["duration_calculation_status"], "success")

    def test_special_frequency_is_removed_before_dose_matching(self):
        for text, expected_frequency, expected_times in (
            ("口服早晚各一次一次1片", "早晚各一次", 2.0),
            ("口服隔日一次一次1片", "隔日一次", 0.5),
            ("口服睡前一次一次1片", "睡前一次", 1.0),
        ):
            with self.subTest(text=text):
                usage = parse_usage_text(text)
                self.assertEqual(usage["frequency_text"], expected_frequency)
                self.assertEqual(usage["times_per_day"], expected_times)
                self.assertEqual(usage["dose_per_time"], 1.0)
                self.assertEqual(usage["dose_unit"], "片")

    def test_prn_usage_keeps_route_and_dose_but_has_no_fixed_frequency(self):
        text = "口服，必要时，一次320.0g"
        self.assertTrue(_usage_candidate(text))
        usage = parse_usage_text(text)
        self.assertEqual(usage["usage_method"], "口服")
        self.assertEqual(usage["frequency_text"], "必要时")
        self.assertIsNone(usage["times_per_day"])
        self.assertEqual(usage["dose_per_time"], 320.0)
        self.assertEqual(usage["dose_unit"], "g")

    def test_dosage_ocr_correction_for_press_unit(self):
        raw_usage = "吸入给药 一日4次 每次2掀"
        usage = parse_usage_text(raw_usage)
        self.assertEqual(usage["usage_text"], raw_usage)
        self.assertEqual(usage["dose_per_time"], 2.0)
        self.assertEqual(usage["dose_unit"], "揿")
        self.assertEqual(usage["times_per_day"], 4.0)

        drug = {
            "drugName": "硫酸沙丁胺醇吸入气雾剂100µg*200揿/盒",
            "drugCount": 6,
        }
        duration = calculate_duration(drug, usage)
        self.assertEqual(duration["duration_days"], 150)
        self.assertEqual(duration["duration_calculation_status"], "success")

    def test_inhalation_discrete_units_are_equivalent(self):
        drug = {
            "drugName": "沙美特罗替卡松吸入粉雾剂50μg:250μg*60泡",
            "drugCount": 2,
        }
        duration = calculate_duration(drug, parse_usage_text("口吸入 一日两次 一次1揿"))
        self.assertEqual(duration["calculation_total_quantity"], 120.0)
        self.assertEqual(duration["calculation_total_unit"], "泡")
        self.assertEqual(duration["daily_quantity"], 2.0)
        self.assertEqual(duration["daily_quantity_unit"], "泡")
        self.assertEqual(duration["duration_days"], 60)
        self.assertEqual(duration["duration_calculation_status"], "success")

    def test_inhalation_discrete_units_do_not_apply_to_non_inhalation_drugs(self):
        drug = {"drugName": "某药50μg*60泡", "drugCount": 1}
        duration = calculate_duration(drug, parse_usage_text("一日两次 一次1揿"))
        self.assertEqual(duration["duration_calculation_status"], "needs_review")
        self.assertEqual(duration["duration_calculation_reason"], "dose_and_package_units_mismatch")

    def test_package_quantity_and_duration(self):
        drug = {"drugName": "盐酸二甲双胍缓释片0.5g*30片/盒", "drugCount": 1}
        package = parse_package_quantity(drug["drugName"], "", drug["drugCount"])
        self.assertEqual(package["total_quantity"], 1)
        self.assertEqual(package["total_unit"], "盒")
        self.assertEqual(package["calculation_total_quantity"], 30.0)
        self.assertEqual(package["calculation_total_unit"], "片")
        duration = calculate_duration(drug, parse_usage_text("口服 一日1次 每次2片"))
        self.assertEqual(duration["duration_days"], 15)
        self.assertEqual(duration["duration_calculation_status"], "success")

    def test_strength_per_unit_slash_package_format(self):
        drug = {"drugName": "[福善美]阿仑膦酸钠片70mg/片/盒", "drugCount": 3}
        package = parse_package_quantity(drug["drugName"], "", drug["drugCount"])
        self.assertEqual(package["package_quantity_per_box"], 1)
        self.assertEqual(package["package_quantity_unit"], "片")
        self.assertEqual(package["unit_count_per_box"], 1)
        self.assertEqual(package["calculation_total_quantity"], 3)
        self.assertEqual(package["calculation_total_unit"], "片")
        self.assertEqual(parse_strength_mg_per_unit(drug["drugName"], ""), Decimal("70"))

        duration = calculate_duration(
            drug,
            parse_usage_text("清晨空腹口服每周1次每次1片"),
        )
        self.assertEqual(duration["duration_days"], 21)
        self.assertEqual(duration["duration_calculation_status"], "success")
        self.assertEqual(duration["duration_calculation_reason"], "calculated_from_package_and_usage")

    def test_blister_multiplier(self):
        package = parse_package_quantity("某药0.5g*10片*2板/盒", "", 4)
        self.assertEqual(package["package_quantity_per_box"], 20.0)
        self.assertEqual(package["total_quantity"], 4)
        self.assertEqual(package["total_unit"], "盒")
        self.assertEqual(package["calculation_total_quantity"], 80.0)

    def test_patch_package_with_dimension_and_bag_multiplier(self):
        drug = {
            "drugName": "[久悦]洛索洛芬钠凝胶贴膏(14cm*10cm)*4贴*2袋/盒",
            "drugCount": 2,
        }
        name, specification = split_drug_name_specification(drug["drugName"], "")
        self.assertEqual(name, "洛索洛芬钠凝胶贴膏")
        self.assertEqual(specification, "(14cm*10cm)*4贴*2袋/盒")
        package = parse_package_quantity(drug["drugName"], "", drug["drugCount"])
        self.assertEqual(package["package_quantity_per_box"], 8.0)
        self.assertEqual(package["package_quantity_unit"], "贴")
        self.assertEqual(package["calculation_total_quantity"], 16.0)
        duration = calculate_duration(drug, parse_usage_text("外用一日1次每次1贴"))
        self.assertEqual(duration["duration_days"], 16)
        self.assertEqual(duration["duration_calculation_status"], "success")

    def test_mass_dose_converts_to_tablets(self):
        drug = {"drugName": "盐酸二甲双胍缓释片0.5g*30片/盒", "drugCount": 1}
        duration = calculate_duration(drug, parse_usage_text("口服 每次0.5g 每日2次"))
        self.assertEqual(duration["daily_quantity"], 2.0)
        self.assertEqual(duration["duration_days"], 15)

    def test_weekly_injection_package_in_doses(self):
        drug = {"drugName": "注射液40mg(10mg*4次)/支/盒", "drugCount": 1}
        duration = calculate_duration(drug, parse_usage_text("皮下注射 每周1次 每次10mg"))
        self.assertEqual(duration["duration_days"], 28)
        self.assertEqual(duration["duration_calculation_status"], "success")

    def test_missing_frequency_requires_review(self):
        drug = {"drugName": "某药10片/盒", "drugCount": 1}
        duration = calculate_duration(drug, parse_usage_text("口服 每次1片"))
        self.assertEqual(duration["duration_calculation_status"], "needs_review")
        self.assertEqual(duration["duration_calculation_reason"], "frequency_missing_or_unparsed")

    def test_meal_time_oral_usage_is_preserved(self):
        usage = parse_usage_text("餐时口服 一日1次 每次1片")
        self.assertEqual(usage["usage_text"], "餐时口服 一日1次 每次1片")
        self.assertEqual(usage["usage_method"], "餐时口服")
        self.assertEqual(usage["administration_route"], "口服")

    def test_additional_administration_routes_are_preserved(self):
        cases = {
            "直肠给药": "直肠给药",
            "阴道给药": "阴道给药",
            "口腔喷入": "口腔喷入",
            "鼻腔喷入": "鼻腔喷入",
            "雾化吸入": "雾化吸入",
            "静脉滴注": "静脉滴注",
            "局部外用": "外用",
            "喷鼻": "鼻腔喷入",
            "含漱": "含漱",
            "嚼服": "嚼服",
            "冲服": "冲服",
            "涂抹": "涂抹",
            "滴耳": "滴耳",
        }
        for text, expected_route in cases.items():
            with self.subTest(text=text):
                usage = parse_usage_text(f"{text} 每日1次 每次1支")
                self.assertEqual(usage["usage_method"], text)
                self.assertEqual(usage["administration_route"], expected_route)

    def test_additional_count_units_support_duration(self):
        cases = (
            ("滴眼液30滴/瓶", "每日1次 每次1滴", "滴", 30),
            ("颗粒剂10小袋/盒", "每日1次 每次1小袋", "袋", 10),
            ("吸入剂60吸/盒", "每日2次 每次1吸", "吸", 30),
            ("泡腾片12片/盒", "每日1次 每次1片", "片", 12),
        )
        for drug_name, usage_text, expected_unit, expected_days in cases:
            with self.subTest(drug_name=drug_name):
                duration = calculate_duration(
                    {"drugName": drug_name, "drugCount": 1},
                    parse_usage_text(usage_text),
                )
                self.assertEqual(duration["calculation_total_unit"], expected_unit)
                self.assertEqual(duration["duration_days"], expected_days)

    def test_international_unit_conversion(self):
        drug = {"drugName": "某注射剂10000IU/瓶", "drugCount": 1}
        duration = calculate_duration(drug, parse_usage_text("每日1次 每次1万单位"))
        self.assertEqual(duration["calculation_total_quantity"], 10000.0)
        self.assertEqual(duration["calculation_total_unit"], "IU")
        self.assertEqual(duration["duration_days"], 1)

    def test_microgram_dose_converts_to_tablets(self):
        drug = {"drugName": "左甲状腺素钠片50μg*100片/盒", "drugCount": 1}
        duration = calculate_duration(drug, parse_usage_text("口服 一日1次 每次200微克"))
        self.assertEqual(duration["daily_quantity"], 4.0)
        self.assertEqual(duration["daily_quantity_unit"], "片")
        self.assertEqual(duration["duration_days"], 25)
        self.assertEqual(duration["duration_calculation_status"], "success")

    def test_bottle_dose_converts_to_milliliters(self):
        drug = {"drugName": "肠内营养混悬液(TPF)1.5kcal*500ml/瓶", "drugCount": 6}
        duration = calculate_duration(drug, parse_usage_text("口服 一日1次 每次1瓶"))
        self.assertEqual(duration["total_quantity"], 6)
        self.assertEqual(duration["total_unit"], "瓶")
        self.assertEqual(duration["calculation_total_quantity"], 3000.0)
        self.assertEqual(duration["daily_quantity"], 500.0)
        self.assertEqual(duration["daily_quantity_unit"], "ml")
        self.assertEqual(duration["duration_days"], 6)
        self.assertEqual(duration["duration_calculation_status"], "success")

    def test_arabic_one_day_frequency(self):
        usage = parse_usage_text("鼻饲 1日1次 每次200ml")
        self.assertEqual(usage["usage_method"], "鼻饲")
        self.assertEqual(usage["times_per_day"], 1.0)

    def test_chinese_one_day_frequency(self):
        self.assertEqual(parse_usage_text("口服 一天一次 每次1片")["times_per_day"], 1.0)
        self.assertEqual(parse_usage_text("口服 一天两次 每次1片")["times_per_day"], 2.0)

    def test_every_night_normalizes_to_once_daily(self):
        usage = parse_usage_text("口服 每晚1次 每次2粒")
        self.assertEqual(usage["frequency_text"], "一日1次")
        self.assertEqual(usage["times_per_day"], 1.0)

    def test_alternate_day_multiple_times(self):
        usage = parse_usage_text("吸入 隔日2次 每次50μg")
        self.assertEqual(usage["frequency_text"], "隔日2次")
        self.assertEqual(usage["times_per_day"], 1.0)

    def test_every_two_weeks_frequency(self):
        drug = {"drugName": "考格列汀片5mg*4片/板/盒", "drugCount": 1}
        usage = parse_usage_text("口服 2周1次 每次2片")
        self.assertEqual(usage["frequency_text"], "2周1次")
        duration = calculate_duration(drug, usage)
        self.assertEqual(duration["duration_days"], 28)
        self.assertEqual(duration["duration_calculation_status"], "success")

    def test_package_mass_and_concentration_formats(self):
        cases = (
            ("某软膏250g/罐", 2, 500.0, "g"),
            ("某注射液2.4ml:20mg/支/盒", 1, 20.0, "mg"),
            ("某注射液1.34mg/ml*1.5ml/支/盒", 1, 2.01, "mg"),
            ("某注射液1.5ml(1.34mg/ml)/支/盒", 1, 2.01, "mg"),
            ("某乳膏10g:10mg", 1, 10.0, "g"),
            ("某乳膏(10g:10mg)20g/支/盒", 1, 20.0, "g"),
            ("某喷雾剂每瓶200揿", 1, 200.0, "揿"),
        )
        for drug_name, count, expected_quantity, expected_unit in cases:
            with self.subTest(drug_name=drug_name):
                package = parse_package_quantity(drug_name, "", count)
                self.assertAlmostEqual(package["calculation_total_quantity"], expected_quantity)
                self.assertEqual(package["calculation_total_unit"], expected_unit)

    def test_container_total_strength_supports_tirzepatide_duration(self):
        drug = {
            "drugName": "替尔泊肽注射液",
            "packingSpec": "2.4ml:10mg/支*1支",
            "drugCount": 1,
        }
        package = parse_package_quantity(
            drug["drugName"], drug["packingSpec"], drug["drugCount"]
        )
        self.assertEqual(package["calculation_total_quantity"], 10)
        self.assertEqual(package["calculation_total_unit"], "mg")
        duration = calculate_duration(drug, parse_usage_text("皮下注射每周一次一次2.5mg"))
        self.assertEqual(duration["duration_days"], 28)

    def test_nested_discrete_package_uses_inner_administration_unit(self):
        drug = {
            "drugName": "摩罗丹(浓缩丸)",
            "packingSpec": "0.115g*16丸*18袋/盒",
            "drugCount": 1,
        }
        package = parse_package_quantity(
            drug["drugName"], drug["packingSpec"], drug["drugCount"]
        )
        self.assertEqual(package["calculation_total_quantity"], 288)
        self.assertEqual(package["calculation_total_unit"], "丸")
        duration = calculate_duration(drug, parse_usage_text("口服一日3次每次16丸"))
        self.assertEqual(duration["duration_days"], 6)

    def test_container_count_package_formats(self):
        cases = (
            ("开塞露每支20ml,每盒2支", 2.0, "支"),
            ("开塞露20*20毫升/支/盒", 20.0, "支"),
        )
        for drug_name, expected_quantity, expected_unit in cases:
            with self.subTest(drug_name=drug_name):
                package = parse_package_quantity(drug_name, "", 1)
                self.assertEqual(package["calculation_total_quantity"], expected_quantity)
                self.assertEqual(package["calculation_total_unit"], expected_unit)

    def test_shorthand_package_counts_use_drug_dosage_form(self):
        capsule = parse_package_quantity("新复方芦荟胶囊0.43g*30s", "", 1)
        tablet = parse_package_quantity("上清片0.31g*20T", "", 1)
        blistered = parse_package_quantity("某胶囊12s*2板/盒", "", 1)
        self.assertEqual((capsule["calculation_total_quantity"], capsule["calculation_total_unit"]), (30.0, "粒"))
        self.assertEqual((tablet["calculation_total_quantity"], tablet["calculation_total_unit"]), (20.0, "片"))
        self.assertEqual((blistered["calculation_total_quantity"], blistered["calculation_total_unit"]), (24.0, "粒"))

    def test_multi_component_package_requires_review(self):
        package = parse_package_quantity("云南白药气雾剂85g+30g", "", 1)
        self.assertIsNone(package["calculation_total_quantity"])

    def test_unlabelled_solid_multiplier_uses_name_suffix(self):
        package = parse_package_quantity("某药片0.5g*20", "", 2)
        self.assertEqual(package["unit_count_per_box"], 20.0)
        self.assertEqual(package["inferred_count_unit"], "片")
        self.assertEqual(package["calculation_total_quantity"], 40.0)
        self.assertEqual(package["calculation_total_unit"], "片")

    def test_unlabelled_liquid_multiplier_keeps_total_volume(self):
        package = parse_package_quantity("某液20ml*20", "", 2)
        self.assertEqual(package["unit_count_per_box"], 20.0)
        self.assertEqual(package["calculation_total_quantity"], 800.0)
        self.assertEqual(package["calculation_total_unit"], "ml")

    def test_unlabelled_solid_unit_conflict_requires_review(self):
        duration = calculate_duration(
            {"drugName": "某药片0.5g*20", "drugCount": 1},
            parse_usage_text("每日1次 每次1粒"),
        )
        self.assertEqual(duration["duration_calculation_status"], "needs_review")
        self.assertEqual(duration["duration_calculation_reason"], "drug_form_and_ocr_unit_conflict")

    def test_multiple_times_in_n_days(self):
        usage = parse_usage_text("口服 3日2次 每次1片")
        self.assertEqual(usage["frequency_text"], "3日2次")
        self.assertAlmostEqual(usage["times_per_day"], 2 / 3, places=6)

    def test_multiple_times_in_n_weeks(self):
        usage = parse_usage_text("皮下注射 2周3次 每次1支")
        self.assertEqual(usage["frequency_text"], "2周3次")
        self.assertAlmostEqual(usage["times_per_day"], 3 / 14, places=6)

    def test_same_drug_different_prices_is_merged(self):
        merged = merge_same_drugs(
            [
                {
                    "drugName": "[能全力]肠内营养混悬液(TPF)1.5kcal*500ml/瓶",
                    "drugCount": 6,
                    "drugPrice": 10,
                },
                {
                    "drugName": "[能全力]肠内营养混悬液(TPF)1.5kcal*500ml/瓶",
                    "drugCount": 3,
                    "drugPrice": 12,
                },
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["drugCount"], 9)
        self.assertEqual(merged[0]["source_drug_counts"], [6, 3])
        self.assertTrue(merged[0]["merged_same_drug"])
        self.assertNotIn("drugPrice", merged[0])

    def test_upload_quantity_keeps_purchase_unit(self):
        drug = {
            "drugName": "[格华止]盐酸二甲双胍缓释片0.5g*30片/盒",
            "drugCount": 3,
        }
        enriched = dict(drug)
        enriched["drcode"] = "47752679122"
        enriched.update(calculate_duration(drug, parse_usage_text("餐时口服 一日1次 每次1片")))
        medicine = build_upload_medicine_info([enriched])[0]
        self.assertEqual(medicine["drug_name"], "盐酸二甲双胍缓释片")
        self.assertEqual(medicine["specification"], "0.5g*30片/盒")
        self.assertEqual(medicine["total_quantity"], 3)
        self.assertEqual(medicine["total_unit"], "盒")
        self.assertEqual(medicine["duration_days"], 90)
        self.assertEqual(medicine["drcode"], "47752679122")

    def test_multi_drug_matches_by_name_and_position(self):
        drugs = [
            {"drugName": "阿司匹林肠溶片100mg*30片/盒"},
            {"drugName": "参松养心胶囊0.4g*36粒/盒"},
        ]
        candidates = [
            {"text": "口服一日1次每次1片", "score": 0.99, "y": 20},
            {"text": "口服一日3次每次2粒", "score": 0.99, "y": 50},
        ]
        lines = [
            {"text": "阿司匹林肠溶片", "score": 0.99, "y": 10},
            candidates[0],
            {"text": "参松养心胶囊", "score": 0.99, "y": 40},
            candidates[1],
        ]
        assignments = _assign_usage_candidates(drugs, candidates, lines)
        self.assertEqual(assignments[0][0]["text"], candidates[0]["text"])
        self.assertEqual(assignments[1][0]["text"], candidates[1]["text"])
        self.assertEqual(assignments[0][1], "drug_name_and_position")

    def test_multi_drug_uses_order_only_when_counts_match(self):
        drugs = [{"drugName": "药甲10片/盒"}, {"drugName": "药乙10片/盒"}]
        candidates = [
            {"text": "口服一日1次每次1片", "score": 0.99, "y": 20},
            {"text": "口服一日2次每次1片", "score": 0.99, "y": 40},
        ]
        assignments = _assign_usage_candidates(drugs, candidates, candidates)
        self.assertTrue(all(item[1] == "vertical_order_fallback" for item in assignments))

    def test_single_drug_with_multiple_usages_requires_review(self):
        assignments = _assign_usage_candidates(
            [{"drugName": "药甲10片/盒"}],
            [
                {"text": "口服一日1次每次1片", "score": 0.99, "y": 20},
                {"text": "口服一日2次每次1片", "score": 0.99, "y": 40},
            ],
            [],
        )
        self.assertIsNone(assignments[0][0])
        self.assertEqual(assignments[0][1], "multiple_usage_candidates_for_single_drug")

    def test_fallback_merges_primary_and_fallback_multi_drug_results(self):
        engine = object.__new__(PaddleUsageOCR)
        engine.min_confidence = 0.75
        engine.crop_top = 0.30
        engine.crop_bottom = 0.75
        engine.single_crop_top = 0.35
        engine.single_crop_bottom = 0.60
        engine.multi_crop_top = 0.35
        engine.multi_crop_bottom = 0.70

        primary_usage = {
            "text": "口服一日1次一次1片",
            "score": 0.99,
            "x": 0,
            "y": 120,
            "width": 100,
            "height": 20,
            "ocr_pass": "primary",
        }
        fallback_usage = {
            "text": "口服一日2次一次2粒",
            "score": 0.98,
            "x": 0,
            "y": 220,
            "width": 100,
            "height": 20,
            "ocr_pass": "fallback",
        }
        preprocessing = {
            "mode": "grayscale",
            "blue_mask_used": False,
            "grayscale_used": True,
            "original_anchor_ocr_used": False,
        }

        def fake_read(_path, _top, _bottom, _count, pass_name):
            if pass_name == "primary":
                return (
                    [primary_usage],
                    3,
                    [
                        {"text": "药甲", "score": 0.99, "y": 100, "height": 20},
                        primary_usage,
                        {"text": "药乙", "score": 0.99, "y": 200, "height": 20},
                    ],
                    dict(preprocessing),
                )
            return (
                [fallback_usage],
                2,
                [
                    {"text": "药乙", "score": 0.99, "y": 200, "height": 20},
                    fallback_usage,
                ],
                dict(preprocessing),
            )

        engine._read_candidates = fake_read
        drugs, metrics = engine.enrich(
            [
                {"drugName": "药甲10片/盒", "drugCount": 1},
                {"drugName": "药乙20粒/盒", "drugCount": 1},
            ],
            Path("unused.png"),
        )

        self.assertEqual([drug["duration_days"] for drug in drugs], [10, 5])
        self.assertEqual(metrics["usage_found"], 2)
        self.assertEqual(metrics["crop_mode"], "multi_fallback_merged")
        self.assertTrue(metrics["fallback_result_reuse_used"])
        self.assertEqual(metrics["primary_candidate_count"], 1)
        self.assertEqual(metrics["fallback_candidate_count"], 1)
        self.assertEqual(metrics["combined_candidate_count"], 2)

    def test_usage_failure_type_distinguishes_no_ocr_text(self):
        engine = object.__new__(PaddleUsageOCR)
        engine.min_confidence = 0.75
        engine.crop_top = engine.single_crop_top = engine.multi_crop_top = 0.35
        engine.crop_bottom = engine.single_crop_bottom = engine.multi_crop_bottom = 0.70
        engine._read_candidates = (
            lambda *_args: (
                [],
                0,
                [],
                {
                    "mode": "blue_mask",
                    "blue_mask_used": True,
                    "grayscale_used": False,
                    "original_anchor_ocr_used": False,
                },
            )
        )

        drugs, metrics = engine.enrich(
            [{"drugName": "药甲10片/盒", "drugCount": 1}],
            Path("unused.png"),
        )

        self.assertEqual(drugs[0]["usage_failure_type"], "ocr_no_text")
        self.assertEqual(metrics["failure_counts"]["ocr_no_text"], 1)

    def test_blue_mask_unit_conflict_retries_original_and_selects_success(self):
        engine = object.__new__(PaddleUsageOCR)
        engine.min_confidence = 0.75
        engine.usage_preprocessing_mode = "blue_mask"
        engine.crop_top = engine.single_crop_top = engine.multi_crop_top = 0.35
        engine.crop_bottom = engine.single_crop_bottom = engine.multi_crop_bottom = 0.70

        def candidate(text, pass_name):
            return {
                "text": text,
                "score": 0.99,
                "x": 0,
                "y": 100,
                "width": 100,
                "height": 20,
                "ocr_pass": pass_name,
            }

        def fake_read(
            _path,
            _top,
            _bottom,
            _count,
            pass_name,
            preprocessing_mode=None,
        ):
            text = "吸入给药一日4次每次2掀" if preprocessing_mode == "original" else "吸入给药一日4次每次2瓶"
            item = candidate(text, pass_name)
            return (
                [item],
                1,
                [item],
                {
                    "mode": preprocessing_mode or "blue_mask",
                    "blue_mask_used": preprocessing_mode != "original",
                    "grayscale_used": False,
                    "original_anchor_ocr_used": preprocessing_mode == "original",
                },
            )

        engine._read_candidates = fake_read
        drugs, metrics = engine.enrich(
            [
                {
                    "drugName": "硫酸沙丁胺醇吸入气雾剂100µg*200揿/盒",
                    "drugCount": 2,
                }
            ],
            Path("unused.png"),
        )

        self.assertEqual(drugs[0]["dose_unit"], "揿")
        self.assertEqual(drugs[0]["duration_days"], 50)
        self.assertEqual(drugs[0]["usage_ocr_selected_pass"], "original_retry")
        self.assertTrue(metrics["usage_original_retry_used"])
        self.assertEqual(metrics["usage_original_retry_selected"], 1)

    def test_original_retry_does_not_replace_another_failed_result(self):
        engine = object.__new__(PaddleUsageOCR)
        engine.min_confidence = 0.75
        engine.usage_preprocessing_mode = "blue_mask"
        engine.crop_top = engine.single_crop_top = engine.multi_crop_top = 0.35
        engine.crop_bottom = engine.single_crop_bottom = engine.multi_crop_bottom = 0.70

        def fake_read(
            _path,
            _top,
            _bottom,
            _count,
            pass_name,
            preprocessing_mode=None,
        ):
            text = "吸入给药一日4次每次2瓶"
            item = {
                "text": text,
                "score": 0.99,
                "x": 0,
                "y": 100,
                "width": 100,
                "height": 20,
                "ocr_pass": pass_name,
            }
            return (
                [item],
                1,
                [item],
                {
                    "mode": preprocessing_mode or "blue_mask",
                    "blue_mask_used": preprocessing_mode != "original",
                    "grayscale_used": False,
                    "original_anchor_ocr_used": preprocessing_mode == "original",
                },
            )

        engine._read_candidates = fake_read
        drugs, metrics = engine.enrich(
            [
                {
                    "drugName": "硫酸沙丁胺醇吸入气雾剂100µg*200揿/盒",
                    "drugCount": 1,
                }
            ],
            Path("unused.png"),
        )

        self.assertEqual(drugs[0]["duration_calculation_status"], "needs_review")
        self.assertEqual(drugs[0]["usage_ocr_selected_pass"], "primary")
        self.assertEqual(metrics["usage_original_retry_selected"], 0)


if __name__ == "__main__":
    unittest.main()

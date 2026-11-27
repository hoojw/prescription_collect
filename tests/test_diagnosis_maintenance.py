import json
import pathlib
import tempfile
import unittest

from diagnosis_maintenance import (
    DiagnosisItem,
    extract_names_from_xlsx,
    load_maintained_names,
    replace_maintained_names,
    split_diagnosis_name,
    unmaintained_diagnoses,
    write_unmaintained_report,
    write_unmaintained_xlsx,
)


class DiagnosisMaintenanceTests(unittest.TestCase):
    def test_splits_only_english_commas(self):
        self.assertEqual(
            ["手术后恢复期", "营养不良", "营养缺乏"],
            split_diagnosis_name("手术后恢复期,营养不良,营养缺乏"),
        )
        self.assertEqual(
            ["双相情感障碍，目前为伴有精神病性症状的躁狂发作"],
            split_diagnosis_name("双相情感障碍，目前为伴有精神病性症状的躁狂发作"),
        )

    def test_imports_names_from_xlsx_first_named_column(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = pathlib.Path(temp_name) / "maintained.xlsx"
            write_unmaintained_xlsx(
                path,
                [
                    DiagnosisItem("手术后恢复期", ""),
                    DiagnosisItem("营养不良", ""),
                ],
            )

            names = extract_names_from_xlsx(path)

            self.assertEqual(["手术后恢复期", "营养不良"], names)

    def test_reports_unmaintained_names_after_splitting(self):
        payload = [
            {
                "hospital": "h1",
                "data_list": [
                    {
                        "diagnosis": {
                            "disease_name": "手术后恢复期,营养不良,营养缺乏",
                            "disease_code": "",
                        }
                    },
                    {
                        "diagnosis": {
                            "disease_name": "双相情感障碍，目前为伴有精神病性症状的躁狂发作",
                            "disease_code": "F31.200x001",
                        }
                    },
                ],
            }
        ]

        result = unmaintained_diagnoses(payload, {"手术后恢复期", "营养不良"})

        self.assertEqual(
            [
                DiagnosisItem("双相情感障碍，目前为伴有精神病性症状的躁狂发作", "F31.200x001"),
                DiagnosisItem("营养缺乏", ""),
            ],
            result,
        )

    def test_writes_report_from_sqlite_reference(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = pathlib.Path(temp_name)
            db = temp / "diagnosis.sqlite3"
            upload = temp / "prescription_upload.json"
            output_dir = temp / "diagnos"
            replace_maintained_names(db, ["手术后恢复期"])
            upload.write_text(
                json.dumps(
                    [
                        {
                            "hospital": "h1",
                            "data_list": [
                                {
                                    "diagnosis": {
                                        "disease_name": "手术后恢复期,营养不良",
                                        "disease_code": "",
                                    }
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report_path, count, added = write_unmaintained_report(
                upload,
                output_dir=output_dir,
                db_path=db,
                store_key="mt-1",
                run_id="20260804_120000",
            )

            self.assertEqual(1, count)
            self.assertEqual(1, added)
            self.assertTrue(report_path.is_file())
            self.assertEqual(
                "20260804_120000_unmaintained_diagnosis.xlsx",
                report_path.name,
            )
            self.assertEqual({"手术后恢复期", "营养不良"}, load_maintained_names(db))

            second_report_path, second_count, second_added = write_unmaintained_report(
                upload,
                output_dir=output_dir,
                db_path=db,
                store_key="mt-1",
                run_id="20260804_120001",
            )

            self.assertEqual("20260804_120001_unmaintained_diagnosis.xlsx", second_report_path.name)
            self.assertEqual(0, second_count)
            self.assertEqual(0, second_added)


if __name__ == "__main__":
    unittest.main()

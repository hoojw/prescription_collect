import json
import pathlib
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

from export_diagnosis_list import (
    collect_diagnoses,
    discover_sources_from_configs,
    write_xlsx,
)


class DiagnosisExportTests(unittest.TestCase):
    def test_discovers_successful_production_uploads_only(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = pathlib.Path(temp_name)
            config_dir = temp / "configs"
            config_dir.mkdir()
            prod_store = temp / "stores" / "mt-1"
            test_store = temp / "stores" / "jd-1"
            failed_store = temp / "stores" / "mt-2"

            self._write_config(
                config_dir / "prod.json",
                prod_store,
                "https://api.yizhujk.com/public/prescriptionUpload/importJson",
                True,
            )
            self._write_config(
                config_dir / "test.json",
                test_store,
                "https://testapi.yizhujk.com/public/prescriptionUpload/importJson",
                True,
            )
            self._write_config(
                config_dir / "disabled.json",
                failed_store,
                "https://api.yizhujk.com/public/prescriptionUpload/importJson",
                False,
            )

            self._write_run(prod_store, "20260720_100000", "success")
            self._write_run(prod_store, "20260720_110000", "failed")
            self._write_run(test_store, "20260720_120000", "success")
            self._write_run(failed_store, "20260720_130000", "success")

            sources = discover_sources_from_configs(str(config_dir / "*.json"))

            self.assertEqual(1, len(sources))
            self.assertEqual(
                prod_store / "runs" / "20260720_100000" / "prescription_upload.json",
                sources[0].path,
            )

    def test_collects_unique_diagnosis_pairs(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = pathlib.Path(temp_name) / "prescription_upload.json"
            payload = [
                {
                    "hospital": "h1",
                    "data_list": [
                        {
                            "diagnosis": {
                                "disease_name": "上呼吸道感染",
                                "disease_code": "J06.900",
                            }
                        },
                        {
                            "diagnosis": {
                                "disease_name": "上呼吸道感染",
                                "disease_code": "J06.900",
                            }
                        },
                        {
                            "diagnosis": {
                                "disease_name": "咳嗽",
                                "disease_code": "",
                            }
                        },
                    ],
                }
            ]
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            result = collect_diagnoses([type("Source", (), {"path": path})()])

            self.assertEqual([("上呼吸道感染", "J06.900"), ("咳嗽", "")], result)

    def test_writes_xlsx_with_two_columns(self):
        with tempfile.TemporaryDirectory() as temp_name:
            output = pathlib.Path(temp_name) / "diagnosis.xlsx"

            write_xlsx(output, [("上呼吸道感染", "J06.900")])

            self.assertTrue(output.is_file())
            rows = self._read_sheet_rows(output)
            self.assertEqual(
                [["disease_name", "disease_code"], ["上呼吸道感染", "J06.900"]],
                rows,
            )

    def _write_config(
        self,
        path: pathlib.Path,
        output_dir: pathlib.Path,
        api_url: str,
        api_submit_enabled: bool,
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "output_dir": str(output_dir),
                    "api_submit_enabled": api_submit_enabled,
                    "api_url": api_url,
                }
            ),
            encoding="utf-8",
        )

    def _write_run(self, output_dir: pathlib.Path, run_id: str, api_status: str) -> None:
        run_dir = output_dir / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "api_status": api_status,
                    "upload_json_file": f"runs/{run_id}/prescription_upload.json",
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "prescription_upload.json").write_text(
            json.dumps([{"hospital": "h1", "data_list": []}]),
            encoding="utf-8",
        )

    def _read_sheet_rows(self, path: pathlib.Path) -> list[list[str]]:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("xl/worksheets/sheet1.xml")
        root = ET.fromstring(xml)
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        rows: list[list[str]] = []
        for row in root.findall(".//x:sheetData/x:row", ns):
            values: list[str] = []
            for cell in row.findall("x:c", ns):
                text = cell.find("x:is/x:t", ns)
                values.append("" if text is None else text.text or "")
            rows.append(values)
        return rows


if __name__ == "__main__":
    unittest.main()

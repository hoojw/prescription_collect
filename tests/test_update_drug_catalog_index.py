import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.update_drug_catalog_index import merge_index


class UpdateDrugCatalogIndexTests(unittest.TestCase):
    def test_merge_replaces_by_id_and_preserves_other_entries(self):
        base = {
            "schema_version": 1,
            "entries": [
                {"id": "keep", "n": "keep"},
                {"id": "replace", "n": "old"},
            ],
        }
        rows = [
            {
                "药品ID": "replace",
                "药品名称": "整蛋白型肠内营养剂(粉剂)",
                "规格": "320g",
                "使用频度": "一日1-4次",
                "每次剂量": "适量",
            },
            {
                "药品ID": "add",
                "药品名称": "整蛋白型肠内营养剂(粉剂)",
                "规格": "320g/听",
                "使用频度": "一日1-4次",
                "每次剂量": "适量",
            },
        ]
        output, summary = merge_index(base, rows)
        self.assertEqual(summary["replaced"], 1)
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["output_entries"], 3)
        entries = {entry["id"]: entry for entry in output["entries"]}
        self.assertEqual(entries["keep"]["n"], "keep")
        self.assertEqual(entries["replace"]["q"], ["适量"])
        self.assertEqual(entries["add"]["q"], ["适量"])


if __name__ == "__main__":
    unittest.main()

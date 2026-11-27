import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector_progress import report_store_progress


class CollectorProgressTests(unittest.TestCase):
    def test_reports_initial_interval_and_final_progress(self):
        messages: list[str] = []
        self.assertTrue(
            report_store_progress(
                messages.append,
                store="MEITUAN:1",
                processed=0,
                total=23,
                saved=0,
                review=0,
                errors=0,
            )
        )
        self.assertFalse(
            report_store_progress(
                messages.append,
                store="MEITUAN:1",
                processed=9,
                total=23,
                saved=8,
                review=1,
                errors=0,
            )
        )
        self.assertTrue(
            report_store_progress(
                messages.append,
                store="MEITUAN:1",
                processed=10,
                total=23,
                saved=9,
                review=1,
                errors=0,
            )
        )
        self.assertTrue(
            report_store_progress(
                messages.append,
                store="MEITUAN:1",
                processed=23,
                total=23,
                saved=22,
                review=1,
                errors=0,
                force=True,
            )
        )
        self.assertIn("processed=23/23 percent=100.0", messages[-1])


if __name__ == "__main__":
    unittest.main()


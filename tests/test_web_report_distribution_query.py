from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from web_report import service
from web_report.tabs.distribution import build_distribution_compact


def _table():
    return SimpleNamespace(
        source="SRC",
        item_columns=["ITEM_A", "ITEM_B"],
        units={"ITEM_A": "V", "ITEM_B": "uA"},
        lolim={"ITEM_A": 0.0, "ITEM_B": 10.0},
        hilim={"ITEM_A": 3.0, "ITEM_B": 40.0},
        data=pd.DataFrame({
            "ITEM_A": [1.0, 2.0, 2.0, 3.0],
            "ITEM_B": [10.0, 20.0, 30.0, 40.0],
        }),
    )


class DistributionQueryTest(unittest.TestCase):
    def test_distribution_query_matches_full_payload(self):
        table = _table()
        full = build_distribution_compact([table], table.item_columns)
        with patch.object(
            service, "_load_tables",
            return_value=({"mode": "Normal"}, [table], {"selected_items": []}),
        ):
            result = service.get_distribution_items(
                "sid", ["ITEM_B", "ITEM_A", "ITEM_B", "UNKNOWN"],
                report_db=object(), upload_root=Path("."),
            )

        self.assertEqual(list(result["items"]), ["ITEM_B", "ITEM_A"])
        self.assertEqual(result["items"]["ITEM_A"], full["items"]["ITEM_A"])
        self.assertEqual(result["items"]["ITEM_B"], full["items"]["ITEM_B"])
        self.assertEqual(result["items"]["ITEM_A"]["sources"]["SRC"], {
            "x": [1.0, 2.0, 3.0],
            "y": [25.0, 75.0, 100.0],
        })

    def test_distribution_query_respects_selected_items(self):
        table = _table()
        with patch.object(
            service, "_load_tables",
            return_value=(
                {"mode": "Normal"}, [table], {"selected_items": ["ITEM_A"]}),
        ):
            result = service.get_distribution_items(
                "sid", ["ITEM_A", "ITEM_B"], report_db=object(), upload_root=Path("."))

        self.assertEqual(list(result["items"]), ["ITEM_A"])

    def test_distribution_query_rejects_invalid_subjects(self):
        for subjects in (None, "ITEM_A", ["ITEM_A", 1], [""]):
            with self.subTest(subjects=subjects), self.assertRaises(ValueError):
                service._normalize_distribution_subjects(subjects)

    def test_distribution_query_accepts_70_and_rejects_71(self):
        subjects = [f"ITEM_{i}" for i in range(70)]
        self.assertEqual(service._normalize_distribution_subjects(subjects), subjects)
        with self.assertRaises(ValueError):
            service._normalize_distribution_subjects(subjects + ["ITEM_70"])


if __name__ == "__main__":
    unittest.main()

"""Issue Table tab placeholder."""
from __future__ import annotations


def build_issue_table_rows(tables, yield_rows=None, cpk_rows=None):
    rows = []
    for row in yield_rows or []:
        bin_value = row.get("bin")
        if str(bin_value).strip() == "1":
            continue
        item = row.get("Item")
        if not item:
            continue
        rows.append({
            "Category": "Yield",
            "Step": row.get("step", ""),
            "Bin": bin_value,
            "TNO": row.get("TNO", ""),
            "Item": item,
            "avg": row.get("avg"),
            "Distribution": "",
            "comment": "",
            "개발 1차 comment": "",
            "PTE 2차 comment": "",
            "개발 2차 comment": "",
        })
    return rows

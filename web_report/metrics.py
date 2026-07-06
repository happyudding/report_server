"""Web report payload orchestration from 7-meta honeyform tables."""
from __future__ import annotations

from .tabs.common import num
from .tabs.cpk import build_cpk_rows
from .tabs.yield_tab import build_yield_rows, fail_counts_by_source


def build_report_payload(tables, selected_items=None, sheets=None) -> dict:
    selected_set = {str(v) for v in (selected_items or []) if str(v)}
    if selected_set:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected_set]

    sources = [{"name": t.source, "file_name": t.file_name} for t in tables]
    all_items = sorted({item for table in tables for item in table.item_columns})
    fail_counts = {table.source: fail_counts_by_source(table) for table in tables}
    yield_rows = build_yield_rows(tables, fail_counts)
    cpk_rows = build_cpk_rows(tables, all_items)

    return {
        "sources": sources,
        "sheets": {
            "Yield": yield_rows,
            "CPK": cpk_rows,
        },
        "selected_items": sorted(selected_set),
        "requested_sheets": list(sheets or []),
    }


def summary_rows_for_db(yield_rows):
    out = []
    for row in yield_rows:
        bin_value = row.get("bin")
        try:
            bin_number = int(float(bin_value))
        except (TypeError, ValueError):
            bin_number = None
        out.append({
            "item_name": str(bin_value),
            "bin_number": bin_number,
            "yield_percent": num(row.get("avg")),
            "fail_count": int(row.get("count") or 0),
            "cpk_val": None,
            "mean_val": num(row.get("avg")),
            "stdev_val": None,
            "lsl": None,
            "usl": None,
            "unit": None,
        })
    return out

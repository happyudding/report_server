"""Web report payload orchestration from 7-meta honeyform tables."""
from __future__ import annotations

from .tabs.cpk import build_cpk_rows
from .tabs.distribution import build_distribution_index, build_distribution_rows
from .tabs.histogram import build_histogram_rows
from .tabs.issue_table import build_issue_bin_summary, build_issue_table_rows
from .tabs.Map_analysis import build_map_analysis_rows
from .tabs.raw_data import build_raw_data_rows
from .tabs.summary import build_summary_rows
from .tabs.trim_analysis import build_trim_analysis_rows
from .tabs.yield_tab import build_yield_rows, fail_bin_ranking, fail_counts_by_source, yield_overview


def build_report_payload(tables, selected_items=None, sheets=None, etc_items=None,
                         issue_comments=None) -> dict:
    selected_set = {str(v) for v in (selected_items or []) if str(v)}
    if selected_set:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected_set]

    sources = [{"name": t.source, "file_name": t.file_name} for t in tables]
    all_items = sorted({c for t in tables for c in t.item_columns})
    fail_counts = {table.source: fail_counts_by_source(table) for table in tables}
    yield_rows = build_yield_rows(tables, fail_counts)
    cpk_rows = build_cpk_rows(tables, all_items)

    return {
        "sources": sources,
        "yield_summary": yield_overview(tables, yield_rows),
        "issue_bin_summary": build_issue_bin_summary(yield_rows),
        "sheets": {
            "Summary": build_summary_rows(tables),
            "Raw Data": build_raw_data_rows(tables),
            "Yield": yield_rows,
            "CPK": cpk_rows,
            "Issue Table": build_issue_table_rows(tables, yield_rows, cpk_rows, etc_items=etc_items,
                                              issue_comments=issue_comments),
            "Distribution": build_distribution_rows(tables, all_items),
            "Trim Analysis": build_trim_analysis_rows(tables),
            "Histogram": build_histogram_rows(tables),
            "Map Analysis": build_map_analysis_rows(tables),
            "Fail Bin": fail_bin_ranking(yield_rows),
        },
        "distribution_index": build_distribution_index(tables, cpk_rows),
        "selected_items": sorted(selected_set),
        "requested_sheets": list(sheets or []),
    }

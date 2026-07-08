"""Web report payload orchestration from 7-meta honeyform tables."""
from __future__ import annotations

from .tabs.cpk import build_cpk_rows
from .tabs.distribution import build_distribution_index
from .tabs.histogram import build_histogram_rows
from .tabs.issue_table import build_issue_bin_summary, build_issue_table_rows
from .tabs.Map_analysis import build_map_analysis_rows
from .tabs.raw_data import build_raw_data_rows
from .tabs.summary import build_summary_rows
from .tabs.trim_analysis import build_trim_analysis_rows
from .tabs.yield_tab import (build_yield_bin_groups, build_yield_rows, fail_bin_ranking,
                             fail_counts_by_source, yield_overview)


def build_report_payload(tables, selected_items=None, sheets=None, etc_items=None,
                         issue_comments=None) -> dict:
    """Distribution ECDF(대용량)는 payload 에 싣지 않고 항상 지연 로드한다
    (distribution_deferred=True, sheets["Distribution"]=[]) — 프런트가 별도 lazy 엔드포인트
    (GET .../web_report/distribution)로 받아간다. distribution_index(경량)는 항상 포함."""
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
        "yield_bin_groups": build_yield_bin_groups(yield_rows),
        "sheets": {
            "Summary": build_summary_rows(tables),
            "Raw Data": build_raw_data_rows(tables),
            "Yield": yield_rows,
            "CPK": cpk_rows,
            "Issue Table": build_issue_table_rows(tables, yield_rows, cpk_rows, etc_items=etc_items,
                                              issue_comments=issue_comments),
            "Distribution": [],
            "Trim Analysis": build_trim_analysis_rows(tables),
            "Histogram": build_histogram_rows(tables),
            "Map Analysis": build_map_analysis_rows(tables),
            "Fail Bin": fail_bin_ranking(yield_rows),
        },
        "distribution_deferred": True,
        "distribution_index": build_distribution_index(tables, cpk_rows),
        "selected_items": sorted(selected_set),
        "requested_sheets": list(sheets or []),
    }

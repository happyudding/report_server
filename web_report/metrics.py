"""Web report payload orchestration from 7-meta honeyform tables."""
from __future__ import annotations

from .tabs.cpk import build_cpk_rows
from .tabs.distribution import build_distribution_index
from .tabs.issue_table import build_issue_bin_summary, build_issue_table_rows
from .tabs.Map_analysis import build_map_analysis_rows
from .tabs.raw_data import build_raw_data_rows
from .tabs.summary import build_summary_rows
from .tabs.yield_tab import (build_yield_bin_groups, build_yield_rows, fail_bin_ranking,
                             fail_counts_by_source, yield_overview)


def build_report_payload(tables, selected_items=None, sheets=None, etc_items=None,
                         issue_comments=None, summary_engr=None, product_type="", product="",
                         mode="Normal", dist_colors=None) -> dict:
    """Distribution ECDF(대용량)는 payload 에 싣지 않고 항상 지연 로드한다
    (distribution_deferred=True, sheets["Distribution"]=[]) — 프런트가 별도 lazy 엔드포인트
    (GET .../web_report/distribution)로 받아간다. distribution_index(경량)는 항상 포함.

    mode: 세션 분석 모드(Normal/Compare/DUT/Commonality). Normal/DUT 는 기존 multi-source
    렌더를 그대로 쓰고(DUT 는 source 가 이미 DUT별로 분할돼 업로드됨), Compare 는 추가
    비교 시트(Compare Stats/Compare Bin/Common Map)를 얹는다. Commonality 의 chip 강조는
    프런트가 기존 distribution/scatter 데이터로 처리하므로 payload 분기는 없다."""
    selected_set = {str(v) for v in (selected_items or []) if str(v)}
    if selected_set:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected_set]

    sources = [{"name": t.source, "file_name": t.file_name} for t in tables]
    all_items = sorted({c for t in tables for c in t.item_columns})
    fail_counts = {table.source: fail_counts_by_source(table) for table in tables}
    yield_rows = build_yield_rows(tables, fail_counts)
    cpk_rows = build_cpk_rows(tables, all_items)

    sheets_out = {
        "Summary": build_summary_rows(tables),
        "Raw Data": build_raw_data_rows(tables),
        "Yield": yield_rows,
        "CPK": cpk_rows,
        "Issue Table": build_issue_table_rows(tables, yield_rows, cpk_rows, etc_items=etc_items,
                                          issue_comments=issue_comments),
        "Distribution": [],
        # Trim Analysis 는 항상 지연 로드 (GET .../web_report/trim_analysis) —
        # Distribution embed 폐지와 동일 관례. 프런트가 탭 진입 시 lazy fetch 한다.
        "Trim Analysis": [],
        "Map Analysis": build_map_analysis_rows(tables, product_type, product),
        "Fail Bin": fail_bin_ranking(yield_rows),
    }

    payload = {
        "mode": mode or "Normal",
        "sources": sources,
        "yield_summary": yield_overview(tables, yield_rows),
        "issue_bin_summary": build_issue_bin_summary(yield_rows),
        "yield_bin_groups": build_yield_bin_groups(yield_rows),
        "sheets": sheets_out,
        "distribution_deferred": True,
        "distribution_index": build_distribution_index(tables, cpk_rows),
        "selected_items": sorted(selected_set),
        "requested_sheets": list(sheets or []),
        # Summary 탭 Engr Comment(Yield/CPK/ETC 3칸) — manifest.summary_engr 에서 채운다.
        "summary_engr": dict(summary_engr or {}),
        # None → 색 미지정(legacy): 프런트가 기본 팔레트. list → source i 가 dist_colors[i] 색.
        "dist_colors": list(dist_colors) if dist_colors else None,
    }

    # Compare 모드: source 2개 이상일 때만 비교 분석을 얹는다 (단일 source 는 비교 대상 없음).
    if mode == "Compare" and len(tables) >= 2:
        from .tabs.compare import build_compare_payload
        payload["compare"] = build_compare_payload(tables, all_items, cpk_rows)

    return payload

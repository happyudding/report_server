"""Web report payload orchestration from 7-meta honeyform tables.

시트 구성은 tabs.TAB_REGISTRY(탭 레지스트리)가 단일 진실 — 여기는 공용 컨텍스트
(yield/cpk 등 1회 계산)를 조립해 레지스트리를 순회할 뿐, 개별 탭 이름을 모른다.
새 탭 추가 절차는 tabs/__init__.py 참조.
"""
from __future__ import annotations

from .tabs import TAB_REGISTRY, TabContext, build_cpk_rows
from .tabs.common import passfail_or_empty_items
from .tabs.distribution import build_distribution_index
from .tabs.issue_table import build_issue_bin_summary
from .tabs.yield_tab import (build_yield_bin_groups, build_yield_rows,
                             build_yield_step_groups, fail_counts_by_source,
                             yield_overview)


def build_report_payload(tables, selected_items=None, sheets=None, etc_items=None,
                         issue_comments=None, summary_engr=None, product_type="", product="",
                         mode="Normal", dist_colors=None, ai_comments=None) -> dict:
    """Distribution ECDF(대용량)는 payload 에 싣지 않고 항상 지연 로드한다
    (distribution_deferred=True, sheets["Distribution"]=[]) — 프런트가 별도 lazy 엔드포인트
    (GET .../web_report/distribution)로 받아간다. distribution_index(경량)는 항상 포함.

    mode: 세션 분석 모드(Normal/Compare/DUT/Commonality). Normal 은 기존 multi-source 렌더,
    DUT 는 source 가 이미 DUT별로 분할돼 있으나 **Map Analysis 만** 하나의 맵으로 병합한다
    (ctx.mode 로 build_map_analysis_rows 에 전달 — 나머지 탭은 DUT 비교 렌더). Compare 는 추가
    비교 시트(Compare Stats/Compare Bin/Common Map)를 얹는다. Commonality 의 chip 강조는
    프런트가 기존 distribution/scatter 데이터로 처리하므로 payload 분기는 없다."""
    selected_set = {str(v) for v in (selected_items or []) if str(v)}
    if selected_set:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected_set]

    sources = [{"name": t.source, "file_name": t.file_name} for t in tables]
    all_items = sorted({c for t in tables for c in t.item_columns})
    # unit 이 Pass/Fail 이거나 측정 data 가 전무한 항목은 cpk·distribution 계산에서 제외.
    excluded_items = passfail_or_empty_items(tables)
    stat_items = [i for i in all_items if i not in excluded_items]
    fail_counts = {table.source: fail_counts_by_source(table) for table in tables}
    yield_rows = build_yield_rows(tables, fail_counts)
    cpk_rows = build_cpk_rows(tables, stat_items)

    ctx = TabContext(
        tables=tables,
        all_items=all_items,
        fail_counts=fail_counts,
        yield_rows=yield_rows,
        cpk_rows=cpk_rows,
        etc_items=list(etc_items or []),
        issue_comments=dict(issue_comments or {}),
        product_type=product_type,
        product=product,
        mode=mode or "Normal",
        # None=컬럼 미표시. dict 전달은 ai_comment 옵션 세션의 콜드 빌드(service)만.
        ai_comments=ai_comments,
    )
    sheets_out = {spec.name: (spec.builder(ctx) if spec.builder else [])
                  for spec in TAB_REGISTRY}

    payload = {
        "mode": mode or "Normal",
        "sources": sources,
        "yield_summary": yield_overview(tables, yield_rows),
        "issue_bin_summary": build_issue_bin_summary(yield_rows),
        # yield_bin_groups: Bin 병합(전체 기준) 그룹 — Excel 내보내기가 사용(유지).
        "yield_bin_groups": build_yield_bin_groups(yield_rows),
        # yield_step_groups: STEP(P1/P2/P3) 별 분리 그룹 — Yield 탭 표시 전용(cascade 수율).
        "yield_step_groups": build_yield_step_groups(yield_rows, tables),
        "sheets": sheets_out,
        "distribution_deferred": True,
        "distribution_index": build_distribution_index(tables, cpk_rows, exclude=excluded_items),
        "selected_items": sorted(selected_set),
        "requested_sheets": list(sheets or []),
        # Summary 탭 Engr Comment(Yield/CPK/ETC 3칸) — 세션 편집 DB 에서 채운다.
        "summary_engr": dict(summary_engr or {}),
        # None → 색 미지정(legacy): 프런트가 기본 팔레트. list → source i 가 dist_colors[i] 색.
        "dist_colors": list(dist_colors) if dist_colors else None,
    }

    # Compare 모드: source 2개 이상일 때만 비교 분석을 얹는다 (단일 source 는 비교 대상 없음).
    if mode == "Compare" and len(tables) >= 2:
        from .tabs.compare import build_compare_payload
        payload["compare"] = build_compare_payload(tables, all_items, cpk_rows)

    return payload

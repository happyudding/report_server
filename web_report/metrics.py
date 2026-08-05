"""Web report payload orchestration from 7-meta honeyform tables.

시트 구성은 tabs.TAB_REGISTRY(탭 레지스트리)가 단일 진실 — 여기는 공용 컨텍스트
(yield/cpk 등 1회 계산)를 조립해 레지스트리를 순회할 뿐, 개별 탭 이름을 모른다.
새 탭 추가 절차는 tabs/__init__.py 참조.
"""
from __future__ import annotations

from . import build_log
from .tabs import TAB_REGISTRY, TabContext, build_cpk_rows
from .tabs.common import empty_items, finite_count_map, passfail_or_empty_items
from .tabs.distribution import build_distribution_index
from .tabs.issue_table import build_issue_bin_summary
from .tabs.yield_tab import (build_yield_bin_groups, build_yield_corner_groups,
                             build_yield_rows, build_yield_step_groups,
                             fail_counts_by_source, resolve_source_basis,
                             yield_basis_payload, yield_overview)


def build_report_payload(tables, selected_items=None, sheets=None, etc_items=None,
                         issue_comments=None, summary_engr=None, product_type="", product="",
                         mode="Normal", dist_colors=None, ai_comments=None,
                         etc_auto_items=None,
                         issue_hidden=None, issue_status=None, gross_die=None,
                         compare_groups=None, yield_basis=None,
                         temperature_groups=None) -> dict:
    """Distribution ECDF(대용량)는 payload 에 싣지 않고 항상 지연 로드한다
    (distribution_deferred=True, sheets["Distribution"]=[]) — 프런트가 별도 lazy 엔드포인트
    (GET .../web_report/distribution)로 받아간다. distribution_index(경량)는 항상 포함.
    Map Analysis 도 dies(대용량)를 빼고 경량 메타만 싣는다 (map_deferred=True, schema v8
    — 프런트가 GET .../web_report/map_analysis 로 die 전량을 지연 로드).

    mode: 세션 분석 모드(Normal/Compare/DUT/Commonality). Normal 은 기존 multi-source 렌더,
    DUT 는 source 가 이미 DUT별로 분할돼 있으나 **Map Analysis 만** 하나의 맵으로 병합한다
    (ctx.mode 로 build_map_analysis_rows 에 전달 — 나머지 탭은 DUT 비교 렌더). Compare 는 추가
    비교 시트(Compare Stats/Compare Bin/Common Map)를 얹는다. Commonality 의 chip 강조는
    프런트가 기존 distribution/scatter 데이터로 처리하므로 payload 분기는 없다.

    gross_die: 수율 **분모**의 기준이 될 제품 기준정보 Gross Die (없거나 값이 유효하지
    않으면 소스별 rawdata 행 수로 폴백).
    yield_basis: 세션에 저장된 소스별 분모 선택 {"mode","sources"} (edits.load_yield_basis_map).
    실제 분모 판정은 yield_tab.resolve_source_basis 한 곳이다 — Gross Die 가 측정 die 수와
    크게 어긋나면(수율 100% 초과 등) 그 소스만 자동으로 test die 기준이 된다.
    temperature_groups: Temperature 모드 RT/CT/HT 그룹 {"groups":[{"rt","members"}]}
    (validation.webreport_temperature_groups). 비RT(CT/HT) 소스는 업로드 전에 RT pass
    좌표로 잘려 있어 분모를 **남은 die 수로 강제**한다."""
    selected_set = {str(v) for v in (selected_items or []) if str(v)}
    if selected_set:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected_set]

    sources = [{"name": t.source, "file_name": t.file_name} for t in tables]
    # Temperature 모드: source 마다 RT(기준) / member(CT·HT) 역할과 그룹 번호를 붙인다.
    # 프런트가 RT 를 표시로 구분하고, 비RT 는 아래 분모 강제 대상이 된다.
    # temp_corner("RT"/"CT"/"HT")는 Distribution 소스 그룹 필터가 쓴다 — 업로드 때 기록된
    # member_roles 가 정본이고, 그게 없는 옛 세션은 members 순서(CT→HT)로 추정한다.
    temp_role, temp_member_names = {}, set()
    if mode == "Temperature" and temperature_groups:
        for gi, group in enumerate(temperature_groups.get("groups") or []):
            temp_role[group["rt"]] = (gi, "rt", "RT")
            members = list(group.get("members") or [])
            roles = list(group.get("member_roles") or [])
            for mi, name in enumerate(members):
                corner = roles[mi] if mi < len(roles) else ("CT" if mi == 0 else "HT")
                temp_role[name] = (gi, "member", corner)
                temp_member_names.add(name)
        for entry in sources:
            role = temp_role.get(entry["name"])
            if role:
                entry["temp_group"], entry["temp_role"], entry["temp_corner"] = role
    all_items = sorted({c for t in tables for c in t.item_columns})
    # 항목별 유한 measurement 개수 — 아래 3곳(cpk 제외집합·dist 제외집합·distribution_index
    # 의 n)이 같은 스캔을 각각 돌던 것을 **1회**로 합친다(대형 세션에서 전 데이터 3회 스캔이었다).
    item_counts = finite_count_map(tables)
    # cpk 는 unit 이 Pass/Fail 이거나 측정 data 가 전무한 항목을 제외한다.
    excluded_items = passfail_or_empty_items(tables, counts=item_counts)
    stat_items = [i for i in all_items if i not in excluded_items]
    # distribution_index 는 Pass/Fail 항목을 하드 제외하지 않고 is_passfail 플래그만 붙여
    # 내려보낸다(프런트 "P/F 없애기" 토글이 필터). data 전무 항목만 제외한다.
    dist_excluded = empty_items(tables, counts=item_counts)
    with build_log.stage("yield_cpk"):
        fail_counts = {table.source: fail_counts_by_source(table) for table in tables}
        # 수율 분모: 소스마다 Gross Die / test die 중 하나 (자동 판정 + 사용자 선택).
        basis_info = resolve_source_basis(tables, gross_die, yield_basis,
                                          force_test=temp_member_names or None)
        totals = {src: info["total"] for src, info in basis_info.items()}
        yield_rows = build_yield_rows(tables, fail_counts, totals=totals)
        cpk_rows = build_cpk_rows(tables, stat_items)

    # Temperature 모드 Corner 분해(RT / CT+HT) — Yield 탭 표 2개와 Issue Table 의
    # RT 기준 계산·TEMP 섹션이 **같은 결과 객체**를 쓴다(1회만 계산).
    temp_corners = None
    if mode == "Temperature" and temperature_groups:
        with build_log.stage("yield_corner"):
            temp_corners = build_yield_corner_groups(
                tables, fail_counts, totals, temperature_groups.get("groups"))

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
        etc_auto_items=list(etc_auto_items or []),
        issue_hidden=list(issue_hidden or []),
        issue_status=dict(issue_status or {}),
        temp_corners=temp_corners,
    )
    sheets_out = {}
    for spec in TAB_REGISTRY:
        if spec.builder is None:
            sheets_out[spec.name] = []
            continue
        with build_log.stage("tab:" + spec.name):
            sheets_out[spec.name] = spec.builder(ctx)
    with build_log.stage("dist_index"):
        distribution_index = build_distribution_index(tables, cpk_rows,
                                                      exclude=dist_excluded,
                                                      counts=item_counts)

    payload = {
        "mode": mode or "Normal",
        "sources": sources,
        "yield_summary": yield_overview(tables, yield_rows, totals=totals),
        # 수율 분모 기준(프런트 요약 박스 배지·소스별 표). basis 는 전 소스가 같을 때 그 값,
        # 소스마다 다르면 "mixed" — 소스별 분해는 by_source 에 있다.
        "yield_basis": yield_basis_payload(basis_info,
                                           (yield_basis or {}).get("mode") or "auto"),
        "issue_bin_summary": build_issue_bin_summary(yield_rows),
        # yield_bin_groups: Bin 병합(전체 기준) 그룹 — Excel 내보내기가 사용(유지).
        "yield_bin_groups": build_yield_bin_groups(yield_rows),
        # yield_step_groups: STEP(P1/P2/P3) 별 분리 그룹 — Yield 탭 표시 전용(전체 rawdata 기준).
        "yield_step_groups": build_yield_step_groups(yield_rows),
        "sheets": sheets_out,
        "distribution_deferred": True,
        "map_deferred": True,
        "distribution_index": distribution_index,
        "selected_items": sorted(selected_set),
        "requested_sheets": list(sheets or []),
        # Summary 탭 Engr Comment(Yield/CPK/ETC 3칸) — 세션 편집 DB 에서 채운다.
        "summary_engr": dict(summary_engr or {}),
        # None → 색 미지정(legacy): 프런트가 기본 팔레트. list → source i 가 dist_colors[i] 색.
        "dist_colors": list(dist_colors) if dist_colors else None,
    }

    # Compare 모드: source 2개 이상일 때만 비교 분석을 얹는다 (단일 source 는 비교 대상 없음).
    # compare_groups(세션 옵션의 Before/After 배치)가 없으면 compare 쪽이 legacy 폴백한다.
    if mode == "Compare" and len(tables) >= 2:
        from .tabs.compare import build_compare_payload
        with build_log.stage("compare"):
            payload["compare"] = build_compare_payload(
                tables, all_items, cpk_rows, stat_items=stat_items,
                compare_groups=compare_groups)

    # Temperature 모드: RT/CT/HT 그룹 구성을 그대로 내려 프런트가 RT 를 표시한다.
    # yield_corner_groups 는 Yield 탭이 표를 RT Corner / Temp Corner 2개로 그리는 근거다
    # (없으면 프런트가 종전 yield_step_groups 렌더로 폴백 — 다른 모드는 이 키가 없다).
    if mode == "Temperature" and temperature_groups:
        payload["temperature"] = temperature_groups
        if temp_corners:
            payload["yield_corner_groups"] = temp_corners

    return payload

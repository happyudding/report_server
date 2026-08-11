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
from .tabs.temp_fail import build_temp_fail_rows
from .tabs.yield_tab import (build_yield_bin_groups, build_yield_rows,
                             build_yield_step_groups, fail_counts_by_source,
                             resolve_source_basis, temperature_corner_sources,
                             yield_basis_payload, yield_overview)


def _temperature_context(tables, sources, mode, temperature_groups):
    """Temperature 모드 분기 한 곳 — (groups, CT/HT 이름 집합, Yield 계열 입력 테이블).

    - ``sources[]`` 에 ``temp_group``/``temp_role``/``temp_corner`` 를 **제자리로** 붙인다.
      ``temp_corner``("RT"/"CT"/"HT")는 Distribution 소스 그룹 필터·Map 항목 축이 쓴다.
      정본은 업로드 때 기록된 ``member_roles`` 이고, 없는 옛 세션은 members 순서(CT→HT)로
      추정한다 — **이 폴백 규칙의 정본은 여기 한 곳이다**(tabs/temp_fail 은 복제하지 않음).
    - CT/HT 는 RT pass 좌표로 잘려 있어 수율 분모를 남은 die 수로 강제한다(force_test).
    - Yield 계열(Yield 시트·요약·Bin/STEP 그룹·Fail Bin·Issue Table)은 **RT source 만**
      본다(2026-08-05 사용자 확정) — CT/HT 는 tabs/temp_fail 이 만드는 별도 시트로 나간다.

    Temperature 가 아니거나 그룹이 없으면 (None, set(), tables) — 다른 모드는 무영향.
    """
    groups = (temperature_groups or {}).get("groups") if mode == "Temperature" else None
    if not groups:
        return None, set(), tables

    role_of, member_names = {}, set()
    for gi, group in enumerate(groups):
        role_of[group["rt"]] = (gi, "rt", "RT")
        members = list(group.get("members") or [])
        roles = list(group.get("member_roles") or [])
        for mi, name in enumerate(members):
            corner = roles[mi] if mi < len(roles) else ("CT" if mi == 0 else "HT")
            role_of[name] = (gi, "member", corner)
            member_names.add(name)
    for entry in sources:
        role = role_of.get(entry["name"])
        if role:
            entry["temp_group"], entry["temp_role"], entry["temp_corner"] = role

    rt_names, _members = temperature_corner_sources(tables, groups)
    yield_tables = ([t for t in tables if t.source in set(rt_names)] if rt_names else tables)
    return groups, member_names, yield_tables


# honeyform STEP 메타가 실데이터에서 사실상 항상 이 값으로 온다 — 업로드 창에서 고른
# 공정 STEP(기본 L2)으로 **표시만** 바꾸는 대상. 다른 STEP(P1/P3 등)은 실제 구분이므로
# 손대지 않는다 (2026-08-11 요청: "P2 라고 들어가는 부분을 그 값으로").
_STEP_PLACEHOLDER = "P2"


def _apply_step_label(tables, step_label):
    """tables 의 STEP 메타에서 ``P2`` 를 세션 STEP 값으로 바꾼다 (조회 시점, 원본 불변).

    여기서 한 번 바꾸면 Yield STEP 분리 표·Issue Table/CPK 의 Step 칸·Excel 다운로드가
    모두 같은 값을 쓴다 — 표시 지점마다 치환하면 한 곳을 빠뜨린다. tables 는 loader 가
    준 **클론**이라(payload 조립이 이미 item_columns 를 제자리 수정한다) 원본 캐시는
    건드리지 않는다. Raw Data 탭은 이 경로를 타지 않아 원본 STEP 을 그대로 보여준다 —
    거기서 본 값을 Excel 로 내려 편집하기 때문에 그게 맞다.
    """
    label = str(step_label or "").strip()
    if not label:
        return
    for table in tables:
        step = getattr(table, "step", None)
        if not isinstance(step, dict):
            continue
        table.step = {k: (label if str(v).strip().upper() == _STEP_PLACEHOLDER else v)
                      for k, v in step.items()}


def build_report_payload(tables, selected_items=None, sheets=None, etc_items=None,
                         issue_comments=None, summary_engr=None, product_type="", product="",
                         mode="Normal", dist_colors=None, ai_comments=None,
                         etc_auto_items=None, ai_signatures=None,
                         signature_options=None, issue_signatures=None,
                         issue_hidden=None, issue_status=None, gross_die=None,
                         compare_groups=None, yield_basis=None,
                         temperature_groups=None, temperature_limits=None,
                         step_label="") -> dict:
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
    좌표로 잘려 있어 분모를 **남은 die 수로 강제**한다. 그리고 **Yield 계열은 전부 RT
    source 기준**이다(2026-08-05) — Yield 시트/요약/Bin·STEP 그룹/Fail Bin/Issue Table 이
    RT 만 본다. CT/HT 는 tabs.temp_fail 이 전 항목을 RT limit 으로 재판정한 별도 시트
    ("Issue Table Temp")로 나간다.
    temperature_limits: manifest["temperature_limits"] — {item: {tno, lsl_bin, usl_bin}}
    (.lt/.pds 유래, 신규 업로드만 존재). Temp 시트의 Bin 표기에만 쓰고 없으면 관측 bin 폴백.
    step_label: 업로드 창에서 고른 공정 STEP(예 "L2"). 주면 honeyform STEP 메타의 ``P2``
    표시를 이 값으로 바꾼다 (_apply_step_label). 빈 값이면 아무것도 하지 않는다.
    ai_signatures/issue_signatures/signature_options: Issue Table Signature 컬럼
    (엔진 발화 제안 / ENGR 확정값 / 선택 목록). ai_comments 와 **같은 조건**에서만
    전달된다 — ai_comments 가 None 이면 컬럼도 payload 키도 생기지 않는다(기존 계약 유지)."""
    _apply_step_label(tables, step_label)
    selected_set = {str(v) for v in (selected_items or []) if str(v)}
    if selected_set:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected_set]

    sources = [{"name": t.source, "file_name": t.file_name} for t in tables]
    # Temperature 분기는 이 한 줄로 모은다 (_temperature_context) — sources 태깅,
    # Yield 계열 입력 테이블(RT only), 분모 강제 대상(CT/HT)이 함께 결정된다.
    temp_groups, temp_member_names, yield_tables = _temperature_context(
        tables, sources, mode, temperature_groups)
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
        yield_rows = build_yield_rows(yield_tables, fail_counts, totals=totals)
        # Temperature 면 CT/HT 만 "RT Bin1 die × RT limit" 기준으로 계산된다 (tabs/cpk.py).
        cpk_rows = build_cpk_rows(tables, stat_items, temp_groups)

    # Temperature 모드: CT/HT 를 RT limit 으로 **전 항목** 재판정한 Temp 시트 행
    # (Issue Table Temp 탭 + Yield 탭 하단 섹션이 같은 객체를 쓴다 — 1회만 계산).
    temp_rows = []
    if temp_groups:
        with build_log.stage("temp_fail"):
            temp_rows = build_temp_fail_rows(
                tables, temp_groups, totals, fail_counts=fail_counts,
                limits_meta=temperature_limits, hidden=issue_hidden,
                status_of=(lambda key: "Close"
                           if (issue_status or {}).get(key) == "Close" else "Open"),
                issue_comments=issue_comments)

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
        signatures=(None if ai_comments is None else
                    {"engine": dict(ai_signatures or {}),
                     "engr": dict(issue_signatures or {})}),
        etc_auto_items=list(etc_auto_items or []),
        issue_hidden=list(issue_hidden or []),
        issue_status=dict(issue_status or {}),
        yield_tables=yield_tables,
        temp_rows=temp_rows,
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
        # Temperature 면 yield_tables = RT source 만 (Summary 의 전체/소스별 수율도 RT 기준).
        "yield_summary": yield_overview(yield_tables, yield_rows, totals=totals),
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

    # Signature dropdown 선택지 — ai_comment 옵션 세션에만 싣는다(그 외 세션은 키 자체가
    # 없어 종전 payload 와 완전히 동일하다).
    if ai_comments is not None:
        payload["signature_options"] = list(signature_options or [])

    # Compare 모드: source 2개 이상일 때만 비교 분석을 얹는다 (단일 source 는 비교 대상 없음).
    # compare_groups(세션 옵션의 Before/After 배치)가 없으면 compare 쪽이 legacy 폴백한다.
    if mode == "Compare" and len(tables) >= 2:
        from .tabs.compare import build_compare_payload
        with build_log.stage("compare"):
            payload["compare"] = build_compare_payload(
                tables, all_items, cpk_rows, stat_items=stat_items,
                compare_groups=compare_groups)

    # Temperature 모드: RT/CT/HT 그룹 구성을 그대로 내려 프런트(Distribution 소스 그룹 필터
    # ·Map 항목 legend·Temp 시트 렌더)가 쓴다. Yield 표는 이미 RT 기준이라 Corner 분해 키
    # (구 yield_corner_groups)는 없앴다 — CT/HT 는 sheets["Issue Table Temp"] 로 나간다.
    if temp_groups:
        payload["temperature"] = temperature_groups

    return payload

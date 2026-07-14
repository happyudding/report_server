"""Issue Table tab payload builder.

레이아웃은 client/report_generator/_xlsx_sheets.py::_fill_issue_table 와 동일한
Category 그룹 구조를 따른다: Yield(Pass bin 포함 전체 bin) → CPK(항목별 worst-case cpk < 임계값) →
ETC(placeholder). cpk_rows 에는 source="total"(합산) 행이 없으므로, 항목(subject)별로
모든 source 행 중 가장 낮은 cpk 값을 기준으로 이슈 여부를 판단한다.
프런트(server/report/report_view.html) 의 renderSheetTable(kind="issue") 가 이 컬럼 순서
(Category/Step/Bin/TNO/Item/avg → {source}_yield... → Distribution → comment...)와
CPK 서브헤더 행(Category="CPK", avg="cpk") 감지를 이미 지원한다.
ETC 섹션은 ENGR 가 임의로 추가한 item(manifest.etc_items, service.update_issue_etc_items 가
갱신)을 받아 Bin/TNO 는 tables 메타에서, avg/{source}_yield 는 yield_rows 매칭 항목에서
매 조회마다 다시 채운다(저장하는 값은 item 이름뿐).
PTE/개발 comment 는 manifest.issue_comments 에 row_key 단위로 저장된다
(service.update_issue_comments 가 갱신, 여기서는 조회 시 채우기만 한다).
row_key: Yield 행 "Yield|<bin>|<item>", CPK 데이터 행 "CPK|<item>", ETC 행 "ETC|<item>".
"""
from __future__ import annotations

from .common import PASS_BIN, fmt_type, item_meta as _item_meta
from .cpk import CPK_THRESHOLD, worst_cpk_by_subject
from .yield_tab import build_yield_bin_groups

_COMMENT_COLS = ["PTE comment", "개발 comment"]

# service.update_issue_comments 의 컬럼 검증용 공개 이름.
# AI Comment(아래) 는 여기 절대 추가하지 말 것 — 미포함이 곧 읽기전용 보장
# (서버 편집 검증 + 프런트 ISSUE_COMMENT_COLS 양쪽에서 편집 불가).
COMMENT_COLS = list(_COMMENT_COLS)

# ai_comment 옵션 세션에만 존재하는 읽기전용 컬럼 (web_report/ai_comment.py 가 값 생성).
# 이름에 "comment" 가 들어가 프런트 orderColumns 가 comment 블록으로 자동 배치하며,
# dict 삽입 순서(PTE comment 앞)가 블록 내 표시 순서다 (docs/13).
AI_COMMENT_COL = "AI Comment"


def _comment_values(issue_comments, row_key, ai_comments=None):
    saved = (issue_comments or {}).get(row_key) or {}
    out = {}
    if ai_comments is not None:
        out[AI_COMMENT_COL] = str(ai_comments.get(row_key) or "")
    for col in _COMMENT_COLS:
        out[col] = str(saved.get(col) or "")
    return out


def _blank_row(sources, ai=False):
    row = {f"{src}_yield": "" for src in sources}
    row["Map"] = ""
    row["Distribution"] = ""
    if ai:
        row[AI_COMMENT_COL] = ""
    for col in _COMMENT_COLS:
        row[col] = ""
    return row


def _etc_rows(tables, yield_rows, etc_items, sources, issue_comments=None,
              ai_comments=None):
    if not etc_items:
        return []
    meta = _item_meta(tables)
    by_item = {}
    for r in yield_rows or []:
        item = r.get("Item")
        if item and item not in by_item:
            by_item[item] = r

    rows = []
    for item in etc_items:
        m = meta.get(item, {})
        match = by_item.get(item) or {}
        data = {
            "Category": "", "Step": fmt_type(m.get("step")), "Bin": match.get("bin", ""),
            "TNO": fmt_type(m.get("tno")), "Item": item, "avg": match.get("avg", ""),
        }
        for src in sources:
            data[f"{src}_yield"] = match.get(f"{src}_yield", "")
        data["Map"] = ""
        data["Distribution"] = ""
        data.update(_comment_values(issue_comments, f"ETC|{item}", ai_comments))
        rows.append(data)
    return rows


def _cpk_fail_subjects(cpk_rows):
    """subject 별 모든 source 행 중 최저(worst-case) cpk 를 기준으로 임계값 미만 항목만 반환."""
    worst = worst_cpk_by_subject(cpk_rows)
    fails = [(subject, cpk) for subject, cpk in worst.items() if cpk < CPK_THRESHOLD]
    # 표의 avg 컬럼(=worst-case cpk) 내림차순으로 정렬(높은 순 위 → 아래).
    fails.sort(key=lambda sc: sc[1], reverse=True)
    return fails


def build_issue_bin_summary(yield_rows):
    """Bin 별 FailTNO(Item) 구성 요약: {bin(str): [yield_row, ...]} (avg 내림차순).

    같은 Bin 이라도 서로 다른 TNO(Item)에서 fail 한 유닛이 섞일 수 있어, Issue Table 에서
    Item 클릭 시 그 Bin 전체의 구성을 한눈에 보여주기 위한 조회용 인덱스.
    """
    groups = {}
    for row in yield_rows or []:
        bin_value = row.get("bin")
        if str(bin_value).strip() == "1":
            continue
        item = row.get("Item")
        if not item:
            continue
        groups.setdefault(str(bin_value), []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda r: r.get("avg") or 0, reverse=True)
    return groups


def build_issue_table_rows(tables, yield_rows=None, cpk_rows=None, etc_items=None,
                           issue_comments=None, ai_comments=None):
    # ai_comments: None = 컬럼 미표시(기존 세션 payload 불변) / dict = AI Comment 컬럼
    # 표시(값은 row_key 매칭, 빈 dict 면 빈 셀). service 가 옵션 판정 후 전달.
    sources = [t.source for t in (tables or [])]
    ai = ai_comments is not None
    rows = []

    # Yield 섹션 최상단: Pass(Bin1) 요약 행 (Yield 탭처럼 전체/소스별 통과율을 맨 위에 표시).
    # build_yield_rows 가 항상 yield_rows[0] 에 넣는 Pass 행을 Issue Table 컬럼 구조로 옮긴다.
    # 프런트(sheets.js)가 Bin==1 을 Pass 행으로 인식해 초록 스타일 + Map/Distribution/빨강강조를 뺀다.
    pass_src = yield_rows[0] if yield_rows else None
    pass_added = bool(pass_src and str(pass_src.get("bin")).strip() == PASS_BIN)
    if pass_added:
        pass_item = pass_src.get("Item") or "Pass"
        prow = {
            "Category": "Yield", "Step": pass_src.get("step", ""),
            "Bin": pass_src.get("bin"), "TNO": pass_src.get("TNO", ""),
            "Item": pass_item, "avg": pass_src.get("avg"),
        }
        for src in sources:
            prow[f"{src}_yield"] = pass_src.get(f"{src}_yield")
        prow["Map"] = ""
        prow["Distribution"] = ""
        prow.update(_comment_values(
            issue_comments, f"Yield|{pass_src.get('bin')}|{pass_item}", ai_comments))
        rows.append(prow)

    # Yield 섹션: Bin 당 대표(Bin 총합 집계, 식별정보는 most-fail TNO) 행 + 그 Bin 의
    # 전체 fail TNO 행(detail, 접힘 — most-fail TNO 포함).
    # 프런트가 대표행 STEP 옆 ▼ 토글로 detail 행을 펼친다(Yield 탭과 동일). 정렬은
    # build_yield_bin_groups 순서(= Bin 별 fail 비중 큰 순)를 그대로 쓴다. Category("Yield")는
    # 섹션 첫 행에만 채우고 이후 행은 ""(프런트가 시각적으로 셀 병합).
    # _grp/_detail/_ndetail 은 프런트 토글 전용 내부 필드(orderColumns 가 화면 컬럼에서 제외).
    for gi, group in enumerate(build_yield_bin_groups(yield_rows)):
        group_rows = group["rows"]
        grp_id = f"y{gi}"
        for j, gr in enumerate(group_rows):
            bin_value = gr.get("bin")
            item = gr.get("Item")
            out = {
                "Category": "Yield" if (gi == 0 and j == 0 and not pass_added) else "",
                "Step": gr.get("step", ""),
                "Bin": bin_value,
                "TNO": gr.get("TNO", ""),
                "Item": item,
                "avg": gr.get("avg"),
                "_grp": grp_id,
                "_detail": j > 0,
            }
            if j == 0:
                out["_ndetail"] = len(group_rows) - 1
            for src in sources:
                out[f"{src}_yield"] = gr.get(f"{src}_yield")
            out["Map"] = ""
            out["Distribution"] = ""
            out.update(_comment_values(issue_comments, f"Yield|{bin_value}|{item}",
                                       ai_comments))
            rows.append(out)

    cpk_fails = _cpk_fail_subjects(cpk_rows)
    # CPK 구간은 source 컬럼({src}_yield)에 source 별 CPK 값을 담는다(Yield 값 대신).
    # subhead 행이 그 컬럼을 "CPK"로 재정의(프런트 isCpkSubheadRow 감지). STEP/TNO 는 항목
    # 메타에서, BIN 은 CPK 항목엔 없어 비운다.
    cpk_by = {}
    for r in cpk_rows or []:
        cpk = r.get("cpk")
        if cpk is not None:
            cpk_by[(r.get("subject"), r.get("source"))] = cpk
    cpk_meta = _item_meta(tables)
    subhead = {"Category": "CPK", "Step": "", "Bin": "", "TNO": "", "Item": "item name", "avg": "cpk"}
    subhead.update(_blank_row(sources, ai))
    for src in sources:
        subhead[f"{src}_yield"] = "CPK"
    rows.append(subhead)
    if cpk_fails:
        for subject, cpk in cpk_fails:
            m = cpk_meta.get(subject, {})
            data = {"Category": "", "Step": fmt_type(m.get("step")), "Bin": "",
                    "TNO": fmt_type(m.get("tno")), "Item": subject, "avg": cpk}
            data.update(_blank_row(sources, ai))
            for src in sources:
                data[f"{src}_yield"] = cpk_by.get((subject, src), "")
            data.update(_comment_values(issue_comments, f"CPK|{subject}", ai_comments))
            rows.append(data)
    else:
        rows.append({"Category": "", "Step": "", "Bin": "", "TNO": "", "Item": "", "avg": "", **_blank_row(sources, ai)})

    etc = {"Category": "ETC", "Step": "", "Bin": "", "TNO": "", "Item": "", "avg": "", **_blank_row(sources, ai)}
    rows.append(etc)
    rows.extend(_etc_rows(tables, yield_rows, etc_items, sources,
                          issue_comments=issue_comments, ai_comments=ai_comments))
    return rows

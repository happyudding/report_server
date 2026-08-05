"""Issue Table tab payload builder.

레이아웃은 client/report_generator/_xlsx_sheets.py::_fill_issue_table 와 동일한
Category 그룹 구조를 따른다: Yield(Pass bin 포함 전체 bin) → CPK(항목별 worst-case Bin1 cpk < 임계값) →
ETC(placeholder). cpk_rows 에는 source="total"(합산) 행이 없으므로, 항목(subject)별로
모든 source 행 중 가장 낮은 cpk 값을 기준으로 이슈 여부를 판단한다.
프런트(server/report/report_view.html) 의 renderSheetTable(kind="issue") 가 이 컬럼 순서
(Category/Step/Bin/TNO/Item/avg → {source}_yield... → Distribution → comment...)와
CPK 서브헤더 행(Category="CPK", avg="cpk") 감지를 이미 지원한다.
ETC 섹션은 ENGR 가 임의로 추가한 item(manifest.etc_items, service.update_issue_etc_items 가
갱신)을 받아 Bin/TNO 는 tables 메타에서, avg/{source}_yield 는 yield_rows 매칭 항목에서
매 조회마다 다시 채운다(저장하는 값은 item 이름뿐). 그 뒤에 etc_auto_items(수율·cpk 는
정상인데 eval 룰만 위반한 item — web_report/ai_comment.py 산출)를 자동 행으로 잇는다.
PTE/개발 comment 는 manifest.issue_comments 에 row_key 단위로 저장된다
(service.update_issue_comments 가 갱신, 여기서는 조회 시 채우기만 한다).
row_key: Yield 행 "Yield|<bin>|<item>", CPK 데이터 행 "CPK|<item>", TEMP 행 "TEMP|<item>",
ETC 행 "ETC|<item>".
행 숨김/Status(edits.KIND_ISSUE_HIDDEN/KIND_ISSUE_STATUS) 키는 이슈 단위:
Yield 는 bin 단위 "Yield|<bin>"(대표행+상세행 일괄), CPK/TEMP/ETC 는
"CPK|<item>"/"TEMP|<item>"/"ETC|<item>".
프런트 sheets.js issueHideStatusKey 와 반드시 동일해야 한다.

Temperature 모드(temp_corners 전달)에서는 섹션이 Yield → CPK → **TEMP** → ETC 가 되고,
Yield/CPK 는 **RT source 기준으로만** 계산한다. TEMP 섹션은 CT/HT 가 RT 의 HILIM/LOLIM 을
벗어나 fail 한 항목을 불량률 높은 순으로 세운 것이다(재판정은 업로드 전
web_report.temperature 가 이미 끝냈고, 여기서는 그 결과를 item 단위로 묶기만 한다).
"""
from __future__ import annotations

from .common import PASS_BIN, fmt_type, item_meta as _item_meta
from .cpk import CPK_THRESHOLD, worst_cpk_by_subject
from .yield_tab import build_yield_bin_groups, row_total_count

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
    row["Status"] = ""
    if ai:
        row[AI_COMMENT_COL] = ""
    for col in _COMMENT_COLS:
        row[col] = ""
    return row


def _etc_rows(tables, yield_rows, etc_items, sources, issue_comments=None,
              ai_comments=None, status_of=None):
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
        data["Status"] = status_of(f"ETC|{item}") if status_of else "Open"
        data.update(_comment_values(issue_comments, f"ETC|{item}", ai_comments))
        rows.append(data)
    return rows


def _corner_of(temp_corners, corner):
    """temp_corners 목록에서 corner("RT"/"TEMP") 하나를 꺼낸다 (없으면 None)."""
    for entry in temp_corners or ():
        if str(entry.get("corner")) == corner:
            return entry
    return None


def _temp_issue_rows(tables, temp_rows, temp_sources, sources, hidden,
                     issue_comments=None, ai_comments=None, status_of=None):
    """Temp Corner yield 행 → **item 단위** 집계 행 (불량률 높은 순).

    CT/HT 는 업로드 전 정리에서 RT limit 으로 재판정됐으므로 BIN != 1 인 행이 곧 "RT 규격
    을 벗어난 die" 다. 같은 item 이 여러 bin 으로 죽었으면 소스별 count/yield 를 합산해 한
    행으로 묶는다(Bin 은 item 단위 집계라 비운다). 정렬 기준은 yield_tab.row_total_count
    내림차순 — Yield 탭 fail_bin_ranking 과 같은 "불량률 높은 순" 이다.
    """
    meta = _item_meta(tables)
    agg = {}
    for r in temp_rows or []:
        if str(r.get("bin")).strip() == PASS_BIN:
            continue
        item = r.get("Item")
        if not item:
            continue
        acc = agg.setdefault(item, {"Item": item, "TNO": r.get("TNO", ""), "step": r.get("step", "")})
        for src in temp_sources:
            acc[f"{src}_yield"] = round(
                float(acc.get(f"{src}_yield") or 0) + float(r.get(f"{src}_yield") or 0), 2)
            acc[f"{src}_count"] = int(acc.get(f"{src}_count") or 0) + int(r.get(f"{src}_count") or 0)

    ordered = sorted(agg.values(), key=row_total_count, reverse=True)
    rows = []
    for acc in ordered:
        item = acc["Item"]
        if f"TEMP|{item}" in hidden:
            continue
        m = meta.get(item, {})
        portions = [float(acc.get(f"{src}_yield") or 0) for src in temp_sources]
        data = {
            "Category": "",
            "Step": fmt_type(m.get("step")) or acc.get("step", ""),
            "Bin": "",
            "TNO": fmt_type(m.get("tno")) or acc.get("TNO", ""),
            "Item": item,
            "avg": round(sum(portions) / len(portions), 2) if portions else "",
        }
        data.update(_blank_row(sources, ai_comments is not None))
        for src in temp_sources:
            data[f"{src}_yield"] = acc.get(f"{src}_yield", "")
        data["Status"] = status_of(f"TEMP|{item}") if status_of else "Open"
        data.update(_comment_values(issue_comments, f"TEMP|{item}", ai_comments))
        rows.append(data)
    return rows


def _cpk_fail_subjects(cpk_rows):
    """subject 별 모든 source 행 중 최저(worst-case) cpk 기준으로 임계값 미만 항목만 반환.

    cpk 는 Bin1(양품) 기준 단일 값이다 (2026-07-23 통일 — CPK 탭과 같은 통계)."""
    worst = worst_cpk_by_subject(cpk_rows)
    fails = [(subject, cpk) for subject, cpk in worst.items() if cpk < CPK_THRESHOLD]
    # 표의 avg 컬럼(=worst-case cpk) 오름차순으로 정렬(낮은 순 위 → 아래).
    fails.sort(key=lambda sc: sc[1])
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


def _auto_etc_items(etc_auto_items, etc_items, cpk_fails, yield_rows, hidden,
                    temp_items=None):
    """룰만 위반한 item(ai_comment.etc_auto_items) 중 ETC 자동 행으로 올릴 목록.

    이미 다른 섹션/행으로 보이는 것은 뺀다: 수동 ETC 항목, CPK 섹션 항목(cpk<1.33),
    Yield 행이 있는 항목(fail bin), TEMP 섹션 항목(Temperature 모드), 사용자가 숨긴 이슈.
    사용자 편집값이 아니라 매 조회마다 다시 계산되는 값이라, 룰이 조용해지면 행도
    자동으로 사라진다.
    """
    if not etc_auto_items:
        return []
    seen = set(etc_items or ())
    seen.update(subject for subject, _ in cpk_fails)
    seen.update(temp_items or ())
    for r in yield_rows or []:
        if str(r.get("bin")).strip() != PASS_BIN and r.get("Item"):
            seen.add(r["Item"])
    return [it for it in etc_auto_items
            if it not in seen and f"ETC|{it}" not in hidden]


def build_issue_table_rows(tables, yield_rows=None, cpk_rows=None, etc_items=None,
                           issue_comments=None, ai_comments=None, etc_auto_items=None,
                           hidden_keys=None, statuses=None, temp_corners=None):
    # ai_comments: None = 컬럼 미표시(기존 세션 payload 불변) / dict = AI Comment 컬럼
    # 표시(값은 row_key 매칭, 빈 dict 면 빈 셀). service 가 옵션 판정 후 전달.
    # hidden_keys: 숨긴 이슈 키 목록("Yield|<bin>"|"CPK|<item>") — 해당 이슈 행 미출력.
    # statuses: 이슈 키 → "Close" dict — 부재=Open. 둘 다 세션 편집 DB(edits.py) 유래.
    # temp_corners: Temperature 모드만(yield_tab.build_yield_corner_groups). 주어지면
    #   Yield/CPK 섹션을 **RT source 기준으로만** 계산하고 CPK 와 ETC 사이에 TEMP 섹션을
    #   넣는다. None 이면 아래 로직이 종전과 한 글자도 다르지 않다.
    sources = [t.source for t in (tables or [])]
    ai = ai_comments is not None
    hidden = set(hidden_keys or ())
    statuses = statuses or {}

    rt_corner = _corner_of(temp_corners, "RT")
    temp_corner = _corner_of(temp_corners, "TEMP")
    # Yield 섹션 원본 행: Temperature 면 RT Corner 행(= RT 소스만), 아니면 종전 전체 행.
    base_rows = rt_corner["rows"] if rt_corner else yield_rows
    rt_sources = set(rt_corner["sources"]) if rt_corner else None

    def _status(key):
        return "Close" if statuses.get(key) == "Close" else "Open"

    rows = []

    # Yield 섹션 최상단: Pass(Bin1) 요약 행 (Yield 탭처럼 전체/소스별 통과율을 맨 위에 표시).
    # build_yield_rows 가 항상 yield_rows[0] 에 넣는 Pass 행을 Issue Table 컬럼 구조로 옮긴다.
    # 프런트(sheets.js)가 Bin==1 을 Pass 행으로 인식해 초록 스타일 + Map/Distribution/빨강강조를 뺀다.
    pass_src = base_rows[0] if base_rows else None
    pass_added = bool(pass_src and str(pass_src.get("bin")).strip() == PASS_BIN)
    if pass_added:
        pass_item = pass_src.get("Item") or "Pass"
        prow = {
            "Category": "Yield", "Step": pass_src.get("step", ""),
            "Bin": pass_src.get("bin"), "TNO": pass_src.get("TNO", ""),
            "Item": pass_item, "avg": pass_src.get("avg"),
        }
        for src in sources:
            prow[f"{src}_yield"] = pass_src.get(f"{src}_yield", "")
        prow["Map"] = ""
        prow["Distribution"] = ""
        prow["Status"] = ""   # Pass 행은 이슈 행이 아님 — Status/숨김 비대상.
        prow.update(_comment_values(
            issue_comments, f"Yield|{pass_src.get('bin')}|{pass_item}", ai_comments))
        rows.append(prow)

    # Yield 섹션: Bin 당 대표(Bin 총합 집계, 식별정보는 most-fail TNO) 행 + 그 Bin 의
    # 전체 fail TNO 행(detail, 접힘 — most-fail TNO 포함).
    # 프런트가 대표행 STEP 옆 ▼ 토글로 detail 행을 펼친다(Yield 탭과 동일). 정렬은
    # build_yield_bin_groups 순서(= Bin 별 fail 비중 큰 순)를 그대로 쓴다. Category("Yield")는
    # 섹션 첫 행에만 채우고 이후 행은 ""(프런트가 시각적으로 셀 병합).
    # _grp/_detail/_ndetail 은 프런트 토글 전용 내부 필드(orderColumns 가 화면 컬럼에서 제외).
    # 숨긴 bin("Yield|<bin>")은 대표행+상세행을 통째로 제외한다(그 아래 comment 는 DB 에
    # 남아 '삭제 전체 초기화' 후 재표시). Category 라벨은 필터 후 첫 그룹 기준.
    groups = [g for g in build_yield_bin_groups(base_rows)
              if f"Yield|{g['bin']}" not in hidden]
    for gi, group in enumerate(groups):
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
                out[f"{src}_yield"] = gr.get(f"{src}_yield", "")
            out["Map"] = ""
            out["Distribution"] = ""
            # Status 는 bin 이슈 단위 — 대표행에만 표시(상세행 빈칸).
            out["Status"] = _status(f"Yield|{bin_value}") if j == 0 else ""
            out.update(_comment_values(issue_comments, f"Yield|{bin_value}|{item}",
                                       ai_comments))
            rows.append(out)

    # Temperature 모드의 CPK 는 RT 기준이다 — CT/HT 는 RT limit 재판정으로 이미 잘려 있어
    # 그 분포로 낸 cpk 를 RT 와 한 표에 섞으면 기준이 어긋난다.
    cpk_src_rows = ([r for r in (cpk_rows or []) if r.get("source") in rt_sources]
                    if rt_sources is not None else cpk_rows)
    cpk_fails = [(subject, cpk) for subject, cpk in _cpk_fail_subjects(cpk_src_rows)
                 if f"CPK|{subject}" not in hidden]
    # CPK 구간은 source 컬럼({src}_yield)에 source 별 CPK 값을 담는다(Yield 값 대신).
    # subhead 행이 그 컬럼을 "CPK"로 재정의(프런트 isCpkSubheadRow 감지). STEP/TNO 는 항목
    # 메타에서, BIN 은 CPK 항목엔 없어 비운다. 값은 선정 기준과 동일한 Bin1 기준 cpk.
    cpk_by = {}
    for r in cpk_src_rows or []:
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
            data["Status"] = _status(f"CPK|{subject}")
            data.update(_comment_values(issue_comments, f"CPK|{subject}", ai_comments))
            rows.append(data)
    else:
        rows.append({"Category": "", "Step": "", "Bin": "", "TNO": "", "Item": "", "avg": "", **_blank_row(sources, ai)})

    # TEMP 섹션(Temperature 모드 전용, CPK 와 ETC 사이) — CT/HT 가 RT 의 HILIM/LOLIM 을
    # 벗어나 fail 한 항목을 불량률 높은 순으로. 값은 Temp Corner 소스 컬럼에만 들어간다.
    temp_rows = []
    if temp_corner:
        head = {"Category": "TEMP", "Step": "", "Bin": "", "TNO": "", "Item": "", "avg": "",
                **_blank_row(sources, ai)}
        rows.append(head)
        temp_rows = _temp_issue_rows(
            tables, temp_corner.get("rows"), temp_corner.get("sources") or [], sources, hidden,
            issue_comments=issue_comments, ai_comments=ai_comments, status_of=_status)
        rows.extend(temp_rows)

    etc = {"Category": "ETC", "Step": "", "Bin": "", "TNO": "", "Item": "", "avg": "", **_blank_row(sources, ai)}
    rows.append(etc)
    # 수동 추가분(ENGR) 뒤에 룰 위반 자동 행을 잇는다 — 행 채움 로직은 동일.
    # TEMP 섹션에 이미 선 item 은 자동 ETC 행에서 뺀다(같은 item 이 두 섹션에 겹치지 않게).
    etc_all = list(etc_items or []) + _auto_etc_items(
        etc_auto_items, etc_items, cpk_fails, base_rows, hidden,
        temp_items=[r["Item"] for r in temp_rows])
    rows.extend(_etc_rows(tables, base_rows, etc_all, sources,
                          issue_comments=issue_comments, ai_comments=ai_comments,
                          status_of=_status))
    return rows

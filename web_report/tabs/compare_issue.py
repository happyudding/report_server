"""Issue Table Compare tab payload builder (Compare 모드 전용, 2026-08-20).

기존 Issue Table(tabs/issue_table.py)이 Yield/CPK/ETC 축이라면 이 표는 **Before/After 비교**
축이다. 카테고리 4개 중 서버가 굽는 것은 2개:

  Distribution : compare.dist_shift 의 focus 행(산포 유의차 검출 조건 통과) + new_items
                 (After 에만 있는 신규 test item). 지표는 dist_shift 가 이미 계산한 값을
                 **그대로 옮기기만 한다** — 재계산 금지(CLAUDE.md 규칙 13). focus 판정도
                 서버 정본(compare._dist_focus)이 붙여준 불린을 쓴다.
  ETC          : ENGR 가 수동으로 추가한 item (edits.KIND_CMP_ETC_ITEM).

나머지 2개(Bin Transition / Log)는 이 시트에 넣지 않는다 — 컬럼 축이 완전히 달라
(좌표 단위 / limit 단위) 한 표에 못 담고, 코멘트도 issue_comment 가 아니라
compare_note(bm:/gl: 키)를 Map 비교·Log 비교 탭과 **공유**해야 하기 때문이다.
프런트(static/webreport/compare_issue.js)가 compare payload 에서 직접 그린다.

row_key 규약 (**저장 키 — 불변**, CLAUDE.md 규칙 12):
  Distribution 행 "CMPDIST|<item>" / ETC 행 "CMPETC|<item>".
숨김/Status 키도 같다(행=item 단위라 comment 키와 동일). 프런트 sheets.js
issueRowKey/issueHideStatusKey 와 반드시 동일해야 한다.
숨김은 CMPDIST 만 허용한다 — CMPETC 는 항목 자체를 지우는 편이 자연스럽다
(기존 ETC 와 같은 취급, service._ISSUE_HIDABLE_PREFIXES).

Signature/AI Comment 컬럼은 싣지 않는다 — eval 엔진은 단일 세션 item 판정기라
Before/After 비교 행에 대한 발화 개념이 없다.

`Unit` 컬럼도 싣지 않는다 (2026-08-27) — Item 이름에 단위가 드러나는 경우가 많아 중복이라
2026-08-27 에 화면(sheets.js orderColumns)에서 먼저 감췄고, payload 에서 빼려면 캐시 세대를
갈라야 해서 보류돼 있었다. REPORT_SCHEMA_VERSION v42 에 얹어 정리했다. 반면 `개발 comment`
는 **화면에서만** 계속 감춘다 — 그 컬럼은 사용자가 입력한 값(_comment_values)을 화면으로
실어 나르는 통로라, payload 에서 빼면 DB 에 값이 남아도 다시 보여줄 길이 사라진다
(CLAUDE.md 규칙 12).
perf-guard: allow S01-report-schema — 이 제거는 **v42 와 같은 세대**다.
REPORT_SCHEMA_VERSION 41→42 bump 는 CPK TOTAL 을 넣은 직전 커밋에 이미 들어가 있고,
그 bump 주석이 이 Unit 정리를 함께 명시한다. 여기서 또 올리면 콜드 폭풍만 한 번 더 난다.
"""
from __future__ import annotations

from .common import fmt_type, item_meta as _item_meta, json_safe, num

# 기존 Issue Table 과 **같은** comment 컬럼 이름을 쓴다 — 저장 키(col)가 같아야
# service.update_issue_comments 검증·프런트 편집 컬럼 목록이 그대로 통한다.
from .issue_table import COMMENT_COLS as _COMMENT_COLS

# 구분(kind) 셀 표시값 — 화면 라벨이지 저장 키가 아니다.
KIND_DIST = "산포"
KIND_NEW = "신규"

ROW_KEY_DIST = "CMPDIST"
ROW_KEY_ETC = "CMPETC"

# Category 셀에는 **섹션 키를 그대로** 넣는다(화면 라벨이 아니다). 기존 Issue Table 이
# "Yield"/"CPK"/"TEMP"/"ETC" 를 그렇게 쓰고, 프런트 sheets.js 가 이 값을 rowSection 으로
# 상속해 row_key 접두를 만든다(issueRowKey). 화면 표시 라벨은 프런트 상수가 따로 갖는다 —
# 여기에 "Distribution"/"ETC" 같은 표시 문구를 넣으면 ETC 가 메인 시트 섹션 키와 충돌해
# 저장 키가 `ETC|<item>` 으로 만들어진다(= 메인 Issue Table 코멘트를 덮어쓴다).


def _stat_cells(stat, prefix):
    """dist_shift 의 after/before 통계 dict → 표 셀. 값 가공 없이 옮기기만 한다."""
    stat = stat or {}
    return {
        f"{prefix}_avg": json_safe(stat.get("average")),
        f"{prefix}_stdev": json_safe(stat.get("stdev")),
        f"{prefix}_cpk": json_safe(stat.get("cpk")),
    }


def _blank_stat_cells(prefix):
    return {f"{prefix}_avg": "", f"{prefix}_stdev": "", f"{prefix}_cpk": ""}


def _comment_values(issue_comments, row_key):
    saved = (issue_comments or {}).get(row_key) or {}
    return {col: str(saved.get(col) or "") for col in _COMMENT_COLS}


def _base_row(category, kind, item, meta):
    m = meta.get(item, {})
    return {
        "Category": category,
        "구분": kind,
        "Step": fmt_type(m.get("step")),
        "TNO": fmt_type(m.get("tno")),
        "Item": item,
    }


def _new_item_stats(cpk_rows, after_sources):
    """신규 item 의 After 통계 — cpk_rows 를 재사용한다(재계산 금지).

    After 그룹 source 가 여러 개면 cpk 가 가장 낮은(worst) 행을 대표로 쓴다 —
    Issue Table CPK 섹션이 worst_cpk_by_subject 로 고르는 것과 같은 관점이다.
    """
    want = set(after_sources or ())
    out: dict = {}
    for r in cpk_rows or []:
        if want and r.get("source") not in want:
            continue
        subject = r.get("subject")
        if not subject:
            continue
        cur = out.get(subject)
        if cur is None:
            out[subject] = r
            continue
        c_new, c_cur = num(r.get("cpk")), num(cur.get("cpk"))
        if c_cur is None or (c_new is not None and c_new < c_cur):
            out[subject] = r
    return out


def build_compare_issue_rows(compare_payload, *, tables=None, cpk_rows=None,
                             cmp_etc_items=None, issue_comments=None,
                             hidden_keys=None, statuses=None):
    """Compare 모드 Issue Table 행. compare_payload 가 없으면 빈 목록.

    compare_payload: tabs.compare.build_compare_payload 결과(dist_shift/new_items 사용).
    cpk_rows: 신규 item 의 After 통계를 채우는 데만 쓴다(그 외 지표는 dist_shift 값).
    """
    if not compare_payload:
        return []
    hidden = set(hidden_keys or ())
    statuses = statuses or {}
    meta = _item_meta(tables or [])

    def _status(key):
        return "Close" if statuses.get(key) == "Close" else "Open"

    dist = compare_payload.get("dist_shift") or {}
    dist_rows = dist.get("rows") or []
    rows = []
    first = True

    for r in dist_rows:
        if not r.get("focus"):
            continue
        item = r.get("subject")
        if not item:
            continue
        row_key = f"{ROW_KEY_DIST}|{item}"
        if row_key in hidden:
            continue
        out = _base_row(ROW_KEY_DIST if first else "", KIND_DIST, item, meta)
        first = False
        out.update(_stat_cells(r.get("before"), "before"))
        out.update(_stat_cells(r.get("after"), "after"))
        out["meanshift_sigma"] = json_safe(r.get("meanshift_sigma"))
        out["stdev_delta_pct"] = json_safe(r.get("stdev_delta_pct"))
        out["cpk_ratio_pct"] = json_safe(r.get("cpk_ratio_pct"))
        out["Distribution"] = ""
        out["Status"] = _status(row_key)
        out.update(_comment_values(issue_comments, row_key))
        rows.append(out)

    # 신규 item(After 에만 있음) — Before 통계·비교 지표가 존재하지 않으므로 빈칸이다.
    # dist_shift 는 **공통 항목만** 다뤄 이 항목들이 아예 없기 때문에 별도로 잇는다.
    new_stats = _new_item_stats(cpk_rows, compare_payload.get("after_sources"))
    for item in compare_payload.get("new_items") or []:
        row_key = f"{ROW_KEY_DIST}|{item}"
        if row_key in hidden:
            continue
        out = _base_row(ROW_KEY_DIST if first else "", KIND_NEW, item, meta)
        first = False
        stat = new_stats.get(item) or {}
        out.update(_blank_stat_cells("before"))
        out.update(_stat_cells({"average": stat.get("average"),
                                "stdev": stat.get("stdev"),
                                "cpk": stat.get("cpk")}, "after"))
        out["meanshift_sigma"] = ""
        out["stdev_delta_pct"] = ""
        out["cpk_ratio_pct"] = ""
        out["Distribution"] = ""
        out["Status"] = _status(row_key)
        out.update(_comment_values(issue_comments, row_key))
        rows.append(out)

    # ETC 섹션 divider — 항목이 없어도 헤더는 낸다(기존 Issue Table 과 같은 관례:
    # 프런트가 여기에 '항목 추가' 버튼을 붙인다).
    etc_head = {"Category": ROW_KEY_ETC, "구분": "", "Step": "", "TNO": "", "Item": "",
                **_blank_stat_cells("before"), **_blank_stat_cells("after"),
                "meanshift_sigma": "", "stdev_delta_pct": "", "cpk_ratio_pct": "",
                "Distribution": "", "Status": ""}
    etc_head.update({col: "" for col in _COMMENT_COLS})
    rows.append(etc_head)

    for item in cmp_etc_items or []:
        row_key = f"{ROW_KEY_ETC}|{item}"
        out = _base_row("", "", item, meta)
        out.update(_blank_stat_cells("before"))
        out.update(_blank_stat_cells("after"))
        out["meanshift_sigma"] = ""
        out["stdev_delta_pct"] = ""
        out["cpk_ratio_pct"] = ""
        out["Distribution"] = ""
        out["Status"] = _status(row_key)
        out.update(_comment_values(issue_comments, row_key))
        rows.append(out)

    return rows

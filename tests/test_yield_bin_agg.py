"""Bin 집계 헤더행(펼침 표시 전용) 회귀 — web_report/yield_agg.py.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_yield_bin_agg.py

**왜 이 파일이 생겼나** (2026-08-25): 같은 Bin 에 TNO 가 여럿이면 서버 대표행(rep)은 숫자가
Bin 합계인데 이름(Step/TNO/Item)은 그 Bin 에서 가장 많이 죽은 항목 것이었다. 접힘에선
"Bin 요약"으로 읽히지만 펼치면 **그 항목 혼자 Bin 전체만큼 죽은 것처럼** 보였고, 그 항목의
실제 fail 수는 화면 어디에도 없었다(100 die 중 TEST1 2 / TEST2 2 / TEST3 1 인 Bin 이
"TEST1 0.5% 5개"로 표시). 이제 펼치면 대표행 자리에 집계 헤더행이 서고 항목은 자기 값을
가진 상세행으로 돌아온다.

검증하는 것:
  (a) 라벨 서식 — ``BIN 15    (3 items)`` (가운데 공백 4칸, 사용자 요청)
  (b) 집계행이 rep 의 **숫자를 그대로 승계**한다 (재계산 금지 — CLAUDE.md 규칙 13)
  (c) 항목이 1개뿐인 Bin 은 집계행을 만들지 않는다 (사용자 확정)
  (d) 합 검산 — 집계행 count == 상세행 count 합, avg == 상세행 avg 합
  (e) expand_bin_group 이 **most-fail 항목 행을 되살린다** (이번 변경의 핵심)
  (f) insert_bin_agg_rows 가 comment 를 첫 TNO 행에, Status 를 집계행에 둔다
  (g) 대표행이 집계행이 **아닌** 표(Issue Table Temp)는 손대지 않는다
  (h) 원본 dict 을 변형하지 않는다

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_report.yield_agg import (BIN_AGG_TNO, bin_agg_label,  # noqa: E402
                                  build_bin_agg_row, expand_bin_group,
                                  insert_bin_agg_rows)

_LABEL = "BIN 15    (3 items)"


# 사용자 요청서의 상황 그대로: 100 die 중 TEST1 2 / TEST2 2 / TEST3 1 fail (Bin 15).
def _yield_group():
    rows = [
        {"step": "P2", "bin": "15", "TNO": "100", "Item": "TEST1", "avg": 0.2,
         "S1_yield": 0.2, "S1_count": 2},
        {"step": "P2", "bin": "15", "TNO": "101", "Item": "TEST2", "avg": 0.2,
         "S1_yield": 0.2, "S1_count": 2},
        {"step": "P2", "bin": "15", "TNO": "102", "Item": "TEST3", "avg": 0.1,
         "S1_yield": 0.1, "S1_count": 1},
    ]
    # build_yield_bin_groups 산출 형태: rep = 합계행(이름은 most-fail 행에서 복사),
    # rows = [rep] + 항목 행 전부.
    rep = {"step": "P2", "bin": "15", "TNO": "100", "Item": "TEST1", "avg": 0.5,
           "S1_yield": 0.5, "S1_count": 5}
    return {"bin": "15", "rep": rep, "rows": [rep] + rows}


def _single_group():
    row = {"step": "P2", "bin": "7", "TNO": "200", "Item": "SOLO", "avg": 0.3,
           "S1_yield": 0.3, "S1_count": 3}
    return {"bin": "7", "rep": dict(row), "rows": [dict(row), dict(row)]}


def test_label_format():
    assert bin_agg_label("15", 3) == _LABEL, bin_agg_label("15", 3)
    assert bin_agg_label(15, 2) == "BIN 15    (2 items)"
    print("[a] 라벨 서식 OK - %r" % _LABEL)


def test_agg_inherits_numbers():
    g = _yield_group()
    agg = build_bin_agg_row(g)
    assert agg is not None
    assert agg["TNO"] == BIN_AGG_TNO
    assert agg["Item"] == _LABEL
    # 숫자는 rep 그대로 — 다시 계산하지 않는다
    for key in ("avg", "S1_yield", "S1_count"):
        assert agg[key] == g["rep"][key], key
    assert agg["step"] == "P2" and agg["bin"] == "15"
    print("[b] 집계행이 rep 숫자를 그대로 승계 OK")


def test_single_item_bin_has_no_agg():
    assert build_bin_agg_row(_single_group()) is None
    expanded = expand_bin_group(_single_group())
    assert len(expanded) == 1 and expanded[0]["Item"] == "SOLO", expanded
    print("[c] 항목 1개 Bin 은 집계행 없음 - 종전과 동일 OK")


def test_totals_match_details():
    g = _yield_group()
    rows = expand_bin_group(g)
    agg, details = rows[0], rows[1:]
    assert agg["S1_count"] == sum(r["S1_count"] for r in details), rows
    assert round(agg["avg"], 6) == round(sum(r["avg"] for r in details), 6), rows
    print("[d] 합 검산 OK - 집계 5 == 2+2+1, avg 0.5 == 0.2+0.2+0.1")


def test_most_fail_row_is_restored():
    """이번 변경의 핵심 — TEST1 이 자기 실제 값(0.2/2)으로 되돌아온다."""
    rows = expand_bin_group(_yield_group())
    assert len(rows) == 4, rows
    names = [r["Item"] for r in rows]
    assert names == [_LABEL, "TEST1", "TEST2", "TEST3"], names
    test1 = rows[1]
    assert test1["S1_count"] == 2 and test1["avg"] == 0.2, test1
    print("[e] most-fail 항목 행 복원 OK - TEST1 = 0.2 / 2")


def _issue_rows():
    """build_issue_table_rows 산출 모양 (Yield 섹션 1그룹)."""
    return [
        {"Category": "Yield", "Step": "P2", "Bin": "15", "TNO": "100", "Item": "TEST1",
         "avg": 0.5, "S1_yield": 0.5, "Map": "", "Distribution": "", "Status": "Close",
         "PTE comment": "메인 코멘트", "개발 comment": "",
         "_grp": "y0", "_detail": False, "_ndetail": 3},
        {"Category": "", "Step": "P2", "Bin": "15", "TNO": "100", "Item": "TEST1",
         "avg": 0.2, "S1_yield": 0.2, "Map": "", "Distribution": "", "Status": "",
         "PTE comment": "메인 코멘트", "개발 comment": "", "_grp": "y0", "_detail": True},
        {"Category": "", "Step": "P2", "Bin": "15", "TNO": "101", "Item": "TEST2",
         "avg": 0.2, "S1_yield": 0.2, "Map": "", "Distribution": "", "Status": "",
         "PTE comment": "", "개발 comment": "", "_grp": "y0", "_detail": True},
        {"Category": "", "Step": "P2", "Bin": "15", "TNO": "102", "Item": "TEST3",
         "avg": 0.1, "S1_yield": 0.1, "Map": "", "Distribution": "", "Status": "",
         "PTE comment": "", "개발 comment": "", "_grp": "y0", "_detail": True},
    ]


def test_issue_rows_layout():
    out = insert_bin_agg_rows(_issue_rows())
    # 대표행(접힘 전용) + 집계행 + 상세 3행
    assert len(out) == 5, [r["Item"] for r in out]
    rep, agg = out[0], out[1]
    assert rep["_hasAgg"] is True and not rep.get("_agg")
    assert agg["_agg"] is True and agg["Item"] == _LABEL
    assert agg["TNO"] == "-" and agg["avg"] == 0.5
    # Status 는 집계행이 갖는다 (저장 키가 bin 단위 Yield|<bin>)
    assert agg["Status"] == "Close", agg
    # comment / 미니셀은 비운다 (저장 키가 항목 단위라 첫 TNO 행이 주인)
    for col in ("PTE comment", "개발 comment", "Map", "Distribution"):
        assert agg[col] == "", (col, agg[col])
    assert out[2]["Item"] == "TEST1" and out[2]["PTE comment"] == "메인 코멘트"
    assert out[2]["avg"] == 0.2, out[2]
    print("[f] Issue Table 배치 OK - Status 는 집계행, comment 는 첫 TNO 행")


def test_temp_style_group_untouched():
    """Issue Table Temp: 대표행이 **항목 행 자체**라 집계 개념이 없다 — 손대지 않는다."""
    rows = [
        {"Category": "TEMP", "Bin": "3", "Item": "ITEM_A", "avg": 1.0,
         "_grp": "t0", "_detail": False, "_ndetail": 2},
        {"Category": "", "Bin": "3", "Item": "ITEM_B", "avg": 0.5,
         "_grp": "t0", "_detail": True},
        {"Category": "", "Bin": "3", "Item": "ITEM_C", "avg": 0.2,
         "_grp": "t0", "_detail": True},
    ]
    out = insert_bin_agg_rows(rows)
    assert len(out) == 3, [r["Item"] for r in out]
    assert not any(r.get("_agg") for r in out)
    assert [r["Item"] for r in out] == ["ITEM_A", "ITEM_B", "ITEM_C"]
    print("[g] Issue Table Temp 표 무변경 OK")


def test_single_item_issue_group():
    rows = [
        {"Category": "Yield", "Bin": "7", "TNO": "200", "Item": "SOLO", "avg": 0.3,
         "_grp": "y0", "_detail": False, "_ndetail": 1},
        {"Category": "", "Bin": "7", "TNO": "200", "Item": "SOLO", "avg": 0.3,
         "_grp": "y0", "_detail": True},
    ]
    out = insert_bin_agg_rows(rows)
    assert len(out) == 1, [r["Item"] for r in out]
    assert out[0]["Item"] == "SOLO" and out[0]["_ndetail"] == 0, out[0]
    print("[c2] 항목 1개 Issue 그룹 - 집계행 없이 1행(종전 동일) OK")


def test_originals_not_mutated():
    src = _issue_rows()
    snap = copy.deepcopy(src)
    insert_bin_agg_rows(src)
    assert src == snap, "insert_bin_agg_rows 가 원본 dict 을 변형했습니다"
    g = _yield_group()
    gsnap = copy.deepcopy(g)
    expand_bin_group(g)
    build_bin_agg_row(g)
    assert g == gsnap, "expand_bin_group/build_bin_agg_row 가 원본을 변형했습니다"
    print("[h] 원본 불변 OK")


if __name__ == "__main__":
    test_label_format()
    test_agg_inherits_numbers()
    test_single_item_bin_has_no_agg()
    test_totals_match_details()
    test_most_fail_row_is_restored()
    test_issue_rows_layout()
    test_temp_style_group_untouched()
    test_single_item_issue_group()
    test_originals_not_mutated()
    print("\n전부 통과")

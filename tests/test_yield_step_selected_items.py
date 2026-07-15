"""Yield 탭 STEP 귀속 회귀 테스트 (selected_items 필터 불일치 버그).

실행:
    python tests/test_yield_step_selected_items.py

배경: manifest.selected_items 에서 빠진 fail 항목은 fail_counts(전체 table.tno 기준)에는
잡히지만, item_meta(구버전은 필터된 item_columns 순회)에서 STEP/TNO 를 못 찾아 빈 STEP
그룹으로 떨어지는 버그가 있었다. item_meta 가 전체 메타 키(table.step)를 순회하도록 고쳐
미선택 fail 항목도 자기 STEP 에 귀속되게 한다.

pytest 미사용(그건 eval_analyzer 전용) — 자체 실행 + assert 스타일(web_report tests/ 관례).
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402


def make_table():
    """합성 honeyform 테이블 1개 (호출마다 fresh — build_report_payload 가 item_columns 를
    in-place 변형하므로 재사용 금지).

    ItemA: STEP P1, TNO 100, data 있음.  ItemB: STEP P2, TNO 200, data 없음(메타만).
    data 행: pass 1 + ItemB fail 2(FAILTNO 200) + ItemA fail 1(FAILTNO 100).
    ItemB(data 무·미선택)의 fail die 가 STEP P2 에 귀속되는지가 핵심.
    """
    cols = META_COLUMNS + ["ItemA", "ItemB"]
    rows = [
        # 메타 6행: col0 = 라벨, 메타 컬럼(SHOT..FAILTNO)은 공백
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P2"],
        ["UNIT", "", "", "", "", "", "", "V", "V"],
        ["HILIM", "", "", "", "", "", "", 10, 10],
        ["LOLIM", "", "", "", "", "", "", 0, 0],
        # data 행: SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO, ItemA, ItemB
        ["s1", 1, 1, 0, 0, 1, "", 5, ""],    # pass (BIN1)
        ["s2", 1, 1, 1, 0, 5, 200, 5, ""],   # fail FAILTNO 200 -> ItemB (STEP P2)
        ["s3", 1, 1, 2, 0, 5, 200, 5, ""],   # fail -> ItemB
        ["s4", 1, 1, 3, 0, 4, 100, 15, ""],  # fail FAILTNO 100 -> ItemA (STEP P1)
    ]
    df = pd.DataFrame(rows, columns=cols)
    return split_honeyform(df, source="src0", file_name="src0")


def _find_step_row(payload, item):
    """yield_step_groups 를 훑어 item 행의 (step, tno) 를 반환 (없으면 (None, None))."""
    for grp in payload["yield_step_groups"]:
        for g in grp["groups"]:
            for r in g["rows"]:
                if r.get("Item") == item:
                    return grp["step"], r.get("TNO")
    return None, None


def _canon_content(payload):
    """계산 콘텐츠만 정준화 — selected_items 는 입력 반향 필드(계산값 아님)라 제외."""
    p = dict(payload)
    p.pop("selected_items", None)
    return json.dumps(p, sort_keys=True, ensure_ascii=False, default=str)


def test_unselected_fail_item_gets_step():
    """미선택 ItemB 의 fail die 가 빈 STEP 이 아니라 자기 STEP(P2)에 귀속된다."""
    payload = build_report_payload([make_table()], selected_items=["ItemA"])
    step, tno = _find_step_row(payload, "ItemB")
    assert step == "P2", f"ItemB STEP expected 'P2', got {step!r} (버그: 빈 STEP 그룹)"
    assert tno == "200", f"ItemB TNO expected '200', got {tno!r}"
    # 선택된 ItemA 는 여전히 정상 귀속
    a_step, a_tno = _find_step_row(payload, "ItemA")
    assert a_step == "P1", f"ItemA STEP expected 'P1', got {a_step!r}"
    assert a_tno == "100", f"ItemA TNO expected '100', got {a_tno!r}"


def test_full_selection_unchanged():
    """전체 선택 세션의 계산 콘텐츠는 필터 미적용과 정준 JSON 완전 일치 (회귀 없음).

    전체 item 이 선택된 정상 세션에선 item_columns == step.keys() 라 이번 수정이 무영향
    임을 보인다 (selected_items echo 필드만 입력 따라 다르므로 비교에서 제외)."""
    none_json = _canon_content(build_report_payload([make_table()], selected_items=None))
    all_json = _canon_content(build_report_payload([make_table()], selected_items=["ItemA", "ItemB"]))
    assert none_json == all_json, "전체 선택 계산 콘텐츠가 필터 미적용과 달라짐 (회귀)"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    test_unselected_fail_item_gets_step()
    test_full_selection_unchanged()
    print("PASS: test_yield_step_selected_items (2 checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

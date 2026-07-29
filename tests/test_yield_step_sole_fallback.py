"""Yield 탭 빈 STEP fail 행의 유일-STEP 흡수 테스트 (_sole_step).

실행:
    python tests/test_yield_step_sole_fallback.py

배경: FAILTNO 가 귀속된 item 의 STEP 메타 셀이 공백이면 그 fail 행이 step="" 버킷으로 떨어져
화면에 "STEP (기타)" 라는 별도 섹션으로 분리됐다. 세션 전체 STEP 이 1종뿐이면 그 fail 은
그 STEP 에서 난 것이 자명하므로 흡수한다 (2026-07-29 사용자 확정). STEP 이 2종 이상이면
어디에 넣을지 알 수 없으므로 종전대로 빈 STEP 을 유지한다.

pytest 미사용(그건 eval_analyzer 전용) — 자체 실행 + assert 스타일(web_report tests/ 관례).
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402


def make_table(steps):
    """합성 honeyform 테이블 1개 (호출마다 fresh — build_report_payload 가 item_columns 를
    in-place 변형하므로 재사용 금지).

    steps: 항목별 STEP 메타값 리스트. 항목 수 = len(steps), 이름은 Item0..ItemN,
    TNO 는 순서대로 1, 2, 3 …  data 행은 pass 1개 + 항목마다 fail 1개(FAILTNO = 그 항목 TNO).
    마지막 항목(= 호출부가 STEP 공백을 주는 자리)의 fail 이 어느 STEP 으로 가는지가 핵심.
    """
    items = [f"Item{i}" for i in range(len(steps))]
    tnos = [i + 1 for i in range(len(steps))]
    cols = META_COLUMNS + items
    blank = ["", "", "", "", "", ""]          # 메타 컬럼(SHOT..FAILTNO) 자리
    rows = [
        ["TSEQ"] + blank + [i + 1 for i in range(len(items))],
        ["TNO"] + blank + tnos,
        ["STEP"] + blank + list(steps),
        ["UNIT"] + blank + ["V"] * len(items),
        ["HILIM"] + blank + [10] * len(items),
        ["LOLIM"] + blank + [0] * len(items),
        # data 행: SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO, <항목 측정값...>
        ["s0", 1, 1, 0, 0, 1, ""] + [5] * len(items),        # pass (BIN1)
    ]
    for i, tno in enumerate(tnos):
        # fail die: BIN 은 항목마다 다르게(4, 5, 6 …) 줘서 bin 그룹이 섞이지 않게 한다
        rows.append([f"f{i}", 1, 1, i + 1, 0, 4 + i, tno] + [15] * len(items))
    df = pd.DataFrame(rows, columns=cols)
    return split_honeyform(df, source="src0", file_name="src0")


def _step_of(payload, item):
    """yield_step_groups 를 훑어 item 행이 속한 섹션의 step 을 반환 (없으면 None)."""
    for grp in payload["yield_step_groups"]:
        for g in grp["groups"]:
            for r in g["rows"]:
                if r.get("Item") == item:
                    return grp["step"]
    return None


def _steps_of(payload):
    """yield_step_groups 섹션의 step 목록."""
    return [g["step"] for g in payload["yield_step_groups"]]


def test_sole_step_absorbs_blank():
    """STEP 이 P1 1종뿐이면 STEP 공백 item 의 fail 이 P1 으로 흡수된다."""
    payload = build_report_payload([make_table(["P1", ""])])
    assert _step_of(payload, "Item1") == "P1", \
        f"STEP 공백 item 이 P1 으로 흡수되지 않음: {_step_of(payload, 'Item1')!r}"
    assert _step_of(payload, "Item0") == "P1"
    assert _steps_of(payload) == ["P1"], f"'(기타)' 섹션이 남음: {_steps_of(payload)!r}"
    by_step = payload["yield_summary"]["by_step"]
    assert [s["step"] for s in by_step] == ["P1"], \
        f"by_step 에 빈 STEP 항목이 남음: {[s['step'] for s in by_step]!r}"


def test_multi_step_keeps_blank():
    """STEP 이 2종 이상이면 STEP 공백 fail 은 종전대로 빈 STEP 에 남는다 (현행 보존)."""
    payload = build_report_payload([make_table(["P1", "P2", ""])])
    assert _step_of(payload, "Item2") == "", \
        f"STEP 2종인데 흡수됨: {_step_of(payload, 'Item2')!r}"
    assert _steps_of(payload) == ["P1", "P2", ""], _steps_of(payload)


def test_no_step_at_all_unchanged():
    """비어있지 않은 STEP 이 0종이면 흡수할 대상이 없어 현행과 동일 (빈 섹션 1개)."""
    payload = build_report_payload([make_table(["", ""])])
    assert _steps_of(payload) == [""], _steps_of(payload)


def test_cumulative_invariant():
    """흡수 후에도 survivor + cum_fail == entered 가 pooled·소스별 양쪽에서 성립한다."""
    payload = build_report_payload([make_table(["P1", ""])])
    for s in payload["yield_summary"]["by_step"]:
        assert s["survivor"] + s["cum_fail"] == s["entered"], f"pooled 불변식 깨짐: {s}"
        for src in s["sources"]:
            assert src["survivor"] + src["cum_fail"] == src["entered"], \
                f"소스별 불변식 깨짐: {src}"
    # 흡수는 배치만 바꾸므로 총 fail die 수는 항목 수(2)와 같아야 한다
    last = payload["yield_summary"]["by_step"][-1]
    assert last["cum_fail"] == 2, f"총 누적 fail 이 2 가 아님: {last['cum_fail']}"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    test_sole_step_absorbs_blank()
    test_multi_step_keeps_blank()
    test_no_step_at_all_unchanged()
    test_cumulative_invariant()
    print("PASS: test_yield_step_sole_fallback (4 checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

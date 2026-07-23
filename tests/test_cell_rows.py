"""Excel 왕복 셀 diff 의 **구조화 행**(cell_rows) 검증.

실행:
    python tests/test_cell_rows.py        # pandas·numpy 필요

배경: 확인창이 한 줄짜리 문자열 목록이던 시절엔 수정이 1,500건이면 200건에서 잘리고
(볼 방법 없음) 각 줄이 창 폭을 넘어 접혔다. 지금은 열이 분리된 표로 그리므로
inspect_edited_frame 이 문자열(cells)과 함께 구조화 행(cell_rows)을 돌려준다.

여기서 고정하는 것:
  (a) 두 표현이 갈라지지 않는다 — 문자열은 같은 행에서 파생된다
  (b) cell_limit 은 행 상한, 문자열은 _CELL_TEXT_LIMIT 에서 따로 끊긴다
  (c) 상한에 걸리면 cell_total > len(cell_rows) 로 초과분이 드러난다 (침묵 잘림 금지)
  (d) 메타 컬럼 변경도 행으로 잡힌다
  (e) build_confirm_sections 가 cell_rows 를 그대로 통과시킨다

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from web_report import rawvalues as rv  # noqa: E402
from web_report.honeyform import DATA_START_ROW, META_COLUMNS  # noqa: E402

from test_rawvalues_frame import make_df  # noqa: E402


def _label(row):
    """표 행 → 평문 위치 라벨 (make_df 픽스처 기준으로 손으로 조립 — 문안 드리프트 감시)."""
    parts = [f"SHOT {row['shot']}", f"DUT {row['dut']}",
             f"(X,Y)=({row['x']},{row['y']})", f"BIN {row['bin']}"]
    return " · ".join(parts)


def test_rows_match_text():
    """(a) cell_rows 로 조립한 문자열이 cells 와 글자 그대로 같다."""
    old = make_df(item_values=(5, 6, 7))
    new = make_df(item_values=(5, 60, 70))
    rep = rv.inspect_edited_frame(old, new, source_name="src0")
    assert rep["cell_total"] == 2, rep["cell_total"]
    assert len(rep["cell_rows"]) == 2, rep["cell_rows"]
    for text, row in zip(rep["cells"], rep["cell_rows"]):
        expect = f"{_label(row)} → [{row['item']}] {row['old']} → {row['new']}"
        assert text == expect, f"문자열/행 불일치:\n  {text}\n  {expect}"
    first = rep["cell_rows"][0]
    assert (first["item"], first["old"], first["new"]) == ("ItemA", "6", "60"), first
    assert first["row"] == 2, first          # 1-base 데이터 행 번호
    print(f"  (a) 문자열 ↔ 구조화 행 정합 2건: {rep['cells'][0]}")


def test_text_capped_rows_not():
    """(b) 행은 cell_limit 까지, 문자열은 _CELL_TEXT_LIMIT 에서 끊긴다."""
    n = 250
    old = make_df(item_values=tuple(range(n)))
    new = make_df(item_values=tuple(range(1, n + 1)))
    rep = rv.inspect_edited_frame(old, new, cell_limit=1000)
    assert rep["cell_total"] == n, rep["cell_total"]
    assert len(rep["cell_rows"]) == n, "표는 전량을 받아야 한다"
    assert len(rep["cells"]) == rv._CELL_TEXT_LIMIT, len(rep["cells"])
    print(f"  (b) 행 {len(rep['cell_rows'])}건 전량 · 평문 {len(rep['cells'])}건에서 절단")


def test_limit_is_visible():
    """(c) 상한에 걸리면 총건수가 행 수보다 커서 UI 가 '외 N건'을 띄울 수 있다."""
    n = 250
    old = make_df(item_values=tuple(range(n)))
    new = make_df(item_values=tuple(range(1, n + 1)))
    rep = rv.inspect_edited_frame(old, new, cell_limit=50)
    assert len(rep["cell_rows"]) == 50, len(rep["cell_rows"])
    assert rep["cell_total"] == n, rep["cell_total"]
    assert rep["cell_total"] > len(rep["cell_rows"]), "초과분이 드러나지 않는다"
    print(f"  (c) 상한 50 → 행 50건 / 총 {rep['cell_total']}건 (초과 노출)")


def test_meta_change_is_a_row():
    """(d) 메타 컬럼(BIN) 변경도 표 행으로 잡힌다."""
    old = make_df()
    new = make_df()
    bin_col = list(META_COLUMNS).index("BIN")
    new.iat[DATA_START_ROW + 1, bin_col] = 9
    rep = rv.inspect_edited_frame(old, new)
    rows = rep["cell_rows"]
    assert len(rows) == 1 and rows[0]["item"] == "BIN", rows
    assert (rows[0]["old"], rows[0]["new"]) == ("1", "9"), rows[0]
    # 위치 라벨은 **원본** 기준 — 편집으로 값이 바뀌어도 어느 행이었는지가 남는다
    assert rows[0]["bin"] == "1", rows[0]
    print(f"  (d) 메타 변경 1건: {rows[0]}")


def test_sections_pass_rows_through():
    """(e) build_confirm_sections 가 cell_rows 를 UI 로 그대로 넘긴다."""
    old = make_df(item_values=(5, 6, 7))
    new = make_df(item_values=(5, 60, 70))
    rep = rv.inspect_edited_frame(old, new, source_name="Lot1")
    payload = rv.build_confirm_sections([rep], [], {})
    section = payload["sections"][0]
    assert len(section["cell_rows"]) == 2, section["cell_rows"]
    assert section["cell_rows"] == rep["cell_rows"]
    # 변경 없는 source 는 여전히 섹션을 만들지 않는다 (확인창 생략 신호)
    same = rv.inspect_edited_frame(make_df(), make_df(), source_name="Lot2")
    assert rv.build_confirm_sections([same], [], {})["sections"] == []
    print("  (e) build_confirm_sections 통과 + 무변경 source 는 섹션 없음")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = [
        test_rows_match_text,
        test_text_capped_rows_not,
        test_limit_is_visible,
        test_meta_change_is_a_row,
        test_sections_pass_rows_through,
    ]
    for fn in checks:
        fn()
    print(f"PASS: test_cell_rows ({len(checks)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

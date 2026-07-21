"""Excel 왕복 프레임 교정·검사 검증 (rawvalues 프레임 단위 함수).

실행:
    python tests/test_rawvalues_frame.py        # pandas·numpy 필요

Excel 은 자유 편집 도구라 셀 단위로 하드 거부하지 않는 대신, 조용히 리포트를 망치는
오염을 (1) 자동 교정하고 (2) 나머지는 확인창 경고로 알린다. 그 두 갈래를 고정한다.

  (a) sanitize_excel_frame — used_range 확장으로 들어온 유령 행/열 제거,
      메타 컬럼명 대소문자 복원 (없으면 저장은 되고 조회만 500)
  (b) restore_int_columns — xlwings 가 float 로 돌려준 값을 '원본이 int 였던' 컬럼만 복원
      ('전부 정수면 int' 로 판정하면 원래 float 컬럼이 뒤집혀 회귀 기준을 반대로 깬다)
  (c) inspect_edited_frame — 구조 변화·메타 6행 경고·값 경고·셀 diff (절대 raise 안 함)
  (d) build_confirm_message — 변경 없으면 빈 문자열(확인창 스킵)

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from web_report import rawvalues as rv  # noqa: E402
from web_report.honeyform import DATA_START_ROW, META_COLUMNS  # noqa: E402


def make_df(item_values=(5, 6, 7), items=("ItemA",), meta=None):
    """최소 honeyform 프레임 — 메타 6행 + die 3행."""
    cols = list(META_COLUMNS) + list(items)
    meta = meta or {}
    head = []
    for label in ("TSEQ", "TNO", "STEP", "UNIT", "HILIM", "LOLIM"):
        row = [label] + [""] * (len(META_COLUMNS) - 1)
        row += [meta.get(label, {}).get(it, {"TNO": 100, "HILIM": 10, "LOLIM": 0}.get(label, ""))
                for it in items]
        head.append(row)
    body = []
    for i, value in enumerate(item_values):
        body.append([f"s{i + 1}", 1, 1, i, 0, 1, ""] + [value] * len(items))
    return pd.DataFrame(head + body, columns=cols)


def test_ghost_rows():
    df = make_df()
    # used_range 가 아래로 2행, 오른쪽 1열 확장된 상태를 흉내낸다
    ghost = pd.DataFrame([[None] * len(df.columns)] * 2, columns=df.columns)
    df = pd.concat([df, ghost], ignore_index=True)
    df[""] = None
    out, fixes = rv.sanitize_excel_frame(df)
    assert len(out) == DATA_START_ROW + 3, f"유령 행이 남았다: {len(out)}"
    assert "" not in list(out.columns), "이름 없는 빈 컬럼이 남았다"
    assert any("빈 행 2개" in f for f in fixes), fixes
    assert any("빈 컬럼" in f for f in fixes), fixes
    # 메타 6행은 보호 — 전부 빈 메타 행이 있어도 지우지 않는다(라벨은 A열에 있다)
    print(f"  (a1) 유령 행/열 제거 + 보고: {fixes}")


def test_meta_case_fix():
    df = make_df()
    df.columns = ["Serial", "shot", "Dut", "XPos", "YPOS", "Bin", "FailTNO", "ItemA"]
    out, fixes = rv.sanitize_excel_frame(df)
    assert list(out.columns[:len(META_COLUMNS)]) == list(META_COLUMNS), list(out.columns)
    assert any("대소문자" in f for f in fixes), fixes
    print("  (a2) 메타 컬럼명 대소문자 복원 (저장 성공 후 조회 500 방지)")


def test_restore_int():
    df = make_df(item_values=(5.0, 6.0, 7.0), items=("IntItem", "FloatItem"))
    # xlwings 는 숫자를 전부 float 로 돌려준다 — 원본이 int 였던 컬럼만 되돌려야 한다
    out, restored = rv.restore_int_columns(df, {"IntItem"})
    ints = out["IntItem"].iloc[DATA_START_ROW:].tolist()
    floats = out["FloatItem"].iloc[DATA_START_ROW:].tolist()
    assert all(isinstance(v, int) for v in ints), ints
    assert all(isinstance(v, float) for v in floats), floats
    assert restored >= 1
    # 사용자가 소수를 넣었으면 정직하게 float 로 남긴다(강제 int 캐스팅 금지)
    df2 = make_df(item_values=(5.5, 6.0, 7.0), items=("IntItem",))
    out2, _ = rv.restore_int_columns(df2, {"IntItem"})
    assert out2["IntItem"].iloc[DATA_START_ROW] == 5.5
    print("  (b) 정수 dtype 복원 — 원본 int 컬럼만, 소수 입력은 보존")


def test_inspect_warnings():
    old = make_df()
    # 규격 뒤집힘 + TNO 0 + 비수치 측정값 + BIN 문자
    new = make_df(meta={"HILIM": {"ItemA": 0}, "LOLIM": {"ItemA": 10}, "TNO": {"ItemA": 0}})
    new.at[DATA_START_ROW, "ItemA"] = "5o"
    new.at[DATA_START_ROW + 1, "BIN"] = "abc"
    rep = rv.inspect_edited_frame(old, new, source_name="src0")
    joined = " ".join(rep["meta_warnings"] + rep["value_warnings"])
    for token in ("규격 상하한이 뒤집힌", "TNO 가 비었거나 0", "숫자로 읽을 수 없는 측정값",
                  "BIN 이 비었거나 정수가 아닌"):
        assert token in joined, f"경고 누락: {token} / 실제: {joined}"
    assert rep["cell_total"] >= 2, rep
    assert not rep["skipped_cell_diff"], rep
    print(f"  (c1) 경고 {len(rep['meta_warnings']) + len(rep['value_warnings'])}건 + "
          f"셀 diff {rep['cell_total']}건")


def test_inspect_structure_and_nan():
    old = make_df(item_values=(5, None, 7))
    new = make_df(item_values=(5, None, 7))
    rep = rv.inspect_edited_frame(old, new, source_name="src0")
    # 양쪽 다 결측인 셀을 '변경'으로 세면 안 된다 (NaN != NaN 함정)
    assert rep["cell_total"] == 0, rep["cells"]
    # item 컬럼 이름 변경은 구조 경고로 잡는다
    renamed = make_df(items=("ItemB",))
    rep2 = rv.inspect_edited_frame(make_df(items=("ItemA",)), renamed)
    assert any("이름이" in s for s in rep2["structure"]), rep2["structure"]
    assert rep2["skipped_cell_diff"], "컬럼이 달라지면 셀 diff 를 건너뛰어야 한다"
    # 행 수 변화 보고
    rep3 = rv.inspect_edited_frame(make_df(item_values=(5, 6, 7)), make_df(item_values=(5, 6)))
    assert any("측정 행이" in s for s in rep3["structure"]), rep3["structure"]
    print("  (c2) 양쪽 결측 제외 · 항목명 변경 · 행 수 변화")


def test_inspect_budget_and_never_raises():
    old, new = make_df(), make_df()
    new.at[DATA_START_ROW, "ItemA"] = 99
    rep = rv.inspect_edited_frame(old, new, cell_budget=1)     # 예산 초과 흉내
    assert rep["skipped_cell_diff"] and rep["cell_total"] == 0, rep
    # 뼈대가 깨진 입력에도 예외를 올리지 않는다(하드 거부는 encode 담당).
    # 특히 메타 컬럼명이 통째로 바뀐 경우 KeyError 를 내면, 재편집 루프가 ValueError 만
    # 잡으므로 Honey 가 친절한 안내 대신 크래시한다.
    rv.inspect_edited_frame(None, new)
    rv.inspect_edited_frame(old, new.iloc[:2])
    renamed = new.copy()
    renamed.columns = ["WRONG"] + list(new.columns[1:])
    rep2 = rv.inspect_edited_frame(old, renamed)
    assert rep2["value_warnings"] == [] and rep2["skipped_cell_diff"], rep2
    print("  (c3) 예산 초과 skip · 메타 컬럼명 파손에도 raise 안 함")


def test_confirm_message():
    assert rv.build_confirm_message([], []) == "", "변경이 없으면 확인창을 띄우지 않는다"
    old, new = make_df(), make_df()
    new.at[DATA_START_ROW, "ItemA"] = 99
    rep = rv.inspect_edited_frame(old, new, source_name="src0")
    msg = rv.build_confirm_message([rep], ["LotB"], fixes_by_source={"src0": ["빈 행 1개 제거"]})
    for token in ("src0", "셀 1개가 바뀌었습니다", "자동 교정", "시트 삭제 감지", "LotB", "반영할까요?"):
        assert token in msg, f"확인창 문안 누락: {token}\n{msg}"
    print("  (d) 확인창 문안 — 셀 diff·자동 교정·시트 삭제 통합")


def main():
    print("(a) sanitize_excel_frame")
    test_ghost_rows()
    test_meta_case_fix()
    print("(b) restore_int_columns")
    test_restore_int()
    print("(c) inspect_edited_frame")
    test_inspect_warnings()
    test_inspect_structure_and_nan()
    test_inspect_budget_and_never_raises()
    print("(d) build_confirm_message")
    test_confirm_message()
    print("\n모든 검증 통과")


if __name__ == "__main__":
    main()

"""item 컬럼명 중복 자동 개명 회귀 테스트 (duplicate item columns 방지책).

실행:
    python tests/test_honeyform_dedupe.py

배경: 소스 CSV 에 같은 측정 항목명(_Vslope 등)이 물리적으로 2번 있고, 컬럼 라벨을 직접
대입하는 로더를 거치면 pandas 의 자동 dedup 을 안 타 중복이 그대로 남는다. 예전에는
validate 가 이를 거부해 업로드 자체가 막혔다. 지금은 dedupe_item_columns 가 등장 순서대로
_2, _3 을 붙여 통과시킨다.

핵심 불변: **첫 등장은 이름이 안 바뀐다** — 편집 DB 의 item_key(edits.py) 보존 + 개명 멱등
(Excel 왕복 재인코딩이 이 멱등성에 의존).

pytest 미사용(그건 eval_analyzer 전용) — 자체 실행 + assert 스타일(web_report tests/ 관례).
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, dedupe_item_columns, decode_split_honeyform_parquet,
    encode_honeyform_parquet, split_honeyform, validate_honeyform_df)


def make_df(items):
    """items 이름 목록으로 최소 honeyform 프레임 (메타 6행 + 데이터 2행).

    pd.DataFrame(rows, columns=...) 은 컬럼 라벨을 직접 대입하므로 중복이 보존된다 —
    실제 버그 재현 경로(csv_loader 의 df.columns 직접 대입)와 같은 조건.
    """
    n = len(items)
    pad = [""] * 6                      # SHOT..FAILTNO
    rows = [
        ["TSEQ"] + pad + list(range(1, n + 1)),
        ["TNO"] + pad + [100 + i for i in range(n)],
        ["STEP"] + pad + ["P1"] * n,
        ["UNIT"] + pad + ["V"] * n,
        ["HILIM"] + pad + [10] * n,
        ["LOLIM"] + pad + [0] * n,
        ["s1", 1, 1, 0, 0, 1, ""] + [1.0 + i for i in range(n)],
        ["s2", 1, 1, 1, 0, 1, ""] + [2.0 + i for i in range(n)],
    ]
    return pd.DataFrame(rows, columns=list(META_COLUMNS) + list(items))


def test_basic_rename():
    """등장 순서대로 _2, _3 — 첫 등장과 중복 아닌 항목은 이름 유지."""
    df, renames = dedupe_item_columns(
        make_df(["_Vslope", "IDD", "_Vslope", "_Vslope"]))
    assert list(df.columns[7:]) == ["_Vslope", "IDD", "_Vslope_2", "_Vslope_3"], \
        list(df.columns[7:])
    assert renames == [("_Vslope", "_Vslope_2"), ("_Vslope", "_Vslope_3")], renames
    assert not validate_honeyform_df(df), validate_honeyform_df(df)
    print("ok: basic rename")


def test_no_duplicate_is_noop():
    """중복이 없으면 원본 df 객체를 그대로 돌려준다 (기존 경로 무영향)."""
    src = make_df(["ItemA", "ItemB"])
    df, renames = dedupe_item_columns(src)
    assert df is src, "중복 없는 프레임은 복사조차 하지 않아야 한다"
    assert renames == [], renames
    print("ok: no-duplicate no-op")


def test_idempotent():
    """개명 결과를 다시 넣어도 불변 — Excel 왕복 재인코딩이 이 성질에 의존."""
    once, r1 = dedupe_item_columns(make_df(["A", "A", "A"]))
    twice, r2 = dedupe_item_columns(once)
    assert list(once.columns) == list(twice.columns), list(twice.columns)
    assert len(r1) == 2 and r2 == [], (r1, r2)
    print("ok: idempotent")


def test_collision_with_existing_name():
    """'A, A, A_2' — 원본 A_2 를 침범하지 않고 A_3 으로 밀어낸다."""
    df, renames = dedupe_item_columns(make_df(["A", "A", "A_2"]))
    assert list(df.columns[7:]) == ["A", "A_3", "A_2"], list(df.columns[7:])
    assert renames == [("A", "A_3")], renames
    assert len(set(df.columns)) == len(df.columns), "개명 후 중복이 남으면 안 된다"
    print("ok: collision avoidance")


def test_strip_then_dedupe():
    """공백만 다른 이름은 strip 후 같은 이름으로 묶여 개명된다.

    이걸 안 잡으면 클라 검증은 통과하고 _string_frame_for_parquet 의 strip 뒤에
    parquet 안에서 비로소 중복이 되어 서버 디코드에서 터진다.
    """
    df, renames = dedupe_item_columns(make_df(["_Vslope", "_Vslope "]))
    assert list(df.columns[7:]) == ["_Vslope", "_Vslope_2"], list(df.columns[7:])
    assert renames == [("_Vslope", "_Vslope_2")], renames
    print("ok: strip then dedupe")


def test_meta_collision_still_rejected():
    """item 이름이 메타 컬럼명과 겹치는 건 개명 대상이 아니라 그대로 거부."""
    df, _ = dedupe_item_columns(make_df(["BIN", "BIN"]))
    issues = validate_honeyform_df(df)
    assert any("collide with meta columns" in i for i in issues), issues
    print("ok: meta collision still rejected")


def test_split_meta_dicts_intact():
    """중복이 남으면 df.at 라벨 조회가 Series 를 반환해 메타 dict 가 뭉개진다 — 핵심 검사."""
    t = split_honeyform(make_df(["_Vslope", "IDD", "_Vslope"]),
                        source="src0", file_name="src0")
    assert t.item_columns == ["_Vslope", "IDD", "_Vslope_2"], t.item_columns
    for name, d in (("tseq", t.tseq), ("tno", t.tno), ("step", t.step),
                    ("units", t.units), ("hilim", t.hilim), ("lolim", t.lolim)):
        assert len(d) == 3, f"{name} 키가 뭉개졌다: {d}"
        assert all(not isinstance(v, pd.Series) for v in d.values()), f"{name}: {d}"
    # TNO 는 컬럼 위치별로 100,101,102 — 개명본이 위치를 유지하는지 확인
    assert [t.tno[c] for c in t.item_columns] == [100, 101, 102], t.tno
    print("ok: split meta dicts intact")


def test_parquet_roundtrip():
    """encode → decode 왕복 후에도 개명본이 유지되고 메타가 항목수만큼 살아있다."""
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        print("skip: parquet roundtrip (pyarrow 없음)")
        return
    data = encode_honeyform_parquet(make_df(["_Vslope", "IDD", "_Vslope"]))
    t = decode_split_honeyform_parquet(data, source="src0", file_name="src0")
    assert t.item_columns == ["_Vslope", "IDD", "_Vslope_2"], t.item_columns
    assert len(t.units) == 3 and len(t.hilim) == 3, (t.units, t.hilim)
    assert list(t.data.columns[7:]) == ["_Vslope", "IDD", "_Vslope_2"], list(t.data.columns)
    print("ok: parquet roundtrip")


def test_normal_frame_unchanged_through_split():
    """중복 없는 프레임의 split 결과가 개명 도입 전과 동일한지 (회귀 방지)."""
    t = split_honeyform(make_df(["ItemA", "ItemB"]), source="s", file_name="s")
    assert t.item_columns == ["ItemA", "ItemB"], t.item_columns
    assert t.tno == {"ItemA": 100, "ItemB": 101}, t.tno
    assert t.data["ItemA"].tolist() == [1.0, 2.0], t.data["ItemA"].tolist()
    print("ok: normal frame unchanged")


if __name__ == "__main__":
    test_basic_rename()
    test_no_duplicate_is_noop()
    test_idempotent()
    test_collision_with_existing_name()
    test_strip_then_dedupe()
    test_meta_collision_still_rejected()
    test_split_meta_dicts_intact()
    test_parquet_roundtrip()
    test_normal_frame_unchanged_through_split()
    print("\nall passed")

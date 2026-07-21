"""웹 Raw Data 셀 편집의 값 검증 (tabs.raw_data.apply_raw_data_edits).

실행:
    python tests/test_raw_data_edit_validation.py       # pandas 필요

지금까지는 편집 값을 그대로 대입해서, 측정값 오타는 NaN 으로 사라지고 BIN 문자는 fail die
로 집계돼 수율이 조용히 바뀌었다. 편집한 셀만 검증하고(기존 데이터는 소급 거부하지 않는다),
위반이 있으면 **한 셀도 쓰지 않고** 거부하는지 확인한다.

  (a) 정상 편집은 정규형으로 저장 ('01' → '1')
  (b) 오타 측정값 / BIN 문자 / SERIAL 빈값 거부 + 메시지에 위치·건수 포함
  (c) 거부 시 df 무손상 (부분 저장 없음)
  (d) 기존 데이터의 이상값은 편집과 무관하면 그대로 통과 (소급 거부 금지)
  (e) 구조 오류(unknown source/column, row_idx 범위)는 기존대로 즉시 거부

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from web_report.honeyform import (  # noqa: E402
    DATA_START_ROW, META_COLUMNS, split_honeyform,
)
from web_report.tabs.raw_data import apply_raw_data_edits  # noqa: E402


def make_table(source="src0"):
    cols = list(META_COLUMNS) + ["ItemA"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1],
        ["TNO", "", "", "", "", "", "", 100],
        ["STEP", "", "", "", "", "", "", "P2"],
        ["UNIT", "", "", "", "", "", "", "V"],
        ["HILIM", "", "", "", "", "", "", 10],
        ["LOLIM", "", "", "", "", "", "", 0],
        ["s1", 1, 1, 0, 0, 1, "", 5],
        ["s2", 1, 1, 1, 0, 1, "", 6],
        ["s3", 1, 1, 2, 0, 2, "", 7],
    ]
    return split_honeyform(pd.DataFrame(rows, columns=cols), source=source)


def edit(source="src0", row=0, column="ItemA", value="9"):
    return {"source": source, "row_idx": row, "column": column, "value": value}


def expect_reject(edits, hint, *, tokens=()):
    table = make_table()
    before = table.df.copy(deep=True)
    try:
        apply_raw_data_edits([table], edits)
    except ValueError as exc:
        message = str(exc)
        for token in tokens:
            assert token in message, f"{hint}: 메시지에 {token!r} 없음 — {message}"
        # 거부 시 원본이 한 셀도 바뀌지 않아야 한다 (부분 저장 없음)
        assert table.df.equals(before), f"{hint}: 거부됐는데 df 가 변형됐다"
        print(f"  거부됨({hint}): {message.splitlines()[0]}")
        return message
    raise AssertionError(f"{hint}: 거부돼야 하는데 통과했다")


def main():
    print("(a) 정상 편집 — 정규형 저장")
    table = make_table()
    out = apply_raw_data_edits([table], [edit(value="9.5"), edit(row=1, column="BIN", value="01")])
    assert out[0].df.at[DATA_START_ROW, "ItemA"] == "9.5"
    assert out[0].df.at[DATA_START_ROW + 1, "BIN"] == "1", out[0].df.at[DATA_START_ROW + 1, "BIN"]
    print("  ItemA='9.5', BIN '01' → '1' (표기 차이 정규화)")

    print("(b)(c) 잘못된 값 거부 + 원본 무손상")
    expect_reject([edit(value="5o")], "측정값 오타",
                  tokens=["숫자만", "ItemA", "값이 올바르지 않아 저장하지 않았습니다"])
    expect_reject([edit(column="BIN", value="abc")], "BIN 문자", tokens=["정수만", "BIN"])
    expect_reject([edit(column="BIN", value="")], "BIN 빈값", tokens=["비울 수 없습니다"])
    expect_reject([edit(column="SERIAL", value="")], "SERIAL 빈값", tokens=["비울 수 없습니다"])
    expect_reject([edit(value="nan")], "NaN 문자열", tokens=["숫자만"])
    # 정상 편집이 섞여 있어도 위반이 하나라도 있으면 전부 저장하지 않는다
    msg = expect_reject([edit(value="1"), edit(row=1, value="bad"), edit(row=2, value="2")],
                        "정상+위반 혼합", tokens=["1건 / 전체 3건"])
    assert "BIN 1" in msg or "SHOT" in msg, f"위치 라벨이 없다: {msg}"

    print("(d) 기존 이상값은 소급 거부하지 않음")
    table = make_table()
    table.df.at[DATA_START_ROW + 2, "ItemA"] = "이미 이상한 값"   # 업로드 당시 들어온 값
    out = apply_raw_data_edits([table], [edit(value="3")])        # 다른 셀만 편집
    assert out[0].df.at[DATA_START_ROW + 2, "ItemA"] == "이미 이상한 값"
    print("  편집하지 않은 기존 이상값은 그대로 유지 (편집한 셀만 검증)")

    print("(e) 구조 오류는 기존대로 거부")
    for edits, hint in (
        ([edit(source="nope")], "unknown source"),
        ([edit(column="NoSuchCol")], "unknown column"),
        ([edit(row=99)], "row_idx 범위 밖"),
        ([edit(row="x")], "row_idx 비정수"),
    ):
        expect_reject(edits, hint)

    print("\n모든 검증 통과")


if __name__ == "__main__":
    main()

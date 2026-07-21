"""메타 컬럼명 정규화 + item↔meta 이름 충돌 검증 (honeyform).

실행:
    python tests/test_honeyform_meta_case.py       # pandas·pyarrow 필요

validate_honeyform_df 는 컬럼명을 대소문자 무시로 비교하므로 Excel 에서 'BIN' → 'Bin' 으로
바꿔도 통과한다. 그런데 하류는 data["BIN"] 처럼 대문자 하드코딩이라, 정규화가 없으면
**저장은 성공하고 조회만 KeyError → 500** 이 되어 "저장됐는데 세션이 안 열리는" 상태가 된다.

  (a) encode 는 오염된 컬럼명을 canonical 로 굳혀 저장한다
  (b) decode 는 **이미 저장된** 오염 parquet 도 조회 시점에 구제한다 (마이그레이션 불필요)
      → encode 를 우회해 to_parquet 로 직접 만든 오염 parquet 으로 확인
  (c) split_honeyform 산물의 data["BIN"] 접근이 성공한다
  (d) item 컬럼 이름이 메타 컬럼명과 겹치면 거부한다 (컬럼 밀림 → 오배정/500 방지)
  (e) 검증 실패 메시지는 한국어다

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, decode_honeyform_parquet, decode_split_honeyform_parquet,
    encode_honeyform_parquet, split_honeyform, validate_honeyform_df,
)

DIRTY = ["Serial", "shot", "Dut", "XPos", "﻿YPOS", "Bin", "FailTNO"]


def make_df(meta_cols=None, items=("ItemA",)):
    cols = list(meta_cols or META_COLUMNS) + list(items)
    rows = [
        ["TSEQ", "", "", "", "", "", ""] + [1] * len(items),
        ["TNO", "", "", "", "", "", ""] + [100] * len(items),
        ["STEP", "", "", "", "", "", ""] + ["P2"] * len(items),
        ["UNIT", "", "", "", "", "", ""] + ["V"] * len(items),
        ["HILIM", "", "", "", "", "", ""] + [10] * len(items),
        ["LOLIM", "", "", "", "", "", ""] + [0] * len(items),
        ["s1", 1, 1, 0, 0, 1, ""] + [5] * len(items),
        ["s2", 1, 1, 1, 0, 1, ""] + [6] * len(items),
    ]
    return pd.DataFrame(rows, columns=cols)


def main():
    print("(a) encode — 오염된 메타 컬럼명을 canonical 로 저장")
    dirty = make_df(meta_cols=DIRTY)
    assert not validate_honeyform_df(dirty), "대소문자만 다른 건 검증을 통과해야 한다(기존 동작)"
    restored = decode_honeyform_parquet(encode_honeyform_parquet(dirty))
    assert list(restored.columns[:7]) == META_COLUMNS, list(restored.columns[:7])
    print(f"  {DIRTY} → {META_COLUMNS}")

    print("(b) decode — 이미 저장된 오염 parquet 구제 (encode 우회)")
    buf = BytesIO()
    # encode 를 거치지 않고 직접 써서, 정규화 이전에 저장된 파일을 재현한다
    dirty.astype("string").to_parquet(buf, index=False, engine="pyarrow", compression="zstd")
    blob = buf.getvalue()
    raw = pd.read_parquet(BytesIO(blob), engine="pyarrow")
    assert list(raw.columns[:7]) == DIRTY, "픽스처가 오염 상태여야 한다"
    rescued = decode_honeyform_parquet(blob)
    assert list(rescued.columns[:7]) == META_COLUMNS, list(rescued.columns[:7])
    print("  저장된 'Bin'/BOM 헤더 parquet 이 조회 시점에 복구됨")

    print("(c) split — data['BIN'] 접근 성공 (500 이 나던 지점)")
    table = decode_split_honeyform_parquet(blob, source="src0")
    for column in META_COLUMNS:
        assert column in table.data.columns, f"{column} 이 data 에 없다: {list(table.data.columns)}"
    assert table.data["BIN"].tolist() == [1, 1] or len(table.data["BIN"]) == 2
    assert list(split_honeyform(dirty, source="src0").data.columns[:7]) == META_COLUMNS
    print("  decode_split·split_honeyform 둘 다 canonical 컬럼")

    print("(d) item 컬럼명이 메타 컬럼명과 겹치면 거부")
    collide = make_df(items=("BIN",))
    issues = validate_honeyform_df(collide)
    assert any("collide" in i for i in issues), issues
    try:
        encode_honeyform_parquet(collide)
    except ValueError as exc:
        assert "예약어" in str(exc), str(exc)
        print(f"  거부됨: {exc}")
    else:
        raise AssertionError("item 'BIN' 이 통과했다 — 컬럼이 밀려 TNO/HILIM 이 오배정된다")
    # 대소문자만 다른 겹침도 막는다
    assert any("collide" in i for i in validate_honeyform_df(make_df(items=("bin",))))

    print("(e) 검증 실패 메시지는 한국어")
    broken = make_df()
    broken.columns = ["WRONG"] + list(broken.columns[1:])
    try:
        encode_honeyform_parquet(broken)
    except ValueError as exc:
        assert "앞 7개 컬럼이 규격과 다릅니다" in str(exc), str(exc)
        print(f"  {str(exc).splitlines()[0]}")
    else:
        raise AssertionError("컬럼명이 깨졌는데 통과했다")

    print("\n모든 검증 통과")


if __name__ == "__main__":
    main()

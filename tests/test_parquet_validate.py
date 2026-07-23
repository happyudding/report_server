"""parquet 뼈대 경량 검증(honeyform.validate_parquet_bytes) 검증.

실행:
    python tests/test_parquet_validate.py

rawdata_replace 는 Honey 가 올린 parquet 을 종전에 **전량 디코드**해서 검증하고 결과를
버렸다(수백만 셀 to_numeric). 이제 스키마+메타 6행만 읽는데, **판정과 한국어 메시지가
종전(decode_honeyform_parquet)과 같아야** 클라의 재편집 루프 문안이 그대로 동작한다.
그 동치성을 정상/파손 케이스로 확인한다.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import io
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import (  # noqa: E402
    META_COLUMNS,
    decode_honeyform_parquet,
    encode_honeyform_parquet,
    validate_parquet_bytes,
)


def make_df(n_data_rows=3, columns=None, meta_labels=None):
    cols = columns or (META_COLUMNS + ["ItemA"])
    labels = meta_labels or ["TSEQ", "TNO", "STEP", "UNIT", "HILIM", "LOLIM"]
    width = len(cols)
    rows = [[label] + [""] * (width - 2) + [i + 1] for i, label in enumerate(labels)]
    for i in range(n_data_rows):
        rows.append([f"s{i}", 1, 1, i, 0, 1, ""] + [10] * (width - 7))
    return pd.DataFrame(rows, columns=cols)


def _raw_parquet(df):
    """encode 의 검증을 건너뛰고 파손 프레임을 그대로 parquet 으로 (거부 케이스 재료)."""
    buf = io.BytesIO()
    df.astype("string").to_parquet(buf, index=False, engine="pyarrow", compression="zstd")
    return buf.getvalue()


def _both_verdicts(data):
    """(경량 검증 결과, 전량 디코드 결과) — 각각 None(통과) 또는 메시지 문자열."""
    def _run(fn):
        try:
            fn(data)
        except ValueError as exc:
            return str(exc)
        return None

    return _run(validate_parquet_bytes), _run(decode_honeyform_parquet)


def test_valid_parquet_passes():
    data = encode_honeyform_parquet(make_df())
    light, full = _both_verdicts(data)
    assert light is None and full is None, (light, full)


def test_single_data_row_passes():
    """데이터 1행짜리(최소 조건)도 통과해야 한다."""
    data = encode_honeyform_parquet(make_df(n_data_rows=1))
    assert _both_verdicts(data)[0] is None


def test_bad_meta_columns_rejected_same_message():
    df = make_df()
    df.columns = ["WRONG"] + list(df.columns[1:])
    light, full = _both_verdicts(_raw_parquet(df))
    assert light is not None, "앞 7컬럼 파손이 통과했다"
    assert light == full, f"메시지가 전량 디코드와 다르다:\n  경량: {light}\n  전량: {full}"


def test_bad_meta_row_labels_rejected_same_message():
    df = make_df(meta_labels=["TSEQ", "TNO", "STEP", "UNIT", "HILIM", "OOPS"])
    light, full = _both_verdicts(_raw_parquet(df))
    assert light is not None, "메타 행 라벨 파손이 통과했다"
    assert light == full, f"메시지가 전량 디코드와 다르다:\n  경량: {light}\n  전량: {full}"


def test_no_data_rows_rejected_same_message():
    df = make_df(n_data_rows=0)
    light, full = _both_verdicts(_raw_parquet(df))
    assert light is not None, "데이터 0행이 통과했다"
    assert light == full, f"메시지가 전량 디코드와 다르다:\n  경량: {light}\n  전량: {full}"


def test_not_parquet_rejected():
    light, _ = _both_verdicts(b"not a parquet file at all")
    assert light and "parquet" in light, light


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = [
        test_valid_parquet_passes,
        test_single_data_row_passes,
        test_bad_meta_columns_rejected_same_message,
        test_bad_meta_row_labels_rejected_same_message,
        test_no_data_rows_rejected_same_message,
        test_not_parquet_rejected,
    ]
    for fn in checks:
        fn()
    print(f"PASS: test_parquet_validate ({len(checks)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

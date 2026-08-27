"""CPK TOTAL(전 source rawdata 를 하나의 source 로 통합) 회귀 테스트 (2026-08-27).

실행:
    python tests/test_cpk_total.py

고정하는 계약:
  1. ``sheets["CPK Total"]`` — 항목당 1행, source 는 전부 "TOTAL".
  2. **TOTAL 행의 키가 source 별 행과 완전히 동일** — CPK 하나만이 아니라
     n/min/median/max/average/stdev/cp/cpl/cpu/cpk 10개 통계가 전부 채워진다.
     TOTAL 의 뜻이 "전 source 를 하나로 통합해 **같은 계산**을 돌린 것"이기 때문이다.
  3. ``sheets["CPK"]`` 에는 TOTAL 행이 **없다** — 섞이면 worst_cpk_by_subject 를 거쳐
     Issue Table CPK 섹션 목록·distribution_index·Excel·public API 가 함께 바뀐다
     (CLAUDE.md 규칙 13).
  4. 통계값은 손으로 합친 Series 와 일치한다 — 특히 median/min/max 는 source 별 값에서
     합성할 수 없으므로 실제 병합 계산이었음의 증거다.
  5. Temperature / 단일 source 는 빈 리스트.
  6. limit·units 는 항목이 처음 등장한 source 기준.

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례). pytest 로 수집해도 동작한다.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402
from web_report.tabs.cpk import TOTAL_SOURCE, _stats  # noqa: E402

# source 마다 분포를 다르게 둔다 — 그래야 TOTAL 이 어느 한 source 의 복사가 아님을
# 값으로 구분할 수 있다(평균이 겹치면 merge 여부를 못 가린다).
_S0_A = [9.8, 9.9, 10.0, 10.1, 10.2] * 2
_S1_A = [10.6, 10.7, 10.8, 10.9, 11.0] * 2
_S0_B = [8.6, 9.2, 9.8, 10.4, 11.0] * 2
_S1_B = [9.0, 9.5, 10.0, 10.5, 11.0] * 2
_FAIL_VALUE = 100.0     # fail die 의 극단값 — Bin1 모집단이면 통계에 안 섞인다


def make_table(source, a_values, b_values, *, hilim=12, lolim=8, unit="V"):
    """합성 honeyform 1개 — 양품 10 die + fail 1 die(극단값)."""
    cols = META_COLUMNS + ["ItemA", "ItemB"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P1"],
        ["UNIT", "", "", "", "", "", "", unit, unit],
        ["HILIM", "", "", "", "", "", "", hilim, hilim],
        ["LOLIM", "", "", "", "", "", "", lolim, lolim],
    ]
    for i, (a, b) in enumerate(zip(a_values, b_values)):
        rows.append([f"{source}p{i}", 1, 1, i, 0, 1, "", a, b])
    rows.append([f"{source}f0", 1, 1, 90, 0, 5, 100, _FAIL_VALUE, _FAIL_VALUE])
    return split_honeyform(pd.DataFrame(rows, columns=cols),
                           source=source, file_name=source)


def two_tables():
    return [make_table("src0", _S0_A, _S0_B), make_table("src1", _S1_A, _S1_B)]


def total_rows(payload):
    return payload["sheets"]["CPK Total"]


def total_row(payload, subject):
    for r in total_rows(payload):
        if r["subject"] == subject:
            return r
    raise AssertionError(f"CPK Total 행 없음: {subject}")


def test_total_rows_exist():
    """항목당 1행 · source 는 전부 TOTAL."""
    payload = build_report_payload(two_tables())
    rows = total_rows(payload)
    assert [r["subject"] for r in rows] == ["ItemA", "ItemB"], rows
    assert all(r["source"] == TOTAL_SOURCE for r in rows), rows


def test_total_row_has_all_statistics():
    """TOTAL 은 CPK 만이 아니라 source 별 행과 **같은 키 전부**를 낸다.

    "TOTAL = 전 source 를 하나로 통합해 같은 계산을 돌린 것" 이라는 정의를 기계로 고정한다
    — 구현이 cpk 하나만 내도록 퇴화하면 여기서 걸린다.
    """
    payload = build_report_payload(two_tables())
    src_keys = set(payload["sheets"]["CPK"][0].keys())
    for row in total_rows(payload):
        assert set(row.keys()) == src_keys, (set(row.keys()) ^ src_keys)
        # 10개 통계가 실제로 채워져 있어야 한다(키만 있고 None 이면 의미가 없다).
        for k in ("n", "min", "median", "max", "average", "stdev",
                  "cp", "cpl", "cpu", "cpk"):
            assert row[k] is not None, (row["subject"], k, row)


def test_cpk_sheet_has_no_total_row():
    """규칙 13 가드 — sheets["CPK"] 오염 금지."""
    payload = build_report_payload(two_tables())
    bad = [r for r in payload["sheets"]["CPK"] if r["source"] == TOTAL_SOURCE]
    assert not bad, bad


def test_total_values_match_merged_series():
    """10개 통계 전부가 손으로 합친 Series 와 일치.

    median/min/max 는 source 별 값에서 합성할 수 없다 — 일치한다는 것 자체가 실제로
    die 를 병합해 계산했다는 증거다(가중평균 합성이었다면 median 이 틀린다).
    """
    payload = build_report_payload(two_tables())
    for subject, values in (("ItemA", _S0_A + _S1_A), ("ItemB", _S0_B + _S1_B)):
        want = _stats(pd.Series(values, dtype="float64"), 8.0, 12.0)
        row = total_row(payload, subject)
        for k, v in want.items():
            assert row[k] == v, (subject, k, row[k], v)


def test_total_n_is_sum_of_bin1_dies():
    """n = 전 source Bin1 die 수의 합 — fail die 는 안 섞인다."""
    payload = build_report_payload(two_tables())
    assert total_row(payload, "ItemA")["n"] == len(_S0_A) + len(_S1_A)
    # fail die 의 극단값(100)이 섞였다면 max 가 그 값이 된다.
    assert total_row(payload, "ItemA")["max"] < _FAIL_VALUE


def test_total_differs_from_each_source():
    """TOTAL 이 어느 한 source 의 복사가 아니다(merge 확인)."""
    payload = build_report_payload(two_tables())
    per_src = [r["average"] for r in payload["sheets"]["CPK"]
               if r["subject"] == "ItemA"]
    total_avg = total_row(payload, "ItemA")["average"]
    assert total_avg not in per_src, (total_avg, per_src)
    assert min(per_src) < total_avg < max(per_src), (total_avg, per_src)


def test_single_source_has_no_total():
    """source 1개면 TOTAL == 그 source — 중복 행만 늘어 만들지 않는다."""
    payload = build_report_payload([make_table("src0", _S0_A, _S0_B)])
    assert total_rows(payload) == [], total_rows(payload)


def test_temperature_has_no_total():
    """Temperature 는 조건이 다른 3집단이라 merge 가 무의미 — 만들지 않는다."""
    tables = [make_table("RT", _S0_A, _S0_B), make_table("CT", _S1_A, _S1_B)]
    payload = build_report_payload(
        tables, mode="Temperature",
        temperature_groups={"groups": [{"rt": "RT", "members": ["RT", "CT"]}]})
    assert total_rows(payload) == [], total_rows(payload)


def test_limit_and_unit_from_first_source():
    """limit·units 는 항목이 처음 등장한 source 기준(setdefault 규약)."""
    tables = [make_table("src0", _S0_A, _S0_B, hilim=12, lolim=8, unit="V"),
              make_table("src1", _S1_A, _S1_B, hilim=20, lolim=1, unit="mV")]
    payload = build_report_payload(tables)
    row = total_row(payload, "ItemA")
    assert (row["lower_limit"], row["upper_limit"]) == (8, 12), row
    assert row["units"] == "V", row


def test_report_survives_total_failure():
    """TOTAL 은 부가 기능 — 계산이 터져도 리포트는 만들어져야 한다.

    실패를 격리하지 않으면 CPK 탭이 아니라 **세션 전체가 안 열린다**.
    """
    import web_report.metrics as metrics

    original = metrics.build_cpk_total_rows
    metrics.build_cpk_total_rows = lambda *a, **k: (_ for _ in ()).throw(
        MemoryError("boom"))
    try:
        payload = build_report_payload(two_tables())
    finally:
        metrics.build_cpk_total_rows = original
    assert total_rows(payload) == [], total_rows(payload)
    # 나머지 시트는 정상 — 리포트가 살아 있다.
    assert payload["sheets"]["CPK"], "CPK 시트가 비었다 — 실패 격리가 안 됐다"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_total_rows_exist,
               test_total_row_has_all_statistics,
               test_cpk_sheet_has_no_total_row,
               test_total_values_match_merged_series,
               test_total_n_is_sum_of_bin1_dies,
               test_total_differs_from_each_source,
               test_single_source_has_no_total,
               test_temperature_has_no_total,
               test_limit_and_unit_from_first_source,
               test_report_survives_total_failure):
        fn()
        checks += 1
    print(f"PASS: test_cpk_total ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""소스별 수율 분모 자동 판정 + 사용자 선택 회귀 테스트 (2026-07-28 규칙).

실행:
    python tests/test_yield_basis_auto.py

고정하는 계약 (사용자 확정 규칙):
  1. 분모는 Gross Die 가 기준이다.
  2. 수율은 100% 를 넘을 수 없다 — 넘으면 분모가 잘못된 것이므로 다른 기준을 쓴다.
  3. 그 source 의 Gross Die 가 그 source 의 test die 보다 작으면 test die 를 분모로.
  4. test die 가 Gross Die 보다 100 개 이상 적으면 test die 를 분모로.
  + 사용자 선택(override)은 존중하되 2번(=3번)은 강제한다 — Gross 를 고를 수 없는 source 는
    무엇을 고르든 test die 분모다.
  + 판정은 **source 마다 독립**이라 한 세션에서 기준이 섞일 수 있다(payload basis="mixed").

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402
from web_report.tabs.yield_tab import (GROSS_SHORTFALL_LIMIT, auto_basis,  # noqa: E402
                                       resolve_source_basis)


def make_table(source, n_dies, n_fail=1):
    """합성 honeyform 테이블 — 측정 die n_dies 개 중 n_fail 개가 ItemA fail(P1)."""
    cols = META_COLUMNS + ["ItemA"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1],
        ["TNO", "", "", "", "", "", "", 100],
        ["STEP", "", "", "", "", "", "", "P1"],
        ["UNIT", "", "", "", "", "", "", "V"],
        ["HILIM", "", "", "", "", "", "", 10],
        ["LOLIM", "", "", "", "", "", "", 0],
    ]
    for i in range(n_dies):
        fail = i >= n_dies - n_fail
        rows.append([f"{source}-{i}", 1, 1, i % 10, i // 10,
                     4 if fail else 1, 100 if fail else "", 15 if fail else 5])
    return split_honeyform(pd.DataFrame(rows, columns=cols), source=source, file_name=source)


def test_auto_basis_boundaries():
    """규칙 1~4 의 경계값 — 순수 함수 auto_basis(gross, tested)."""
    assert GROSS_SHORTFALL_LIMIT == 100
    assert auto_basis(1000, 1000) == ("gross", "")            # 정확히 일치
    assert auto_basis(1000, 901) == ("gross", "")             # 부족분 99 — 아직 gross
    assert auto_basis(1000, 900) == ("test", "tested_short")  # 부족분 100 — 규칙 4
    assert auto_basis(1000, 1001) == ("test", "gross_lt_tested")   # 규칙 2·3
    assert auto_basis(None, 10) == ("test", "no_gross")       # 기준정보 없음
    assert auto_basis(0, 10) == ("test", "no_gross")


def test_resolve_per_source_independently():
    """한 세션 안에서 source 마다 따로 판정한다 (규칙 3 — '각 Source 의')."""
    tables = [make_table("A", 100), make_table("B", 220)]     # gross 200 기준
    info = resolve_source_basis(tables, gross_die=200)
    assert info["A"]["basis"] == "test" and info["A"]["reason"] == "tested_short", info["A"]
    assert info["A"]["total"] == 100, info["A"]
    assert info["B"]["basis"] == "test" and info["B"]["reason"] == "gross_lt_tested", info["B"]
    assert info["B"]["total"] == 220, info["B"]

    # 둘 다 gross 와 100 미만 차이면 gross 기준
    tables = [make_table("A", 190), make_table("B", 200)]
    info = resolve_source_basis(tables, gross_die=200)
    assert [i["basis"] for i in info.values()] == ["gross", "gross"], info
    assert [i["total"] for i in info.values()] == [200, 200], info


def test_override_respected_and_forced():
    """사용자 선택은 존중하되, 수율 100% 초과가 되는 Gross 선택은 test 로 강제한다."""
    tables = [make_table("A", 150), make_table("B", 220)]
    basis_map = {"mode": "auto", "sources": {"A": "gross", "B": "gross"}}
    info = resolve_source_basis(tables, gross_die=200, basis_map=basis_map)
    # A: 부족분 50 (<100) 이라 자동도 gross — 사용자 선택과 같다
    assert info["A"]["basis"] == "gross" and info["A"]["forced"] is False, info["A"]
    assert info["A"]["gross_allowed"] is True, info["A"]
    # B: 측정 die 220 > gross 200 → Gross 를 고를 수 없다(고르면 수율 110%)
    assert info["B"]["basis"] == "test" and info["B"]["forced"] is True, info["B"]
    assert info["B"]["gross_allowed"] is False, info["B"]

    # 전역 test(구 세션 스위치)는 소스별 지정이 없는 소스의 override 로 쓰인다
    info = resolve_source_basis(tables, gross_die=200, basis_map={"mode": "test"})
    assert [i["basis"] for i in info.values()] == ["test", "test"], info


def test_payload_mixed_basis():
    """소스마다 기준이 다르면 payload.yield_basis.basis == "mixed" + by_source 분해."""
    tables = [make_table("A", 200, n_fail=20), make_table("B", 100, n_fail=10)]
    payload = build_report_payload(
        tables, gross_die=200, yield_basis={"mode": "auto", "sources": {"B": "test"}})
    basis = payload["yield_basis"]
    assert basis["basis"] == "mixed" and basis["gross_die"] == 200, basis
    by_src = {b["source"]: b for b in basis["by_source"]}
    assert (by_src["A"]["basis"], by_src["A"]["total"]) == ("gross", 200), by_src["A"]
    assert (by_src["B"]["basis"], by_src["B"]["total"]) == ("test", 100), by_src["B"]

    # Yield 표·요약이 그 분모를 그대로 쓴다 (A: 180/200=90%, B: 90/100=90%)
    pass_row = payload["sheets"]["Yield"][0]
    assert (pass_row["A_yield"], pass_row["B_yield"]) == (90.0, 90.0), pass_row
    ov = payload["yield_summary"]
    assert ov["total"] == 300 and ov["pass"] == 270, ov
    src = {s["source"]: s for s in ov["by_source"]}
    assert (src["A"]["total"], src["B"]["total"]) == (200, 100), src

    # Issue Table 은 yield_rows 를 그대로 옮기므로 같은 값
    issue_pass = [r for r in payload["sheets"]["Issue Table"]
                  if str(r.get("Bin")).strip() == "1"]
    assert issue_pass and issue_pass[0]["A_yield"] == 90.0, issue_pass[:1]


def test_yield_never_exceeds_100():
    """규칙 2 — 자동 판정이면 어떤 소스도 수율 100% 를 넘지 않는다."""
    tables = [make_table("A", 500, n_fail=0), make_table("B", 30, n_fail=0)]
    payload = build_report_payload(tables, gross_die=100)
    for s in payload["yield_summary"]["by_source"]:
        assert s["yield_pct"] <= 100.0, s
    assert payload["yield_summary"]["yield_pct"] <= 100.0, payload["yield_summary"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_auto_basis_boundaries, test_resolve_per_source_independently,
               test_override_respected_and_forced, test_payload_mixed_basis,
               test_yield_never_exceeds_100):
        fn()
        checks += 1
    print(f"PASS: test_yield_basis_auto ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

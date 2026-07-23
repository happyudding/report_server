"""CPK 통계 = Bin1(양품) 기준 단일 값 회귀 테스트 (2026-07-23 통일).

실행:
    python tests/test_cpk_bin1_basis.py

고정하는 계약:
  1. ``build_cpk_rows`` 의 base 필드(n/average/stdev/cpk/…)는 **BIN==1 die 만**으로 낸다
     — 전체 die 기준이 아니다(fail die 의 극단값이 통계에 섞이지 않는다).
  2. ``*_bin1`` / ``*_limited`` 병기는 없다 — 기준이 하나뿐이라 CPK 탭 토글도 없앴다.
  3. Issue Table CPK 섹션(1.33 미만 선정 + 표시값)과 distribution_index 의 cpk 가
     **CPK 시트와 같은 값**이다 — 리포트 전체에서 CPK 가 한 가지 뜻이다.

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례). pytest 로 수집해도 동작한다.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402

# ItemA = 양품이 촘촘(높은 cpk) / ItemB = 양품이 넓게 퍼짐(cpk < 1.33 → Issue Table 대상).
_A_BIN1 = [9.8, 9.9, 10.0, 10.1, 10.2] * 4
_B_BIN1 = [8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 11.5] + [10.0] * 13
_FAIL_VALUE = 100.0     # fail die 의 극단값 — 전체 die 기준이면 통계가 여기에 끌려간다


def make_table():
    """합성 honeyform 1개 — 양품 20 die + fail 2 die(극단값). LOLIM 8 / HILIM 12."""
    cols = META_COLUMNS + ["ItemA", "ItemB"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P1"],
        ["UNIT", "", "", "", "", "", "", "V", "V"],
        ["HILIM", "", "", "", "", "", "", 12, 12],
        ["LOLIM", "", "", "", "", "", "", 8, 8],
    ]
    for i, (a, b) in enumerate(zip(_A_BIN1, _B_BIN1)):
        rows.append([f"p{i}", 1, 1, i, 0, 1, "", a, b])
    # fail die 2개 — BIN≠1 이라 통계에서 빠져야 한다(수율에는 그대로 잡힌다).
    rows.append(["f1", 1, 1, 20, 0, 5, 100, _FAIL_VALUE, 10.0])
    rows.append(["f2", 1, 1, 21, 0, 6, 200, 10.0, _FAIL_VALUE])
    return split_honeyform(pd.DataFrame(rows, columns=cols), source="src0", file_name="src0")


def expected(values, lo=8.0, hi=12.0):
    """Bin1 값 목록으로 직접 계산한 (n, average, cpk) — 구현과 독립된 기대값."""
    s = pd.Series(values, dtype="float64")
    avg = s.mean()
    std = s.std(ddof=1)
    cpk = min((avg - lo) / (3.0 * std), (hi - avg) / (3.0 * std))
    return len(values), round(avg, 4), round(cpk, 3)


def cpk_row(payload, subject):
    for r in payload["sheets"]["CPK"]:
        if r["subject"] == subject:
            return r
    raise AssertionError(f"CPK 행 없음: {subject}")


def test_base_fields_are_bin1():
    """base 통계 = BIN==1 die 만 — fail die 의 극단값이 섞이지 않는다."""
    payload = build_report_payload([make_table()])
    for subject, values in (("ItemA", _A_BIN1), ("ItemB", _B_BIN1)):
        n, avg, cpk = expected(values)
        row = cpk_row(payload, subject)
        assert row["n"] == n, (subject, row["n"], n)
        assert row["average"] == avg, (subject, row["average"], avg)
        assert row["cpk"] == cpk, (subject, row["cpk"], cpk)
    # 전체 die(=fail die 포함) 기준이었다면 stdev 가 20 배 이상 커져 cpk 가 1 밑으로 떨어진다.
    assert cpk_row(payload, "ItemA")["cpk"] > 3.0, cpk_row(payload, "ItemA")


def test_no_alternate_basis_fields():
    """*_bin1 / *_limited 병기 없음 — 기준이 하나뿐이다(payload 도 그만큼 가벼워진다)."""
    payload = build_report_payload([make_table()])
    for row in payload["sheets"]["CPK"]:
        extra = [k for k in row if k.endswith("_bin1") or k.endswith("_limited")]
        assert not extra, extra


def test_issue_table_and_index_use_same_cpk():
    """Issue Table CPK 섹션·distribution_index 가 CPK 시트와 같은 값을 쓴다."""
    payload = build_report_payload([make_table()])
    a_cpk = cpk_row(payload, "ItemA")["cpk"]
    b_cpk = cpk_row(payload, "ItemB")["cpk"]
    assert b_cpk < 1.33 <= a_cpk, (a_cpk, b_cpk)

    rows = payload["sheets"]["Issue Table"]
    start = next(i for i, r in enumerate(rows) if r.get("Category") == "CPK")
    section = []
    for r in rows[start + 1:]:
        if r.get("Category") == "ETC":
            break
        if r.get("Item"):
            section.append(r)
    items = [r["Item"] for r in section]
    assert items == ["ItemB"], items          # 1.33 미만만 선정 (ItemA 제외)
    assert section[0]["avg"] == b_cpk, (section[0]["avg"], b_cpk)
    assert section[0]["src0_yield"] == b_cpk, (section[0]["src0_yield"], b_cpk)

    index = {r["subject"]: r["cpk"] for r in payload["distribution_index"]}
    assert index["ItemA"] == a_cpk and index["ItemB"] == b_cpk, index
    status = {r["subject"]: r["status"] for r in payload["distribution_index"]}
    # 두 항목 모두 fail die 가 있어 status 는 fail — cpk 기준 분류는 fail 이 우선한다.
    assert status == {"ItemA": "fail", "ItemB": "fail"}, status


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_base_fields_are_bin1, test_no_alternate_basis_fields,
               test_issue_table_and_index_use_same_cpk):
        fn()
        checks += 1
    print(f"PASS: test_cpk_bin1_basis ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

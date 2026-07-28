"""Yield 분모 = 제품 기준정보 Gross Die (없으면 rawdata 폴백) 회귀 테스트.

실행:
    python tests/test_yield_gross_die.py

고정하는 계약 (2026-07-23):
  1. gross_die 가 유효하면 Yield/Issue Table/Summary 의 **분모만** 그 값이 된다
     (분자 = 실측 pass/fail die 수는 불변).
  2. gross_die 가 없거나 형식이 이상하면(빈 값/0/문자) 계산 콘텐츠가 종전(rawdata 분모)과
     정준 JSON 완전 일치 — 폴백이 무회귀임을 보인다.
  3. payload.yield_basis 가 어느 기준을 썼는지 알려주고, yield_summary.tested 가 실제
     측정 die 수를 병기한다 (Gross Die 기준에선 pass+fail < total 일 수 있으므로).

2026-07-28: 분모는 **소스별** 자동 판정이 됐다(test_yield_basis_auto.py 가 규칙 정본).
여기서는 "gross 와 측정 die 차이가 100 미만" 인 픽스처라 자동 판정이 Gross Die 를 고른다.

pytest 미사용 — 자체 실행 + assert 스타일(web_report tests/ 관례).
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402

GROSS_DIE = 20      # 합성 세션의 측정 die 는 10개 — 분모가 2배가 되는 값


def make_table():
    """합성 honeyform 테이블 1개 (호출마다 fresh — build_report_payload 가 item_columns 를
    in-place 변형하므로 재사용 금지).

    측정 die 10개 = pass(BIN1) 8 + ItemA fail 1(P1) + ItemB fail 1(P2).
    rawdata 분모면 pass 80%, Gross Die(20) 분모면 40%.
    """
    cols = META_COLUMNS + ["ItemA", "ItemB"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P2"],
        ["UNIT", "", "", "", "", "", "", "V", "V"],
        ["HILIM", "", "", "", "", "", "", 10, 10],
        ["LOLIM", "", "", "", "", "", "", 0, 0],
    ]
    for i in range(8):
        rows.append([f"p{i}", 1, 1, i, 0, 1, "", 5, 5])          # pass
    rows.append(["f1", 1, 1, 8, 0, 4, 100, 15, 5])               # ItemA fail (P1)
    rows.append(["f2", 1, 1, 9, 0, 5, 200, 5, 15])               # ItemB fail (P2)
    df = pd.DataFrame(rows, columns=cols)
    return split_honeyform(df, source="src0", file_name="src0")


def _canon_content(payload):
    """계산 콘텐츠만 정준화 — selected_items 는 입력 반향 필드(계산값 아님)라 제외."""
    p = dict(payload)
    p.pop("selected_items", None)
    return json.dumps(p, sort_keys=True, ensure_ascii=False, default=str)


def _basis(payload):
    """payload.yield_basis 에서 소스별 분해를 뺀 요약 3필드 (기존 계약 부분)."""
    b = dict(payload["yield_basis"])
    b.pop("by_source", None)
    b.pop("mode", None)
    return b


def _pass_row(payload):
    return payload["sheets"]["Yield"][0]


def _issue_pass_row(payload):
    for row in payload["sheets"]["Issue Table"]:
        if str(row.get("Bin")).strip() == "1":
            return row
    raise AssertionError("Issue Table Pass 행 없음")


def test_rawdata_basis_default():
    """gross_die 미지정 = 종전 동작 (분모 10 → pass 80%)."""
    payload = build_report_payload([make_table()])
    ov = payload["yield_summary"]
    assert ov["total"] == 10 and ov["tested"] == 10, ov
    assert ov["pass"] == 8 and ov["fail"] == 2, ov
    assert ov["yield_pct"] == 80.0, ov
    assert _basis(payload) == {"basis": "test", "gross_die": None}, payload["yield_basis"]
    assert _pass_row(payload)["src0_yield"] == 80.0, _pass_row(payload)


def test_gross_die_basis():
    """gross_die=20 → 분모만 20 (pass 40%), 분자(pass/fail die)는 실측 그대로."""
    payload = build_report_payload([make_table()], gross_die=GROSS_DIE)
    ov = payload["yield_summary"]
    assert ov["total"] == 20, ov            # 분모 = Gross Die
    assert ov["tested"] == 10, ov           # 실제 측정 die 병기
    assert ov["pass"] == 8 and ov["fail"] == 2, ov   # 분자는 실측 그대로
    assert ov["yield_pct"] == 40.0, ov
    assert _basis(payload) == {"basis": "gross", "gross_die": 20}, payload["yield_basis"]

    # 소스별 요약도 같은 분모
    src = ov["by_source"][0]
    assert (src["total"], src["tested"], src["yield_pct"]) == (20, 10, 40.0), src

    # bin fail 행: fail 1건 / 분모 20 = 5%
    fail_rows = [r for r in payload["sheets"]["Yield"] if r.get("Item") == "ItemA"]
    assert fail_rows and fail_rows[0]["src0_yield"] == 5.0, fail_rows

    # STEP 누적: entered=20 고정, P1 = (20-1)/20 = 95%, P2 = (20-2)/20 = 90%
    by_step = {s["step"]: s for s in ov["by_step"]}
    assert by_step["P1"]["entered"] == 20 and by_step["P1"]["step_yield_pct"] == 95.0, by_step["P1"]
    assert by_step["P2"]["step_yield_pct"] == 90.0, by_step["P2"]

    # Issue Table 은 yield_rows 를 그대로 옮기므로 같은 값이어야 한다
    assert _issue_pass_row(payload)["src0_yield"] == 40.0, _issue_pass_row(payload)


def test_invalid_gross_die_falls_back():
    """빈 값/0/문자/음수 gross_die 는 rawdata 분모로 폴백 — 계산 콘텐츠 정준 JSON 완전 일치."""
    base = _canon_content(build_report_payload([make_table()]))
    for bad in (None, "", "  ", "0", "-5", "abc", 0):
        got = _canon_content(build_report_payload([make_table()], gross_die=bad))
        assert got == base, f"gross_die={bad!r} 폴백이 종전 payload 와 다름 (회귀)"


def test_gross_die_text_forms():
    """product_info.db 는 TEXT 컬럼 — '20' / '20.0' / '1,200' 형태를 모두 정수로 읽는다.

    측정 die 가 10개뿐이라 1,200 은 자동 판정이 test 로 내린다(규칙 4) — 파싱 자체를 보려면
    소스별로 Gross 를 명시해야 한다. 파싱값(payload.yield_basis.gross_die)은 어느 쪽이든 같다.
    """
    for value, expected in (("20", 20), ("20.0", 20), (20, 20), ("1,200", 1200)):
        payload = build_report_payload(
            [make_table()], gross_die=value,
            yield_basis={"mode": "auto", "sources": {"src0": "gross"}})
        assert payload["yield_basis"]["gross_die"] == expected, (value, payload["yield_basis"])
        assert payload["yield_summary"]["total"] == expected, (value, payload["yield_summary"])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_rawdata_basis_default, test_gross_die_basis,
               test_invalid_gross_die_falls_back, test_gross_die_text_forms):
        fn()
        checks += 1
    print(f"PASS: test_yield_gross_die ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

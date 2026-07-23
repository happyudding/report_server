"""Rawdata 반영 확인 요약의 **구조화** 빌더 검증 (rawvalues.build_confirm_sections).

실행:
    python tests/test_confirm_sections.py

배경: 확인 내용을 QMessageBox 본문 한 칸에 넣던 시절엔 40줄에서 잘라야 했고(그 위로는
창이 화면을 넘어가 [예]/[아니오] 가 사라졌다), 잘린 내용은 볼 방법이 없었다. 스크롤되는
전용 확인창으로 바꾸면서 **자르지 않는** 구조화 payload 를 쓴다.

여기서는 (1) 변경 없음이면 빈 payload (호출부가 확인창을 건너뛴다), (2) 집계가 실제
내용과 맞는지, (3) 잘림이 없는지, (4) 기존 평문 빌더와 항목 구성이 어긋나지 않는지를 본다.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report import rawvalues  # noqa: E402


def make_report(source="Lot1", n_cells=0, cell_total=0, warnings=(), structure=(),
                skipped=False):
    return {
        "source": source,
        "structure": list(structure),
        "meta_warnings": list(warnings),
        "value_warnings": [],
        "cells": [f"SHOT 1 · (X,Y)=({i},0) → [ItemA] 1 → 2" for i in range(n_cells)],
        "cell_total": cell_total,
        "skipped_cell_diff": skipped,
    }


def test_no_changes_gives_empty_payload():
    """변경이 없는 source 는 섹션을 만들지 않는다 (= 확인창 생략 신호)."""
    payload = rawvalues.build_confirm_sections([make_report()], [], {})
    assert payload["sections"] == [] and payload["removed"] == []
    assert payload["totals"]["sources"] == 0
    # 기존 평문 빌더도 같은 판정(빈 문자열)이어야 한다
    assert rawvalues.build_confirm_message([make_report()], [], {}) == ""


def test_totals_match_content():
    reports = [
        make_report("Lot1", n_cells=20, cell_total=1234, warnings=["규격 뒤집힘"]),
        make_report("Lot2", n_cells=3, cell_total=3),
    ]
    payload = rawvalues.build_confirm_sections(
        reports, ["Lot3"], {"Lot1": ["빈 행 5개 제거", "컬럼 2개 제거"]})
    totals = payload["totals"]
    assert totals == {"sources": 2, "cells": 1237, "warnings": 1, "fixes": 2, "removed": 1}, totals
    assert [s["name"] for s in payload["sections"]] == ["Lot1", "Lot2"]
    assert payload["sections"][0]["fixes"] == ["빈 행 5개 제거", "컬럼 2개 제거"]
    assert payload["removed"] == ["Lot3"]


def test_not_truncated():
    """섹션 payload 는 줄 수 상한이 없다 — 평문 빌더는 40줄에서 잘린다."""
    reports = [make_report("Lot1", n_cells=200, cell_total=200)]
    payload = rawvalues.build_confirm_sections(reports, [], {})
    assert len(payload["sections"][0]["cells"]) == 200
    plain = rawvalues.build_confirm_message(reports, [], {})
    assert "줄 생략" in plain, "평문 빌더의 잘림 동작(하위호환)이 사라졌다"


def test_skipped_cell_diff_still_reported():
    """셀 비교를 생략한 source 도 '변경 있음'으로 봐서 확인창을 띄운다."""
    payload = rawvalues.build_confirm_sections([make_report(skipped=True)], [], {})
    assert len(payload["sections"]) == 1
    assert payload["sections"][0]["skipped_cell_diff"] is True


def test_removed_only():
    """시트 삭제만 있어도 확인 대상이다 (되돌릴 수 없는 동작)."""
    payload = rawvalues.build_confirm_sections([], ["Lot9"], {})
    assert payload["removed"] == ["Lot9"] and payload["totals"]["removed"] == 1


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = [
        test_no_changes_gives_empty_payload,
        test_totals_match_content,
        test_not_truncated,
        test_skipped_cell_diff_still_reported,
        test_removed_only,
    ]
    for fn in checks:
        fn()
    print(f"PASS: test_confirm_sections ({len(checks)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

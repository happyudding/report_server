"""Excel 왕복 편집의 시트↔source 매칭·병합 규칙 검증 (excel_session 순수 함수 2개).

실행:
    python tests/test_excel_sheet_match.py

시트 순서를 바꿔도 원본 순서를 복원해야 하고(순서 기반 매칭의 오귀속 방지), 시트를 지우면
그 source 를 삭제 대상으로 잡아야 한다. 이름만 바뀐 경우는 기존 동작(위치 기반)으로 폴백한다.

_merge_sources 는 **일부 source 만 Excel 로 연 경우**(허브에서 체크) 편집 결과와 손대지 않은
원본 bytes 를 원본 idx 순서로 합친다 — 서버는 업로드 목록에 없는 source 를 지우므로 여기서
빠뜨리면 안 연 source 가 사라진다.

xlwings/Excel 불필요 — 순수 함수만 호출한다. pytest 미사용 (tests/ 관례).
"""
from __future__ import annotations

import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "client"))

# match_sheets 는 순수 문자열 로직이지만 excel_session 모듈 상단이 requests·pandas(honeyform
# 경유)를 import 한다. 둘 다 이 함수와 무관하므로, 없는 환경에서도 검증할 수 있게 빈 스텁으로
# 채운다 (설치돼 있으면 진짜 모듈을 그대로 쓴다).
for _name in ("requests", "pandas"):
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = types.ModuleType(_name)

from excel_edit.excel_session import _Sources, _merge_sources, match_sheets  # noqa: E402

TITLES = ["LotA", "LotB", "LotC"]


def _src(sel):
    """원본 3개 중 sel 만 Excel 로 연 상태의 _Sources (others = 원본 bytes)."""
    return _Sources(dfs=[], titles=[TITLES[i] for i in sel], int_cols=[], manifest={},
                    sel=sel, others={i: f"raw{i}".encode() for i in range(3) if i not in sel},
                    all_titles=TITLES)


def expect_error(sheet_names, titles, hint):
    try:
        match_sheets(sheet_names, titles)
    except ValueError as exc:
        print(f"  거부됨({hint}): {exc}")
        return
    raise AssertionError(f"{hint}: 거부돼야 하는데 통과했다 — {sheet_names}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # (a) 그대로 — 전체 유지, 순서 동일
    pairs, removed = match_sheets(["LotA", "LotB", "LotC"], TITLES)
    print(f"(a) 무변경: pairs={pairs} removed={removed}")
    assert pairs == [(0, 0), (1, 1), (2, 2)], pairs
    assert removed == [], removed

    # (b) 시트 순서 변경 — 원본 idx 순으로 복원돼야 한다 (오귀속 방지)
    pairs, removed = match_sheets(["LotC", "LotA", "LotB"], TITLES)
    print(f"(b) 순서 변경: pairs={pairs} removed={removed}")
    assert pairs == [(1, 0), (2, 1), (0, 2)], pairs
    assert removed == [], removed

    # (c) 대소문자·공백 차이는 같은 시트로 본다 (Excel 시트명은 대소문자 무구분)
    pairs, removed = match_sheets([" lota ", "LOTB", "LotC"], TITLES)
    print(f"(c) 대소문자/공백: pairs={pairs} removed={removed}")
    assert pairs == [(0, 0), (1, 1), (2, 2)], pairs

    # (d) 개수 같고 이름만 바뀜 → 위치 기반 폴백(None) — 기존 동작 유지
    pairs, removed = match_sheets(["LotA", "renamed", "LotC"], TITLES)
    print(f"(d) 이름 변경(동수): pairs={pairs} removed={removed}")
    assert pairs is None, pairs
    assert removed == [], removed

    # (e) 시트 삭제 — 지운 원본 idx 가 removed 로 잡힌다
    pairs, removed = match_sheets(["LotA", "LotC"], TITLES)
    print(f"(e) 가운데 시트 삭제: pairs={pairs} removed={removed}")
    assert pairs == [(0, 0), (1, 2)], pairs
    assert removed == [1], removed

    # (f) 삭제 + 순서 변경 동시
    pairs, removed = match_sheets(["LotC", "LotA"], TITLES)
    print(f"(f) 삭제+순서 변경: pairs={pairs} removed={removed}")
    assert pairs == [(1, 0), (0, 2)], pairs
    assert removed == [1], removed

    # (g) 거부 케이스 — 매칭 불가/추가/빈 시트
    expect_error(["LotA", "renamed"], TITLES, "삭제+이름변경")
    expect_error(["LotA", "LotB", "LotC", "LotD"], TITLES, "시트 추가")
    expect_error([], TITLES, "시트 없음")

    # ── _merge_sources: 부분 선택 편집 결과 병합 ────────────────────────────
    # (h) 전체 선택·삭제 없음 → 구 경로 그대로 (kept=None = 전체 교체)
    parquets, kept, titles = _merge_sources(_src([0, 1, 2]), [b"n0", b"n1", b"n2"], None)
    print(f"(h) 전체 선택: kept={kept} titles={titles}")
    assert parquets == [b"n0", b"n1", b"n2"] and kept is None and titles == TITLES

    # (i) 가운데 1개만 선택 → 안 연 source 는 원본 bytes 그대로, kept 는 None(삭제 없음)
    parquets, kept, titles = _merge_sources(_src([1]), [b"edited"], None)
    print(f"(i) 1개만 선택: parquets={parquets} kept={kept}")
    assert parquets == [b"raw0", b"edited", b"raw2"], parquets
    assert kept is None and titles == TITLES

    # (j) 2개 선택 후 그중 1개 시트 삭제 → 원본 idx 로 환산된 kept + 안 연 source 보존
    #     sel=[0,2] 에서 로컬 idx 1(=원본 2)만 남김 → kept=[0,1] (1 은 안 연 source)
    parquets, kept, titles = _merge_sources(_src([0, 2]), [b"edited2"], [1])
    print(f"(j) 부분 선택 + 시트 삭제: parquets={parquets} kept={kept} titles={titles}")
    assert parquets == [b"raw1", b"edited2"], parquets
    assert kept == [1, 2], kept
    assert titles == ["LotB", "LotC"], titles

    print("\nPASS — 시트명 매칭(순서 복원·삭제 감지·이름변경 폴백·거부 3종) + 부분 선택 병합 3종")


if __name__ == "__main__":
    main()

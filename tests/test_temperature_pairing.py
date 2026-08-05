"""Temperature 그룹 자동 배치(폴더 역할 + 파일명 유사도) 회귀 테스트.

실행:
    python tests/test_temperature_pairing.py

고정하는 계약:
  - 역할은 폴더에서 확정되고(role_of), 그룹(pair)은 **파일명 유사도**(pair_key)로 묶는다.
  - 역할을 모르는 source 는 파일명 토큰(RT/CT/HT)으로 폴백한다.
  - 같은 (stem, 역할) 이 2개면 뒤엣것은 제안에서 빠진다 — 조용히 덮어쓰면 source 가 사라진다.
  - RT 가 없는 stem 은 그룹을 만들지 않는다(_accept 가 RT 없는 그룹을 거부하므로).

PyQt6 위젯을 만들지 않는 **순수 함수만** 검사한다(헤드리스에서 QApplication 불필요).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

from honey_ui.temperature_pairing import (pair_key,  # noqa: E402
                                          suggest_groups,
                                          suggest_groups_by_role)


def test_pair_key_strips_role_token():
    """RT/CT/HT 토큰을 떼고 소문자화 — 온도만 다른 이름은 같은 pair 키가 된다."""
    assert pair_key("WF1_RT") == pair_key("WF1_CT") == pair_key("WF1_HT") == "wf1"
    assert pair_key("wf1_rt") == pair_key("WF1_RT")
    assert pair_key("LOT9_W03") == "lot9_w03"           # 토큰 없으면 그대로(소문자)
    assert pair_key("WF1_RT") != pair_key("WF2_RT")


def test_by_role_pairs_by_name_stem():
    """1단계 — 이름에 온도 토큰이 든 경우는 stem 이 같은 것끼리 묶인다."""
    names = ["WF1_RT", "WF2_RT", "WF1_CT", "WF2_CT", "WF1_HT", "WF2_HT"]
    roles = {"WF1_RT": "RT", "WF2_RT": "RT", "WF1_CT": "CT",
             "WF2_CT": "CT", "WF1_HT": "HT", "WF2_HT": "HT"}
    groups = suggest_groups_by_role(names, roles.get)
    assert groups == [
        {"RT": "WF1_RT", "CT": "WF1_CT", "HT": "WF1_HT"},
        {"RT": "WF2_RT", "CT": "WF2_CT", "HT": "WF2_HT"},
    ], groups


def test_by_role_pairs_same_filename_across_folders():
    """2단계 — 폴더마다 같은 파일명이면(a / a_2 / a_3) 역할별 순번으로 짝짓는다.

    실사용 형태다: EP1/RT/a.stdf · EP1/CT/a.stdf · EP1/HT/a.stdf → source 이름이
    중복 해소로 a / a_2 / a_3 가 되어 stem 이 갈린다. 순번으로 묶어야 짝이 맞는다.
    """
    names = ["a", "b", "a_2", "b_2", "a_3", "b_3"]
    roles = {"a": "RT", "b": "RT", "a_2": "CT", "b_2": "CT", "a_3": "HT", "b_3": "HT"}
    groups = suggest_groups_by_role(names, roles.get)
    assert groups == [
        {"RT": "a", "CT": "a_2", "HT": "a_3"},
        {"RT": "b", "CT": "b_2", "HT": "b_3"},
    ], groups


def test_by_role_leftover_members_stay_unassigned():
    """RT 보다 CT 가 많으면 남는 CT 는 배치하지 않는다(미배정 → 사용자가 놓는다)."""
    names = ["r1", "c1", "c2"]
    groups = suggest_groups_by_role(names, {"r1": "RT", "c1": "CT", "c2": "CT"}.get)
    assert groups == [{"RT": "r1", "CT": "c1"}], groups
    placed = {v for g in groups for v in g.values()}
    assert "c2" not in placed, placed


def test_by_role_uses_filename_token_when_role_unknown():
    """폴더 역할을 못 얻은 source 는 파일명 토큰으로 폴백한다."""
    names = ["WF1_RT", "WF1_CT", "WF1_HT"]
    groups = suggest_groups_by_role(names, {}.get)     # 역할 정보 전무
    assert groups == [{"RT": "WF1_RT", "CT": "WF1_CT", "HT": "WF1_HT"}], groups
    # 혼합 — RT 만 폴더에서 알고 나머지는 파일명에서
    groups = suggest_groups_by_role(names, {"WF1_RT": "RT"}.get)
    assert groups == [{"RT": "WF1_RT", "CT": "WF1_CT", "HT": "WF1_HT"}], groups


def test_by_role_duplicate_role_is_left_unassigned():
    """같은 (stem, 역할) 이 2개면 1단계는 첫 번째만 쓴다 — 조용한 덮어쓰기 금지."""
    names = ["WF1_RT", "WF1_CT", "WF1_CT_b"]
    roles = {"WF1_RT": "RT", "WF1_CT": "CT", "WF1_CT_b": "CT"}
    groups = suggest_groups_by_role(names, roles.get)
    assert len(groups) == 1, groups
    assert groups[0]["CT"] == "WF1_CT", groups            # 첫 번째만 배치
    placed = {v for g in groups for v in g.values()}
    assert "WF1_CT_b" not in placed, placed               # 남은 것은 미배정


def test_by_role_drops_group_without_rt():
    """RT 없는 그룹은 만들지 않는다 — 기준 limit 이 없어 재판정을 못 한다."""
    names = ["WF1_CT", "WF1_HT"]
    groups = suggest_groups_by_role(names, {"WF1_CT": "CT", "WF1_HT": "HT"}.get)
    assert groups == [], groups


def test_by_role_rt_only_group_is_kept():
    """CT/HT 없이 RT 만 있는 그룹은 그대로 둔다(RT 단독 그룹 허용)."""
    groups = suggest_groups_by_role(["r1", "r2"], {"r1": "RT", "r2": "RT"}.get)
    assert groups == [{"RT": "r1"}, {"RT": "r2"}], groups


def test_legacy_suggest_groups_unchanged():
    """폴더 역할이 없을 때 쓰는 종전 파일명 추정은 그대로 (회귀 가드)."""
    names = ["WF1_RT", "WF1_CT", "WF1_HT", "WF2_RT", "NOISE"]
    assert suggest_groups(names) == [
        {"RT": "WF1_RT", "CT": "WF1_CT", "HT": "WF1_HT"},
        {"RT": "WF2_RT"},
    ], suggest_groups(names)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_pair_key_strips_role_token,
               test_by_role_pairs_by_name_stem,
               test_by_role_pairs_same_filename_across_folders,
               test_by_role_leftover_members_stay_unassigned,
               test_by_role_uses_filename_token_when_role_unknown,
               test_by_role_duplicate_role_is_left_unassigned,
               test_by_role_drops_group_without_rt,
               test_by_role_rt_only_group_is_kept,
               test_legacy_suggest_groups_unchanged):
        fn()
        checks += 1
    print(f"PASS: test_temperature_pairing ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

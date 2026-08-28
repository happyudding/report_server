"""민감도 게이지 단계표(rules/sensitivity.yaml) 자체의 정합성.

이 파일이 지키는 계약은 하나다 — **L3 열은 현행 기본값과 같다.** 게이지 3단계가 곧 "지금까지
쓰던 값" 이라는 약속이 깨지면, 기본 설정으로 올린 세션이 조용히 다른 판정을 받는다.
나머지는 그 약속을 떠받치는 것들(단조성·관계식·값 범위·키 존재).
"""
import yaml

import pytest

from eval_engine import config


def _load():
    return yaml.safe_load(config.SENSITIVITY_FILE.read_text(encoding="utf-8"))


def _defaults():
    return yaml.safe_load(config.THRESHOLDS_FILE.read_text(encoding="utf-8"))["default"]


def _all_keys(doc):
    """{키: [L1..L5]} — 전 그룹 평탄화. 키가 두 그룹에 중복 선언되면 그 자리에서 실패."""
    out = {}
    for name, group in doc["groups"].items():
        for key, levels in group["keys"].items():
            assert key not in out, f"{key} 가 여러 그룹에 선언됨 (그룹 {name})"
            out[key] = levels
    return out


@pytest.mark.rules_as_deployed
def test_level3_equals_thresholds_default():
    """L3 == thresholds.yaml default. 게이지 3단계 = 현행 동작이라는 약속의 실체."""
    defaults = _defaults()
    for key, levels in _all_keys(_load()).items():
        assert key in defaults, f"{key} 가 thresholds.yaml default 에 없다"
        assert levels[2] == pytest.approx(defaults[key]), f"{key} L3 가 기본값과 다르다"


def test_five_levels_each():
    for key, levels in _all_keys(_load()).items():
        assert len(levels) == 5, f"{key} 단계가 5개가 아니다"
        assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in levels)


def test_levels_are_monotonic():
    """레벨 순서대로 단조 증가 또는 감소여야 한다 (방향은 키마다 다르다).

    단조가 깨지면 게이지를 올렸는데 오히려 덜 잡히는 구간이 생긴다. 고정 키
    (gauge_fixed, 예: cpk_warn)는 전 단계 동일값이라 양쪽 다 통과한다.
    """
    for key, levels in _all_keys(_load()).items():
        up = all(a <= b for a, b in zip(levels, levels[1:]))
        down = all(a >= b for a, b in zip(levels, levels[1:]))
        assert up or down, f"{key} 단계가 단조가 아니다: {levels}"


def test_gauge_fixed_group_has_identical_levels():
    """gauge_fixed 그룹은 전 단계 같은 값이어야 한다 (게이지로 안 움직인다는 선언)."""
    doc = _load()
    for name, group in doc["groups"].items():
        if not group.get("gauge_fixed"):
            continue
        for key, levels in group["keys"].items():
            assert len(set(levels)) == 1, f"{name}.{key} 는 gauge_fixed 인데 값이 변한다"


def test_tail_mass_band_holds_at_every_level():
    """모든 레벨에서 heavy_tail_mass_min <= max. 밴드가 뒤집히면 그 룰이 영원히 침묵한다."""
    keys = _all_keys(_load())
    for lo, hi in zip(keys["heavy_tail_mass_min"], keys["heavy_tail_mass_max"]):
        assert lo <= hi


def test_ratio_keys_stay_in_range():
    """비율형 키는 전 레벨에서 0~1 안에 있어야 한다(1 을 넘으면 도달 불가 = 영구 침묵)."""
    ratio_keys = {"outlier_jump_ratio_min", "mean_shift_warn", "heavy_tail_mass_min",
                  "heavy_tail_mass_max", "tail_side_share_min", "region_fail_share_min",
                  "code_edge_hit_warn", "func_fail_pass_fix_min", "bimodality_warn",
                  "subpop_density_gap_strong", "subpop_density_gap_warn",
                  "subpop_value_gap_warn", "subpop_outlier_ratio_max"}
    keys = _all_keys(_load())
    for key in ratio_keys:
        assert key in keys, f"{key} 가 단계표에 없다"
        for v in keys[key]:
            assert 0.0 < v <= 1.0, f"{key} 레벨 값 {v} 가 0~1 밖"


def test_count_keys_stay_integer():
    """count 형은 정수여야 한다 — 소수 fail 수는 없다."""
    for v in _all_keys(_load())["spatial_fail_count_min"]:
        assert isinstance(v, int) and v >= 1


def test_structural_keys_are_not_gaugeable():
    """구조 키는 단계표에 없어야 한다.

    판정 체계 자체(최소 표본·영역 분할·trump·RING/SPOT no-gap 경계선)를 정의하는 값들이라,
    게이지로 흔들면 룰이 덜/더 뜨는 게 아니라 판정의 의미가 달라진다.
    """
    forbidden = {"n_min", "edge_region_pct", "center_region_pct", "outlier_body_z",
                 "outlier_sigma", "subpop_outlier_sigma", "subpop_n_min",
                 "cpk_bad", "cpk_trump_yield_floor", "gross_yield_bad",
                 "spot_fail_spread_max"}
    assert forbidden.isdisjoint(_all_keys(_load()))


@pytest.mark.rules_as_deployed
def test_declared_signatures_exist():
    """그룹이 지목한 signature id 가 signatures.yaml 에 실재해야 한다(오타 방지)."""
    declared = {s["id"] for s in
                yaml.safe_load(config.SIGNATURES_FILE.read_text(encoding="utf-8"))["signatures"]}
    for name, group in _load()["groups"].items():
        for sig_id in group["signatures"]:
            assert sig_id in declared, f"{name} 이 없는 signature {sig_id} 를 지목"

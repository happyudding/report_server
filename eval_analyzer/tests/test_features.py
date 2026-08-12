"""L2 features 단독 테스트 — CODE_TO_PORT §5."""
import numpy as np
import pytest

from eval_engine.pipeline import features
from eval_engine.pipeline._rules import thresholds_for


def _case(values, lsl=0, usl=20, **kw):
    """측정값만 있는 case_ctx — 좌표·site·fail 은 비워 공간/site feature 를 결측으로 둔다.

    산포·spec margin 공식만 겨냥하려는 것이므로, 공간 feature 가 필요한 테스트는 kw 로 채운다.
    """
    c = {"values": values, "lsl": lsl, "usl": usl, "value_type": "V",
         "x_pos": [None] * len(values), "y_pos": [None] * len(values),
         "site": [None] * len(values),
         "fail_mask": [False] * len(values),
         "skewness": None, "product_type": None, "item_class": None}
    c.update(kw)
    return c


def test_spread_norm_matches_formula():
    vals = [10, 12, 14, 16, 18]  # median=14, MAD=2
    m = {"stdev": float(np.std(vals, ddof=1))}
    f = features.compute(_case(vals, lsl=0, usl=20), m, "ev1")
    expected = 1.4826 * 2 / (20 - 0)
    assert f["spread_norm"] == pytest.approx(expected)


def test_outlier_ratio_detects_extreme():
    vals = [10, 11, 12, 13, 14, 15, 100]  # 100 = 명백한 outlier
    m = {"stdev": float(np.std(vals, ddof=1))}
    f = features.compute(_case(vals, lsl=0, usl=200), m, "ev1")
    assert f["outlier_ratio"] == pytest.approx(1 / 7)


def test_cdf_gap_large_for_two_clusters():
    two_clusters = np.array([0.0, 0.0, 0.0, 10.0, 10.0, 10.0])
    uniform = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    assert features._cdf_gap(two_clusters) == pytest.approx(50.0)
    assert features._cdf_gap(uniform) < features._cdf_gap(two_clusters)


def test_outlier_ratio_mad_zero_fallback():
    """과반 동일값(MAD=0)이라도 소수 폭주값을 outlier 로 잡는다 (meanAD 폴백)."""
    vals = [5.0] * 20 + [100.0]          # MAD=0, 1개 폭주
    m = {"stdev": float(np.std(vals, ddof=1))}
    f = features.compute(_case(vals, lsl=0, usl=200), m, "ev1")
    assert f["outlier_ratio"] == pytest.approx(1 / 21)
    # 완전 동일값은 여전히 0 (CONSTANT_VALUE 몫)
    same = [5.0] * 10
    f2 = features.compute(_case(same, lsl=0, usl=10), {"stdev": 0.0}, "ev1")
    assert f2["outlier_ratio"] == 0.0


def test_value_gap_separated_vs_quantized():
    """separated 는 값축 빈 구간 기준 — 양자화(동일값 쏠림)와 구분된다."""
    # 두 무리(0 근처 45개 + 10 근처 15개) — 값축 간격이 범위의 대부분
    two = np.array([0.0 + i * 0.01 for i in range(45)] + [10.0 + i * 0.01 for i in range(15)])
    gap, minor = features._value_gap(two)
    assert gap > 0.9
    assert minor == pytest.approx(15 / 60)
    # 양자화 데이터(0/1/2 반복) — cdf_gap 은 크지만 값축 간격은 균등
    quant = np.array([0.0, 1.0, 2.0] * 20)
    gap_q, _ = features._value_gap(quant)
    assert gap_q == pytest.approx(0.5)
    assert features._cdf_gap(quant) > 30.0   # 구 지표는 여기서 컸다(오발화 원인)


def test_modality_v2_separated_uses_value_gap():
    """이산(양자화) 단봉 데이터는 separated 로 오발화하지 않아야 한다."""
    th = dict(thresholds_for({}))
    # 값 2개뿐인 양자화: n_modes=2 가 아닐 수 있으니 직접 분기 함수를 검증
    assert features._classify_modality_v2(
        100, 0.0, 1, 0.2, 0.6, 0.05, 0.4, th) is None      # value_gap 작음 → 미발화
    assert features._classify_modality_v2(
        100, 0.0, 1, 0.2, 0.6, 0.9, 0.25, th) == "separated"  # 진짜 분리
    assert features._classify_modality_v2(
        100, 0.0, 1, 0.2, 0.6, 0.9, 0.01, th) is None      # 소수쪽 질량 미달 → 미발화


def _disc(radius=6):
    """반경 radius 안의 정수격자 die 좌표 목록 (원형 웨이퍼 모사)."""
    return [(x, y) for y in range(-radius, radius + 1) for x in range(-radius, radius + 1)
            if x * x + y * y <= radius * radius]


def test_spatial_edge_concentration():
    th = thresholds_for({})
    # 원형 die 배치에서 fail 을 **가장자리 밴드(E1 제외)** 에만 둔다.
    # 2026-08-12: EDGE 는 최외곽 1열(E1)을 뺀 영역으로 의미가 좁아졌다 — 한 줄짜리
    # 데이터(x=1..10)는 전부 E1 이라 EDGE 표본이 0 이 되므로 2차원 배치를 쓴다.
    dies = _disc()
    e1 = features._e1_mask(np.array([d[0] for d in dies], dtype=float),
                           np.array([d[1] for d in dies], dtype=float))
    rmax = max((x * x + y * y) ** 0.5 for x, y in dies)
    fail = [(not e1[i]) and ((x * x + y * y) ** 0.5 / rmax >= th["edge_region_pct"])
            for i, (x, y) in enumerate(dies)]
    case = {"values": [1.0] * len(dies),
            "x_pos": [float(x) for x, _ in dies], "y_pos": [float(y) for _, y in dies],
            "fail_mask": fail, "lsl": 0, "usl": 11}
    out = features._spatial_features(case, th)
    assert out["edge_fail_ratio"] is not None
    assert out["edge_fail_ratio"] > 1.0
    assert out["wafer_zone_signature"] == "EDGE"


def test_spatial_e1_concentration():
    """최외곽 1열(E1)에만 fail 이 몰리면 e1_fail_ratio 가 뜨고 zone 이 E1 로 분류된다."""
    th = thresholds_for({})
    dies = _disc()
    e1 = features._e1_mask(np.array([d[0] for d in dies], dtype=float),
                           np.array([d[1] for d in dies], dtype=float))
    case = {"values": [1.0] * len(dies),
            "x_pos": [float(x) for x, _ in dies], "y_pos": [float(y) for _, y in dies],
            "fail_mask": list(e1), "lsl": 0, "usl": 11}
    out = features._spatial_features(case, th)
    assert out["e1_fail_ratio"] > th["e1_fail_ratio_warn"]
    assert out["edge_fail_ratio"] == 0.0          # E1 을 뺀 밴드에는 fail 이 없다
    assert out["wafer_zone_signature"] == "E1"


def test_spatial_none_when_no_coords():
    th = thresholds_for({})
    case = {"values": [1.0, 2.0, 3.0], "x_pos": [None, None, None],
            "y_pos": [None, None, None], "fail_mask": [True, False, True],
            "lsl": 0, "usl": 10}
    out = features._spatial_features(case, th)
    assert out["edge_fail_ratio"] is None
    assert out["wafer_zone_signature"] is None


def test_site_cpk_delta_none_without_site():
    vals = [10, 12, 14, 16, 18]
    m = {"stdev": float(np.std(vals, ddof=1))}
    f = features.compute(_case(vals), m, "ev1")
    assert f["site_cpk_delta"] is None


def test_empty_values_gives_empty_features():
    f = features.compute(_case([]), {}, "ev1")
    assert f["n_dut"] == 0
    assert f["spread_norm"] is None
    assert f["outlier_ratio"] is None


def test_code_edge_hit_only_for_code_type():
    vals = [5, 5, 10, 10]  # limit 에 정확히 닿음
    m = {"stdev": float(np.std(vals, ddof=1))}
    f_v = features.compute(_case(vals, lsl=5, usl=10, value_type="V"), m, "ev1")
    f_code = features.compute(_case(vals, lsl=5, usl=10, value_type="CODE"), m, "ev1")
    assert f_v["code_edge_hit"] is None
    assert f_code["code_edge_hit"] is not None


def test_compute_returns_exactly_the_declared_keys():
    """반환 키 집합 == _FEATURE_KEYS. 결측 경로(빈 값)도 같은 모양이어야 한다.

    소비자(store.save_features / status.decide / 관리자 트레이스)가 키 존재를 전제하므로,
    새 feature 를 계산만 하고 _FEATURE_KEYS 에 안 넣거나 그 반대면 여기서 갈린다.
    """
    vals = [10, 12, 14, 16, 18]
    m = {"stdev": float(np.std(vals, ddof=1))}
    declared = set(features._FEATURE_KEYS)
    assert set(features.compute(_case(vals), m, "ev1")) == declared
    assert set(features.compute(_case([]), {}, "ev1")) == declared
    # 2026-08-03 신설 파생값(DB 미저장) — separated 판정·트레이스 표시용
    assert {"value_gap_ratio", "value_gap_minor_mass"} <= declared

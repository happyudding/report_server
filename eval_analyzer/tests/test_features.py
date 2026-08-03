"""L2 features 단독 테스트 — CODE_TO_PORT §5."""
import numpy as np
import pytest

from eval_engine.pipeline import features
from eval_engine.pipeline._rules import thresholds_for


def _case(values, lsl=0, usl=20, **kw):
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


def test_spatial_edge_concentration():
    th = thresholds_for({})
    # x=1..10 (y=0), fail 은 edge(x=9,10) 에만
    xs = list(range(1, 11))
    case = {"values": [float(x) for x in xs],
            "x_pos": [float(x) for x in xs], "y_pos": [0.0] * 10,
            "fail_mask": [x in (9, 10) for x in xs], "lsl": 0, "usl": 11}
    out = features._spatial_features(case, th)
    assert out["edge_fail_ratio"] is not None
    assert out["edge_fail_ratio"] > 1.0
    assert out["wafer_zone_signature"] == "EDGE"


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

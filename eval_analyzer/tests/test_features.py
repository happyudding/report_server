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


def test_fail_mad_min_measures_distance():
    """OUTLIER 판정축① 거리 — 중심에 **가장 가까운** fail 이 MAD 의 몇 배 밖인가."""
    body = [10.0 + 0.1 * i for i in range(20)]        # median≈11, MAD≈0.5
    m = {"stdev": 1.0}
    far = features.compute(_case(body + [100.0], usl=200,
                                 fail_mask=[False] * 20 + [True]), m, "ev1")
    near = features.compute(_case(body + [11.6], usl=200,
                                  fail_mask=[False] * 20 + [True]), m, "ev1")
    assert far["fail_mad_min"] > 4
    assert near["fail_mad_min"] < 4
    # fail 이 없으면 판정 대상 자체가 없다 — 0 이 아니라 None(결측을 양호로 읽지 않는다)
    assert features.compute(_case(body), m, "ev1")["fail_mad_min"] is None


def test_fail_mad_min_mad_zero_fallback():
    """과반 동일값(MAD=0)이어도 폭주한 fail 의 거리를 잰다 — outlier_ratio 와 같은 폴백."""
    vals = [5.0] * 20 + [100.0]
    f = features.compute(_case(vals, lsl=0, usl=200,
                               fail_mask=[False] * 20 + [True]),
                         {"stdev": float(np.std(vals, ddof=1))}, "ev1")
    assert f["fail_mad_min"] > 4


def test_fail_pass_gap_sigma_separates_tail_from_outlier():
    """OUTLIER 판정축② 끊김 — 꼬리가 이어졌으면 작고, 뚝 떨어졌으면 크다.

    거리만으로는 못 가른다는 것이 이 지표의 존재 이유다: 아래 두 입력은 fail 이 **같은
    위치**(30.0)인데, 하나는 중간값들이 채워져 있고 하나는 비어 있다.
    """
    body = [10.0 + 0.1 * i for i in range(20)]
    tail = body + [14.0, 18.0, 24.0, 30.0]            # 꼬리가 이어져 마지막이 fail
    jump = body + [30.0]                              # 몸통과 뚝 끊긴 fail
    m = {"stdev": 5.0}
    f_tail = features.compute(_case(tail, usl=28, fail_mask=[False] * 23 + [True]), m, "ev1")
    f_jump = features.compute(_case(jump, usl=28, fail_mask=[False] * 20 + [True]), m, "ev1")
    assert f_tail["fail_pass_gap_sigma"] < f_jump["fail_pass_gap_sigma"]
    # pass 가 하나도 없으면(전량 fail) 잴 수가 없다 → None → 조건 False → 미발화
    allfail = features.compute(_case(body, usl=200, fail_mask=[True] * 20), m, "ev1")
    assert allfail["fail_pass_gap_sigma"] is None


def test_fail_body_jump_ratio_separates_tail_from_outlier():
    """OUTLIER 판정축② (2026-08-14 교체) — 몸통~최근접 fail 구간이 얼마나 비었나.

    위 `fail_pass_gap_sigma` 와 같은 것을 재려던 지표인데, 그쪽은 `|z|` 라 **양쪽 꼬리를
    한 자에 섞는다** — 반대쪽에 더 먼 pass 가 하나만 있어도 음수가 되어 끊긴 fail 이
    통째로 미발화했다. 아래 `two_sided` 가 정확히 그 경우다: fail 은 위쪽에서 몸통과
    뚝 끊겼는데, 아래쪽에 더 멀리 나간 pass 가 있어 구 지표는 음수가 된다.
    """
    body = [10.0 + 0.1 * i for i in range(20)]
    tail = body + [14.0, 18.0, 24.0, 30.0]            # 꼬리가 이어져 마지막이 fail
    jump = body + [30.0]                              # 몸통과 뚝 끊긴 fail
    m = {"stdev": 5.0}
    f_tail = features.compute(_case(tail, usl=28, fail_mask=[False] * 23 + [True]), m, "ev1")
    f_jump = features.compute(_case(jump, usl=28, fail_mask=[False] * 20 + [True]), m, "ev1")
    assert f_tail["fail_body_jump_ratio"] < f_jump["fail_body_jump_ratio"]
    assert f_jump["fail_body_jump_ratio"] > 0.35      # 배포 임계(outlier_jump_ratio_min)

    # 반대쪽 꼬리에 더 먼 pass 가 있어도 이쪽 끊김을 그대로 잡는다(구 지표는 여기서 죽었다)
    two_sided = [-40.0] + body + [30.0]
    f_two = features.compute(_case(two_sided, lsl=-60, usl=28,
                                   fail_mask=[False] * 21 + [True]), m, "ev1")
    assert f_two["fail_pass_gap_sigma"] < 0            # 구 지표: 음수 → 미발화
    assert f_two["fail_body_jump_ratio"] > 0.35        # 새 지표: 끊김을 본다

    # fail 이 없으면 판정 대상 자체가 없다 → None(결측을 양호로 읽지 않는다)
    assert features.compute(_case(body), m, "ev1")["fail_body_jump_ratio"] is None


def test_tail_mass_splits_by_direction():
    """꼬리 질량은 **방향으로 나뉜다** — USL_TAIL/LSL_TAIL 이 이 두 값으로 갈린다.

    합(high+low)이 방향 없는 종전 지표(tail_mass_3s)와 같아야 한다 — 두 룰이 옛 밴드
    임계(heavy_tail_mass_min/max)를 그대로 쓰기 때문이다.
    """
    body = [10.0] * 200
    vals = body + [30.0, 31.0]            # 위쪽으로만 튄 꼬리
    f = features.compute(_case(vals, lsl=0, usl=40),
                         {"stdev": float(np.std(vals, ddof=1))}, "ev1")
    assert f["tail_mass_3s_high"] > 0 and f["tail_mass_3s_low"] == 0.0
    assert f["tail_mass_3s"] == pytest.approx(f["tail_mass_3s_high"] + f["tail_mass_3s_low"])

    vals = body + [-10.0, -11.0]          # 아래쪽으로만 처진 꼬리
    f = features.compute(_case(vals, lsl=-40, usl=40),
                         {"stdev": float(np.std(vals, ddof=1))}, "ev1")
    assert f["tail_mass_3s_low"] > 0 and f["tail_mass_3s_high"] == 0.0
    assert f["tail_mass_3s"] == pytest.approx(f["tail_mass_3s_high"] + f["tail_mass_3s_low"])


def test_tail_extent_measures_stretch_against_body():
    """꼬리 extent 는 **몸통 σ 대비 뻗은 정도**다 — 정규는 ≈2.6, 늘어질수록 커진다.

    질량(tail_mass)과 짝이다: 질량은 "꼬리가 실재하나", extent 는 "얼마나 늘어졌나".
    """
    rng = np.random.default_rng(7)
    normal = list(rng.normal(10.0, 1.0, 4000))
    f = features.compute(_case(normal, lsl=0, usl=20),
                         {"stdev": float(np.std(normal, ddof=1))}, "ev1")
    assert 2.2 < f["tail_extent_high"] < 3.2
    assert 2.2 < f["tail_extent_low"] < 3.2

    # 같은 몸통에 위쪽으로만 긴 꼬리를 붙이면 그 방향만 커진다
    stretched = normal + list(rng.normal(10.0, 6.0, 200))
    f2 = features.compute(_case(stretched, lsl=0, usl=40),
                          {"stdev": float(np.std(stretched, ddof=1))}, "ev1")
    assert f2["tail_extent_high"] > f["tail_extent_high"]


def test_tail_extent_none_when_body_has_no_width():
    """과반 동일값(MAD=0)이면 extent 는 **None** — meanAD 폴백을 쓰지 않는다.

    v13 오탐의 재발 방지선이다. 폴백을 쓰면 자(尺)가 "모드에서 벗어난 값의 평균 이탈량" 이
    되어, 눈으로는 1자인 산포에서 이탈 die 몇 개가 통째로 꼬리로 계산됐다.
    같은 데이터라도 outlier 계열 지표는 폴백을 그대로 쓴다(그쪽은 잡아야 하는 현상이다).
    """
    vals = [5.0] * 200 + [6.0] * 3 + [4.0] * 3
    f = features.compute(_case(vals, lsl=0, usl=255),
                         {"stdev": float(np.std(vals, ddof=1))}, "ev1")
    assert f["tail_extent_high"] is None and f["tail_extent_low"] is None
    assert f["tail_mass_3s_high"] > 0          # 질량 쪽은 폴백으로 계속 계산된다


def test_pass_limit_hit_ratio_excludes_fails():
    """pass 만 모수로 잡는다 — fail 이 많아도 "pass 는 전부 고정값" 이 희석되지 않는다."""
    vals = [0.0] * 70 + [999.0] * 30
    fail_mask = [False] * 70 + [True] * 30
    f = features.compute(_case(vals, lsl=0.0, usl=0.0, fail_mask=fail_mask),
                         {"stdev": float(np.std(vals, ddof=1))}, "ev1")
    assert f["pass_limit_hit_ratio"] == pytest.approx(1.0)
    assert f["limit_hit_ratio"] == pytest.approx(0.7)   # 전체 기준은 fail 에 희석된다
    # pass 값이 흩어져 있으면 기능성 item 이 아니다
    loose = [0.0] * 40 + [1.0] * 30 + [999.0] * 30
    f2 = features.compute(_case(loose, lsl=0.0, usl=0.0, fail_mask=fail_mask),
                          {"stdev": float(np.std(loose, ddof=1))}, "ev1")
    assert f2["pass_limit_hit_ratio"] == pytest.approx(40 / 70)


def test_quantized_steps_are_not_bimodal():
    """양자화(계단형) 단봉을 이봉으로 오판하지 않는다 — 히스토그램 격자 정렬 회귀 방지선.

    bin 폭이 계단 간격보다 좁으면 빈 칸이 사이사이 끼어 **가짜 봉우리**가 생긴다
    (실측: 8단 단봉이 봉우리 8개로 잡혀 BIMODALITY 오발화).
    """
    step = 0.125
    counts = [23, 87, 573, 1393, 1797, 1443, 568, 115]   # 실데이터에서 뽑은 단봉 계단
    vals = [1.0 + i * step for i, c in enumerate(counts) for _ in range(c)]
    v = np.asarray(vals, dtype=float)
    peaks, _hist, grid = features._histogram_peaks(v)
    assert len(peaks) == 1, f"계단형 단봉인데 봉우리 {len(peaks)}개로 잡혔다"
    # 격자로 인식됐고 빈 계단이 없다 → 이봉 판정 게이트에서 차단된다
    assert features._grid_empty_levels(grid) == 0
    assert features._density_gap(v) == 0.0
    assert features._grid_step(v) == pytest.approx(step)
    # 격자가 아닌(연속) 값에는 가드가 걸리지 않는다
    assert features._grid_step(np.linspace(0, 1, 500)) is None


def test_region_fail_share_is_occupancy_not_density():
    """공간 룰 판정축 — 전체 fail 중 그 영역이 차지하는 점유율(밀도 비와 다르다)."""
    th = thresholds_for({})
    dies = _disc()
    e1 = features._e1_mask(np.array([d[0] for d in dies], dtype=float),
                           np.array([d[1] for d in dies], dtype=float))
    case = {"values": [1.0] * len(dies),
            "x_pos": [float(x) for x, _ in dies], "y_pos": [float(y) for _, y in dies],
            "fail_mask": list(e1), "lsl": 0, "usl": 11}
    out = features._spatial_features(case, th)
    assert out["e1_fail_share"] == pytest.approx(1.0)     # fail 이 전부 E1
    assert out["edge_fail_share"] == pytest.approx(0.0)
    # 밀도 비는 영역 면적에 좌우돼 값이 다르다 — 두 지표를 혼동하지 말 것
    assert out["e1_fail_ratio"] != pytest.approx(out["e1_fail_share"])


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
    # 소수쪽 질량 하한(`subpop_minor_mass_min`)은 2026-08-19 에 0.05 → 0.0003 으로 크게
    # 완화됐다(떨어져 나간 소수 무리를 분리로 보기 위해). 하한이 **여전히 작동한다**는
    # 것만 확인한다 — 오탐을 막는 실제 판별자는 density_gap 쪽으로 옮겨갔다.
    assert features._classify_modality_v2(
        100, 0.0, 1, 0.2, 0.6, 0.9, 0.0001, th) is None    # 소수쪽 질량 미달 → 미발화
    assert features._classify_modality_v2(
        100, 0.0, 1, 0.2, 0.15, 0.9, 0.25, th) is None     # density_gap 미달 → 미발화


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


def test_e1_mask_is_die_pitch_agnostic():
    """die pitch 가 1 이 아니어도 최외곽 비율이 같아야 한다.

    4-이웃(x±1) 조회로 판정하던 시절에는 pitch=2 인 map 의 **모든 die 가 E1** 로 잡혔고,
    그 여파로 `edge = 반경밴드 & ~E1` 이 비어 EDGE·RING 룰이 조용히 죽었다.
    """
    base = _disc(8)
    ratios = []
    for step in (1, 2, 5):
        xs = np.array([x * step for x, _ in base], dtype=float)
        ys = np.array([y * step for _, y in base], dtype=float)
        m = features._e1_mask(xs, ys)
        assert 0 < m.mean() < 0.5, (step, m.mean())
        ratios.append(m.sum())
    assert len(set(ratios)) == 1, ratios          # 간격이 달라도 같은 die 집합


def test_e1_mask_undecidable_when_degenerate():
    """한 줄짜리 배치처럼 전부가 가장자리면 판정 불가(전부 False) — EDGE/RING 을 살린다."""
    xs = np.arange(1.0, 11.0)
    m = features._e1_mask(xs, np.zeros(10))
    assert not m.any()


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
    assert out["e1_fail_share"] >= th["region_fail_share_min"]
    assert out["edge_fail_ratio"] == 0.0          # E1 을 뺀 밴드에는 fail 이 없다
    assert out["wafer_zone_signature"] == "E1"


def test_spatial_features_are_translation_invariant():
    """XPOS/YPOS 는 실데이터에서 **항상 양수**다 — 좌표를 평행이동해도 결과가 같아야 한다.

    엔진이 반경을 원점(0,0) 기준으로 재던 시절에는 0-based 좌표에서 웨이퍼 한 귀퉁이가
    중심이 되어 edge/center/ring/quadrant 가 통째로 어긋났다.
    """
    th = thresholds_for({})
    dies = _disc()
    e1 = features._e1_mask(np.array([d[0] for d in dies], dtype=float),
                           np.array([d[1] for d in dies], dtype=float))
    fail = list(e1)
    keys = ("e1_fail_ratio", "edge_fail_ratio", "center_fail_ratio", "ring_fail_ratio",
            "quadrant_imbalance", "wafer_zone_signature")

    def run(offset):
        case = {"values": [1.0] * len(dies),
                "x_pos": [float(x + offset) for x, _ in dies],
                "y_pos": [float(y + offset) for _, y in dies],
                "fail_mask": fail, "lsl": 0, "usl": 11}
        out = features._spatial_features(case, th)
        return {k: out[k] for k in keys}

    assert run(0) == run(50)          # 중심 정렬(음수 포함) == 양수 0-based
    assert run(50)["wafer_zone_signature"] == "E1"


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


# ── DUT 편중 (_dut_features, 2026-09-01) ──────────────────────────────────────

def _dut_case(dut, fail, **kw):
    """DUT 편중 전용 case — 공간 축(전체 die) 키만 채운다."""
    c = {"spatial_dut": dut, "spatial_fail_mask": fail}
    c.update(kw)
    return c


def test_dut_share_uses_top_channel():
    """점유율 = 최다 fail DUT 의 fail 수 / 전체 fail 수. 배수는 채널 수를 곱한 값."""
    dut = [0, 1, 2, 3] * 5                       # 채널 4개
    fail = [d == 2 for d in dut]                 # 5개 전부 DUT 2
    f = features._dut_features(_dut_case(dut, fail))
    assert f["dut_top"] == 2
    assert f["dut_fail_share"] == 1.0
    assert f["dut_fail_ratio"] == 4.0            # 균등 대비 4배 (채널 4개)
    assert f["n_dut_sites"] == 4


def test_dut_single_channel_is_none():
    """채널이 1개뿐이면 점유율이 항상 1.0 이라 편중을 말할 수 없다 → 미판정."""
    f = features._dut_features(_dut_case([7] * 10, [True] * 3 + [False] * 7))
    assert f["dut_fail_share"] is None and f["dut_fail_ratio"] is None
    assert f["n_dut_sites"] == 1                 # 채널 수 자체는 남긴다(진단용)


def test_dut_top_is_deterministic_on_tie():
    """동률이면 **작은 DUT**. 재실행마다 흔들리면 제안 문구의 채널 번호가 요동친다."""
    dut = [5, 1, 5, 1]
    fail = [True, True, True, True]              # 두 채널 2개씩 동률
    assert features._dut_features(_dut_case(dut, fail))["dut_top"] == 1


def test_dut_length_mismatch_gives_none():
    """길이가 어긋나면 어느 die 가 어느 채널인지 알 수 없다 → 조용히 틀리지 않고 포기."""
    f = features._dut_features(_dut_case([0, 1, 2], [True, False]))
    assert f["dut_fail_share"] is None


def test_dut_survives_empty_values():
    """측정값이 하나도 없어도 DUT 편중은 낸다 — FAILTNO 와 DUT 만 보기 때문.

    공간 feature 가 2026-08-28 에 같은 이유로 살아난 것과 짝이다. 여기서 비우면 값이
    전부 빈 item(functional test 등)은 채널 문제를 영영 판정할 수 없다.
    """
    dut = [0, 1, 2, 3] * 5
    case = {"values": [], "fail_mask": [], "x_pos": [], "y_pos": [],
            "spatial_dut": dut, "spatial_fail_mask": [d == 1 for d in dut],
            "spatial_x_pos": list(range(20)), "spatial_y_pos": [0] * 20,
            "lsl": 0.0, "usl": 10.0, "value_type": "NUM",
            "product_type": None, "item_class": None}
    f = features.compute(case, {"cpk": None}, "ev1")
    assert f["n_dut"] == 0                       # 측정 표본은 실제로 0
    assert f["dut_fail_share"] == 1.0 and f["dut_top"] == 1

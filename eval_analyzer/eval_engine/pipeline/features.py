"""L2 Feature — robust 산포/spec margin/공간 판단지표 계산.

공식: docs/CODE_TO_PORT §5 (spread_norm/outlier_ratio/skewness/kurtosis/density_gap/cdf_gap,
  spec_margin, edge/center/radial/quadrant/x/y gradient, wafer_zone_signature,
  n_dut, site_cpk_delta, code_edge_hit).
원칙: estimator=표준 robust(MAD 등), 임계값 하드코딩 금지(_rules.thresholds_for).
  결측은 None(좌표/site 없으면 공간/site feature None).
반환: features dict (DB_SCHEMA §5 컬럼) — engine_version 별.
"""
import numpy as np

from ._rules import thresholds_for
from .metrics import cpk_summary

_FEATURE_KEYS = [
    "spread_norm", "skewness", "kurtosis", "outlier_ratio", "modality",
    "bimodality_score", "density_gap", "cdf_gap", "spec_margin_low",
    "spec_margin_high", "nearest_spec_side", "limit_hit_ratio",
    "edge_fail_ratio", "center_fail_ratio", "radial_gradient",
    "quadrant_imbalance", "x_gradient", "y_gradient", "wafer_zone_signature",
    "n_dut", "site_cpk_delta", "code_edge_hit",
    "ring_fail_ratio",
    "radial_gradient_norm", "x_gradient_norm", "y_gradient_norm",
    "n_modes","modality_v2",
    # 파생(DB 미저장 — store.save_features cols 에 없음): separated 판정·트레이스 표시용
    "value_gap_ratio", "value_gap_minor_mass",
    # 파생(DB 미저장): E1(최외곽 1 chip line) 집중도 · 모멘트 왜도
    "e1_fail_ratio", "skewness_moment",
]


def _empty_features():
    """전 feature 를 None 으로 채운 dict(n_dut 만 0). 측정값이 하나도 없을 때 쓴다."""
    f = {k: None for k in _FEATURE_KEYS}
    f["n_dut"] = 0
    return f


def _cdf_gap(v):
    """ECDF(CODE_TO_PORT §3) 후 인접 누적% 최대 점프.

    주의: 이 값은 "최다 동일값 하나가 차지하는 질량(%)"이다 — 값 축의 빈 구간(분리)이
    아니라 동일값 쏠림(양자화/clamp) 신호다. 분리 판정에는 _value_gap 을 쓴다.
    """
    v = v[np.isfinite(v)]
    if v.size == 0:
        return None
    uniq, cnt = np.unique(np.sort(v), return_counts=True)
    cum = np.cumsum(cnt) / v.size * 100.0
    return float(np.max(np.diff(cum))) if len(cum) > 1 else 0.0


def _value_gap(v):
    """값 축 최대 인접 간격 → (간격/전체범위, 간격 양쪽 중 소수쪽 질량).

    separated(분리) 판정용 — 두 무리 사이의 실제 빈 구간을 본다. cdf_gap(동일값 질량)과
    다르다. 고유값 2개 미만이거나 범위 0 이면 (None, None).
    """
    v = v[np.isfinite(v)]
    uniq = np.unique(v)
    if uniq.size < 2:
        return None, None
    rng = float(uniq[-1] - uniq[0])
    if rng <= 0:
        return None, None
    diffs = np.diff(uniq)
    i = int(np.argmax(diffs))
    gap_ratio = float(diffs[i] / rng)
    below = float(np.mean(v <= uniq[i]))   # 간격 아래쪽 질량
    return gap_ratio, float(min(below, 1.0 - below))

def _histogram_peaks(v):
    """히스토그램 + 국소 최대(양옆 bin 보다 큰) 인덱스. 표본 8 미만이면 None(판정 불가).

    반환: (peaks, hist). bin 수는 표본 크기에 따라 5~20 사이. `_density_gap` 과 `_n_modes`
    가 **같은 히스토그램**을 봐야 골 깊이와 봉우리 수가 어긋나지 않으므로 둘이 공유한다.
    """
    if v.size < 8:
        return None
    hist, _ = np.histogram(v, bins=min(20,max(5, v.size // 5)))
    return [i for i in range(1, len(hist) - 1)
            if hist[i] > hist[i-1] and hist[i] > hist[i+1]], hist

def _density_gap(v):
    """히스토그램 기반 이봉 골 깊이(0~1 정규화). 단봉이면 0, 표본 부족이면 None."""
    peaks_hist = _histogram_peaks(v)
    if peaks_hist is None:
        return None
    peaks, hist = peaks_hist

    if len(peaks) < 2:
        return 0.0
    p1, p2 = sorted(peaks, key=lambda i: -hist[i])[:2]
    lo, hi = sorted([p1, p2])
    valley = int(hist[lo:hi + 1].min())
    peak_max = int(hist.max())
    if peak_max == 0:
        return 0.0
    return float((min(int(hist[p1]), int(hist[p2])) - valley) / peak_max)

def _n_modes(v):
    """히스토그램 봉우리 개수(최소 1). 표본 부족이면 None."""
    peaks_hist = _histogram_peaks(v)
    if peaks_hist is None:
        return None
    peaks, _ = peaks_hist
    return max(len(peaks), 1)

def _gradient(coord, fail_mask, bins=8):
    """coord 를 bins 구간으로 나눠 구간별 fail율 회귀 기울기."""
    coord = np.asarray(coord, dtype=float)
    fm = np.asarray(fail_mask, dtype=float)
    ok = np.isfinite(coord)
    coord, fm = coord[ok], fm[ok]
    if coord.size < 2 or coord.max() == coord.min():
        return None
    edges = np.linspace(coord.min(), coord.max(), bins + 1)
    centers, rates = [], []
    for i in range(bins):
        m = (coord >= edges[i]) & (coord <= edges[i + 1] if i == bins - 1 else coord < edges[i + 1])
        if m.sum() == 0:
            continue
        centers.append((edges[i] + edges[i + 1]) / 2)
        rates.append(fm[m].mean())
    if len(centers) < 2:
        return None
    return float(np.polyfit(centers, rates, 1)[0])


def _e1_mask(xs, ys):
    """**최외곽 1 chip line(E1)** die 마스크 — 상하좌우 4-이웃 중 하나라도 비면 최외곽.

    "웨이퍼 가장자리 한 줄" 은 반경 비율로 표현할 수 없다(웨이퍼 크기·die 크기에 따라
    두께가 달라진다). 좌표 집합만으로 판정하므로 추가 입력이 필요 없고, 결손 die 가 있는
    실제 map 에서도 그 구멍의 테두리를 잡는다.
    좌표가 정수 격자가 아니면(간격이 1이 아니면) 판정할 수 없어 전부 False 를 돌려준다 —
    그 경우 E1 관련 feature 는 자연히 결측이 되고, 결측은 "양호" 로 읽지 않는다(규칙).
    """
    xi = np.rint(xs).astype(np.int64)
    yi = np.rint(ys).astype(np.int64)
    if xi.size == 0 or not np.allclose(xs, xi) or not np.allclose(ys, yi):
        return np.zeros(xs.shape, dtype=bool)
    coords = set(zip(xi.tolist(), yi.tolist()))
    return np.array([((x + 1, y) not in coords) or ((x - 1, y) not in coords)
                     or ((x, y + 1) not in coords) or ((x, y - 1) not in coords)
                     for x, y in zip(xi.tolist(), yi.tolist())], dtype=bool)


def _spatial_features(case_ctx, th):
    """웨이퍼 좌표 기반 fail 편중 feature. 좌표가 없거나 fail 이 0 이면 전부 None.

    반경을 최대반경으로 정규화해 edge/center/ring 영역을 가르고, 각 영역의 fail 율을
    **전체 fail 율로 나눈 비**를 낸다 — 1.0 이면 편중 없음, 클수록 그 영역에 몰린 것.
    gradient 는 좌표를 8구간으로 나눈 구간별 fail 율의 회귀 기울기이고, `_norm` 변형은
    좌표 스케일이 제품마다 달라도 비교되도록 정규화 좌표로 다시 잰 값이다.
    영역 경계(edge_region_pct/center_region_pct)는 thresholds.yaml.
    """
    x = case_ctx.get("x_pos") or []
    y = case_ctx.get("y_pos") or []
    fail_mask = case_ctx.get("fail_mask") or []
    out = {"edge_fail_ratio": None, "center_fail_ratio": None, "radial_gradient": None,
           "quadrant_imbalance": None, "x_gradient": None, "y_gradient": None,
           "wafer_zone_signature": None, "ring_fail_ratio" : None,
           "radial_gradient_norm" : None, "x_gradient_norm" : None, "y_gradient_norm" : None,
           "e1_fail_ratio" : None}
    xs = np.array([v if v is not None else np.nan for v in x], dtype=float)
    ys = np.array([v if v is not None else np.nan for v in y], dtype=float)
    fm = np.asarray(fail_mask, dtype=bool)
    valid = np.isfinite(xs) & np.isfinite(ys)
    if valid.sum() < 2 or fm.sum() == 0:
        return out

    xs, ys, fm = xs[valid], ys[valid], fm[valid]
    radius = np.sqrt(xs ** 2 + ys ** 2)
    rmax = radius.max()
    xmax = float(np.max(np.abs(xs))) or None
    ymax = float(np.max(np.abs(ys))) or None
    overall_fail = fm.mean()
    if rmax > 0 and overall_fail > 0:
        rnorm = radius / rmax
        # E1(최외곽 1열)은 EDGE 밴드에서 뺀다 — 같은 die 를 두 룰이 각각 세면 "가장자리
        # 한 줄 문제" 와 "바깥 밴드 전체 문제" 가 구분되지 않는다(2026-08-12 공간축 세분).
        e1 = _e1_mask(xs, ys)
        edge_mask = (rnorm >= th["edge_region_pct"]) & (~e1)
        center_mask = rnorm <= th["center_region_pct"]
        ring_mask = (rnorm > th["center_region_pct"]) & (rnorm <th["edge_region_pct"]) & (~e1)
        if e1.sum():
            out["e1_fail_ratio"] = float(fm[e1].mean() / overall_fail)
        if edge_mask.sum():
            out["edge_fail_ratio"] = float(fm[edge_mask].mean() / overall_fail)
        if center_mask.sum():
            out["center_fail_ratio"] = float(fm[center_mask].mean() / overall_fail)
        if ring_mask.sum():
            out["ring_fail_ratio"] = float(fm[ring_mask].mean() / overall_fail)
        out["radial_gradient"] = _gradient(radius, fm)
        out["radial_gradient_norm"] = _gradient(rnorm,fm)

    out["x_gradient"] = _gradient(xs, fm)
    out["y_gradient"] = _gradient(ys, fm)
    if xmax :
        out["x_gradient_norm"] = _gradient(xs / xmax, fm)
    if ymax :
        out["y_gradient_norm"] = _gradient(ys / ymax, fm)

    # 사분면 불균형
    quad_rates = []
    for sx in (True, False):
        for sy in (True, False):
            qm = ((xs >= 0) == sx) & ((ys >= 0) == sy)
            if qm.sum():
                quad_rates.append(fm[qm].mean())
    if quad_rates:
        mean_rate = float(np.mean(quad_rates))
        if mean_rate > 0:
            out["quadrant_imbalance"] = float((max(quad_rates) - min(quad_rates)) / mean_rate)

    out["wafer_zone_signature"] = _classify_zone(out, th)
    return out

def _classify_modality_v2(n_dut, outlier_ratio, n_modes, bimodality_score, density_gap,
                          value_gap_ratio, value_gap_minor_mass, th):
    """이봉·다봉·분리 판정 — SUBPOP_GAP 발화의 유일한 근거. 반환: bimodal|multimodal|separated|None.

    게이트 2개를 먼저 통과해야 한다: 표본이 `subpop_n_min` 이상이고, outlier_ratio 가
    `subpop_outlier_ratio_max` 미만일 것. ⚠ 후자 때문에 **소수 모드가 outlier 로 잡히는
    분포는 이봉으로 발화하지 못한다** — 오발화를 줄이려는 의도된 보수적 게이트다.
    separated 는 값 축의 실제 빈 구간(`_value_gap`) 기준이다. 구 cdf_gap(동일값 질량)
    조건은 이산/양자화 데이터에서 오발화라 2026-08-03 에 교체했다.
    """
    if n_dut is None or n_dut < th["subpop_n_min"]:
        return None
    if outlier_ratio is not None and outlier_ratio >= th["subpop_outlier_ratio_max"]:
        return None
    if n_modes is not None and n_modes >= 3 and density_gap is not None and density_gap >= th["subpop_density_gap_warn"]:
        return "multimodal"
    if n_modes == 2 and bimodality_score is not None and bimodality_score >= th["bimodality_warn"] and density_gap is not None and density_gap >= th["subpop_density_gap_warn"]:
        return "bimodal"
    # separated: 값 축의 실제 빈 구간(_value_gap) 기준 — 구 cdf_gap(동일값 질량) 조건은
    # 이산/양자화 데이터에서 오발화라 교체(2026-08-03). 소수쪽 질량 하한으로 극단 소수점 배제.
    if (density_gap is not None and density_gap >= th["subpop_density_gap_strong"]
            and value_gap_ratio is not None and value_gap_ratio >= th["subpop_value_gap_warn"]
            and value_gap_minor_mass is not None
            and value_gap_minor_mass >= th["subpop_minor_mass_min"]):
        return "separated"
    return None


def _classify_zone(spatial, th):
    """공간 feature → wafer_zone_signature. 앞에서 걸리는 것이 이긴다.

    E1(최외곽 한 줄) → EDGE(가장자리 밴드) → CENTER(중앙 편중) → CLUSTER(사분면 불균형)
    순으로 보고, 아무 것도 임계를 넘지 못하면 RANDOM.
    """
    e1 = spatial.get("e1_fail_ratio")
    edge = spatial.get("edge_fail_ratio")
    center = spatial.get("center_fail_ratio")
    quad = spatial.get("quadrant_imbalance")
    if e1 is not None and e1 >= th.get("e1_fail_ratio_warn", th["edge_fail_ratio_warn"]):
        return "E1"
    if edge is not None and edge >= th["edge_fail_ratio_warn"]:
        return "EDGE"
    if center is not None and center >= th["edge_fail_ratio_warn"]:
        return "CENTER"
    if quad is not None and quad >= th["quadrant_imbalance_warn"]:
        return "CLUSTER"
    return "RANDOM"


def _site_cpk_delta(case_ctx):
    """site 별 cpk 의 최대-최소 차. site 가 없거나 cpk 를 낼 수 있는 site 가 2개 미만이면 None.

    site 간 공정능력 편차가 크면 장비/소켓 쪽을 의심하게 하는 지표(EQUIPMENT_SUSPECT).
    """
    site = case_ctx.get("site") or []
    values = case_ctx.get("values") or []
    if not site or all(s is None for s in site):
        return None
    lsl, usl = case_ctx.get("lsl"), case_ctx.get("usl")
    by_site = {}
    for s, v in zip(site, values):
        if s is None or v is None:
            continue
        by_site.setdefault(s, []).append(v)
    cpks = []
    for vals in by_site.values():
        c = cpk_summary(vals, lsl, usl).get("cpk")
        if c is not None:
            cpks.append(c)
    if len(cpks) < 2:
        return None
    return float(max(cpks) - min(cpks))


def compute(case_ctx: dict, raw_metrics: dict, engine_version: str) -> dict:
    """L2 진입점 — 측정값에서 robust 산포/spec margin/공간 feature 산출 (CODE_TO_PORT §5).

    산포는 표준편차가 아니라 MAD 기반 robust sigma 를 쓴다 — 소수의 폭주값이 산포를 통째로
    부풀려 정상 분포를 이상으로 오판하는 것을 막기 위해서다. MAD=0(과반이 같은 값)이면
    Iglewicz-Hoaglin meanAD 로 폴백한다. 그냥 0 으로 두면 "대부분 동일값 + 소수 폭주"
    케이스를 outlier 룰이 통째로 놓친다.
    PF(양불) item 은 측정값 기반 feature 를 전부 None 으로 비운다 — 값이 없는데 계산하면
    허수 판정이 된다. 공간 feature 와 n_dut 는 남는다.
    반환: DB_SCHEMA §5 features 컬럼 + 파생 2개(value_gap_ratio/value_gap_minor_mass —
    DB 에는 저장하지 않고 separated 판정·트레이스 표시에만 쓴다).
    """
    values = case_ctx.get("values") or []
    lsl, usl = case_ctx.get("lsl"), case_ctx.get("usl")
    n = len(values)
    th = thresholds_for(case_ctx)
    is_pf = case_ctx.get("value_type") == "PF"
    if n == 0:
        return _empty_features()

    v = np.asarray(values, dtype=float)
    median = float(np.median(v))
    mad = float(np.median(np.abs(v - median)))
    robust_sigma = 1.4826 * mad

    spread_norm = None
    if lsl is not None and usl is not None and (usl - lsl) != 0:
        spread_norm = robust_sigma / (usl - lsl)

    if mad != 0:
        modified_z = 0.6745 * (v - median) / mad
        outlier_ratio = float(np.mean(np.abs(modified_z) > th["outlier_sigma"]))
    else:
        # MAD=0(과반 동일값) — Iglewicz-Hoaglin meanAD 폴백. 그냥 0 으로 두면
        # "대부분 동일값 + 소수 폭주" 케이스를 outlier 룰이 통째로 놓친다.
        mean_ad = float(np.mean(np.abs(v - median)))
        if mean_ad > 0:
            modified_z = (v - median) / (1.253314 * mean_ad)
            outlier_ratio = float(np.mean(np.abs(modified_z) > th["outlier_sigma"]))
        else:
            outlier_ratio = 0.0

    mean = float(v.mean())
    stdev = raw_metrics.get("stdev")
    skewness = case_ctx.get("skewness")
    if skewness is None and stdev:
        skewness = (mean - median) / stdev
    kurtosis = float(np.mean(((v - mean) / stdev) ** 4) - 3) if stdev else None
    # 모멘트 왜도(3차) — `skewness`(비모수 (mean-median)/stdev)는 **수학적 상한이 1.0** 이라
    # `skew_warn: 1.0` 을 넘을 수 없다(TAIL_RISK 가 영원히 발화 못 하던 원인). 상한이 없는
    # 3차 모멘트를 따로 둬 룰이 실제로 판정할 수 있게 한다. 기존 컬럼은 그대로 둔다
    # (eval.db features.skewness 의 의미가 바뀌면 과거 데이터와 섞인다).
    skewness_moment = float(np.mean(((v - mean) / stdev) ** 3)) if stdev else None

    bimodality_score = raw_metrics.get("bimodality")
    if bimodality_score is not None:
        modality = "bi" if bimodality_score > th["bimodality_warn"] else "uni"
    else:
        modality = None

    density_gap = _density_gap(v)
    cdf_gap = _cdf_gap(v)
    n_modes = _n_modes(v)
    value_gap_ratio, value_gap_minor_mass = _value_gap(v)
    modality_v2 = _classify_modality_v2(n, outlier_ratio, n_modes, bimodality_score,
                                        density_gap, value_gap_ratio,
                                        value_gap_minor_mass, th)
    spec_margin_low = (mean - lsl) / stdev if (lsl is not None and stdev) else None
    spec_margin_high = (usl - mean) / stdev if (usl is not None and stdev) else None
    nearest_spec_side = None
    if spec_margin_low is not None and spec_margin_high is not None:
        nearest_spec_side = "LOW" if spec_margin_low < spec_margin_high else "HIGH"

    limit_hit_ratio = None
    if lsl is not None and usl is not None:
        limit_hit_ratio = float(np.mean(np.isclose(v, lsl) | np.isclose(v, usl)))

    spatial = _spatial_features(case_ctx, th)
    site_cpk_delta = _site_cpk_delta(case_ctx)
    code_edge_hit = limit_hit_ratio if case_ctx.get("value_type") == "CODE" else None

    if is_pf:
        spread_norm = skewness = kurtosis = skewness_moment = None
        outlier_ratio = modality = bimodality_score = density_gap = cdf_gap = None
        spec_margin_low = spec_margin_high = nearest_spec_side = limit_hit_ratio = None
        n_modes = modality_v2 = None
        value_gap_ratio = value_gap_minor_mass = None

    return {
        "spread_norm": spread_norm, "skewness": skewness, "kurtosis": kurtosis,
        "skewness_moment": skewness_moment,
        "outlier_ratio": outlier_ratio, "modality": modality,
        "bimodality_score": bimodality_score, "density_gap": density_gap, "cdf_gap": cdf_gap,
        "spec_margin_low": spec_margin_low, "spec_margin_high": spec_margin_high,
        "nearest_spec_side": nearest_spec_side, "limit_hit_ratio": limit_hit_ratio,
        **spatial,
        "n_dut": n, "site_cpk_delta": site_cpk_delta, "code_edge_hit": code_edge_hit,
        "n_modes" : n_modes, "modality_v2" : modality_v2,
        "value_gap_ratio": value_gap_ratio, "value_gap_minor_mass": value_gap_minor_mass,
    }

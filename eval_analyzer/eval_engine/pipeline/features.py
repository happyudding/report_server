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
]


def _empty_features():
    f = {k: None for k in _FEATURE_KEYS}
    f["n_dut"] = 0
    return f


def _cdf_gap(v):
    """ECDF(CODE_TO_PORT §3) 후 인접 누적% 최대 점프."""
    v = v[np.isfinite(v)]
    if v.size == 0:
        return None
    uniq, cnt = np.unique(np.sort(v), return_counts=True)
    cum = np.cumsum(cnt) / v.size * 100.0
    return float(np.max(np.diff(cum))) if len(cum) > 1 else 0.0


def _density_gap(v):
    """히스토그램 기반 이봉 골 깊이(0~1 정규화). 단봉이면 0, 표본 부족이면 None."""
    if v.size < 8:
        return None
    hist, _ = np.histogram(v, bins=min(20, max(5, v.size // 5)))
    peaks = [i for i in range(1, len(hist) - 1)
             if hist[i] > hist[i - 1] and hist[i] > hist[i + 1]]
    if len(peaks) < 2:
        return 0.0
    p1, p2 = sorted(peaks, key=lambda i: -hist[i])[:2]
    lo, hi = sorted([p1, p2])
    valley = int(hist[lo:hi + 1].min())
    peak_max = int(hist.max())
    if peak_max == 0:
        return 0.0
    return float((min(int(hist[p1]), int(hist[p2])) - valley) / peak_max)


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


def _spatial_features(case_ctx, th):
    x = case_ctx.get("x_pos") or []
    y = case_ctx.get("y_pos") or []
    fail_mask = case_ctx.get("fail_mask") or []
    out = {"edge_fail_ratio": None, "center_fail_ratio": None, "radial_gradient": None,
           "quadrant_imbalance": None, "x_gradient": None, "y_gradient": None,
           "wafer_zone_signature": None}
    xs = np.array([v if v is not None else np.nan for v in x], dtype=float)
    ys = np.array([v if v is not None else np.nan for v in y], dtype=float)
    fm = np.asarray(fail_mask, dtype=bool)
    valid = np.isfinite(xs) & np.isfinite(ys)
    if valid.sum() < 2 or fm.sum() == 0:
        return out

    xs, ys, fm = xs[valid], ys[valid], fm[valid]
    radius = np.sqrt(xs ** 2 + ys ** 2)
    rmax = radius.max()
    overall_fail = fm.mean()
    if rmax > 0 and overall_fail > 0:
        rnorm = radius / rmax
        edge_mask = rnorm >= th["edge_region_pct"]
        center_mask = rnorm <= th["center_region_pct"]
        if edge_mask.sum():
            out["edge_fail_ratio"] = float(fm[edge_mask].mean() / overall_fail)
        if center_mask.sum():
            out["center_fail_ratio"] = float(fm[center_mask].mean() / overall_fail)
        out["radial_gradient"] = _gradient(radius, fm)

    out["x_gradient"] = _gradient(xs, fm)
    out["y_gradient"] = _gradient(ys, fm)

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


def _classify_zone(spatial, th):
    edge = spatial.get("edge_fail_ratio")
    center = spatial.get("center_fail_ratio")
    quad = spatial.get("quadrant_imbalance")
    if edge is not None and edge >= th["edge_fail_ratio_warn"]:
        return "EDGE"
    if center is not None and center >= th["edge_fail_ratio_warn"]:
        return "CENTER"
    if quad is not None and quad >= th["quadrant_imbalance_warn"]:
        return "CLUSTER"
    return "RANDOM"


def _site_cpk_delta(case_ctx):
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
    values = case_ctx.get("values") or []
    lsl, usl = case_ctx.get("lsl"), case_ctx.get("usl")
    n = len(values)
    th = thresholds_for(case_ctx)

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
        outlier_ratio = float(np.mean(np.abs(modified_z) > th["modified_z"]))
    else:
        outlier_ratio = 0.0

    mean = float(v.mean())
    stdev = raw_metrics.get("stdev")
    skewness = case_ctx.get("skewness")
    if skewness is None and stdev:
        skewness = (mean - median) / stdev
    kurtosis = float(np.mean(((v - mean) / stdev) ** 4) - 3) if stdev else None

    bimodality_score = raw_metrics.get("bimodality")
    if bimodality_score is not None:
        modality = "bi" if bimodality_score > th["bimodality_warn"] else "uni"
    else:
        modality = None

    density_gap = _density_gap(v)
    cdf_gap = _cdf_gap(v)

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

    return {
        "spread_norm": spread_norm, "skewness": skewness, "kurtosis": kurtosis,
        "outlier_ratio": outlier_ratio, "modality": modality,
        "bimodality_score": bimodality_score, "density_gap": density_gap, "cdf_gap": cdf_gap,
        "spec_margin_low": spec_margin_low, "spec_margin_high": spec_margin_high,
        "nearest_spec_side": nearest_spec_side, "limit_hit_ratio": limit_hit_ratio,
        **spatial,
        "n_dut": n, "site_cpk_delta": site_cpk_delta, "code_edge_hit": code_edge_hit,
    }

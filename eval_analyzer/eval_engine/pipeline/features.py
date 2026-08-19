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
    # ── 아래는 전부 **룰의 판정 기준값**이라 v9(2026-08-19)부터 DB 에 저장한다
    # (store._V9_FEATURE_COLS). 저장 이전 행은 NULL 이며 소급 채움이 불가능하다
    # (per-DUT 원본에서만 나온다) — 재수집해야 채워진다.
    # separated 판정·트레이스 표시용
    "value_gap_ratio", "value_gap_minor_mass",
    # OUTLIER 판정용 — 거리(MAD 배수) AND 끊김(body_jump_ratio).
    # gap_sigma·z_max 는 evidence 참고값(2026-08-14 판정에서 제외 — features 상단 참조)
    "fail_mad_min", "fail_pass_gap_sigma", "fail_robust_z_max", "fail_body_jump_ratio",
    # 공간 룰 판정용 — 전체 fail 중 그 영역이 차지하는 **점유율**
    "edge_fail_share", "center_fail_share", "ring_fail_share",
    # fail 좌표 몰림도(SPOT_CLUSTER) · 꼬리 질량(HEAVY_TAIL) ·
    # CODE 레일 상/하단 분리(CODE_RAIL evidence)
    "fail_spread_norm", "tail_mass_3s", "rail_low_ratio", "rail_high_ratio",
    # 파생(DB 미저장): E1(최외곽 1 chip line) 집중도 · 모멘트 왜도
    "e1_fail_ratio", "skewness_moment",
    # E1_FAIL 판정용 점유율 — v9 저장 대상
    "e1_fail_share",
]


def _empty_features():
    """전 feature 를 None 으로 채운 dict(n_dut 만 0). 측정값이 하나도 없을 때 쓴다."""
    f = {k: None for k in _FEATURE_KEYS}
    f["n_dut"] = 0
    return f


# "인자 미전달" 구분용 센티널 — 계산 결과가 None 인 경우와 갈라야 하는 선택 인자에 쓴다.
_UNSET = object()


def _finite_uniq(v, finite=None):
    """유한값의 (finite_v, uniq, counts) — `np.unique` 정렬 1회를 세 소비자가 공유한다.

    `_cdf_gap`·`_value_gap`·`_grid_step` 이 각자 같은 배열을 정렬하던 것을 합친 것이라
    **값은 완전히 동일**하다(2026-08-19). `finite` 를 주면 유한 필터를 건너뛴다
    (`_grid_step` 은 호출부가 이미 유한값만 넘긴다 — 종전 동작 유지).
    """
    fv = v if finite is not None else v[np.isfinite(v)]
    uniq, cnt = np.unique(fv, return_counts=True)
    return fv, uniq, cnt


def _cdf_gap(v, uq=None):
    """ECDF(CODE_TO_PORT §3) 후 인접 누적% 최대 점프.

    주의: 이 값은 "최다 동일값 하나가 차지하는 질량(%)"이다 — 값 축의 빈 구간(분리)이
    아니라 동일값 쏠림(양자화/clamp) 신호다. 분리 판정에는 _value_gap 을 쓴다.
    `uq` 는 `_finite_uniq` 결과 공유용(없으면 여기서 만든다 — 값 동일).
    """
    fv, _, cnt = uq if uq is not None else _finite_uniq(v)
    if fv.size == 0:
        return None
    cum = np.cumsum(cnt) / fv.size * 100.0
    return float(np.max(np.diff(cum))) if len(cum) > 1 else 0.0


def _value_gap(v, uq=None):
    """값 축 최대 인접 간격 → (간격/전체범위, 간격 양쪽 중 소수쪽 질량).

    separated(분리) 판정용 — 두 무리 사이의 실제 빈 구간을 본다. cdf_gap(동일값 질량)과
    다르다. 고유값 2개 미만이거나 범위 0 이면 (None, None).
    `uq` 는 `_finite_uniq` 결과 공유용(없으면 여기서 만든다 — 값 동일).
    """
    v, uniq, _ = uq if uq is not None else _finite_uniq(v)
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

# 양자화 격자 검출 상수 — 판정 임계가 아니라 **히스토그램을 어떻게 만들지**의 규칙이라
# thresholds.yaml 이 아니라 코드에 둔다(불변 규칙 5 는 판정 임계에 대한 것).
_GRID_MIN_LEVELS = 3        # 격자로 인정할 최소 계단 수
_GRID_MASS_MIN = 0.8        # 계단 위에 있어야 할 최소 질량 비율
_GRID_TOL = 0.25            # 계단 간격이 기본 간격의 정수배에서 벗어나도 되는 허용 오차


def _grid_step(v, uq=None):
    """값이 일정 간격 격자(양자화 계단) 위에 있으면 그 간격, 아니면 None.

    도수가 적은 값(밀려난 fail chip 등)은 제외하고 **몸통을 이루는 계단**만 본다.
    `uq` 는 `_finite_uniq` 결과 공유용(없으면 여기서 만든다 — 값 동일).
    """
    if uq is not None:
        _, uniq, cnt = uq
    else:
        uniq, cnt = np.unique(v, return_counts=True)
    # 같은 마스크를 두 번 만들던 것을 1회로 (값 동일).
    keep = cnt >= max(2, 0.005 * v.size)
    heavy = uniq[keep]
    if heavy.size < _GRID_MIN_LEVELS:
        return None
    if float(cnt[keep].sum()) < _GRID_MASS_MIN * v.size:
        return None
    diffs = np.diff(heavy)
    step = float(np.median(diffs))
    if step <= 0:
        return None
    # 간격이 전부 step 의 정수배여야 진짜 격자다(불규칙하면 그냥 이산 분포)
    k = np.round(diffs / step)
    if np.any(k < 1) or np.any(np.abs(diffs - k * step) > _GRID_TOL * step):
        return None
    return step


def _histogram_peaks(v, uq=None):
    """히스토그램 + 국소 최대(양옆 bin 보다 큰) 인덱스. 표본 8 미만이면 None(판정 불가).

    반환: (peaks, hist). bin 수는 표본 크기에 따라 5~20 사이. `_density_gap` 과 `_n_modes`
    가 **같은 히스토그램**을 봐야 골 깊이와 봉우리 수가 어긋나지 않으므로 둘이 공유한다.
    `uq` 는 `_grid_step` 에 넘길 `_finite_uniq` 결과 — **v 와 같은 배열에서 나온 것만**
    넘겨야 한다(호출부가 유한 필터 no-op 을 확인한다). 값은 동일.

    ⚠ **양자화 격자 정렬**(2026-08-13): 값이 계단형(CODE·PCT 등)이면 bin 폭이 계단 간격보다
    좁아 **빈 칸이 사이사이 끼며 가짜 봉우리**가 생긴다. 실측 예 — step 0.125 인 단봉
    데이터의 히스토그램이 `[28,0,23,0,87,0,573,0,1393,1797,0,…]` 이 되어 봉우리 8개로
    잡혔다(계단별 도수는 완벽한 단봉인데도 BIMODALITY 오발화). bin 경계를 격자에 맞추면
    사라진다. 격자가 촘촘하면(CODE 64레벨) 칸별 도수가 잡음이 되므로 m칸씩 묶어 집계한다.
    """
    if v.size < 8:
        return None
    bins = min(20, max(5, v.size // 5))
    step = _grid_step(v, uq)
    grid_counts = None
    if step:
        idx = np.round((v - v.min()) / step).astype(int)
        grid_counts = np.bincount(idx)
        counts = grid_counts
        m = max(1, int(np.ceil(counts.size / bins)))
        if m > 1:                                  # m칸씩 묶기 — 남는 칸은 0 으로 패딩
            pad = (-counts.size) % m
            counts = np.concatenate([counts, np.zeros(pad, dtype=counts.dtype)])
            counts = counts.reshape(-1, m).sum(axis=1)
        hist = counts
    else:
        hist, _ = np.histogram(v, bins=bins)
    peaks = [i for i in range(1, len(hist) - 1)
             if hist[i] > hist[i-1] and hist[i] > hist[i+1]]
    return peaks, hist, grid_counts


def _grid_empty_levels(grid_counts) -> int:
    """격자 데이터에서 **값이 하나도 없는 계단** 수 (양끝 빈 칸은 제외).

    이산(CODE) 값의 이봉 판정 게이트다. 계단형은 값이 몇 개 레벨에만 놓이는 것이 정상이고
    (히스토그램처럼 생긴 정규분포도 계단이다), 진짜로 무리가 갈라졌다면 **레벨 자체가
    비어 있는 구간**이 생긴다. 그게 없으면 "이산이라서 울퉁불퉁한 것" 이지 이봉이 아니다.
    """
    if grid_counts is None:
        return -1                                  # 격자가 아님 → 게이트 미적용 표식
    nz = np.flatnonzero(grid_counts)
    if nz.size < 2:
        return 0
    inner = grid_counts[nz[0]:nz[-1] + 1]
    return int((inner == 0).sum())

def _density_gap(v, peaks_hist=_UNSET):
    """히스토그램 기반 이봉 골 깊이(0~1 정규화). 단봉이면 0, 표본 부족이면 None.

    peaks_hist 를 넘기면(compute 가 _histogram_peaks 1회 계산 후 _n_modes 와 공유)
    재계산하지 않는다 — 같은 히스토그램을 두 번 만들던 낭비 제거(docstring 의 "둘이
    공유한다" 를 실제로 수행).
    """
    if peaks_hist is _UNSET:
        peaks_hist = _histogram_peaks(v)
    if peaks_hist is None:
        return None
    peaks, hist = peaks_hist[0], peaks_hist[1]

    if len(peaks) < 2:
        return 0.0
    p1, p2 = sorted(peaks, key=lambda i: -hist[i])[:2]
    lo, hi = sorted([p1, p2])
    valley = int(hist[lo:hi + 1].min())
    peak_max = int(hist.max())
    if peak_max == 0:
        return 0.0
    return float((min(int(hist[p1]), int(hist[p2])) - valley) / peak_max)

def _n_modes(v, peaks_hist=_UNSET):
    """히스토그램 봉우리 개수(최소 1). 표본 부족이면 None. peaks_hist 공유는 _density_gap 참조."""
    if peaks_hist is _UNSET:
        peaks_hist = _histogram_peaks(v)
    if peaks_hist is None:
        return None
    return max(len(peaks_hist[0]), 1)

def _modified_z(v, median, mad):
    """Iglewicz-Hoaglin modified z (MAD 기반). MAD=0(과반 동일값)이면 meanAD 폴백.

    표준편차 대신 중앙값·MAD 를 쓰는 이유는 **자가 오염** 때문이다 — 멀리 튄 값이 몇 개만
    있어도 stdev 가 그만큼 커져 그 값의 z 가 도로 작아진다(masking). 중앙값 기준은 표본
    절반이 오염돼도 흔들리지 않는다. 정규분포에서 1.4826·MAD ≈ σ 라 스케일도 호환된다.
    둘 다 0(전부 동일값)이면 흩어짐 자체가 없으므로 None.
    """
    if mad != 0:
        return 0.6745 * (v - median) / mad
    mean_ad = float(np.mean(np.abs(v - median)))
    if mean_ad > 0:
        return (v - median) / (1.253314 * mean_ad)
    return None


def _fail_outlier_features(v, fail_mask, median, mad, z=_UNSET):
    """fail 이 정상 몸통에서 **얼마나 떨어졌고 얼마나 끊겼는지** — OUTLIER 판정의 두 축.

    반환 (fail_mad_min, fail_pass_gap_sigma, fail_robust_z_max):
      · `fail_mad_min`         = 중심에 **가장 가까운** fail 의 |x−median|/MAD (= MAD 배수)
      · `fail_pass_gap_sigma`  = min(fail 거리) − max(pass 거리), robust σ 단위
      · `fail_robust_z_max`    = 가장 먼 fail 의 거리 (판정 미사용, evidence 참고값)

    **왜 두 축인가** (2026-08-13, 사용자 v6 검토로 확정): 거리 하나로는 가릴 수 없다.
    실측에서 z 13.2 인 항목이 heavy tail 이고 z 8.5 인 항목이 outlier 였다 — 거리 순서가
    라벨과 뒤집힌다. 가르는 것은 **연속성**이다:
      · 꼬리가 몸통에서 limit 까지 이어져 넘어갔으면 → gap ≈ 0 → HEAVY_TAIL
      · 몸통과 뚝 끊겨 따로 놀면            → gap 큼   → OUTLIER
    거리(mad) 조건은 "limit 바로 밖에 붙은 fail"(공정능력 문제)을 걸러내는 하한이다.

    ⚠ **`fail_pass_gap_sigma` 는 2026-08-14 부터 판정에 쓰지 않는다**(evidence·저장은 유지).
    연속성이라는 착안은 맞았으나 이 식이 그것을 못 쟀다 — `dist` 가 `|z|` 라 **양쪽 꼬리를
    한 자에 섞어서**, 반대쪽에 더 먼 pass 가 하나만 있어도 음수가 됐다. 연속성 축은 같은
    쪽만 보는 `_fail_body_jump_ratio` 로 옮겼다(그 docstring에 근거).

    거리를 MAD 배수로 돌려주는 이유: 사용자가 "MAD 기준 4σ" 라고 말하는 값이 화면·트레이스에
    그대로 보여야 한다. `0.6745 × 1.4826 ≈ 1` 이라 `|x−med|/robustσ ≡ |modified z|` 이고,
    MAD 배수는 거기에 `/0.6745` 한 것이다 — 자(尺)는 하나다.

    fail 이 없거나 pass 가 하나도 없으면(전량 fail) gap 을 잴 수 없어 None → 조건 False
    → 미발화. 결측을 양호로 읽지 않는 규칙과 같은 취급이다.
    """
    fm = np.asarray(fail_mask, dtype=bool)
    if fm.size != v.size or not fm.any():
        return None, None, None
    if z is _UNSET:
        z = _modified_z(v, median, mad)
    if z is None:                                  # 전부 동일값 — 흩어짐 자체가 없다
        return None, None, None
    ok = np.isfinite(v)
    dist = np.abs(z)
    df, dp = dist[fm & ok], dist[(~fm) & ok]
    if not df.size:
        return None, None, None
    mad_min = float(df.min()) / 0.6745
    z_max = float(df.max())
    gap = float(df.min() - dp.max()) if dp.size else None
    return mad_min, gap, z_max


def _fail_body_jump_ratio(v, fail_mask, median, mad, th, z=_UNSET):
    """몸통과 최근접 fail 사이가 **얼마나 비어 있나** — OUTLIER 연속성 축(2026-08-14).

    `fail_pass_gap_sigma` 를 판정에서 대체한다. 그 지표는 이름과 달리 연속성을 재지
    못했다 — `min(|z| of fail) − max(|z| of pass)` 라 **양쪽 꼬리를 한 자에 섞는다**.
    반대쪽 꼬리에 더 먼 pass 가 하나만 있어도 음수가 되어, 몸통과 뚝 끊긴 fail 덩어리가
    통째로 미발화했다(v9 실측: 사용자가 outlier 로 지목한 8건이 −3.4 ~ +1.5).

    그래서 **같은 쪽에서, 몸통 경계부터 최근접 fail 까지만** 본다:
      · side  = `fail_mad_min` 을 만든 그 fail 이 있는 쪽 (두 축의 기준을 일치시킨다)
      · body  = min(outlier_body_z, 0.8·zfm) — 몸통 경계. 0.8 배는 fail 이 몸통 경계보다
                안쪽에 있어(zfm < 3) 구간이 뒤집히는 경우의 폴백이다.
      · 반환  = 그 구간에서 **한 번에 비어 있는 최대 폭 / 구간 전체 폭**

    1.0 에 가까우면 구간이 통째로 비었다(= 몸통과 끊긴 별개 덩어리 → OUTLIER),
    0 에 가까우면 점들이 촘촘히 이어져 넘어갔다(= 꼬리가 길어진 것 → HEAVY_TAIL).
    v9 겨냥 세트 실측: HEAVY_TAIL 0.170~0.287 / OUTLIER 0.940~1.000 으로 갈린다.

    **DB 에 저장하지 않는 파생 키다**(value_gap_ratio 선례) — eval.db 스키마 무변경.
    fail 이 없거나 흩어짐 자체가 없으면(전부 동일값) None → 조건 False → 미발화.
    """
    fm = np.asarray(fail_mask, dtype=bool)
    if fm.size != v.size or not fm.any():
        return None
    if z is _UNSET:
        z = _modified_z(v, median, mad)
    if z is None:                                  # 전부 동일값 — 흩어짐이 없다
        return None
    ok = np.isfinite(v)
    fm = fm & ok
    if not fm.any():
        return None
    dist = np.abs(z)
    i = int(np.argmin(np.where(fm, dist, np.inf)))  # 중심에 가장 가까운 fail = mad_min 의 주인
    zfm = float(dist[i])
    body = min(float(th["outlier_body_z"]), 0.8 * zfm)
    span = zfm - body
    if span <= 0:
        return None
    side = (z > 0) if z[i] > 0 else (z < 0)
    pz = dist[(~fm) & ok & side]
    band = np.sort(pz[(pz >= body) & (pz < zfm)])
    seq = np.concatenate(([body], band, [zfm]))
    return float(np.max(np.diff(seq)) / span)


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


def _edge_of_line(key, val):
    """key 로 묶은 각 줄에서 val 이 **양끝**인 die 마스크 (행별 좌우끝 / 열별 상하끝).

    그룹별 min/max 는 정렬 + reduceat 로 구한다 — 종전 `np.minimum.at`/`maximum.at`
    (unbuffered ufunc.at, 벡터 연산 대비 수십 배 느리고 GIL 도 놓지 않음)과 결과 동일
    (min/max 는 순서 무관). inv 는 np.unique 산출이라 0..k-1 을 모두 가진다(빈 그룹 없음).
    """
    uniq, inv = np.unique(key, return_inverse=True)
    order = np.argsort(inv, kind="stable")
    sv = np.asarray(val)[order]
    starts = np.searchsorted(inv[order], np.arange(uniq.size))
    lo = np.minimum.reduceat(sv, starts)
    hi = np.maximum.reduceat(sv, starts)
    return (val == lo[inv]) | (val == hi[inv])


def _e1_mask(xs, ys):
    """**최외곽 1 chip line(E1)** die 마스크 — 각 행의 좌·우 끝 + 각 열의 위·아래 끝.

    "웨이퍼 가장자리 한 줄" 은 반경 비율로 표현할 수 없다(웨이퍼 크기·die 크기에 따라
    두께가 달라진다). 그렇다고 4-이웃(x±1)을 조회하면 **die pitch 가 1 이라는 가정**이
    생겨, 좌표 간격이 2 이거나 격자를 띄엄띄엄 측정한 map 에서 **모든 die 를 최외곽으로
    오판한다**(실측 100%). 그 경우 `edge = 반경밴드 & ~E1` 이 비어 EDGE·RING 룰이 조용히
    죽는다. 그래서 간격을 전혀 가정하지 않는 "줄의 양끝" 정의를 쓴다.

    E1 이 전체의 절반을 넘으면(한 줄짜리 배치 등) 개념이 성립하지 않으므로 **판정 불가**로
    보고 전부 False 를 돌려준다 — E1 feature 는 결측이 되고(결측을 양호로 읽지 않는 규칙),
    EDGE/RING 은 원래 밴드를 그대로 쓴다.
    """
    if xs.size == 0:
        return np.zeros(xs.shape, dtype=bool)
    mask = _edge_of_line(ys, xs) | _edge_of_line(xs, ys)
    return mask if mask.mean() <= 0.5 else np.zeros(xs.shape, dtype=bool)


def _spatial_geometry(case_ctx):
    """fail 과 무관한 좌표 전처리(중심 정렬·반경·E1) — 같은 item 의 case(bin)들이 공유.

    valid 좌표가 2개 미만이면 None(공간 feature 전부 결측 — 종전 게이트와 동일 조건).
    **웨이퍼 중심을 좌표에서 구한다** — XPOS/YPOS 는 실데이터에서 항상 양수(0/1-based die
    인덱스)라, 원점(0,0) 기준으로 반경을 재면 웨이퍼 한 귀퉁이가 중심이 되어 edge/center/
    ring/quadrant 판정이 통째로 어긋난다. 좌표 범위의 중앙(bounding box 중심)을 쓴다.
    이미 중심 정렬된(음수 포함) 입력에는 no-op 이다.
    """
    x = case_ctx.get("x_pos") or []
    y = case_ctx.get("y_pos") or []
    xs = np.array([v if v is not None else np.nan for v in x], dtype=float)
    ys = np.array([v if v is not None else np.nan for v in y], dtype=float)
    valid = np.isfinite(xs) & np.isfinite(ys)
    if valid.sum() < 2:
        return None
    xs, ys = xs[valid], ys[valid]
    xs = xs - (float(xs.max()) + float(xs.min())) / 2.0
    ys = ys - (float(ys.max()) + float(ys.min())) / 2.0
    radius = np.sqrt(xs ** 2 + ys ** 2)
    rmax = radius.max()
    return {"xs": xs, "ys": ys, "valid": valid, "radius": radius, "rmax": rmax,
            "xmax": float(np.max(np.abs(xs))) or None,
            "ymax": float(np.max(np.abs(ys))) or None,
            "rnorm": (radius / rmax) if rmax > 0 else None,
            "e1": _e1_mask(xs, ys)}


def _spatial_features(case_ctx, th, geom=_UNSET, fm_in=None):
    """웨이퍼 좌표 기반 fail 편중 feature. 좌표가 없거나 fail 이 0 이면 전부 None.

    반경을 최대반경으로 정규화해 edge/center/ring 영역을 가르고, 영역마다 두 가지를 낸다:
      · `*_fail_ratio`  = 영역 fail율 / 전체 fail율 — 1.0 이면 편중 없음(밀도 비)
      · `*_fail_share`  = 영역 fail 수 / 전체 fail 수 — "그 영역이 fail 을 몇 % 가졌나"(점유율)
    **룰 판정은 share 를 쓴다**(2026-08-12). 밀도 비는 영역이 좁을수록 쉽게 커지고
    수학적 상한도 영역마다 달라(edge≈2.8, center≈11, ring≈1.8) 임계값을 공유할 수 없는데,
    엔지니어가 "edge 불량"이라 부르는 것은 결국 "fail 이 죄다 edge 에 있다"이기 때문이다.
    gradient 는 좌표를 8구간으로 나눈 구간별 fail 율의 회귀 기울기이고, `_norm` 변형은
    좌표 스케일이 제품마다 달라도 비교되도록 정규화 좌표로 다시 잰 값이다.
    영역 경계(edge_region_pct/center_region_pct)는 thresholds.yaml.
    geom 은 _spatial_geometry 결과 재사용용(compute 가 item 단위로 공유) — 미전달이면
    여기서 계산한다(종전 동작·직접 호출 테스트 호환).
    fm_in 은 이미 bool ndarray 로 만든 fail_mask 재사용용(compute 가 case 단위로 1회
    변환) — 미전달이면 여기서 변환한다. 값은 동일하다.
    """
    fail_mask = case_ctx.get("fail_mask") or []
    out = {"edge_fail_ratio": None, "center_fail_ratio": None, "radial_gradient": None,
           "quadrant_imbalance": None, "x_gradient": None, "y_gradient": None,
           "wafer_zone_signature": None, "ring_fail_ratio" : None,
           "radial_gradient_norm" : None, "x_gradient_norm" : None, "y_gradient_norm" : None,
           "e1_fail_ratio" : None,
           "e1_fail_share": None, "edge_fail_share": None,
           "center_fail_share": None, "ring_fail_share": None,
           "fail_spread_norm": None}
    if geom is _UNSET:
        geom = _spatial_geometry(case_ctx)
    fm = fm_in if fm_in is not None else np.asarray(fail_mask, dtype=bool)
    if geom is None or fm.sum() == 0:
        return out

    xs, ys, radius = geom["xs"], geom["ys"], geom["radius"]
    rmax, xmax, ymax = geom["rmax"], geom["xmax"], geom["ymax"]
    fm = fm[geom["valid"]]
    overall_fail = fm.mean()
    if rmax > 0 and overall_fail > 0:
        rnorm = geom["rnorm"]
        # E1(최외곽 1열)은 EDGE 밴드에서 뺀다 — 같은 die 를 두 룰이 각각 세면 "가장자리
        # 한 줄 문제" 와 "바깥 밴드 전체 문제" 가 구분되지 않는다(2026-08-12 공간축 세분).
        e1 = geom["e1"]
        edge_mask = (rnorm >= th["edge_region_pct"]) & (~e1)
        center_mask = rnorm <= th["center_region_pct"]
        ring_mask = (rnorm > th["center_region_pct"]) & (rnorm <th["edge_region_pct"]) & (~e1)
        fail_total = float(fm.sum())
        for name, mask in (("e1", e1), ("edge", edge_mask),
                           ("center", center_mask), ("ring", ring_mask)):
            if mask.sum():
                out[f"{name}_fail_ratio"] = float(fm[mask].mean() / overall_fail)
                out[f"{name}_fail_share"] = float(fm[mask].sum() / fail_total)
        out["radial_gradient"] = _gradient(radius, fm)
        out["radial_gradient_norm"] = _gradient(rnorm,fm)

    out["x_gradient"] = _gradient(xs, fm)
    out["y_gradient"] = _gradient(ys, fm)
    if xmax :
        out["x_gradient_norm"] = _gradient(xs / xmax, fm)
    if ymax :
        out["y_gradient_norm"] = _gradient(ys / ymax, fm)

    # 사분면 불균형 — **0° 와 45° 두 격자의 max**(2026-08-13).
    # 축에 걸친 뭉침은 0° 격자에서 두 사분면으로 쪼개져 편중이 반토막 난다(실측: 사분면
    # 한가운데 blob 4.00 → x축 경계 blob 2.20 으로 임계 2.5 미달 = 미검출). 격자를 45°
    # 돌려 다시 재면 그 blob 이 4.00 으로 잡힌다 — 둘 중 큰 값을 쓴다.
    imbalances = [v for v in (_quadrant_imbalance(xs, ys, fm),
                              _quadrant_imbalance(xs + ys, ys - xs, fm)) if v is not None]
    if imbalances:
        out["quadrant_imbalance"] = max(imbalances)

    # fail 좌표의 **몰림 정도** — 위치·모양과 무관하게 "서로 가까이 붙어 있나" 만 본다.
    # 존(E1/EDGE/CENTER/RING)으로도 사분면으로도 설명 안 되는 국부 뭉침(scratch·국부 결함)이
    # 여기 걸린다. fail 무게중심에서의 RMS 거리를 웨이퍼 반경으로 정규화 — 웨이퍼 전면에
    # 고루 흩어지면 ≈0.6, 한 점에 뭉치면 0 에 가깝다.
    if fm.sum() >= 2 and rmax > 0:
        fx, fy = xs[fm], ys[fm]
        cx, cy = float(fx.mean()), float(fy.mean())
        out["fail_spread_norm"] = float(
            np.sqrt(np.mean((fx - cx) ** 2 + (fy - cy) ** 2)) / rmax)

    out["wafer_zone_signature"] = _classify_zone(out, th)
    return out


def _quadrant_imbalance(ax, ay, fm):
    """주어진 두 축이 만드는 4분면의 fail율 편중 (max−min)/mean. 판정 불가면 None."""
    rates = []
    for sx in (True, False):
        for sy in (True, False):
            qm = ((ax >= 0) == sx) & ((ay >= 0) == sy)
            if qm.sum():
                rates.append(fm[qm].mean())
    if not rates:
        return None
    mean_rate = float(np.mean(rates))
    return float((max(rates) - min(rates)) / mean_rate) if mean_rate > 0 else None

def _classify_modality_v2(n_dut, outlier_ratio, n_modes, bimodality_score, density_gap,
                          value_gap_ratio, value_gap_minor_mass, th, grid_empty=-1):
    """이봉·다봉·분리 판정 — BIMODALITY 발화의 유일한 근거. 반환: bimodal|multimodal|separated|None.

    게이트 3개를 먼저 통과해야 한다: 표본이 `subpop_n_min` 이상이고, outlier_ratio 가
    `subpop_outlier_ratio_max` 미만이며, **격자(이산) 데이터면 빈 계단이 2개 이상**일 것.
    ⚠ 둘째 게이트 때문에 소수 모드가 outlier 로 잡히는 분포는 이봉으로 발화하지 못한다
    (오발화를 줄이려는 의도된 보수적 게이트).
    셋째 게이트(2026-08-13, `grid_empty`)는 **CODE 같은 이산값** 때문이다 — 값이 몇 개
    레벨에만 놓이는 것은 이산이면 정상이고(계단으로 그린 정규분포도 울퉁불퉁하다), 진짜로
    무리가 갈라졌다면 **레벨 자체가 빈 구간**이 생긴다. `grid_empty = -1` 은 격자가 아니라는
    표식이라 게이트를 적용하지 않는다(연속값은 종전 그대로).
    separated 는 값 축의 실제 빈 구간(`_value_gap`) 기준이다. 구 cdf_gap(동일값 질량)
    조건은 이산/양자화 데이터에서 오발화라 2026-08-03 에 교체했다.
    """
    if n_dut is None or n_dut < th["subpop_n_min"]:
        return None
    if outlier_ratio is not None and outlier_ratio >= th["subpop_outlier_ratio_max"]:
        return None
    if 0 <= grid_empty < 2:
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

    E1(최외곽 한 줄) → EDGE(가장자리 밴드) → CENTER(중앙 편중) → RING(중간 밴드) →
    SPOT(국부 뭉침) → CLUSTER(사분면 불균형) 순으로 보고, 아무 것도 임계를 넘지 못하면 RANDOM.
    **공간 룰과 같은 기준(share)을 쓴다** — 이 라벨과 발화 룰이 갈라지면 같은 화면에서
    "zone 은 RANDOM 인데 EDGE_FAIL 이 떴다" 같은 모순이 보인다.
    """
    quad = spatial.get("quadrant_imbalance")
    spread = spatial.get("fail_spread_norm")
    for name, key in (("E1", "e1_fail_share"), ("EDGE", "edge_fail_share"),
                      ("CENTER", "center_fail_share"), ("RING", "ring_fail_share")):
        share = spatial.get(key)
        if share is not None and share >= th["region_fail_share_min"]:
            return name
    if spread is not None and spread <= th["spot_cluster_spread_max"]:
        return "SPOT"
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

    # item 단위 공유(_shared — ingest 가 같은 item 의 case(bin)들에 같은 dict 를 붙임):
    # fail bin 과 무관한 계산(배열 변환·정렬·히스토그램·robust 통계·좌표 전처리)을 case
    # 수만큼 반복하지 않는다(2026-08-13 콜드 빌드 최적화 — 값 완전 동일, 재계산만 제거).
    # 값은 1-튜플로 감싸 "계산 결과가 None" 과 "미계산" 을 구분한다. th 의존값
    # (outlier_ratio·공간 밴드·modality_v2 등)은 item_class 오버레이가 bin 축을 가질 수
    # 있어 공유하지 않는다. 동시 계산 경합은 무해(같은 입력 → 같은 값, dict 대입 원자적).
    shared = case_ctx.get("_shared")
    # 좌표 전처리만 **run(소스) 단위** 통을 쓴다 — 한 소스의 item 들은 같은 die 목록이라
    # 좌표가 하나뿐인데 종전에는 item 마다 중심정렬·반경·E1 마스크를 다시 만들었다
    # (실측 콜드 평가의 10%). ingest 가 좌표를 소스 공용 배열 그대로 넘긴 case 에만
    # `_geom_shared` 를 붙여 준다 — 없으면(NaN 이 섞여 좌표를 따로 만든 item, 레거시 입력
    # 경로) 종전대로 item 단위 `_shared` 로 폴백한다.
    geom_store = case_ctx.get("_geom_shared")
    if geom_store is None:
        geom_store = shared

    def _memo(key, fn, store=_UNSET):
        store = shared if store is _UNSET else store
        if store is None:
            return fn()
        hit = store.get(key)
        if hit is None:
            hit = (fn(),)
            store[key] = hit
        return hit[0]

    v = _memo("v", lambda: np.asarray(values, dtype=float))
    median = _memo("median", lambda: float(np.median(v)))
    mad = _memo("mad", lambda: float(np.median(np.abs(v - median))))
    robust_sigma = 1.4826 * mad

    spread_norm = None
    if lsl is not None and usl is not None and (usl - lsl) != 0:
        spread_norm = robust_sigma / (usl - lsl)

    # MAD=0(과반 동일값)이면 meanAD 폴백 — 그냥 0 으로 두면 "대부분 동일값 + 소수 폭주"
    # 케이스를 outlier 룰이 통째로 놓친다. 폴백 규칙은 _modified_z 가 갖고 있고,
    # fail 거리 지표들도 같은 함수를 써서 두 지표의 자(尺)가 갈라지지 않게 한다.
    modified_z = _memo("modified_z", lambda: _modified_z(v, median, mad))
    outlier_ratio = (0.0 if modified_z is None
                     else float(np.mean(np.abs(modified_z) > th["outlier_sigma"])))
    # 꼬리 **질량** — 중심에서 3 robust σ 밖에 있는 값의 비율. kurtosis 를 보조한다:
    # kurtosis 는 4제곱이라 **점 몇 개**로도 치솟고(질량 0.9% 에 kurt 21.5 — 꼬리가 아니라
    # 튄 점), 반대로 몸통이 갈라진 다봉에서도 커진다(질량 17% — 그건 꼬리가 아니다).
    # "평소엔 얌전한데 가끔 크게 튄다" 는 그 사이 밴드(1~5%)에 있다.
    tail_mass_3s = (None if modified_z is None
                    else float(np.mean(np.abs(modified_z) > 3.0)))

    mean = _memo("mean", lambda: float(v.mean()))
    stdev = raw_metrics.get("stdev")
    skewness = case_ctx.get("skewness")
    if skewness is None and stdev:
        skewness = (mean - median) / stdev
    kurtosis = _memo("kurtosis", lambda: (
        float(np.mean(((v - mean) / stdev) ** 4) - 3) if stdev else None))
    # 모멘트 왜도(3차) — `skewness`(비모수 (mean-median)/stdev)는 **수학적 상한이 1.0** 이라
    # `skew_warn: 1.0` 을 넘을 수 없다(TAIL_RISK 가 영원히 발화 못 하던 원인). 상한이 없는
    # 3차 모멘트를 따로 둬 룰이 실제로 판정할 수 있게 한다. 기존 컬럼은 그대로 둔다
    # (eval.db features.skewness 의 의미가 바뀌면 과거 데이터와 섞인다).
    skewness_moment = _memo("skewness_moment", lambda: (
        float(np.mean(((v - mean) / stdev) ** 3)) if stdev else None))

    bimodality_score = raw_metrics.get("bimodality")
    if bimodality_score is not None:
        modality = "bi" if bimodality_score > th["bimodality_warn"] else "uni"
    else:
        modality = None

    # 값 정렬(np.unique)을 **1회만** 한다 — _cdf_gap·_value_gap·_grid_step 이 각자
    # 같은 배열을 정렬하던 것을 합쳤다(2026-08-19, 값 동일). item 단위 메모라 같은
    # item 의 bin 이 여러 개여도 정렬은 한 번뿐이다.
    uq = _memo("finite_uniq", lambda: _finite_uniq(v))
    # _grid_step 은 호출부가 유한 필터를 안 거친 v 를 쓰던 함수라, **필터가 no-op 일
    # 때만** 공유본을 넘긴다(비유한이 섞였으면 종전대로 v 로 다시 계산 — 값 보존).
    uq_raw = uq if uq[0].size == v.size else None
    # _density_gap 과 _n_modes 는 같은 히스토그램을 봐야 한다(각 함수 docstring) —
    # 1회 계산해 공유(종전에는 같은 입력으로 두 번 계산했다).
    peaks_hist = _memo("peaks_hist", lambda: _histogram_peaks(v, uq_raw))
    density_gap = _memo("density_gap", lambda: _density_gap(v, peaks_hist))
    cdf_gap = _memo("cdf_gap", lambda: _cdf_gap(v, uq))
    n_modes = _memo("n_modes", lambda: _n_modes(v, peaks_hist))
    value_gap_ratio, value_gap_minor_mass = _memo("value_gap", lambda: _value_gap(v, uq))
    modality_v2 = _classify_modality_v2(
        n, outlier_ratio, n_modes, bimodality_score, density_gap, value_gap_ratio,
        value_gap_minor_mass, th,
        grid_empty=_grid_empty_levels(peaks_hist[2] if peaks_hist else None))
    spec_margin_low = (mean - lsl) / stdev if (lsl is not None and stdev) else None
    spec_margin_high = (usl - mean) / stdev if (usl is not None and stdev) else None
    nearest_spec_side = None
    if spec_margin_low is not None and spec_margin_high is not None:
        nearest_spec_side = "LOW" if spec_margin_low < spec_margin_high else "HIGH"

    # limit/rail 3종은 **같은 isclose 두 개**로 전부 나온다 — 종전에는 4번 돌렸다
    # (limit 이 lo|hi 를 자기 것으로 또 계산). 1회로 합친다(2026-08-19, 값 동일).
    rail_hits = _memo("rail_hits", lambda: (
        (None if lsl is None else np.isclose(v, lsl),
         None if usl is None else np.isclose(v, usl))))
    lo_hit, hi_hit = rail_hits
    limit_hit_ratio = _memo("limit_hit_ratio", lambda: (
        float(np.mean(lo_hit | hi_hit))
        if (lo_hit is not None and hi_hit is not None) else None))
    # 레일 포화를 **상·하단으로 갈라** 보여 준다 — 조치가 다르다(하단 레일은 trim 하한
    # 부족, 상단은 상한 부족). 판정은 종전대로 limit_hit_ratio 합계(code_edge_hit).
    rail_low_ratio = _memo("rail_low_ratio", lambda: (
        float(np.mean(lo_hit)) if lo_hit is not None else None))
    rail_high_ratio = _memo("rail_high_ratio", lambda: (
        float(np.mean(hi_hit)) if hi_hit is not None else None))

    # fail_mask 는 case 단위(bin 마다 다름)라 item 메모가 아니라 여기서 1회 변환해
    # 세 소비자가 공유한다 — 종전에는 파이썬 bool 리스트를 3번 다시 ndarray 로 만들었다
    # (2026-08-19, 값 동일 — np.asarray 는 이미 bool ndarray 면 그대로 돌려준다).
    fm = np.asarray(case_ctx.get("fail_mask") or [], dtype=bool)
    spatial = _spatial_features(case_ctx, th,
                                _memo("spatial_geom",
                                      lambda: _spatial_geometry(case_ctx),
                                      store=geom_store),
                                fm_in=fm)
    site_cpk_delta = _site_cpk_delta(case_ctx)
    code_edge_hit = limit_hit_ratio if case_ctx.get("value_type") == "CODE" else None
    fail_mad_min, fail_pass_gap_sigma, fail_robust_z_max = _fail_outlier_features(
        v, fm, median, mad, z=modified_z)
    fail_body_jump_ratio = _fail_body_jump_ratio(
        v, fm, median, mad, th, z=modified_z)

    if is_pf:
        spread_norm = skewness = kurtosis = skewness_moment = None
        outlier_ratio = modality = bimodality_score = density_gap = cdf_gap = None
        spec_margin_low = spec_margin_high = nearest_spec_side = limit_hit_ratio = None
        n_modes = modality_v2 = None
        value_gap_ratio = value_gap_minor_mass = None
        fail_mad_min = fail_pass_gap_sigma = fail_robust_z_max = None
        fail_body_jump_ratio = None
        tail_mass_3s = rail_low_ratio = rail_high_ratio = None

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
        "fail_mad_min": fail_mad_min, "fail_pass_gap_sigma": fail_pass_gap_sigma,
        "fail_body_jump_ratio": fail_body_jump_ratio,
        "fail_robust_z_max": fail_robust_z_max, "tail_mass_3s": tail_mass_3s,
        "rail_low_ratio": rail_low_ratio, "rail_high_ratio": rail_high_ratio,
    }

"""eval_analyzer 디버깅용 합성 web_report 테스트 데이터 생성기 (L0~L6 트레이스 / signature 검증).

무엇을 만드나
  - 7-meta honeyform parquet 여러 개(= source = wafer) + manifest.json.
    그대로 `POST /pe/report/upload_webreport` 로 올리면 web_report 세션이 되고,
    `/pe/eval` 트레이스(L0~L6)와 Issue Table 의 Signature/AI Comment 컬럼을 바로 볼 수 있다.
  - 정답표 `answer_key.csv` — item 마다 "어떤 signature 를 몇 단계 세기로 겨냥했는지" +
    생성 시점 실측 지표값 + 임계값.
  - 검증 결과 `verify.csv` — 실제 엔진(web_report.ai_comment) 이 발화시킨 signature 와
    정답표의 차이. 데이터가 의도대로 만들어졌는지 스스로 증명한다.

설계 요약 (자세한 배경은 같은 폴더 README.md)
  - test item 500개, **전부 fail item** (FAILTNO == 그 item 의 TNO 인 chip 이 1개 이상).
    서버 기본값 WEB_REPORT_EVAL_FAIL_ONLY=1 에서 전부 평가 대상이 되도록.
  - signature 별 **5단계 세기**: 1=정상범위(미발화) / 2=임계값 살짝 초과 / 3=더 초과 /
    4=크게 초과 / 5=심각. "세기" 의 기준은 그 룰의 when_metric 첫 지표와 임계값의 거리다.
  - 단독 세트(21 signature × 5단계) + 동시발화 조합 세트(2~3개 × 5단계) + 축 변형
    (value_type / bin / TRIM) + 경계 스윕 + 정상군(오탐 검사).
  - **chip 1행의 FAILTNO 는 하나뿐**이라 한 source 안에서 item 들의 fail 행은 서로 겹칠 수
    없다. 그래서 fail 예산(행 수)에 맞춰 item 을 source 로 bin-packing 한다 —
    source 개수는 데이터가 정한다(수율 룰 항목은 혼자 한 source 를 거의 다 쓴다).

실행 (repo 루트에서)
    server\\.venv\\Scripts\\python.exe tools\\eval_testdata\\make_eval_testdata.py
    ... --no-verify                 # 검증 생략(생성만)
    ... --upload http://127.0.0.1:8080   # 생성 후 그 서버에 업로드
    ... --items 120 --radius 20     # 작게 스모크

pytest 미사용 (tools/ 관례 — 단독 실행).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT), str(_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, decode_split_honeyform_parquet,
    encode_honeyform_parquet)

try:                                              # 한국어 Windows 콘솔(cp949) 깨짐 방지
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LEVELS = [1, 2, 3, 4, 5]
UNKNOWN_ID = "UNKNOWN"          # 엔진 미분류 표식 — 발화 목록에 섞여 오지만 오탐이 아니다
# 2026-08-12 레벨 재편: 강도를 한 단계씩 내려 **L1/L2 사이가 fail 경계**가 되게 했다.
# (구 L3=발화 시작 → 신 L2, 구 L4 → 신 L3, 구 L5 → 신 L4, 신 L5 는 구 L5 보다 강하게)
LEVEL_KO = {1: "정상(fail 0)", 2: "발화 시작(임계값 소폭 초과)", 3: "초과",
            4: "크게 초과", 5: "심각"}
FIRE_FROM = 2

# 레벨별 fail chip 수 — **fail 은 limit 위반으로만 만든다**(값이 spec 안인데 FAILTNO 만
# 찍힌 항목이 없도록). L1 은 fail 0 이라 서버 기본값 WEB_REPORT_EVAL_FAIL_ONLY=1 에서
# 평가 대상에서 아예 빠진다(= 미발화가 구조적으로 보장된다).
FAIL_N = [0, 6, 15, 30, 60]
# fail chip 을 limit 밖으로 얼마나 밀지(spec 폭 대비) — 레벨이 오를수록 더 멀리 나간다.
# ⚠ 이 값이 커지면 밀려난 chip 이 정상 몸통과 **끊겨** 보여(gap↑) 겨냥하지 않은 OUTLIER 가
# 동반발화한다. 산포가 좁은 시나리오일수록 먼저 닿는다 — limit 을 겨우 넘는 정도로 둔다
# (2026-08-13 축소: 공간 룰 항목의 gap 이 1.5σ 를 넘어 OUTLIER 가 붙던 문제).
FAIL_MARGIN = [0.002, 0.004, 0.006, 0.008, 0.010]

# fail 유형별 bin — Map Analysis 에서 **유형을 색으로 구분**하기 위한 배정.
# 1=Pass. 18(defective)·31(abnormal)은 bin_taxonomy.yaml 예약값이라 피한다.
# 11(WIDE_DISTRIBUTION)·13(OUTLIER_WARN)·14(SPEC_TOO_TIGHT)·32(WAFER_GRADIENT)는
# 그 룰들이 2026-08-13 에 삭제되며 결번. 12 = OUTLIER(구 SEVERE_OUTLIER 자리).
SIG_BIN = {
    "OUTLIER": 12,
    "LOW_CPK": 15, "MEAN_SHIFT": 16, "BIDIR_TAIL": 17,
    "HEAVY_TAIL": 19, "BIMODALITY": 20, "TAIL_RISK": 21, "CONSTANT_VALUE": 22,
    "EQUIPMENT_SUSPECT": 23, "CODE_RAIL": 24, "MISSING_LIMIT": 25,
    "LOW_SAMPLE_UNCERTAIN": 26, "EDGE_FAIL": 27, "CENTER_FAIL": 28, "RING_FAIL": 29,
    "CLUSTER_FAIL": 30, "GROSS_FAIL": 33, "E1_FAIL": 34, "SPOT_CLUSTER": 35,
}
NORMAL_BIN = 2                  # 겨냥한 룰이 없는 항목(정상군·경계군)의 fail bin
RANDOM_BIN_BASE = 40            # 관찰군(random) — Map 에서 40번대 색으로 한눈에 갈린다
UNKNOWN_BIN_BASE = 50           # 미분류군(unknown) — 50번대. 관찰군과 색을 가른다

# 기준 limit — 폭(WIDTH)=1.0 이라 spread_norm·center_bias 계산이 그대로 눈에 보인다.
LSL, USL = 0.5, 1.5
WIDTH = USL - LSL
CENTER = (USL + LSL) / 2

# rules/thresholds.yaml default (표시·목표 산정용 — 정본은 yaml, 여기 값은 참고치다).
TH = {
    "cpk_warn": 1.33, "cpk_bad": 1.00,
    "outlier_ratio_warn": 0.02, "outlier_ratio_bad": 0.05, "outlier_sigma": 4.5,
    "spread_norm_warn": 0.18, "skew_warn": 1.0, "spec_margin_warn": 1.0,
    "edge_fail_ratio_warn": 2.0, "n_min": 20, "bimodality_warn": 0.555,
    "edge_region_pct": 0.8, "center_region_pct": 0.3, "quadrant_imbalance_warn": 2.5,
    "mean_shift_warn": 0.30, "site_cpk_delta_warn": 0.5, "gross_yield_bad": 0.5,
    "code_edge_hit_warn": 0.05, "kurtosis_warn": 8.0, "spatial_fail_count_min": 10,
    "severe_outlier_count_min": 5, "ring_fail_ratio_warn": 2.0,
    # OUTLIER 는 거리 AND 끊김(2026-08-13), 공간 룰 4종은 점유율로 판정한다.
    "outlier_fail_mad_min": 4.0, "outlier_fail_gap_sigma_min": 1.5,
    "region_fail_share_min": 0.95,
    "gradient_norm_warn": 0.3, "subpop_n_min": 50, "subpop_outlier_ratio_max": 0.03,
    "subpop_density_gap_warn": 0.3, "subpop_density_gap_strong": 0.5,
    "subpop_value_gap_warn": 0.3, "subpop_minor_mass_min": 0.05,
}

# 현 룰셋에서 **구조적으로 발화할 수 없는** signature 와 이유 (README §4 와 같은 내용).
# TAIL_RISK 는 지표가 모멘트 왜도로 바뀌어(2026-08-12), RING_FAIL 은 판정이 점유율로
# 바뀌어 발화 가능해졌다 — 목록에서 뺐다.
UNFIRABLE = {
    "EQUIPMENT_SUSPECT": "raw_df 경로는 site 를 항상 None 으로 채운다(ingest._ingest_raw_df) "
                         "→ site_cpk_delta 가 영구 None",
}


# ──────────────────────────────────────────────────────────────────────────────
# 지표 복제 (엔진 pipeline/metrics.py · features.py 공식과 동일)
#   목표값을 겨냥해 데이터를 만들고, 정답표에 실측치를 적기 위한 것이다.
#   엔진과 갈라지면 --verify 가 잡는다(실제 발화는 엔진이 낸 값이 정본).
# ──────────────────────────────────────────────────────────────────────────────

def m_robust_sigma(v):
    med = float(np.median(v))
    return 1.4826 * float(np.median(np.abs(v - med)))


def m_spread_norm(v, lsl=LSL, usl=USL):
    if lsl is None or usl is None or usl == lsl:
        return None
    return m_robust_sigma(v) / (usl - lsl)


def m_outlier_ratio(v):
    med = float(np.median(v))
    mad = float(np.median(np.abs(v - med)))
    if mad != 0:
        z = 0.6745 * (v - med) / mad
    else:
        mean_ad = float(np.mean(np.abs(v - med)))
        if mean_ad <= 0:
            return 0.0
        z = (v - med) / (1.253314 * mean_ad)
    return float(np.mean(np.abs(z) > TH["outlier_sigma"]))


def m_fail_outlier(values, fail_idx):
    """OUTLIER 판정 지표 3종 — 엔진 `features._fail_outlier_features` 와 같은 식.

    반환 (fail_mad_min, fail_pass_gap_sigma, fail_robust_z_max).
    `values` 는 **전체 길이** 배열(NaN 포함), `fail_idx` 는 그 배열의 인덱스다.
    엔진은 파싱된(=유한) 값만 보므로 여기서도 유한값 기준으로 맞춘다.
    """
    v = np.asarray(values, dtype=float)
    ok = np.isfinite(v)
    if not ok.any() or len(fail_idx) == 0:
        return None, None, None
    fm = np.zeros(v.size, dtype=bool)
    fm[np.asarray(fail_idx, dtype=int)] = True
    fm, vv = fm[ok], v[ok]
    if not fm.any():
        return None, None, None
    med = float(np.median(vv))
    mad = float(np.median(np.abs(vv - med)))
    if mad != 0:
        z = 0.6745 * (vv - med) / mad
    else:
        mean_ad = float(np.mean(np.abs(vv - med)))
        if mean_ad <= 0:
            return None, None, None
        z = (vv - med) / (1.253314 * mean_ad)
    dist = np.abs(z)
    df, dp = dist[fm], dist[~fm]
    gap = float(df.min() - dp.max()) if dp.size else None
    return float(df.min()) / 0.6745, gap, float(df.max())


def m_tail_mass(v):
    """꼬리 질량 — 중심에서 3 robust σ 밖 비율 (엔진 features.tail_mass_3s)."""
    med = float(np.median(v))
    mad = float(np.median(np.abs(v - med)))
    if mad != 0:
        z = 0.6745 * (v - med) / mad
    else:
        mean_ad = float(np.mean(np.abs(v - med)))
        if mean_ad <= 0:
            return None
        z = (v - med) / (1.253314 * mean_ad)
    return float(np.mean(np.abs(z) > 3.0))


def m_fail_spread(fail_idx, x, y, rmax):
    """fail 좌표 몰림도 — 무게중심 기준 RMS 거리 / 웨이퍼 반경 (엔진 fail_spread_norm)."""
    if len(fail_idx) < 2 or not rmax:
        return None
    fx, fy = x[fail_idx], y[fail_idx]
    cx, cy = float(fx.mean()), float(fy.mean())
    return float(np.sqrt(np.mean((fx - cx) ** 2 + (fy - cy) ** 2)) / rmax)


def m_std(v):
    return float(np.std(v, ddof=1)) if v.size > 1 else 0.0


def m_cpk(v, lsl=LSL, usl=USL):
    s = m_std(v)
    if s == 0 or lsl is None or usl is None:
        return None
    mean = float(v.mean())
    return min((mean - lsl) / (3 * s), (usl - mean) / (3 * s))


def m_center_bias(v, lsl=LSL, usl=USL):
    if lsl is None or usl is None or m_std(v) == 0:
        return None
    return ((usl + lsl) - 2 * float(v.mean())) / (usl - lsl)


def m_kurtosis(v):
    s = m_std(v)
    if s == 0:
        return None
    return float(np.mean(((v - v.mean()) / s) ** 4) - 3)


def m_skew_np(v):
    s = m_std(v)
    if s == 0:
        return None
    return (float(v.mean()) - float(np.median(v))) / s


def m_skew_moment(v):
    """3차 모멘트 왜도 — 엔진 features.skewness_moment 와 같은 식(상한 없음)."""
    sd = m_std(v)
    if sd == 0:
        return None
    return float(np.mean(((v - v.mean()) / sd) ** 3))


def m_bimodality(v):
    s = m_std(v)
    if v.size < 4 or s == 0:
        return None
    z = (v - v.mean()) / s
    skew, kurt = float(np.mean(z ** 3)), float(np.mean(z ** 4))
    return None if kurt == 0 else (skew ** 2 + 1) / kurt


def m_limit_hit(v, lsl=LSL, usl=USL):
    if lsl is None or usl is None:
        return None
    return float(np.mean(np.isclose(v, lsl) | np.isclose(v, usl)))


def _grid_step_gen(v):
    """양자화 격자 간격 — 엔진 `features._grid_step` 과 **같은 규칙**(복제)."""
    uniq, cnt = np.unique(v, return_counts=True)
    floor = max(2, 0.005 * v.size)
    heavy = uniq[cnt >= floor]
    if heavy.size < 3 or float(cnt[cnt >= floor].sum()) < 0.8 * v.size:
        return None
    diffs = np.diff(heavy)
    step = float(np.median(diffs))
    if step <= 0:
        return None
    k = np.round(diffs / step)
    if np.any(k < 1) or np.any(np.abs(diffs - k * step) > 0.25 * step):
        return None
    return step


def _hist_peaks(v):
    """엔진 `features._histogram_peaks` 복제 — 양자화 격자 정렬 포함.

    ⚠ 엔진과 갈라지면 정답표(answer_key)의 density_gap·modality 가 실제 판정과 어긋난다.
    """
    if v.size < 8:
        return None
    bins = min(20, max(5, v.size // 5))
    step = _grid_step_gen(v)
    if step:
        idx = np.round((v - v.min()) / step).astype(int)
        counts = np.bincount(idx)
        m = max(1, int(np.ceil(counts.size / bins)))
        if m > 1:
            pad = (-counts.size) % m
            counts = np.concatenate([counts, np.zeros(pad, dtype=counts.dtype)])
            counts = counts.reshape(-1, m).sum(axis=1)
        hist = counts
    else:
        hist, _ = np.histogram(v, bins=bins)
    return [i for i in range(1, len(hist) - 1)
            if hist[i] > hist[i - 1] and hist[i] > hist[i + 1]], hist


def m_density_gap(v):
    ph = _hist_peaks(v)
    if ph is None:
        return None
    peaks, hist = ph
    if len(peaks) < 2:
        return 0.0
    p1, p2 = sorted(peaks, key=lambda i: -hist[i])[:2]
    lo, hi = sorted([p1, p2])
    valley, peak_max = int(hist[lo:hi + 1].min()), int(hist.max())
    return 0.0 if peak_max == 0 else float((min(int(hist[p1]), int(hist[p2])) - valley) / peak_max)


def m_n_modes(v):
    ph = _hist_peaks(v)
    return None if ph is None else max(len(ph[0]), 1)


def m_value_gap(v):
    uniq = np.unique(v)
    if uniq.size < 2:
        return None, None
    rng = float(uniq[-1] - uniq[0])
    if rng <= 0:
        return None, None
    diffs = np.diff(uniq)
    i = int(np.argmax(diffs))
    below = float(np.mean(v <= uniq[i]))
    return float(diffs[i] / rng), float(min(below, 1.0 - below))


def m_modality_v2(v):
    """features._classify_modality_v2 복제 — bimodal|multimodal|separated|None."""
    n = v.size
    if n < TH["subpop_n_min"]:
        return None
    if m_outlier_ratio(v) >= TH["subpop_outlier_ratio_max"]:
        return None
    n_modes, dg, bc = m_n_modes(v), m_density_gap(v), m_bimodality(v)
    vg, mass = m_value_gap(v)
    if n_modes is not None and n_modes >= 3 and dg is not None and dg >= TH["subpop_density_gap_warn"]:
        return "multimodal"
    if (n_modes == 2 and bc is not None and bc >= TH["bimodality_warn"]
            and dg is not None and dg >= TH["subpop_density_gap_warn"]):
        return "bimodal"
    if (dg is not None and dg >= TH["subpop_density_gap_strong"]
            and vg is not None and vg >= TH["subpop_value_gap_warn"]
            and mass is not None and mass >= TH["subpop_minor_mass_min"]):
        return "separated"
    return None


def tune(fn, target, lo, hi, iters=36):
    """단조 증가 fn 의 fn(x)=target 을 이분법으로 찾는다 (분포 파라미터 역산용)."""
    for _ in range(iters):
        mid = (lo + hi) / 2
        if fn(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ──────────────────────────────────────────────────────────────────────────────
# 웨이퍼 좌표 — 엔진은 반경을 **원점(0,0) 기준**으로 재므로 좌표를 중심 정렬한다.
# ──────────────────────────────────────────────────────────────────────────────

def _edge_of_line(key, val):
    """key 로 묶은 각 줄에서 val 이 양끝인 die 마스크 — 엔진 `features._edge_of_line` 과 동일.

    4-이웃(x±1) 조회로 최외곽을 찾으면 **die pitch 가 1 이라는 가정**이 생겨 좌표 간격이
    2 인 map 에서 모든 die 를 최외곽으로 오판한다. 간격을 가정하지 않는 정의를 쓴다.
    """
    uniq, inv = np.unique(key, return_inverse=True)
    lo = np.full(uniq.size, np.inf)
    hi = np.full(uniq.size, -np.inf)
    np.minimum.at(lo, inv, val)
    np.maximum.at(hi, inv, val)
    return (val == lo[inv]) | (val == hi[inv])


def build_wafer(radius: int):
    """반경 radius 안의 정수격자 die 목록 → (x, y, rnorm, e1) 배열.

    e1 = **최외곽 1 chip line** — 각 행의 좌·우 끝 + 각 열의 위·아래 끝 die.
    엔진(`features._e1_mask`)과 **같은 정의**를 써야 E1_FAIL 이 겨냥한 대로 뜬다.
    """
    xs, ys = [], []
    for y in range(-radius, radius + 1):
        for x in range(-radius, radius + 1):
            if x * x + y * y <= radius * radius:
                xs.append(x)
                ys.append(y)
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    r = np.sqrt(x ** 2 + y ** 2)
    e1 = _edge_of_line(y, x) | _edge_of_line(x, y)
    return x, y, r / r.max(), e1


# ──────────────────────────────────────────────────────────────────────────────
# 측정값 합성
# ──────────────────────────────────────────────────────────────────────────────

def synth_values(plan: dict, n: int, rng: np.random.Generator, lsl, usl):
    """value plan(dict) → 길이 n 측정값 배열 (NaN 포함 가능).

    plan 키: kind(normal|constant|mixture|uniform) / mean / sigma / value /
      comps[(w, mean, sigma)...] / spike{p,z} / rail{p} / quantize / n_valid.
    spike 의 offset 은 z × sigma 로 잡는다 — 산포가 넓은 항목에서도 robust σ 기준
    outlier 컷(4.5σ) 밖에 확실히 떨어지게 하기 위해서다.
    """
    kind = plan.get("kind", "normal")
    sigma = float(plan.get("sigma", 0.02))
    mean = float(plan.get("mean", CENTER))

    if kind == "constant":
        v = np.full(n, float(plan["value"]), dtype=float)
    elif kind == "uniform":
        v = rng.uniform(plan["low"], plan["high"], n)
    elif kind == "mixture":
        comps = plan["comps"]
        weights = np.asarray([c[0] for c in comps], dtype=float)
        weights = weights / weights.sum()
        pick = rng.choice(len(comps), size=n, p=weights)
        v = np.empty(n, dtype=float)
        for i, (_, mu, sd) in enumerate(comps):
            m = pick == i
            v[m] = rng.normal(mu, sd, int(m.sum()))
    else:
        v = rng.normal(mean, sigma, n)

    spike = plan.get("spike")
    if spike and spike.get("p", 0) > 0:
        k = max(1, int(round(spike["p"] * n)))
        idx = rng.choice(n, size=k, replace=False)
        # 기준 산포 — mixture 면 성분 폭 중 최대. spike 는 robust σ 의 4.5배 밖에 있어야
        # outlier 로 잡히므로 "현재 분포의 폭" 을 기준으로 삼는다.
        base = sigma if kind != "mixture" else max(c[2] for c in plan["comps"])
        off = spike.get("z", 8.0) * (base if base > 0 else 0.02)
        if plan.get("bounded") and lsl is not None and usl is not None:
            # bounded 는 spec 밖 값을 재추출하므로, 튀는 값이 spec 밖으로 나가면 spike 자체가
            # 사라진다(실측: kurtosis 목표 2.5 → 0.83). spec 안쪽 끝으로 제한한다.
            off = min(off, 0.47 * (usl - lsl))
        sign = np.where(rng.random(k) < spike.get("neg_p", 0.0), -1.0, 1.0)
        v[idx] = v[idx] + sign * off

    rail = plan.get("rail")
    if rail and rail.get("p", 0) > 0 and lsl is not None and usl is not None:
        k = max(1, int(round(rail["p"] * n)))
        idx = rng.choice(n, size=k, replace=False)
        half = k // 2
        v[idx[:half]] = float(lsl)
        v[idx[half:]] = float(usl)

    if plan.get("quantize"):
        step = float(plan["quantize"])
        v = np.round(v / step) * step

    if plan.get("bounded") and lsl is not None and usl is not None:
        # **spec 밖으로 새는 꼬리를 spec 안으로 재추출**한다. "spec 밖 = fail" 이 불변
        # 법칙이라, 꼬리가 새면 그만큼 fail 이 되어 한 웨이퍼가 순식간에 찬다.
        # 이렇게 가둬 두면 fail 은 레벨 사다리(FAIL_N)만큼만 정확히 만들 수 있고,
        # 지표(spread_norm·cpk 등)는 역산(with_tune)이 실측값 기준으로 맞춰 준다.
        out = np.isfinite(v) & ((v < lsl) | (v > usl))
        if out.any():
            v[out] = rng.uniform(lsl, usl, int(out.sum()))

    n_valid = plan.get("n_valid")
    if n_valid is not None and n_valid < n:
        keep = np.sort(rng.choice(n, size=int(n_valid), replace=False))
        out = np.full(n, np.nan)
        out[keep] = v[keep]
        v = out
    return v


# ──────────────────────────────────────────────────────────────────────────────
# fail 행 배치 (공간 패턴)
# ──────────────────────────────────────────────────────────────────────────────

def _strata(free, rnorm, xn, yn):
    """사분면 4 × 반경대 3 = 12 층 — 임의 배치를 층화추출해 공간 지표를 1.0 근처로 유지한다.

    그냥 무작위로 12개를 뽑으면 사분면 fail 률 편중(quadrant_imbalance)이 우연히 1.0 을
    넘어 CLUSTER_FAIL 이 오발화한다(소표본 편중). 공간 룰을 겨냥하지 않은 항목은
    "공간적으로 균일" 해야 겨냥한 룰만 뜬다.
    """
    band = np.digitize(rnorm[free], [TH["center_region_pct"], TH["edge_region_pct"]])
    quad = (xn[free] >= 0).astype(int) * 2 + (yn[free] >= 0).astype(int)
    key = band * 4 + quad
    return [free[key == k] for k in range(12)]


def pick_fail_rows(pattern: str, count: int, free: np.ndarray, rnorm, xn, yn,
                   share: float, rng: np.random.Generator, e1=None):
    """free(아직 다른 item 이 안 쓴 행 인덱스)에서 패턴에 맞는 fail 행 count 개 선택.

    `e1` 은 최외곽 1열 마스크(build_wafer). 영역 패턴에서 `share` 는 **그 영역에 둘 fail 의
    몫**(0~1)이고, 그것이 곧 엔진의 판정 지표(`*_fail_share`)다 — 종전 밀도 배수 시절에는
    영역 면적으로 나눠 몫을 역산해야 했지만 이제 환산이 필요 없다.
    """
    count = int(min(count, free.size))
    if count <= 0:
        return np.asarray([], dtype=int)

    if pattern == "random":
        picked = []
        strata = _strata(free, rnorm, xn, yn)
        sizes = np.asarray([s.size for s in strata], dtype=float)
        if sizes.sum() == 0:
            return np.asarray([], dtype=int)
        quota = np.floor(count * sizes / sizes.sum()).astype(int)
        for i in np.argsort(-sizes)[:int(count - quota.sum())]:
            quota[i] += 1
        for pool, k in zip(strata, quota):
            k = int(min(k, pool.size))
            if k:
                picked.append(rng.choice(pool, size=k, replace=False))
        return (np.concatenate(picked) if picked else np.asarray([], dtype=int)).astype(int)

    def _from(mask, k):
        pool = free[mask[free]]
        k = int(min(k, pool.size))
        return rng.choice(pool, size=k, replace=False) if k > 0 else np.asarray([], dtype=int)

    e1_mask = np.zeros_like(rnorm, dtype=bool) if e1 is None else e1
    # 엔진과 같은 정의: EDGE/RING 은 E1 을 뺀 영역이다(features._spatial_features).
    edge = (rnorm >= TH["edge_region_pct"]) & (~e1_mask)
    center = rnorm <= TH["center_region_pct"]
    ring = (rnorm > TH["center_region_pct"]) & (rnorm < TH["edge_region_pct"]) & (~e1_mask)

    if pattern == "spot":
        # 국부 뭉침 — 중심을 **x축 위(사분면 경계)** 에 두어 CLUSTER_FAIL 의 약점 자리를
        # 그대로 재현한다. share 는 blob 반경 / 웨이퍼 반경.
        r = max(0.02, float(share))
        cx, cy = 0.55, 0.0
        d2 = (xn - cx) ** 2 + (yn - cy) ** 2
        pool = free[d2[free] <= r * r]
        if pool.size < count:                      # 반경이 좁아 모자라면 가까운 순으로 채운다
            pool = free[np.argsort(d2[free])][:count]
        k = int(min(count, pool.size))
        return rng.choice(pool, size=k, replace=False).astype(int) if k else \
            np.asarray([], dtype=int)

    if pattern in ("edge", "center", "ring", "quadrant", "e1"):
        if pattern == "e1":
            mask = e1_mask
        elif pattern == "edge":
            mask = edge
        elif pattern == "center":
            mask = center
        elif pattern == "ring":
            mask = ring
        else:
            mask = (xn >= 0) & (yn >= 0)
        k_in = int(round(share * count))
        chosen = _from(mask, k_in)
        rest = count - chosen.size
        if rest > 0:
            other = np.zeros_like(mask)
            other[:] = ~mask
            other_pick = _from(other, rest)
            chosen = np.concatenate([chosen, other_pick])
        return chosen.astype(int)

    if pattern in ("grad_r", "grad_x"):
        # 엔진의 gradient 는 좌표를 8구간으로 나눈 **구간별 fail 률의 회귀 기울기**다.
        # 확률 추출로는 남은 행 사정에 따라 기울기가 크게 흔들리므로(실측 0.35→0.27),
        # 구간마다 필요한 개수를 직접 계산해 뽑아 목표 기울기를 정확히 만든다.
        coord = rnorm if pattern == "grad_r" else xn
        edges = np.linspace(coord.min(), coord.max(), 9)
        chosen = []
        for i in range(8):
            lo_e, hi_e = edges[i], edges[i + 1]
            in_bin = (coord >= lo_e) & (coord <= hi_e if i == 7 else coord < hi_e)
            pool = free[in_bin[free]]
            if pool.size == 0:
                continue
            center_c = (lo_e + hi_e) / 2
            rate = share * (center_c if pattern == "grad_r" else center_c + 1.0)
            k = int(min(pool.size, round(np.clip(rate, 0.0, 1.0) * int(in_bin.sum()))))
            if k:
                chosen.append(rng.choice(pool, size=k, replace=False))
        return (np.concatenate(chosen) if chosen else np.asarray([], dtype=int)).astype(int)

    return rng.choice(free, size=count, replace=False).astype(int)


# ──────────────────────────────────────────────────────────────────────────────
# item 스펙 카탈로그
# ──────────────────────────────────────────────────────────────────────────────

def spec(name, sig, level, *, values, fails=None, unit="V", lsl=LSL, usl=USL, bin_=None,
         metric="", target=None, group="", note="", fail_values="auto", expect=None):
    """test item 1건.

    - `expect`: 겨냥한 룰이 'fire'(L3~L7) 인지 'not_fire'(L1~L2) 인지.
    - fail chip 수는 레벨 사다리(FAIL_N)를 따르고, **fail chip 은 항상 limit 밖**이다
      (`fail_values="as_is"` 인 예외만 제외 — limit 이 없거나 상수인 항목).
    - 이름에 `(FAIL)` 을 붙이는 기준도 레벨이다 — L3 부터가 "값이 죽어서 fail 난 항목".
    - bin 은 겨냥한 signature 별 고유값(SIG_BIN) — Map Analysis 에서 유형을 색으로 가른다.
    """
    sig = list(sig)
    fires = level >= FIRE_FROM
    if fires and "(FAIL)" not in name:
        name = f"{name}(FAIL)"
    if bin_ is None:
        # bin = 유형 × 10 + 레벨 (예: 123 = OUTLIER(12) L3). Map Analysis 에서
        # 십의 자리 이상으로 유형이, 일의 자리로 세기가 갈려 색이 다양하게 나온다.
        base = SIG_BIN.get(sig[0]) if sig else None
        bin_ = base * 10 + level if (base and fires) else NORMAL_BIN
    return {
        "name": name, "intent": sig, "level": level, "group": group,
        "expect": expect or ("fire" if fires else "not_fire"),
        "unit": unit, "lsl": lsl, "usl": usl, "bin": bin_,
        "values": values,
        "fails": fails or {"pattern": "random", "count": FAIL_N[level - 1]},
        "metric": metric, "target": target, "note": note, "fail_values": fail_values,
    }


def with_tune(plan, apply, metric_fn, target, lo, hi, ensure=None):
    """생성 시점(실제 행 수·난수)에서 파라미터를 역산할 항목 — 표본 크기에 따라
    kurtosis·왜도·density_gap 이 목표에서 벗어나는 것을 막는다.

    ensure: 역산 뒤에도 만족해야 하는 조건(callable(values)->bool). 만족할 때까지
    파라미터를 키운다 — SUBPOP 처럼 목표 지표(density_gap) 를 맞춰도 다른 조건
    (bimodality_score ≥ 임계)에서 걸려 발화하지 못하는 경우가 있기 때문이다.
    """
    # 재료를 조합하면 역산이 2개 이상 붙는다(예: 산포 목표 + kurtosis 목표) — 하나만
    # 남기면 뒤 재료가 앞 재료의 역산을 지워 목표를 벗어난다. 순서대로 모두 적용한다.
    plan.setdefault("tune", []).append(
        {"apply": apply, "metric": metric_fn, "target": target, "lo": lo, "hi": hi,
         "ensure": ensure})
    return plan


# ── 레벨별 목표치 (단독 세트) ────────────────────────────────────────────────
# 전부 **L1 = 정상(미발화) / L2 = 임계 소폭 초과(발화 시작) / L5 = 심각** 사다리다.
# 통합 OUTLIER — 판정은 **거리 AND 끊김** 두 축이다(2026-08-13).
#   거리: fail_mad_min ≥ 4  (중심에 가장 가까운 fail 의 MAD 배수)
#   끊김: fail_pass_gap_sigma ≥ 1.5  (마지막 pass ↔ 첫 fail 빈 구간, robust σ)
# spike 를 spec 밖에 두면 두 축이 함께 커지므로 사다리는 거리(MAD 배수)로 매기고 gap 은
# 따라오게 둔다 — 실측 gap 은 answer_key/verify 에 기록된다.
# ⚠ 정규 몸통에서는 두 축이 **양의 상관**이라 낮은 MAD 배수를 만들 수 없다: 몸통 최대
# pass 거리가 ≈3.85σ 로 고정이라 gap≥1.5 를 만족하려면 fail 이 최소 5.35σ(≈8 MAD) 밖이어야
# 한다. 그래서 단독 세트는 8 부터 시작하고, **임계 4 앞뒤 경계는 균등분포 몸통**으로 만든다
# (균등분포는 pass 최대 거리가 1.35σ 뿐이라 4 MAD 에서도 gap 이 선다 — 실데이터의
# FLAT 항목이 정확히 그 구조였다). 경계 스윕은 BOUNDARY 참조.
OUTLIER_MAD = [8.0, 16.0, 22.0, 32.0, 50.0]                # fail_mad_min (warn 4)
# spike 비율은 레벨별 fail 수(FAIL_N)와 맞춘다 — spike 자체가 spec 밖이라 그대로 fail 이
# 되고, fail 은 chip 을 배타적으로 쓰므로 비율이 크면 한 item 이 웨이퍼 예산을 다 먹는다.
# (반경 40 ≈ 5,025 chip 기준으로 6/15/30/60개 = FAIL_N 과 같은 눈금)
OUTLIER_P = [0.0000, 0.0012, 0.0030, 0.0060, 0.0120]
LOWCPK_T = [1.80, 1.25, 0.95, 0.75, 0.62]                  # cpk (bounded 분포 하한 0.58)
MEANSHIFT_T = [0.15, 0.36, 0.55, 0.80, 0.92]               # |center_bias| (warn 0.30)
BIDIR_T = [1.60, 0.90, 0.60, 0.35, 0.20]                   # min spec margin (warn 1.0, 작을수록 나쁨)
# HEAVY_TAIL 은 kurtosis(warn 10) **AND 꼬리 질량 1~5%** 다(2026-08-13). 연속 꼬리
# (scale mixture)로 만들면 두 지표가 함께 오르므로 사다리는 kurtosis 로 매기고 질량은
# 따라오게 둔다 — 실측 질량은 answer_key/verify 에 기록된다.
KURT_T = [2.0, 12.0, 15.0, 19.0, 25.0]                     # excess kurtosis (warn 10.0)
RAIL_T = [0.010, 0.070, 0.150, 0.300, 0.450]               # limit_hit_ratio (warn 0.05)
# 공간 4종은 **점유율**(전체 fail 중 그 영역 몫, warn 0.95)로 바뀌었다 — 종전 밀도 배수는
# 영역마다 상한이 달라 사다리를 공유할 수 없었고 ring 은 아예 도달 불가였다.
REGION_SHARE_T = [0.50, 0.96, 0.98, 0.99, 1.00]            # *_fail_share (warn 0.95)
CLUSTER_T = [0.5, 2.7, 3.0, 3.4, 3.8]                      # quadrant_imbalance (warn 2.5, 상한 4)
# SPOT_CLUSTER — fail 을 반경 r 의 원 안에 몰아넣는다. 지표(fail_spread_norm)는 무게중심
# 기준 RMS 거리/웨이퍼반경 이고 균일 원판이면 ≈ r/(√2·R) 이라, 웨이퍼 반경 대비 blob
# 반경 비율로 사다리를 매긴다(warn 0.25 → blob 반경 ≈ 웨이퍼의 35%).
SPOT_R_T = [0.60, 0.30, 0.20, 0.12, 0.06]                  # blob 반경 / 웨이퍼 반경
# 공간 룰 항목의 몸통 σ — 겨냥한 것은 **위치 편중**이므로 값 쪽 룰이 붙으면 안 된다.
# 0.10 이 두 요구의 접점이다:
#   · 더 좁으면 밀려난 fail 이 몸통과 끊겨 보여(gap = (0.5+margin)/σ − 3.85 ≥ 1.5) OUTLIER
#   · 더 넓으면 cpk 가 1.33 아래로 내려가 LOW_CPK (fail 이 stdev 를 밀어올린다 —
#     σ 0.12·fail 1.2% 면 실효 stdev 0.132 → cpk 1.26 으로 실제 발화했다)
# σ 0.10 → gap 1.17 · 실효 stdev 0.114 → cpk 1.46. 둘 다 안전 구간.
SPATIAL_SIGMA = 0.10
YIELD_T = [0.97, 0.46, 0.28, 0.08, 0.03]                   # yield (bad 0.5)
NSAMPLE_T = [400, 19, 12, 8, 4]                            # n_dut (n_min 20)
SITEDELTA_T = [0.1, 0.6, 1.5, 3.0, 4.5]                    # site_cpk_delta (warn 0.5) — 미발화
# TAIL_RISK 는 2026-08-12 에 지표가 **모멘트 왜도**로 바뀌었다(비모수 왜도는 상한 1.0 이라
# 임계 1.0 을 넘을 수 없었다). warn 1.0 기준 L1 은 미달, L2~L5 는 초과.
SKEW_T = [0.30, 1.30, 2.20, 3.50, 5.00]                    # |모멘트 왜도| (warn 1.0)
# BIMODALITY(구 SUBPOP_GAP) 세기는 **모드 간 분리폭 / 성분 σ** 으로 매긴다. density_gap 을
# 목표로 잡으면 약한 분리에서 bimodality_score 가 임계 미달이라 사다리가 무너진다.
# 성분 σ 를 레벨마다 좁히는 이유: 분리폭(절대값)은 **모드가 spec 안에 있어야** 하므로
# 0.4 근처가 천장이다(넘으면 bounded 재추출이 봉우리를 뭉갠다). 폭을 좁혀 같은 절대
# 분리에서 σ 대비 배수를 올린다. L5 는 3봉(다봉) — 인접 모드 간격이라 천장이 더 낮다.
SUBPOP_SEP_SD = [0.0, 5.0, 6.5, 8.0, 9.5]                  # 모드 간 거리 / 성분 σ (L5=3봉)
SUBPOP_SD = [0.12, 0.12, 0.12, 0.09, 0.040]                # 성분 σ
# 상수값 항목 — L2 부터는 **spec 밖 상수**(그래서 fail 이 난다). 2진수로 정확히 표현되는
# 값만 쓴다(1.4 같은 값은 표본표준편차가 2e-16 으로 떠 stdev<=0 이 성립하지 않는다).
CONST_V = [None, 1.5625, 2.0, 3.0, 5.0]


def _quadrant_share(imbalance):
    """quadrant_imbalance 목표 → 1사분면에 몰아줄 fail 비율 s. v=(16s-4)/3."""
    return min(1.0, max(0.25, (3 * imbalance + 4) / 16))


def _spot_plan(lv):
    """SPOT_CLUSTER — 값은 평범하고 **위치만** 뭉치게 한다(공간 룰 공용 σ)."""
    return {"kind": "normal", "mean": CENTER, "sigma": SPATIAL_SIGMA, "bounded": True}


def _spike_plan(p, base_sigma=0.02):
    plan = {"kind": "normal", "mean": CENTER, "sigma": base_sigma, "bounded": True}
    if p > 0:
        plan["spike"] = {"p": p, "z": 10.0, "neg_p": 0.5}
    return plan


def _outlier_plan(lv, base_sigma=0.05):
    """통합 OUTLIER — spike 를 **spec 밖**에 두어 그 자체가 fail 이 되게 한다.

    `bounded` 를 걸지 않는 것이 요점이다. spike 를 spec 안에 가두면(bounded) 그 chip 은
    fail 이 아니고 대신 limit 바로 밖으로 밀린 평범한 chip 이 fail 이 되는데, 그건
    몸통과 이어져 있어(gap≈0) OUTLIER 가 아니라 그냥 공정능력 문제로 읽힌다.
    spike 오프셋은 `z × base_sigma` 이고 modified z ≈ z 이므로 MAD 배수는 z/0.6745 다 —
    목표 MAD 배수에서 역산해 z 를 잡는다.
    """
    mad_target = OUTLIER_MAD[lv]
    if OUTLIER_P[lv] <= 0:                         # L1 = 정상(미발화)
        return {"kind": "normal", "mean": CENTER, "sigma": base_sigma, "bounded": True}
    return {"kind": "normal", "mean": CENTER, "sigma": base_sigma,
            "spike": {"p": OUTLIER_P[lv], "z": mad_target * 0.6745, "neg_p": 0.5}}


def _lowcpk_plan(lv):
    return {"kind": "normal", "mean": CENTER, "sigma": WIDTH / (6 * LOWCPK_T[lv]),
            "bounded": True}


def _meanshift_plan(lv, sigma=0.05):
    plan = {"kind": "normal", "mean": CENTER - MEANSHIFT_T[lv] * WIDTH / 2, "sigma": sigma,
            "bounded": True}
    # bounded 는 spec 밖 값을 spec 안에서 재추출하므로 치우침이 조금 되돌아온다
    # (실측 목표 0.30 → 0.2998 로 임계 바로 아래). 실측 center_bias 로 역산한다.
    return with_tune(plan, lambda pl, m: pl.update(mean=m),
                     lambda v: -(m_center_bias(v, LSL, USL) or 0.0), -MEANSHIFT_T[lv],
                     CENTER - WIDTH * 0.49, CENTER)


def _bidir_plan(lv):
    return {"kind": "normal", "mean": CENTER, "sigma": (WIDTH / 2) / BIDIR_T[lv]}


def _heavytail_plan(lv):
    """spike 비율은 outlier warn(2%) 아래로 고정하고 z 를 튜닝해 kurtosis 목표를 맞춘다."""
    return _kurt_plan(KURT_T[lv])


def _kurt_plan(target, p=0.03, sigma=0.05):
    """HEAVY_TAIL — **연속 꼬리**(scale mixture, 2026-08-13).

    같은 중심의 넓은 성분을 소수 섞어 꼬리가 몸통에서 limit 까지 **이어지게** 한다.
    `bounded` 를 걸지 않아 꼬리 끝이 자연히 spec 을 넘고 그 chip 이 fail 이 된다 →
    마지막 pass 와 첫 fail 이 붙어 **gap≈0** → OUTLIER(gap≥1.5)와 구조적으로 갈린다.
    종전 고정 오프셋 spike 방식은 spike 가 전부 같은 거리에 뭉쳐 gap 이 1.4σ 까지
    올라가 판정선 1.5 에 아슬아슬했다(HEAVY_TAIL_L5 가 OUTLIER 로 넘어갈 뻔한 자리).
    """
    def apply(pl, k):
        pl["comps"] = [(1 - p, CENTER, sigma), (p, CENTER, k * sigma)]

    plan = {"kind": "mixture", "mean": CENTER, "comps": [(1, CENTER, sigma)]}
    apply(plan, 3.0)
    return with_tune(plan, apply, m_kurtosis, target, 1.0, 12.0)


def _coderail_plan(lv, p=None):
    """CODE 항목 — 레일(0/63) 값이 outlier 컷(4.5 robust σ) 안에 들도록 산포를 잡는다.

    폭이 좁으면 레일 값이 통째로 몸통과 끊겨 보여 OUTLIER 가 대신 뜬다.
    """
    return {"kind": "normal", "mean": 31.5, "sigma": 8.0, "quantize": 1.0, "bounded": True,
            "rail": {"p": RAIL_T[lv] if p is None else p}}


def _subpop_plan(lv, sd=None, center=None):
    """이봉/다봉 — density_gap 목표에 맞춰 모드 간 분리폭을 생성 시점에 역산한다.

    좌우 대칭·동일 가중이라 median 이 골 한가운데 떨어져 MAD 가 커진다 → 소수 무리가
    outlier 로 잡혀 SUBPOP 게이트(outlier_ratio<3%)에 걸리는 것을 피한다.
    (가중을 한쪽으로 기울이면 소수 무리가 그대로 outlier 가 되어 발화가 막힌다.)
    """
    # 성분 폭 — 좁게 잡으면 두 무리가 전부 spec 안이라 fail 이 안 생기고, 불변 법칙
    # ("spec 밖=fail")을 맞추려 chip 을 밀어내면 그 극단값이 kurtosis 를 올려
    # bimodality_score 를 임계 아래로 끌어내린다(실측 L3~L5 미발화). 폭을 키워
    # **바깥 무리의 꼬리가 자연스럽게 spec 을 넘게** 한다.
    sd = sd or SUBPOP_SD[lv]
    mid = CENTER if center is None else center
    if lv == 0:
        return {"kind": "normal", "mean": mid, "sigma": sd}

    def apply(pl, sep):
        # 중심은 **적용 시점의 plan["mean"]** 을 따른다 — 뒤에 온 MEAN_SHIFT 가 옮겨 둔
        # 중심을, 생성 시점 재역산(resolve_tuning)이 원래 자리로 되돌리지 않게.
        c = pl.get("mean", mid)
        if lv == len(LEVELS) - 1:                  # 마지막 레벨 = 다봉(3봉)
            pl["comps"] = [(1, c - sep, sd), (1, c, sd), (1, c + sep, sd)]
        else:                                      # 이봉
            pl["comps"] = [(1, c - sep / 2, sd), (1, c + sep / 2, sd)]

    def ok(v):  # noqa: D401
        """발화 여유를 둔다 — bimodality_score 가 임계(0.555) 를 간신히 넘으면 parquet
        왕복 반올림·표본 잡음만으로 엔진 쪽에서 미달이 될 수 있다(실측 0.5678 미발화)."""
        if m_modality_v2(v) is None:
            return False
        bc, modes = m_bimodality(v), m_n_modes(v)
        return (modes or 0) >= 3 or (bc or 0) >= TH["bimodality_warn"] * 1.12

    plan = {"kind": "mixture", "mean": mid, "comps": [(1, mid, sd)], "bounded": True}
    apply(plan, SUBPOP_SEP_SD[lv] * sd)
    # 분리폭은 위에서 정해졌다. 역산은 안전망 — 발화 단계(L3+)에서만 ensure 를 건다.
    # L1·L2 에 걸면 "발화하면 안 되는 단계"의 분리폭을 키워 버린다.
    return with_tune(plan, apply, m_density_gap, 0.0, SUBPOP_SEP_SD[lv] * sd,
                     SUBPOP_SEP_SD[lv] * sd,
                     ensure=ok if lv + 1 >= FIRE_FROM else None)


def _skew_plan(lv):
    """모멘트 왜도 목표 — 한쪽으로 긴 꼬리.

    **bounded 를 걸지 않는다**: TAIL_RISK 의 두 번째 조건 `spec_margin_min < 1σ` 는 분포
    폭이 spec 여유보다 커야 성립해서, 꼬리가 spec 을 넘을 수밖에 없다(= fail 이 대량으로
    생긴다). 그래서 이 항목은 CSV 1장 세트에서 빠지고 전체 세트에서만 다룬다.
    """
    p, sd = 0.25, 0.30

    def apply(pl, d):
        pl["comps"] = [(1 - p, CENTER - 0.15, sd), (p, CENTER - 0.15 + d, sd)]

    plan = {"kind": "mixture", "comps": [(1, CENTER - 0.15, sd)]}
    return with_tune(plan, apply, m_skew_moment, SKEW_T[lv], 0.0, 4.0)


def _constant_plan(lv):
    """상수값 항목. **2진수로 정확히 표현되는 값만** 쓴다 — 1.4 같은 값은 numpy 표본
    표준편차가 2e-16 으로 떠서 `stdev <= 0` 조건이 성립하지 않는다(README §4 부동소수 함정).
    """
    if CONST_V[lv] is None:                        # L1·L2 = 미세 잡음(상수 아님 → 미발화)
        return {"kind": "normal", "mean": CENTER, "sigma": 0.02, "bounded": True}
    return {"kind": "constant", "value": CONST_V[lv]}


def _site_plan(lv):
    """DUT 마다 산포가 다른 항목 — site 가 전달되면 EQUIPMENT_SUSPECT 를 낼 데이터."""
    d = SITEDELTA_T[lv]
    return {"kind": "mixture", "bounded": True,
            "comps": [(1, CENTER, 0.02), (1, CENTER, 0.02 + d * 0.02),
                      (1, CENTER, 0.02 + d * 0.04), (1, CENTER, 0.02 + d * 0.06)]}


def _spatial_n(lv):
    """공간 룰의 fail 수 — 레벨 사다리를 따르되 최소 24개는 준다.

    공간 지표는 **비율**이라 개수 자체는 판정에 무관하지만, 너무 적으면 영역 배분이
    정수 반올림에 휘둘려 목표 비율이 안 나온다(가드 spatial_fail_count_min=5).
    """
    return max(24, FAIL_N[lv])


SINGLE_BUILDERS = {
    "OUTLIER": lambda lv: (_outlier_plan(lv), None, "fail_mad_min", OUTLIER_MAD[lv], {}),
    "LOW_CPK": lambda lv: (_lowcpk_plan(lv), None, "cpk", LOWCPK_T[lv], {}),
    "MEAN_SHIFT": lambda lv: (_meanshift_plan(lv), None, "center_bias", MEANSHIFT_T[lv], {}),
    "BIDIR_TAIL": lambda lv: (_bidir_plan(lv), None, "spec_margin_min", BIDIR_T[lv], {}),
    # fail 은 **자연 꼬리로만** 만든다 — `_kurt_plan` 이 bounded 를 안 걸어 꼬리가 스스로
    # spec 을 넘는데, 거기에 레벨 사다리(FAIL_N)만큼 `_push_out_of_spec` 로 보충하면
    # 중간 꼬리의 chip 을 limit 밖으로 **옮겨서** 그 자리에 구멍이 난다. 그 구멍이 곧
    # `fail_body_jump_ratio` 라 HEAVY_TAIL 겨냥이 OUTLIER 로 넘어갔다(v10 L4 0.487).
    "HEAVY_TAIL": lambda lv: (_heavytail_plan(lv), {"mode": "natural", "pattern": "random"},
                              "kurtosis", KURT_T[lv], {}),
    "BIMODALITY": lambda lv: (_subpop_plan(lv), None, "density_gap", None,
                              {"note": f"모드 분리 {SUBPOP_SEP_SD[lv]}σ (성분 σ {SUBPOP_SD[lv]})"
                                       + (" · 3봉(다봉)" if lv == len(LEVELS) - 1 else "")}),
    "TAIL_RISK": lambda lv: (_skew_plan(lv), None, "skewness_moment", SKEW_T[lv], {}),
    # L2 부터 상수 자체를 spec 밖에 둔다 — fail 원인이 값으로 설명된다(전 chip 이 spec
    # 밖이지만 FAILTNO 는 레벨 사다리 개수만 찍는다). L1 은 미세잡음이라 fail chip 만
    # spec 밖으로 밀리고, 그 결과 OUTLIER 가 동반발화한다(현실적인 산발 이상).
    "CONSTANT_VALUE": lambda lv: (_constant_plan(lv), None, "stdev", 0.0, {}),
    "EQUIPMENT_SUSPECT": lambda lv: (_site_plan(lv), None, "site_cpk_delta", SITEDELTA_T[lv], {}),
    "CODE_RAIL": lambda lv: (_coderail_plan(lv), None, "code_edge_hit", RAIL_T[lv],
                             {"unit": "CODE", "lsl": 0.0, "usl": 63.0}),
    "MISSING_LIMIT": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.05, "bounded": True}, None, "limit_missing",
        [0, 1, 1, 1, 1][lv],
        dict({}, **[{}, {"usl": None}, {"lsl": None}, {"lsl": None, "usl": None},
                    {"lsl": None, "usl": None, "unit": ""}][lv],
             )),   # limit 이 없는 단계는 밀 대상이 없다(_push_out_of_spec 가 알아서 건너뛴다)
    "LOW_SAMPLE_UNCERTAIN": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.05, "n_valid": NSAMPLE_T[lv],
         "bounded": True},
        None, "n_dut", NSAMPLE_T[lv], {}),
    # 공간 4종 — share 가 그대로 "전체 fail 중 그 영역 몫"이라 면적 환산이 필요 없다
    # (배수 지표 시절에는 영역 면적으로 나눠 몫을 역산해야 했다).
    "E1_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": SPATIAL_SIGMA, "bounded": True},
        {"pattern": "e1", "count": _spatial_n(lv), "share": REGION_SHARE_T[lv]},
        "e1_fail_share", REGION_SHARE_T[lv], {}),
    "EDGE_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": SPATIAL_SIGMA, "bounded": True},
        {"pattern": "edge", "count": _spatial_n(lv), "share": REGION_SHARE_T[lv]},
        "edge_fail_share", REGION_SHARE_T[lv], {}),
    "CENTER_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": SPATIAL_SIGMA, "bounded": True},
        {"pattern": "center", "count": _spatial_n(lv), "share": REGION_SHARE_T[lv]},
        "center_fail_share", REGION_SHARE_T[lv], {}),
    "RING_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": SPATIAL_SIGMA, "bounded": True},
        {"pattern": "ring", "count": _spatial_n(lv), "share": REGION_SHARE_T[lv]},
        "ring_fail_share", REGION_SHARE_T[lv], {}),
    "CLUSTER_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": SPATIAL_SIGMA, "bounded": True},
        {"pattern": "quadrant", "count": _spatial_n(lv), "share": _quadrant_share(CLUSTER_T[lv])},
        "quadrant_imbalance", CLUSTER_T[lv], {}),
    # SPOT_CLUSTER — 위치·모양 무관 국부 뭉침. **사분면 경계에 일부러 걸친다**(중심을 x축
    # 위에 둔다) — CLUSTER_FAIL 이 놓치던 자리를 이 룰이 잡는지 데이터로 보이기 위해서다.
    "SPOT_CLUSTER": lambda lv: (
        _spot_plan(lv),
        {"pattern": "spot", "count": _spatial_n(lv), "share": SPOT_R_T[lv]},
        "fail_spread_norm", None,
        {"note": f"blob 반경 = 웨이퍼의 {SPOT_R_T[lv]:.0%} · 사분면 경계(x축) 위"}),
    "GROSS_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.05, "bounded": True},
        {"pattern": "random", "fraction": 1 - YIELD_T[lv]}, "yield", YIELD_T[lv], {}),
}


def single_specs():
    """단독 세트 — signature 21종 × 7단계."""
    out = []
    for sig, build in SINGLE_BUILDERS.items():
        for lv in LEVELS:
            values, fails, metric, target, over = build(lv - 1)
            kw = {k: v for k, v in over.items()}
            note = kw.pop("note", "") or UNFIRABLE.get(sig, "")
            out.append(spec(f"{sig}_L{lv}", [sig], lv,
                            values=values, fails=fails, metric=metric, target=target,
                            group="single", note=note, **kw))
    return out


# ── 조합(동시발화) 세트 ──────────────────────────────────────────────────────
# 재료 = (값 plan 변형, fail plan 변형, 메타 변형). 레벨은 재료 전체에 함께 적용된다.

def _ing(name, lv, plan, fails, meta):
    """재료 1개를 plan/fails/meta 에 얹는다 — **자기 손잡이만** 건드린다.

    조합에서 뒤 재료가 앞 재료의 결과를 지우면 둘 다 미발화가 된다(예전엔 HEAVY_TAIL 이
    mean 을 CENTER 로 되돌려 MEAN_SHIFT 를 통째로 지웠다). 그래서 산포·중심·spike 를
    각각 독립 키로만 쓰고, 값 범위는 **그 시점 meta 의 limit** 에서 유도한다
    (CODE 처럼 스케일이 다른 항목과 섞여도 목표 지표가 성립한다).
    """
    i = lv - 1
    lo = LSL if meta.get("lsl") is None else meta["lsl"]
    hi = USL if meta.get("usl") is None else meta["usl"]
    width, center = hi - lo, (hi + lo) / 2

    def _shift(delta):
        plan["mean"] = plan.get("mean", center) + delta
        if plan.get("comps"):
            plan["comps"] = [(w, mu + delta, sd) for w, mu, sd in plan["comps"]]

    if name == "OUTLIER":
        if OUTLIER_P[i] > 0:
            # spike 는 **spec 밖**에 둔다 — 그 chip 이 곧 fail 이고, 몸통과 끊겨 있어야
            # 한다. bounded 를 지워야(spec 안 재추출 금지) spike 가 살아남는다.
            plan["sigma"] = min(plan.get("sigma", 0.05), 0.05)
            plan.pop("bounded", None)
            plan["spike"] = {"p": OUTLIER_P[i], "z": OUTLIER_MAD[i] * 0.6745, "neg_p": 0.5}
    elif name == "MEANSHIFT":
        _shift(center - MEANSHIFT_T[i] * width / 2 - plan.get("mean", center))
        if not plan.get("comps"):                  # 단봉일 때만 역산(위 _meanshift_plan 과 동일 이유)
            with_tune(plan, lambda pl, m: pl.update(mean=m),
                      lambda v: -(m_center_bias(v, lo, hi) or 0.0), -MEANSHIFT_T[i],
                      center - width * 0.49, center)
    elif name == "HEAVYTAIL":
        # 연속 꼬리(scale mixture) — `_kurt_plan` 과 같은 구조를 얹는다. 기준 산포는
        # 좁게 유지해야 꼬리가 limit 을 넘는다.
        base = _kurt_plan(KURT_T[i], sigma=min(plan.get("sigma", 0.05), 0.06))
        plan["comps"] = base["comps"]
        plan["kind"] = "mixture"
        plan.pop("bounded", None)
        plan.setdefault("tune", []).extend(base["tune"])   # 앞 재료의 역산을 지우지 않는다
    elif name == "SUBPOP":
        # 앞선 재료가 정한 산포를 모드 폭으로, 현재 중심을 모드 중심으로 물려받는다.
        # 모드 폭이 좁으면 fail chip 을 limit 밖으로 밀 때 그 값이 극단 outlier 가 되어
        # bimodality_score 를 임계 아래로 끌어내린다(조합에서 SUBPOP 미발화 30건).
        plan.update(_subpop_plan(i, sd=max(plan.get("sigma", 0.05), 0.10),
                                 center=plan.get("mean", center)))
    elif name == "CODERAIL":
        meta.update(unit="CODE", lsl=0.0, usl=63.0)
        plan.update(_coderail_plan(i))
    elif name == "CONSTANT":
        plan.clear()
        plan.update(_constant_plan(i))
    elif name == "MISSLIMIT":
        meta.update(lsl=None if i >= 2 else LSL, usl=None if i >= 1 else USL)
    elif name in ("E1", "EDGE", "CENTER", "RING"):
        fails.update(pattern=name.lower(), count=_spatial_n(i), share=REGION_SHARE_T[i])
    elif name == "CLUSTER":
        fails.update(pattern="quadrant", count=80, share=_quadrant_share(CLUSTER_T[i]))
    elif name == "SPOT":
        fails.update(pattern="spot", count=_spatial_n(i), share=SPOT_R_T[i])
    elif name == "GROSS":
        fails.update(pattern="random", fraction=1 - YIELD_T[i])
    else:
        raise KeyError(name)


ING_SIG = {"OUTLIER": "OUTLIER",
           "MEANSHIFT": "MEAN_SHIFT", "HEAVYTAIL": "HEAVY_TAIL", "SUBPOP": "BIMODALITY",
           "CODERAIL": "CODE_RAIL", "CONSTANT": "CONSTANT_VALUE",
           "MISSLIMIT": "MISSING_LIMIT", "EDGE": "EDGE_FAIL", "CENTER": "CENTER_FAIL",
           "RING": "RING_FAIL", "SPOT": "SPOT_CLUSTER",
           "CLUSTER": "CLUSTER_FAIL", "GROSS": "GROSS_FAIL",
           "E1": "E1_FAIL"}

# 재료 적용 순서 — 이름을 준 순서와 무관하게 이 순서로 얹는다. 순서에 따라 뒤 재료가 앞
# 재료의 결과를 지우면(예: 산포를 정한 뒤 mixture 로 갈아끼우면) 둘 다 미발화가 된다.
ING_ORDER = {"CONSTANT": 0, "CODERAIL": 1, "SUBPOP": 3,
             "MEANSHIFT": 4, "OUTLIER": 5, "HEAVYTAIL": 5, "MISSLIMIT": 6,
             "E1": 7, "EDGE": 7, "CENTER": 7, "RING": 7, "CLUSTER": 7, "SPOT": 7,
             "GROSS": 7}
# fail 행 배치는 item 당 하나만 고를 수 있다 (패턴이 서로 덮어쓴다).
FAIL_ING = {"E1", "EDGE", "CENTER", "RING", "CLUSTER", "SPOT", "GROSS"}

COMBOS = [
    ("MEANSHIFT", "OUTLIER"), ("SUBPOP", "E1"), ("MEANSHIFT", "HEAVYTAIL"),
    ("RING", "MEANSHIFT"), ("SPOT", "MEANSHIFT"),
    ("CLUSTER", "MEANSHIFT"), ("CODERAIL", "MEANSHIFT"),
    ("SUBPOP", "CENTER"), ("HEAVYTAIL", "CLUSTER"), ("MISSLIMIT", "CLUSTER"),
    ("CODERAIL", "CENTER"), ("SUBPOP", "SPOT"),
    ("MEANSHIFT", "HEAVYTAIL", "CLUSTER"), ("MEANSHIFT", "HEAVYTAIL", "RING"),
]



def combo_spec(names, lv, group="combo", seq=None):
    plan = {"kind": "normal", "mean": CENTER, "sigma": 0.03, "bounded": True}
    fails = {"pattern": "random", "count": FAIL_N[lv - 1]}
    meta = {"unit": "V", "lsl": LSL, "usl": USL, "bin_": None}
    for nm in sorted(names, key=lambda n: ING_ORDER[n]):
        _ing(nm, lv, plan, fails, meta)
    label = "-".join(names)
    name = f"MIX{len(names)}_{label}_L{lv}" + (f"_{seq:03d}" if seq is not None else "")
    return spec(name, [ING_SIG[n] for n in names], lv, values=plan, fails=fails,
                metric="(조합)", target=None, group=group, **meta)


def combo_specs():
    # 고정 조합이 INCOMPATIBLE 을 어기면 그 항목은 영원히 미발화로 남는다 — 즉시 잡는다.
    bad = [c for c in COMBOS if not _compatible(c)]
    assert not bad, f"동시에 성립할 수 없는 조합: {bad}"
    return [combo_spec(names, lv) for names in COMBOS for lv in LEVELS]


# ── 축 변형 세트 ─────────────────────────────────────────────────────────────
# UNIT 원문 → 엔진 value_type. 마지막 둘은 "표에 없는 단위" 경로 확인용 —
# 엔진 UNIT_TO_VALUE_TYPE 은 정확일치 표라 모르는 표기는 조용히 PF(무판정)로 떨어진다.
UNIT_AXIS = [("v", "V"), ("ma", "A"), ("khz", "Hz"), ("ohm", "Ohm"), ("ms", "Sec"),
             ("CODE", "CODE"), ("PCT", "PCT"), ("", "PF"), ("DEGC", "PF(오분류)")]
AXIS_SCENARIOS = ["OUTLIER", "MEANSHIFT", "HEAVYTAIL", "SUBPOP"]
BIN_AXIS = [3, 4, 5, 8, 18, 31]


def axis_specs():
    out = []
    # value_type 축 — 같은 시나리오를 UNIT 만 바꿔 룰 스코프(item_class)를 흔든다.
    for unit_raw, vt in UNIT_AXIS:
        for scen in AXIS_SCENARIOS:
            for lv in (2, 5):
                s = combo_spec((scen,), lv, group="unit_axis")
                # 이름에 "_CODE_" 가 들어가면 exclusions.yaml(item_contains)에 걸려
                # 평가 자체가 제외된다 → 구분자 없이 붙여 쓴다.
                s["name"] = f"UNIT{vt.replace('(오분류)', 'MISS')}_{scen}_L{lv}"
                s["unit"] = unit_raw
                s["note"] = f"value_type={vt}"
                if vt.startswith("PF"):
                    # PF(양불)는 L1/L2 가 측정 통계를 전부 None 으로 비운다 → 값 기반 룰은
                    # 어떤 세기여도 발화하지 않는다("무판정"). UNIT 오분류 진단용 항목.
                    s["expect"] = "not_fire"
                    s["note"] += " — 측정 통계 없음(무판정), 값 기반 룰 발화 불가"
                out.append(s)
    # bin 축 — bin_taxonomy severity_bias(PMIC 18/31)와 item_class bin 축 검증.
    for b in BIN_AXIS:
        for scen in ("OUTLIER", "MEANSHIFT", "SUBPOP"):
            for lv in (3, 5):
                s = combo_spec((scen,), lv, group="bin_axis")
                s["name"] = f"BIN{b}_{scen}_L{lv}"
                s["bin"] = b
                out.append(s)
    # TRIM 축 — item 명에 TRIM 이 들어가면 category_major=TRIM.
    for scen in AXIS_SCENARIOS:
        for lv in LEVELS:
            s = combo_spec((scen,), lv, group="trim_axis")
            s["name"] = f"TRIM_{scen}_L{lv}"
            out.append(s)
    return out


# ── 경계 스윕 (임계값 바로 앞뒤) ──────────────────────────────────────────────
BOUNDARY = [
    # 경계 항목은 **역산해서** 목표 지표를 정확히 맞춘다 — 표본 잡음으로 0.179 가 0.182 가
    # 되면 경계 검증이 아니라 그냥 발화 항목이 된다(실측으로 겪은 오발화 1건).
    # OUTLIER 는 **거리 AND 끊김** 두 축이다 — spike 를 spec 밖에 두면 둘이 함께 커지므로
    # 거리(MAD 배수) 축으로 임계 4 앞뒤를 훑는다(gap 은 실측으로 answer_key 에 남는다).
    # 균등분포 몸통 — 반폭 h 를 손잡이로 두면 `mad_min ≈ 1/h` 이고 gap 도 같이 움직인다
    # (limit 밖으로 밀린 fail 의 거리 0.5/0.741h, pass 최대 1.35σ). 정규 몸통으로는
    # 4 근처를 만들 수 없다(위 OUTLIER_MAD 주석).
    ("fail_mad_min", TH["outlier_fail_mad_min"], [3.0, 3.6, 3.9, 4.2, 5.0, 8.0],
     lambda t: {"kind": "uniform", "low": CENTER - 1.0 / t, "high": CENTER + 1.0 / t},
     "OUTLIER"),
    ("cpk_warn", TH["cpk_warn"], [1.45, 1.38, 1.34, 1.32, 1.25, 1.10],
     # 산포(sigma)를 손잡이로 두고 -cpk 를 목표로 잡는다 — sigma 가 커질수록 -cpk 가
     # 커지므로(단조 증가) 이분법이 성립한다.
     lambda t: with_tune({"kind": "normal", "mean": CENTER, "sigma": WIDTH / (6 * t)},
                         lambda pl, s: pl.update(sigma=s),
                         lambda v: -(m_cpk(v, LSL, USL) or 0), -t, WIDTH * 0.02, WIDTH),
     "LOW_CPK"),
    ("center_bias", TH["mean_shift_warn"], [0.26, 0.285, 0.298, 0.305, 0.33, 0.40],
     lambda t: {"kind": "normal", "mean": CENTER - t * WIDTH / 2, "sigma": 0.01}, "MEAN_SHIFT"),
    ("kurtosis", TH["kurtosis_warn"], [8.0, 9.2, 9.8, 10.3, 12.0, 16.0],
     _kurt_plan, "HEAVY_TAIL"),
    ("code_edge_hit", TH["code_edge_hit_warn"], [0.038, 0.045, 0.049, 0.052, 0.060, 0.080],
     lambda t: _coderail_plan(0, p=t), "CODE_RAIL"),
]


def boundary_specs():
    out = []
    for metric, th, points, build, sig in BOUNDARY:
        for i, t in enumerate(points, start=1):
            over = ({"unit": "CODE", "lsl": 0.0, "usl": 63.0}
                    if metric == "code_edge_hit" else {})
            # 임계값보다 나쁜 쪽인지로 발화 기대를 정한다 (cpk 만 작을수록 나쁨).
            worse = t < th if metric == "cpk_warn" else t > th
            # "_CODE_" 를 피하려고 metric 이름의 밑줄을 뗀다 (exclusions.yaml item_contains).
            out.append(spec(f"EDGEC_{metric.replace('_', '')}_{i}", [sig], 2 if worse else 1,
                            values=build(t), metric=metric, target=t, group="boundary",
                            expect="fire" if worse else "not_fire",
                            note=f"임계값 {th} 대비 {'초과' if worse else '이내'}", **over))
    # 부동소수 함정 — 같은 '상수값' 인데 2진수 표현 가능 여부로 CONSTANT_VALUE 판정이 갈린다.
    for value, fire in ((1.25, True), (1.5, True), (1.4, False), (1.8, False)):
        out.append(spec(f"EDGEC_constant_{str(value).replace('.', 'p')}", ["CONSTANT_VALUE"],
                        2 if fire else 1, values={"kind": "constant", "value": value},
                        metric="stdev", target=0.0, group="boundary",
                        expect="fire" if fire else "not_fire",
                        note="2진수 정확표현" if fire else
                             "표본표준편차가 2e-16 으로 떠 stdev<=0 이 성립하지 않음"))
    return out


# ── 정상군 (오탐 검사) / 현실형 혼합 ─────────────────────────────────────────

def normal_specs(count, rng: random.Random):
    out = []
    for i in range(count):
        sigma = rng.uniform(0.02, 0.09)            # spread_norm 0.02~0.09, cpk 1.9~8
        unit_raw, _vt = UNIT_AXIS[i % len(UNIT_AXIS)]
        if unit_raw in ("", "VOLTS"):
            unit_raw = "v"
        out.append(spec(f"NORMAL_{i:03d}", [], 1,
                        values={"kind": "normal", "mean": CENTER + rng.uniform(-0.03, 0.03),
                                "sigma": sigma, "bounded": True},
                        fails={"pattern": "random", "count": rng.randint(3, 12)},
                        unit=unit_raw, bin_=NORMAL_BIN,
                        metric="spread_norm", target=round(sigma / WIDTH, 4),
                        group="normal", note="발화 0건 기대(오탐 검사)"))
    return out


# ── 관찰군(random) — 정답 없이 "엔진이 뭐라고 하는지" 만 보는 항목 ──────────────
# 겨냥한 룰이 없다(intent=[]). **유형을 정해 놓고 만들지 않는다**(2026-08-13 전면 재설계):
# "wide/bimodal/spiky…" 같은 목록에서 고르면 결국 우리가 아는 유형만 나와, 실데이터처럼
# 유형 사이 어딘가에 걸친 분포를 못 만든다. 그래서 **파라미터 공간에서 직접 뽑는다** —
# 모드 수·무게·중심·산포·왜도·양자화·spike·rail·절단·결측을 각각 확률로 굴려 섞는다.
# verify 는 이 그룹을 누락·오발화로 세지 않고 발화 분포만 따로 요약한다.
RANDOM_COUNT = 30               # 관찰군 기본 개수 (--random-items 로 조절)
# grad_r(반경 경사)은 뺐다 — fail 수를 "행 비율"로 정하는 패턴이라 item 하나가 웨이퍼
# 예산을 통째로 먹는다(실측 1,900 chip). 국부 뭉침은 "spot" 패턴이 담당한다.
RANDOM_REGIONS = [None, None, None, None, None,        # 45% 는 위치 편중 없음
                  "e1", "edge", "center", "ring", "quadrant", "spot"]
# unit → (lsl, usl) 생성 규칙. PF 로 떨어지는 표기(공란·DEGC)는 관찰 대상이 아니라 뺀다.
RANDOM_UNITS = ["v", "mv", "ma", "ua", "khz", "ohm", "ms", "CODE", "PCT", "LSB"]


def _random_limits(unit: str, rng: random.Random):
    """관찰군 limit — 단위에 어울리는 범위를 랜덤으로. (lsl, usl) 반환."""
    if unit == "CODE":
        return 0.0, float(rng.choice([31, 63, 255]))
    if unit == "PCT":
        return 0.0, 100.0
    center = rng.uniform(0.2, 5.0)
    width = math.exp(rng.uniform(math.log(0.05), math.log(4.0)))
    return center - width / 2, center + width / 2


def _sample_random_plan(rng: random.Random, lo: float, hi: float):
    """관찰군 1건의 값 plan — **유형 이름 없이** 파라미터를 굴려 만든다.

    반환 (plan, 요약문). 요약문은 note 에 적어 "무엇을 만들었는지" 는 남기되, 그것이
    정답은 아니다(엔진 판정과 대조해 보라는 관찰용 기록).
    """
    span = hi - lo
    c0 = (hi + lo) / 2
    k = rng.choice([1, 1, 1, 1, 2, 2, 3, 4])               # 단봉이 흔한 게 현실이다
    comps, notes = [], []
    for _ in range(k):
        w = rng.uniform(0.15, 1.0)
        # 모드 중심은 **중앙 근처**에서 뽑는다 — spec 폭 전체에 흩뿌리면 어떤 조합이든
        # 실효 산포가 커져 거의 모든 관찰 항목이 LOW_CPK 하나로 수렴한다(실측 96%).
        mu = c0 + rng.uniform(-0.22, 0.22) * span
        sd = span * math.exp(rng.uniform(math.log(0.015), math.log(0.13)))
        comps.append((w, mu, sd))
    notes.append(f"모드 {k}개")

    if k == 1:
        plan = {"kind": "normal", "mean": comps[0][1], "sigma": comps[0][2]}
    else:
        plan = {"kind": "mixture", "mean": c0, "comps": comps}

    if rng.random() < 0.25:                                # 양자화(계단형) — CODE 류 모사
        step = span / rng.choice([8, 16, 32, 64, 128])
        plan["quantize"] = step
        notes.append(f"양자화 {step:.4g}")
    if rng.random() < 0.30:                                # 튀는 값 — 거리·비율·방향 전부 랜덤
        plan["spike"] = {"p": math.exp(rng.uniform(math.log(0.0005), math.log(0.01))),
                         "z": rng.uniform(3.0, 30.0), "neg_p": rng.uniform(0.0, 1.0)}
        notes.append(f"spike z{plan['spike']['z']:.1f}")
    if rng.random() < 0.15:                                # limit 레일 포화
        plan["rail"] = {"p": rng.uniform(0.01, 0.30)}
        notes.append("rail")
    # 절단 — 안 걸면 꼬리가 spec 을 넘어 그만큼 fail 이 된다. 비절단이 잦으면 관찰 항목
    # 하나가 웨이퍼 fail 예산을 통째로 먹어(실측 748 chip) 뒤 항목이 밀려난다.
    if rng.random() < 0.80:
        plan["bounded"] = True
    else:
        notes.append("비절단")
    return plan, " · ".join(notes)


def random_specs(count, rng: random.Random, salt: str = ""):
    """관찰군 — 정답 기대 없음(expect='observe'). 분포·limit·fail 배치를 전부 굴린다."""
    out = []
    for i in range(count):
        unit = rng.choice(RANDOM_UNITS)
        lo, hi = _random_limits(unit, rng)
        plan, shape_note = _sample_random_plan(rng, lo, hi)
        region = rng.choice(RANDOM_REGIONS)
        n_fail = int(round(math.exp(rng.uniform(math.log(4), math.log(60)))))
        fails = {"pattern": region or "random", "count": n_fail}
        if region == "spot":
            # spot 은 share 가 blob 반경 비율이다 — 임계(0.25) 앞뒤가 고루 섞이게.
            fails["share"] = 0.05 + 0.55 * rng.betavariate(2, 2)
        elif region:
            # 점유율을 **일부러 애매하게** 흩는다 — 임계 0.95 를 넘는 것도, 못 넘는 것도 섞인다.
            # Beta(2,2) 를 [0.3,1.0] 으로 사상해 가운데가 흔하게.
            fails["share"] = 0.3 + 0.7 * rng.betavariate(2, 2)
        if rng.random() < 0.20:                            # 측정 결측(표본 부족 상황)
            # 행 수를 아직 모르므로 비율로 남긴다 — prepare_specs 가 n_valid 로 바꾼다.
            plan["n_valid_ratio"] = rng.uniform(0.3, 0.95)
        # 레벨은 fail 수만 정하는 눈금이 아니다(관찰군은 자기 count 를 쓴다) —
        # TNO/bin 번호가 겹치지 않게 흩는 용도로만 쓴다.
        lv = 2 + i % 4
        item = spec(f"RANDOM_{i:03d}", [], lv, values=plan, fails=fails, unit=unit,
                    lsl=lo, usl=hi, bin_=RANDOM_BIN_BASE + i % 10,
                    metric="(관찰)", target=None, group="random", expect="observe",
                    note=f"관찰용 무작위 — {shape_note} · fail {n_fail}개"
                         + (f" ({region} 편중 {fails['share']:.2f})" if region else " (위치 무관)"))
        item["seed_salt"] = salt        # --seed 를 바꾸면 관찰군만 새 표본이 된다
        out.append(item)
    return out


UNKNOWN_COUNT = 5               # 미분류군 기본 개수 (--unknown-items 로 조절)

# 미분류군이 쓰는 단위 — CODE/PCT 는 제외한다(CODE_RAIL·양자화가 끼어들어 "아무 룰도 안
# 걸림" 이 깨진다). limit 은 관찰군과 같은 `_random_limits` 로 굴린다.
UNKNOWN_UNITS = [u for u in RANDOM_UNITS if u not in ("CODE", "PCT")]


def unknown_specs(count, rng: random.Random, salt: str = ""):
    """미분류군 — **어떤 룰도 안 걸려 UNKNOWN 으로 떨어지는** 항목(2026-08-14 신설).

    왜 따로 만드나: 관찰군은 fail 을 `_push_out_of_spec` 로 limit 바로 밖에 몰아 만들어
    몸통과 fail 사이가 늘 비어 있다 — 값 축에서 구조적으로 outlier 모양이라 UNKNOWN 이
    나올 수 없었다(v9 실측: 관찰군 30개 중 23개가 OUTLIER 조건 충족). 그래서 fail 을
    `mode: "natural"`(분포가 스스로 넘긴 chip 만) 로 만든다.

    모양은 **중심이 같은 좁은 몸통 + 넓은 소수 성분** 하나다. 이 조합이라야 세 가지가
    동시에 성립한다 — 넓은 성분이 limit 을 넘겨 fail 을 10개 이상 만들고(자연 꼬리),
    몸통~limit 구간을 촘촘히 채워 끊김이 없고(OUTLIER 회피), 그러면서도 전체 σ 가 작아
    cpk 가 1.33 위에 남는다(LOW_CPK 회피). 단봉 정규분포로는 불가능하다 — cpk ≥ 1.33 이면
    limit 이 4σ 밖이라 자연 초과가 5025 chip 에 0.3 개꼴이다.

    ⚠ kurtosis 는 이 모양에서 10 을 넘지만 HEAVY_TAIL 은 뜨지 않는다 — 넓은 성분이
    `tail_mass_3s` 를 밴드 상한(0.05) 위로 밀어 올리기 때문이다. 그 밴드는 원래 "다봉이라
    몸통이 갈라진 경우" 를 빼려고 둔 것인데, 여기서는 꼬리가 두꺼운 정도가 밴드를 넘는다.

    파라미터는 관찰군처럼 **굴린다**(고정 형상을 손으로 박지 않는다). 다만 위 세 조건이
    서로 밀고 당기므로(꼬리를 키우면 σ 가 커져 cpk 가 떨어진다) **cpk 목표에서 역산**한다:
      · cpk 목표 U(1.55, 2.10) → 전체 σ = 반폭 / (3·cpk)     → LOW_CPK(1.33) 회피
      · 몸통 σ = 전체 σ × U(0.35, 0.50), 넓은 성분 w = U(0.10, 0.16)
        → 넓은 성분 σ 는 전체 σ 를 맞추도록 역산 → fail 20~50개(5025 chip 기준)
      · 중심 = 정중앙 ± 0.03·span            → MEAN_SHIFT(center_bias 0.30) 회피
      · 위치 편중 없음(region=None)          → 공간 룰 회피
    실제로 UNKNOWN 이 떴는지는 생성 직후 내장 verify(2패스)가 확인한다.
    """
    out = []
    for i in range(count):
        unit = rng.choice(UNKNOWN_UNITS)
        lo, hi = _random_limits(unit, rng)
        span = hi - lo
        c0 = (hi + lo) / 2 + rng.uniform(-0.03, 0.03) * span
        cpk_goal = rng.uniform(1.55, 2.10)
        sd_all = (span / 2) / (3 * cpk_goal)
        sd_body = sd_all * rng.uniform(0.35, 0.50)
        w_wide = rng.uniform(0.10, 0.16)
        # σ_all² = (1−w)·σ_body² + w·σ_wide² 를 σ_wide 에 대해 푼다.
        sd_wide = math.sqrt(max(sd_all ** 2 - (1 - w_wide) * sd_body ** 2, 1e-12) / w_wide)
        plan = {"kind": "mixture", "mean": c0,
                "comps": [(1.0 - w_wide, c0, sd_body), (w_wide, c0, sd_wide)]}
        # bounded 를 걸지 않는다 — 가두면 자연 꼬리가 사라져 fail 이 0 이 된다.
        item = spec(f"UNKNOWN_{i:03d}", [], 3, values=plan,
                    fails={"mode": "natural", "pattern": "random"},
                    unit=unit, lsl=lo, usl=hi, bin_=UNKNOWN_BIN_BASE + i,
                    metric="(미분류)", target=None, group="unknown", expect="observe",
                    note=f"미분류 겨냥 — cpk 목표 {cpk_goal:.2f} · 몸통 σ {sd_body:.4g} + "
                         f"넓은 성분 {w_wide:.0%}·σ {sd_wide:.4g} · 자연 꼬리 fail (위치 무관)")
        item["seed_salt"] = salt
        out.append(item)
    return out


MIX_POOL = ["OUTLIER", "MEANSHIFT", "HEAVYTAIL", "SUBPOP",
            "E1", "EDGE", "CENTER", "RING", "CLUSTER", "SPOT"]

# **동시에 성립할 수 없는 재료 쌍** — 룰 조건이 서로 배타적이라 둘 다 발화시킬 수 없다.
#   SUBPOP 은 outlier_ratio<3% 게이트가 있어 outlier·heavy tail 계열과 같이 못 간다.
INCOMPATIBLE = [{"SUBPOP", "OUTLIER"}, {"SUBPOP", "HEAVYTAIL"},
                # OUTLIER 의 spike(spec 밖, 몸통과 끊김)와 HEAVY_TAIL 의 연속 꼬리는 같은
                # 손잡이를 반대로 쓴다 — 끊기면 gap 이 서고 이어지면 안 선다.
                {"OUTLIER", "HEAVYTAIL"},
                # OUTLIER + 공간 룰: OUTLIER 의 spike 는 spec 밖이라 **위치와 무관하게** fail 이
                # 된다. 공간 룰은 "fail 의 95% 이상이 그 영역"(SPOT 은 좌표 몰림)이 조건이라,
                # 무작위 위치의 spike fail 이 섞이는 순간 깨진다.
                {"OUTLIER", "E1"}, {"OUTLIER", "EDGE"}, {"OUTLIER", "CENTER"},
                {"OUTLIER", "RING"}, {"OUTLIER", "CLUSTER"}, {"OUTLIER", "SPOT"},
                # 이봉 + 중심 치우침: 모드 재배치가 치우침을 상쇄한다.
                {"SUBPOP", "MEANSHIFT"},
                # 상수(전 chip spec 밖 = 전량 fail) + 공간 룰: 전부 fail 이면 특정 영역
                # 집중이라는 개념 자체가 성립하지 않는다(비율이 항상 1.0).
                {"CONSTANT", "EDGE"}, {"CONSTANT", "CENTER"}, {"CONSTANT", "CLUSTER"},
                {"CONSTANT", "GROSS"}, {"CONSTANT", "E1"},
                {"CONSTANT", "RING"}, {"CONSTANT", "SPOT"}]


def _compatible(names) -> bool:
    s = set(names)
    if len(s & FAIL_ING) > 1:                      # fail 배치 패턴은 item 당 하나
        return False
    return not any(pair <= s for pair in INCOMPATIBLE)


def mixed_specs(count, rng: random.Random):
    out = []
    for i in range(count):
        k = rng.choice([1, 2, 2, 3])
        for _ in range(30):
            names = tuple(rng.sample(MIX_POOL, k))
            if _compatible(names):
                break
        lv = rng.choice([3, 4, 5])
        # bin 은 spec() 이 겨냥 signature 로 이미 정했다(Map 에서 유형 색 구분).
        out.append(combo_spec(names, lv, group="mixed", seq=i))
    return out


def assign_tno(specs):
    """**TNO = 유형(SIG_BIN)×1000 + 레벨×100 + 순번** — fail item 을 TNO 만으로 구분한다.

    bin 규칙(유형×10+레벨)과 대칭이라 Issue Table·Map 에서 번호만 보고 불량 유형·세기를
    읽을 수 있고, source 를 넘어 **전역 유일**이다(종전엔 source 안 순번이라 다른 source 의
    다른 item 이 같은 TNO 를 가졌다). 겨냥한 룰이 없는 항목(정상군 등)은 유형 99.
    fail 귀속이 `FAILTNO == TNO` 비교라 값이 겹치면 fail 이 엉뚱한 item 에 붙는다.
    """
    seq: dict = {}
    for s in specs:
        # 관찰군은 유형 98 로 분리한다 — 정상군·경계군과 같은 99 를 쓰면 (유형,레벨) 당
        # 순번 99개 상한에 먼저 걸린다(assert dup==0).
        base = (98 if s.get("group") == "random"
                else 97 if s.get("group") == "unknown"
                else SIG_BIN.get(s["intent"][0], 99) if s["intent"] else 99)
        key = (base, s["level"])
        seq[key] = seq.get(key, 0) + 1
        s["tno"] = base * 1000 + s["level"] * 100 + seq[key]
    dup = len(specs) - len({s["tno"] for s in specs})
    assert dup == 0, f"TNO 중복 {dup}건 — 유형/레벨당 99개를 넘었는지 확인"
    return specs


def build_catalog(total: int, seed: int):
    rng = random.Random(seed)
    specs = (single_specs() + combo_specs() + axis_specs() + boundary_specs()
             + random_specs(RANDOM_COUNT, rng))
    fixed = len(specs)
    rest = max(0, total - fixed)
    n_normal = min(rest, max(0, int(round(rest * 0.4))))
    specs += normal_specs(n_normal, rng)
    specs += mixed_specs(rest - n_normal, rng)
    specs = specs[:total] if total < fixed else specs
    for i, s in enumerate(specs):                  # 이름 유일성 보장 + 제외규칙 회피
        s["name"] = f"{s['name']}_{i:03d}"
        assert "_CODE_" not in s["name"].upper(), f"exclusions.yaml 에 걸리는 이름: {s['name']}"
    return assign_tno(specs)


# ──────────────────────────────────────────────────────────────────────────────
# source 로 패킹 → honeyform DataFrame
# ──────────────────────────────────────────────────────────────────────────────

def _stable_seed(name: str, salt: str = "") -> int:
    """item 이름(+salt)으로 고정 seed — 배치(순서·source)가 달라져도 같은 측정값이 나온다.

    salt 는 **관찰군에만** 준다(`--seed` 값). 겨냥 세트는 salt 없이 두어 seed 를 바꿔도
    값이 그대로다 — 룰 회귀를 비교할 때 기준선이 흔들리면 안 되기 때문이다.
    """
    return int(hashlib.sha1(f"{name}{salt}".encode("utf-8")).hexdigest()[:8], 16)


def _pattern_count(s, n_rows):
    """공간·수율 패턴이 요구하는 fail 행 수(값과 무관하게 위치/비율로 정해지는 몫)."""
    f = s["fails"]
    if f.get("fraction"):
        return int(round(f["fraction"] * n_rows))
    if f.get("pattern") == "grad_r":
        return int(round(f.get("share", 0.3) * 0.67 * n_rows))
    if f.get("pattern") == "grad_x":
        return int(round(f.get("share", 0.3) * n_rows))
    return int(f.get("count", FAIL_N[s["level"] - 1]))


def prepare_specs(specs, n_rows):
    """item 별 측정값을 미리 만들고 **fail 원가(쓸 행 수)** 를 확정한다.

    fail 규칙(사용자 확정, 2026-08-12):
      · **spec 을 벗어난 chip 은 예외 없이 fail** — "limit 밖인데 pass" 를 만들지 않는다.
        그래서 fail 수는 분포가 정한다(산포가 넓을수록 자동으로 많아진다).
      · **L1·L2(정상 단계)는 fail 0** — spec 밖 값이 나오면 안쪽으로 당겨 없앤다.
        fail 이 0 이므로 서버 평가 범위(WEB_REPORT_EVAL_FAIL_ONLY=1)에서 아예 빠진다.
      · 값만으로 fail 이 부족한 항목(좁은 분포·공간 룰)은 레벨 사다리(FAIL_N)만큼 밀어 채운다.
    """
    for s in specs:
        lsl, usl = s["lsl"], s["usl"]
        seed = _stable_seed(s["name"], s.get("seed_salt", ""))
        # 관찰군은 행 수를 모른 채 결측을 비율로 선언한다 — 여기서 개수로 바꾼다.
        ratio = s["values"].pop("n_valid_ratio", None)
        if ratio is not None:
            s["values"]["n_valid"] = int(n_rows * ratio)
        plan = resolve_tuning(s["values"], n_rows, seed, lsl, usl)
        v = synth_values(plan, n_rows, np.random.default_rng(seed), lsl, usl)
        fires = s["level"] >= FIRE_FROM
        oos = np.zeros(n_rows, dtype=bool)
        if lsl is not None and usl is not None:
            oos = np.isfinite(v) & ((v < lsl) | (v > usl))
            if not fires and oos.any():
                span = usl - lsl                   # 정상 단계 — spec 안으로 당긴다
                v[oos] = np.where(v[oos] > usl, usl - 0.01 * span, lsl + 0.01 * span)
                oos[:] = False
        s["_values"] = v
        s["_oos"] = oos
        s["_seed"] = seed
        if not fires:
            cost = 0
        elif s["fails"].get("mode") == "natural":
            cost = int(oos.sum())              # 자연 초과분이 곧 fail (build_source_df 참조)
        elif s["fails"].get("pattern") in SPATIAL_PATTERNS or s["fails"].get("fraction"):
            cost = max(_pattern_count(s, n_rows), int(oos.sum()))
        else:
            # `fails["count"]` 를 존중한다 — 종전엔 비공간 항목에서 이 값을 무시하고
            # 레벨 사다리만 봐서, 관찰군의 "fail 16개" 선언이 실제로는 60개가 됐다.
            cost = max(int(s["fails"].get("count", FAIL_N[s["level"] - 1])), int(oos.sum()))
        s["_cost"] = int(min(cost, n_rows))
    return specs


def fail_budget(s, n_rows):
    return s.get("_cost", _pattern_count(s, n_rows))


SPATIAL_PATTERNS = {"e1", "edge", "center", "ring", "quadrant", "spot", "grad_r", "grad_x"}


def pack_sources(specs, n_rows, reserve=0.03):
    """fail 예산(=행 수)에 맞춰 item 을 source 로 나눈다. 큰 것부터 first-fit.

    제약 2개 — 둘 다 **공간 패턴이 망가지는 것**을 막기 위한 것이다. chip 1행의 FAILTNO 는
    하나뿐이라 먼저 배치된 item 이 쓴 행은 다음 item 이 못 쓴다. 수율 룰 항목처럼 행을
    대량으로 먹는 item 과 공간 룰 항목이 같은 source 에 있으면, 남은 행이 편중돼 있어
    edge/center/gradient 목표치가 나오지 않는다(실제로 gradient 가 0.35→0.08 로 떨어졌다).
      ① heavy(행의 15% 초과) item 은 source 당 1개.
      ② 공간 패턴 item 은 heavy item 과 같은 source 에 두지 않고, 그 source 의 사용률이
         25% 를 넘으면 새 source 로 보낸다.
    """
    cap = int(n_rows * (1 - reserve))
    heavy_line, spatial_line = 0.15 * n_rows, 0.25 * n_rows
    # 영역별 행 수 비중(반경 정규화 면적) — 중앙은 9% 뿐이라 CENTER_FAIL 항목 몇 개면
    # 중앙 행이 바닥나 뒤 항목의 center_fail_ratio 가 0 이 된다(실측 미발화 4건).
    region_area = {"center": TH["center_region_pct"] ** 2,
                   "e1": 0.10,                 # 최외곽 1열 ≈ 2/반경 (반경 22 에서 9%)
                   "edge": 1 - TH["edge_region_pct"] ** 2,
                   "ring": TH["edge_region_pct"] ** 2 - TH["center_region_pct"] ** 2,
                   "quadrant": 0.25}
    order = sorted(specs, key=lambda s: -fail_budget(s, n_rows))
    sources: list[dict] = []
    for s in order:
        need = max(1, min(fail_budget(s, n_rows), cap))
        pattern = s["fails"].get("pattern")
        heavy = need > heavy_line
        spatial = pattern in SPATIAL_PATTERNS
        region = pattern if pattern in region_area else None
        # 영역 예산은 그 영역 행 수의 절반까지만 쓴다(뒤 항목이 쓸 몫을 남긴다).
        region_need = need * s["fails"].get("share", 1.0) if region else 0
        placed = False
        for src in sources:
            if src["used"] + need > cap or len(src["specs"]) >= 60:
                continue
            if heavy and (src["heavy"] or src["spatial"]):
                continue
            if spatial and (src["heavy"] or src["used"] > spatial_line):
                continue
            if region and (src["region"].get(region, 0) + region_need
                           > 0.5 * region_area[region] * n_rows):
                continue
            src["specs"].append(s)
            src["used"] += need
            src["heavy"] = src["heavy"] or heavy
            src["spatial"] = src["spatial"] or spatial
            if region:
                src["region"][region] = src["region"].get(region, 0) + region_need
            placed = True
            break
        if not placed:
            sources.append({"specs": [s], "used": need, "heavy": heavy, "spatial": spatial,
                            "region": {region: region_need} if region else {}})
    def order_key(s):
        """source 안에서 fail 행을 고르는 순서 — ① 공간 패턴(소량) ② 임의 ③ heavy.

        heavy 를 마지막에 두는 것이 핵심이다. 반경 gradient 처럼 heavy 이면서 공간 편중인
        item 이 먼저 행을 가져가면 **남는 행이 중앙에 몰려**, 뒤따르는 임의 배치 item 이
        중앙 편중이 되어 CENTER_FAIL 이 오발화한다(실측 center_fail_ratio 3.7).
        """
        need = fail_budget(s, n_rows)
        spatial = s["fails"].get("pattern") in SPATIAL_PATTERNS
        rank = 2 if need > heavy_line else (0 if spatial else 1)
        return (rank, s["name"])

    for src in sources:
        src["specs"].sort(key=order_key)
    return [src["specs"] for src in sources]


def resolve_tuning(plan: dict, n: int, seed: int, lsl, usl) -> dict:
    """with_tune 로 표시된 plan 을 실제 행 수·난수로 역산해 확정 plan 을 만든다."""
    import copy

    plan = copy.deepcopy(plan)
    tunes = plan.pop("tune", None) or []
    for spec_tune in tunes:
        def values_at(x, _t=spec_tune):
            trial = copy.deepcopy(plan)
            _t["apply"](trial, x)
            v = synth_values(trial, n, np.random.default_rng(seed), lsl, usl)
            return v[np.isfinite(v)]

        def measured(x, _t=spec_tune):
            v = values_at(x, _t)
            got = _t["metric"](v) if v.size > 3 else None
            return -1e9 if got is None else got

        x = tune(measured, spec_tune["target"], spec_tune["lo"], spec_tune["hi"])
        ensure = spec_tune.get("ensure")
        if ensure is not None:
            for _ in range(40):                    # 조건을 만족할 때까지 파라미터를 키운다
                v = values_at(x)
                if v.size > 3 and ensure(v):
                    break
                x *= 1.15
        spec_tune["apply"](plan, x)
    return plan


def _fail_rows_by_value(values, free_idx, want, lsl, usl):
    """spec 을 벗어난 chip 을 fail 로 고른다 — 많으면 극단값 순, 모자라면 있는 만큼.

    fail 을 "값이 죽은 chip" 으로 정의해야 Issue Table 의 fail 과 Distribution 의
    limit 위반이 같은 chip 을 가리킨다. 모자란 분은 호출부가 `_push_out_of_spec` 으로
    극단값 chip 을 밀어 채운다.
    """
    want = int(min(want, free_idx.size))
    if want <= 0:
        return np.asarray([], dtype=int)
    v = values[free_idx]
    dist = np.where(np.isfinite(v), np.maximum(lsl - v, v - usl), -np.inf)   # >0 = spec 밖
    # limit 에 정확히 걸린 값(rail)은 CODE_RAIL 의 판정 지표(limit_hit_ratio)라 밀면 지표가
    # 깎인다 — spec 밖 chip 이 이미 있으면 그쪽을 먼저 쓰고 rail 은 마지막에 고른다.
    dist = np.where(np.isclose(v, lsl) | np.isclose(v, usl), dist - 1e6, dist)
    order = free_idx[np.argsort(-dist)]            # spec 을 많이 벗어난 순
    return order[:want].astype(int)


def _push_out_of_spec(values, fail_idx, lsl, usl, margin, rng):
    """fail chip 중 아직 spec 안에 있는 값을 가까운 limit 밖으로 민다 (제자리 수정).

    미는 방향은 그 chip 의 원래 값이 중앙보다 위면 USL 밖, 아래면 LSL 밖 — 분포의
    좌우 균형(center_bias)을 흐트러뜨리지 않는다. margin 은 레벨이 오를수록 커진다.
    """
    if fail_idx.size == 0 or lsl is None or usl is None:
        return
    finite = values[np.isfinite(values)]
    if finite.size and np.unique(finite).size == 1:
        return                                     # 상수 항목 — 밀면 상수가 아니게 된다
    span = usl - lsl
    mid = (usl + lsl) / 2
    v = values[fail_idx]
    inside = np.isfinite(v) & (v >= lsl) & (v <= usl)
    if not inside.any():
        return
    over = span * margin * (1 + rng.random(int(inside.sum())))
    high = v[inside] >= mid
    v[inside] = np.where(high, usl + over, lsl - over)
    values[fail_idx] = v


def build_source_df(specs, wafer, seed):
    """한 source 의 honeyform DataFrame + item 별 실측 요약 리스트."""
    x, y, rnorm, e1 = wafer
    n = x.size
    rng = np.random.default_rng(seed)
    xmax, ymax = float(np.max(np.abs(x))), float(np.max(np.abs(y)))
    xn, yn = x / xmax, y / ymax

    free = np.ones(n, dtype=bool)
    bin_col = np.ones(n, dtype=int)
    failtno_col = np.zeros(n, dtype=int)
    cols: dict[str, list] = {}
    rows_meta = {k: {} for k in META_ROW_LABELS}
    summary = []

    for tseq, s in enumerate(specs, start=1):
        tno = s["tno"]                             # 유형·레벨 인코딩(assign_tno) — 전역 유일
        lsl, usl = s["lsl"], s["usl"]
        values = s["_values"].copy()               # prepare_specs 가 미리 만든 값
        oos = s["_oos"]
        fires = s["level"] >= FIRE_FROM
        f = s["fails"]
        pattern = f.get("pattern", "random")
        free_idx = np.flatnonzero(free)

        # ── 불변 법칙: **spec 밖 = fail** ────────────────────────────────────
        # ① 이미 spec 을 벗어난 chip 은 무조건 fail 로 찍는다(예외 없음).
        # ② 공간·수율 룰은 위치/비율로 chip 을 더 골라 limit 밖으로 밀어 fail 로 만든다.
        # ③ 값만으로 fail 이 모자라면 레벨 사다리(FAIL_N)만큼 극단값 chip 을 밀어 채운다.
        # L1·L2(정상 단계)는 prepare_specs 가 spec 밖 값을 없앴으므로 fail 이 0 이다.
        #
        # ⚠ **`mode: "natural"` 은 ②③ 을 건너뛴다**(2026-08-14). ②③ 의 `_push_out_of_spec`
        # 은 고른 chip 을 limit 바로 밖으로 옮기는데, 몸통이 좁으면 몸통과 limit 사이가
        # 통째로 비어 **어떤 겨냥이든 값 축에서 outlier 모양**이 된다. 그래서 이 방식만으로는
        # "꼬리가 자연히 limit 을 넘은" 항목 — 즉 어떤 룰도 안 걸리는 UNKNOWN — 을 만들 수
        # 없었다. natural 모드는 fail 을 ① 로만, 곧 분포가 스스로 넘긴 chip 으로만 만든다.
        natural = fires and f.get("mode") == "natural"
        fail_idx = free_idx[oos[free_idx]] if fires else np.asarray([], dtype=int)
        if natural:
            pass
        elif fires and (pattern in SPATIAL_PATTERNS or f.get("fraction")):
            want = _pattern_count(s, n)
            pick_from = np.setdiff1d(free_idx, fail_idx, assume_unique=False)
            extra = pick_fail_rows(pattern, max(0, want - fail_idx.size), pick_from,
                                   rnorm, xn, yn,
                                   f.get("share", 0.3 if pattern.startswith("grad") else 1.0),
                                   rng, e1=e1)
            _push_out_of_spec(values, extra, lsl, usl, FAIL_MARGIN[s["level"] - 1], rng)
            fail_idx = np.concatenate([fail_idx, extra]).astype(int)
        elif fires and fail_idx.size < int(f.get("count", FAIL_N[s["level"] - 1])):
            # 선언된 fail 수를 존중한다(관찰군은 사다리와 무관한 자기 count 를 쓴다).
            need = int(f.get("count", FAIL_N[s["level"] - 1])) - fail_idx.size
            pick_from = np.setdiff1d(free_idx, fail_idx, assume_unique=False)
            # 측정값이 없는(NaN) chip 은 fail 로 찍지 않는다 — 값으로 설명할 수 없는 fail 이
            # 생긴다(LOW_SAMPLE 처럼 대부분이 공란인 항목에서 실제로 195건 나왔다).
            pick_from = pick_from[np.isfinite(values[pick_from])]
            need = int(min(need, pick_from.size))
            extra = _fail_rows_by_value(values, pick_from, need, lsl, usl) \
                if (lsl is not None and usl is not None) \
                else rng.choice(pick_from, size=int(min(need, pick_from.size)), replace=False)
            _push_out_of_spec(values, extra, lsl, usl, FAIL_MARGIN[s["level"] - 1], rng)
            fail_idx = np.concatenate([fail_idx, extra]).astype(int)

        # 다른 item 이 이미 쓴 chip 에서 이 item 이 spec 을 벗어나면 fail 로 찍을 수 없다
        # (FAILTNO 는 chip 당 하나). 그 값은 spec 안으로 당겨 불변 법칙을 지킨다 —
        # 실제 테스터에서도 chip 은 처음 걸린 test 하나로 귀속된다.
        # ⚠ **몸통 안으로** 당긴다(2026-08-13). limit 바로 안쪽(usl−0.01·span)으로 당기던
        # 종전 방식은 σ=0.05 항목에서 중심 9.8 robust σ 짜리 인공 pass 를 만들어,
        # OUTLIER 판정의 gap(마지막 pass ↔ 첫 fail 빈 구간)을 통째로 메웠다 —
        # OUTLIER_L2·L3 가 gap 0.7~0.9 로 죽던 원인. 실데이터에도 그런 pass 는 없다.
        if lsl is not None and usl is not None:
            stuck = np.flatnonzero(oos & ~free)
            if stuck.size:
                # **그 item 의 spec 안 값에서 재추출**한다 — 분포 모양을 그대로 물려받으므로
                # 이봉이면 이봉에서, 정규면 정규에서 뽑힌다.
                # ⚠ 중앙값 근처로 당기면 안 된다: 이봉 분포의 중앙값은 **두 봉우리 사이 골**
                # 이라 거기에 값을 채우면 골이 메워져 density_gap 이 0 이 된다(BIMODALITY_L2
                # 미발화로 실제로 겪음). limit 바로 안쪽으로 당기던 그 이전 방식은 반대로
                # 인공 극단 pass 를 만들어 OUTLIER 의 gap 을 메웠다.
                inside = values[np.isfinite(values) & (values >= lsl) & (values <= usl)]
                if inside.size:
                    values[stuck] = rng.choice(inside, size=stuck.size, replace=True)
                else:
                    span = usl - lsl
                    values[stuck] = np.where(values[stuck] > usl, usl - 0.01 * span,
                                             lsl + 0.01 * span)

        free[fail_idx] = False
        bin_col[fail_idx] = int(s["bin"])
        failtno_col[fail_idx] = tno

        v_ok = values[np.isfinite(values)]
        summary.append({
            "spec": s, "tno": tno, "fail_count": int(fail_idx.size),
            "n_dut": int(v_ok.size),
            "actual": measure(v_ok, lsl, usl, fail_idx, n, x, y, rnorm, xn, yn, e1,
                              all_values=values),
        })

        cols[s["name"]] = ["" if not np.isfinite(v) else f"{v:.6g}" for v in values]
        rows_meta["TSEQ"][s["name"]] = str(tseq)
        rows_meta["TNO"][s["name"]] = str(tno)
        rows_meta["STEP"][s["name"]] = "P2"
        rows_meta["UNIT"][s["name"]] = s["unit"]
        rows_meta["HILIM"][s["name"]] = "" if usl is None else f"{usl:g}"
        rows_meta["LOLIM"][s["name"]] = "" if lsl is None else f"{lsl:g}"

    item_names = [s["name"] for s in specs]
    head = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row["SERIAL"] = label
        row.update({it: rows_meta[label].get(it, "") for it in item_names})
        head.append(row)

    body = {
        "SERIAL": [f"C{i:06d}" for i in range(n)],
        "SHOT": [str(i // 4 + 1) for i in range(n)],
        "DUT": [str(i % 4 + 1) for i in range(n)],
        # **실데이터 규약: XPOS/YPOS 는 항상 양수**(0-based die 인덱스). 내부 계산은 중심
        # 정렬 좌표(x, y)로 하고, 파일에는 좌하단을 (0,0)으로 옮겨 적는다.
        # 엔진은 좌표 범위의 중앙을 웨이퍼 중심으로 잡으므로 판정은 동일하다.
        "XPOS": [str(int(v - x.min())) for v in x],
        "YPOS": [str(int(v - y.min())) for v in y],
        "BIN": [str(int(b)) for b in bin_col],
        "FAILTNO": ["" if t == 0 else str(int(t)) for t in failtno_col],
    }
    body.update(cols)
    df = pd.concat([pd.DataFrame(head, columns=META_COLUMNS + item_names),
                    pd.DataFrame(body, columns=META_COLUMNS + item_names)],
                   ignore_index=True)
    return df, summary


def measure(v, lsl, usl, fail_idx, n_rows, x, y, rnorm, xn, yn, e1=None, all_values=None):
    """생성된 값·fail 배치의 실측 지표 (정답표 기록용).

    `v` 는 유한값만 담은 배열, `all_values` 는 NaN 을 포함한 전체 길이 배열이다
    (fail_idx 가 전체 길이 기준이라 fail 지표 계산에는 후자가 필요하다).
    """
    out = {"n_dut": int(v.size), "fail_count": int(fail_idx.size),
           "yield": round(1 - fail_idx.size / n_rows, 4)}
    if v.size >= 2:
        out.update(
            spread_norm=_r(m_spread_norm(v, lsl, usl)), outlier_ratio=_r(m_outlier_ratio(v)),
            cpk=_r(m_cpk(v, lsl, usl)), center_bias=_r(m_center_bias(v, lsl, usl)),
            kurtosis=_r(m_kurtosis(v)), skewness=_r(m_skew_np(v)),
            skewness_moment=_r(m_skew_moment(v)),
            bimodality=_r(m_bimodality(v)), density_gap=_r(m_density_gap(v)),
            modality_v2=m_modality_v2(v), limit_hit=_r(m_limit_hit(v, lsl, usl)),
            stdev=_r(m_std(v)), tail_mass_3s=_r(m_tail_mass(v)),
            spec_margin_min=_r(_spec_margin_min(v, lsl, usl)),
            limit_missing=int(lsl is None or usl is None))
    if fail_idx.size:
        if all_values is not None:
            mad_min, gap, z_max = m_fail_outlier(all_values, fail_idx)
            out["fail_mad_min"] = _r(mad_min)
            out["fail_pass_gap_sigma"] = _r(gap)
            out["fail_robust_z_max"] = _r(z_max)
        fm = np.zeros(n_rows, dtype=bool)
        fm[fail_idx] = True
        overall = fm.mean()
        total = float(fm.sum())
        e1_mask = np.zeros_like(rnorm, dtype=bool) if e1 is None else e1
        edge = (rnorm >= TH["edge_region_pct"]) & (~e1_mask)
        center = rnorm <= TH["center_region_pct"]
        ring = (rnorm > TH["center_region_pct"]) & (rnorm < TH["edge_region_pct"]) & (~e1_mask)
        for nm, mask in (("e1", e1_mask), ("edge", edge), ("center", center), ("ring", ring)):
            if mask.any():
                out[f"{nm}_fail_ratio"] = _r(fm[mask].mean() / overall)
                # 공간 룰의 판정 지표 — 전체 fail 중 그 영역이 차지하는 **점유율**
                out[f"{nm}_fail_share"] = _r(fm[mask].sum() / total)
            else:
                out[f"{nm}_fail_ratio"] = out[f"{nm}_fail_share"] = None
        # 사분면 편중은 0°·45° 두 격자의 max — 엔진과 같다(축에 걸친 뭉침 보완).
        imb = [q for q in (_quad_imb(x, y, fm), _quad_imb(x + y, y - x, fm)) if q is not None]
        out["quadrant_imbalance"] = _r(max(imb)) if imb else None
        out["fail_spread_norm"] = _r(m_fail_spread(fail_idx, x, y,
                                                   float(np.sqrt(x ** 2 + y ** 2).max())))
        out["gradient_norm_abs_max"] = _r(max(abs(g) for g in
                                              (_grad(rnorm, fm), _grad(xn, fm), _grad(yn, fm))
                                              if g is not None))
    return out


def _quad_imb(ax, ay, fm):
    """주어진 두 축이 만드는 4분면의 fail율 편중 — 엔진 `_quadrant_imbalance` 복제."""
    rates = []
    for sx in (True, False):
        for sy in (True, False):
            q = ((ax >= 0) == sx) & ((ay >= 0) == sy)
            if q.any():
                rates.append(fm[q].mean())
    if not rates:
        return None
    mean_rate = float(np.mean(rates))
    return (max(rates) - min(rates)) / mean_rate if mean_rate else None


def _spec_margin_min(v, lsl, usl):
    """양쪽 spec margin(σ 단위) 중 작은 쪽 — BIDIR_TAIL/TAIL_RISK 의 판단 지표."""
    sd = m_std(v)
    if sd == 0:
        return None
    margins = [(float(v.mean()) - lsl) / sd if lsl is not None else None,
               (usl - float(v.mean())) / sd if usl is not None else None]
    margins = [m for m in margins if m is not None]
    return min(margins) if margins else None


def _grad(coord, fm, bins=8):
    edges = np.linspace(coord.min(), coord.max(), bins + 1)
    centers, rates = [], []
    for i in range(bins):
        m = (coord >= edges[i]) & (coord <= edges[i + 1] if i == bins - 1 else coord < edges[i + 1])
        if m.sum():
            centers.append((edges[i] + edges[i + 1]) / 2)
            rates.append(fm[m].mean())
    if len(centers) < 2:
        return None
    return float(np.polyfit(centers, rates, 1)[0])


def _r(v, nd=4):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), nd)


# ──────────────────────────────────────────────────────────────────────────────
# 산출물 쓰기 / 검증 / 업로드
# ──────────────────────────────────────────────────────────────────────────────

def write_outputs(out_dir: Path, sources, args):
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("source_*.parquet"):
        old.unlink()

    files, manifest_sources = [], []
    for idx, (df, _summary) in enumerate(sources):
        data = encode_honeyform_parquet(df)
        path = out_dir / f"source_{idx:02d}.parquet"
        path.write_bytes(data)
        files.append(path)
        manifest_sources.append({"index": idx, "name": f"EVALTEST_W{idx + 1:02d}",
                                 "file_name": f"source_{idx:02d}.parquet"})

    manifest = {
        "meta": {"product_type": args.product_type, "family_product": args.family,
                 "product": args.product, "lot_id": args.lot,
                 "revision": "1.0", "file_name": args.file_name},
        "mode": "Normal",
        "sources": manifest_sources,
        "selected_items": [],
        "sheets": [],
        # AI Comment · Signature 컬럼은 이 두 키가 모두 참인 세션에만 생긴다
        # (validation.webreport_ai_comment — docs/13 §7).
        "options": {"ai_comment": True, "ai_comment_optin": True},
        "client": {"user": "evaltest", "host": "evaltest-pc", "domain": ""},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    return files, manifest


ANSWER_COLS = ["item", "source", "wafer", "group", "level", "level_ko", "intent", "expect",
               "unit", "lsl", "usl", "bin", "metric", "target", "actual_metric",
               "threshold", "n_dut", "fail_count", "yield", "note"]


def write_answer_key(out_dir: Path, sources):
    rows = []
    for idx, (_df, summary) in enumerate(sources):
        for rec in summary:
            s = rec["spec"]
            actual = rec["actual"]
            metric = s["metric"]
            th_key = {"spread_norm": "spread_norm_warn", "outlier_ratio": "outlier_ratio_warn",
                      "cpk": "cpk_warn", "center_bias": "mean_shift_warn",
                      "kurtosis": "kurtosis_warn", "code_edge_hit": "code_edge_hit_warn",
                      "fail_mad_min": "outlier_fail_mad_min",
                      "fail_pass_gap_sigma": "outlier_fail_gap_sigma_min",
                      "fail_spread_norm": "spot_cluster_spread_max",
                      "tail_mass_3s": "heavy_tail_mass_min",
                      "e1_fail_share": "region_fail_share_min",
                      "edge_fail_share": "region_fail_share_min",
                      "center_fail_share": "region_fail_share_min",
                      "ring_fail_share": "region_fail_share_min",
                      "quadrant_imbalance": "quadrant_imbalance_warn",
                      "gradient_norm_abs_max": "gradient_norm_warn",
                      "yield": "gross_yield_bad", "n_dut": "n_min",
                      "skewness": "skew_warn", "site_cpk_delta": "site_cpk_delta_warn",
                      "spec_margin_min": "spec_margin_warn"}.get(metric)
            actual_metric = actual.get({"code_edge_hit": "limit_hit"}.get(metric, metric))
            rows.append({
                "item": s["name"], "source": idx, "wafer": idx + 1, "group": s["group"],
                "level": s["level"], "level_ko": LEVEL_KO[s["level"]],
                "intent": ";".join(s["intent"]), "expect": s["expect"], "unit": s["unit"],
                "lsl": s["lsl"], "usl": s["usl"], "bin": s["bin"],
                "metric": metric, "target": s["target"], "actual_metric": actual_metric,
                "threshold": TH.get(th_key) if th_key else None,
                "n_dut": rec["n_dut"], "fail_count": rec["fail_count"],
                "yield": actual.get("yield"), "note": s["note"],
            })
    path = out_dir / "answer_key.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ANSWER_COLS)
        w.writeheader()
        w.writerows(rows)
    (out_dir / "metrics_detail.json").write_text(
        json.dumps([{"item": rec["spec"]["name"], "source": idx, **rec["actual"]}
                    for idx, (_d, summary) in enumerate(sources) for rec in summary],
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return rows


def run_eval_pass(out_dir: Path, args, dump_path: Path):
    """이 파일이 자식 프로세스로 다시 불렸을 때 — parquet 을 실제 엔진 경로로 평가해 JSON 덤프.

    부모가 두 번 부른다: ① 현재 서버 룰 그대로 ② 전 룰 enabled 사본(EVAL_RULES_DIR).
    ②를 같은 프로세스에서 못 하는 이유는 eval_engine.config 가 **import 시점에** env 를
    읽어 rules 경로를 고정하기 때문 — 프로세스 경계가 곧 격리다(docs/13 §10 과 같은 이유).
    """
    from web_report import ai_comment

    tables = []
    for path in sorted(out_dir.glob("source_*.parquet")):
        idx = int(path.stem.split("_")[1])
        tables.append(decode_split_honeyform_parquet(
            path.read_bytes(), source=f"EVALTEST_W{idx + 1:02d}", file_name=path.name,
            keep_df=False))
    session = {"product_type": args.product_type, "family_product": args.family,
               "product": args.product, "lot_id": args.lot, "revision": "1.0",
               "session_id": "evaltest-dryrun", "analysis_key": "evaltest-dryrun"}
    t0 = time.perf_counter()
    result = ai_comment.build_ai_comments(tables, session, fail_only=True)

    sig_by_item: dict[str, list] = {}
    for key, ids in (result.get("row_signatures") or {}).items():
        parts = key.split("|")
        item = parts[2] if key.startswith("Yield|") else parts[1]
        for sid in ids:
            if sid not in sig_by_item.setdefault(item, []):
                sig_by_item[item].append(sid)
    comments = {k: v for k, v in (result.get("comments") or {}).items()
                if k.startswith("CPK|")}
    dump_path.write_text(json.dumps({"signatures": sig_by_item, "comments": comments,
                                     "elapsed": round(time.perf_counter() - t0, 2)},
                                    ensure_ascii=False), encoding="utf-8")


def all_enabled_rules(tmp_dir: Path) -> Path:
    """현재 rules 폴더 사본 + 모든 signature enabled:true → 임시 rules 디렉토리 경로.

    **운영 rules 는 건드리지 않는다** — 사본에만 쓰고 EVAL_RULES_DIR 로 자식에게 준다.
    비활성 룰까지 "데이터가 조건을 맞췄는지" 확인하기 위한 검증 전용 경로다.
    """
    import shutil

    import yaml

    src = _ROOT / "eval_analyzer" / "eval_engine" / "rules"
    dst = tmp_dir / "rules_all_enabled"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    for path in [dst / "signatures.yaml"] + sorted((dst / "signatures").rglob("*.yaml")):
        if not path.exists():
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        sigs = doc.get("signatures")
        if isinstance(sigs, list):
            for s in sigs:
                s["enabled"] = True
        elif isinstance(sigs, dict):
            for s in sigs.values():
                if isinstance(s, dict):
                    s["enabled"] = True
        path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    return dst


def _spawn_eval(out_dir: Path, args, dump_path: Path, rules_dir: Path | None):
    import os
    import subprocess

    cmd = [sys.executable, str(Path(__file__).resolve()), "--_eval-dump", str(dump_path),
           "--out", str(out_dir), "--product-type", args.product_type,
           "--family", args.family, "--product", args.product, "--lot", args.lot]
    env = dict(os.environ)
    env["WEB_REPORT_EVAL_FAIL_ONLY"] = "1"
    if rules_dir is not None:
        env["EVAL_RULES_DIR"] = str(rules_dir)
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0 or not dump_path.exists():
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise RuntimeError("엔진 평가 자식 프로세스 실패")
    return json.loads(dump_path.read_text(encoding="utf-8"))


def live_enabled_ids(args) -> set:
    """현재 서버 룰에서 켜져 있는 signature id (제품군 오버레이 반영)."""
    from web_report import eval_debug
    return {str(s.get("id")) for s in eval_debug.signatures_scoped(args.product_type, args.family)
            if s.get("enabled") is not False}


def suppressor_map(args) -> dict:
    """{signature id: [나를 지우는 id ...]} — signatures.yaml 의 suppressed_by 선언.

    "조건은 맞았는데 더 구체적인 룰에 눌려 목록에서 빠진" 경우를 누락으로 세지 않기 위해
    필요하다(예: OUTLIER 가 뜨면 LOW_CPK 는 primary 를 양보한다).
    """
    from web_report import eval_debug
    out = {}
    for s in eval_debug.signatures_scoped(args.product_type, args.family):
        raw = s.get("suppressed_by") or []
        if isinstance(raw, str):
            raw = [raw]
        if raw:
            out[str(s.get("id"))] = [str(v) for v in raw]
    return out


def verify(out_dir: Path, answer_rows, args):
    """생성 데이터 ↔ 정답표 대조. 2패스: ① 현재 룰 ② 전 룰 enabled 사본."""
    live = _spawn_eval(out_dir, args, out_dir / "_eval_live.json", None)
    rules_all = all_enabled_rules(out_dir)
    full = _spawn_eval(out_dir, args, out_dir / "_eval_all.json", rules_all)
    enabled_now = live_enabled_ids(args)
    suppressors = suppressor_map(args)

    rows = []
    stat = {"ok": 0, "miss": 0, "false_fire": 0, "normal_fp": 0, "cofire": 0,
            "suppressed": 0, "unknown": 0}
    cofire: dict[str, int] = {}
    observed: dict[str, int] = {}       # 관찰군(random) — 엔진이 뭐라고 했나
    observed_n = observed_silent = 0
    for r in answer_rows:
        want = [s for s in r["intent"].split(";") if s]
        want_fireable = [s for s in want if s not in UNFIRABLE]
        fired_live = live["signatures"].get(r["item"], [])
        fired_all = full["signatures"].get(r["item"], [])
        suppressed = []
        if r["expect"] == "observe":
            # 관찰군 — 겨냥한 룰이 없으므로 누락/오발화 개념이 없다. 분포만 센다.
            missing = false_fire = []
            observed_n += 1
            live_ids = [s for s in fired_live if s != UNKNOWN_ID]
            if not live_ids:
                observed_silent += 1
            for s in live_ids:
                observed[s] = observed.get(s, 0) + 1
        elif r["expect"] == "fire":
            missing = [s for s in want_fireable if s not in fired_all]
            # 조건은 맞았지만 더 구체적인 룰에 눌린 것(suppressed_by)은 누락이 아니다.
            suppressed = [s for s in missing
                          if any(b in fired_all for b in suppressors.get(s, []))]
            missing = [s for s in missing if s not in suppressed]
            false_fire = []
        else:                                       # 1단계(정상범위) — 겨냥한 룰이 뜨면 안 된다
            missing = []
            false_fire = [s for s in want if s in fired_all]
        # UNKNOWN 은 "아무 룰도 안 뜬 케이스" 표식이라 오탐이 아니다 — 정상군에서는
        # 오히려 이게 떠야 맞다(엔진 미분류). 별도로만 센다.
        unknown = UNKNOWN_ID in fired_all
        # 관찰군은 겨냥이 없어 모든 발화가 "겨냥 밖"으로 세어져 동반발화 통계를 왜곡한다.
        unexpected = ([] if r["expect"] == "observe"
                      else [s for s in fired_all if s not in want and s != UNKNOWN_ID])
        stat["unknown"] += 1 if unknown else 0
        stat["suppressed"] += len(suppressed)
        stat["miss"] += len(missing)
        stat["false_fire"] += len(false_fire)
        stat["cofire"] += len(unexpected)
        if r["group"] == "normal" and unexpected:
            stat["normal_fp"] += 1
        if not missing and not false_fire:
            stat["ok"] += 1
        for s in unexpected:
            cofire[f"{';'.join(want) or '(정상군)'} → +{s}"] = \
                cofire.get(f"{';'.join(want) or '(정상군)'} → +{s}", 0) + 1
        cell = live["comments"].get(f"CPK|{r['item']}", "")
        rows.append({
            "item": r["item"], "source": r["source"], "group": r["group"],
            "level": r["level"], "intent": r["intent"], "expect": r["expect"],
            "metric": r["metric"], "target": r["target"], "actual_metric": r["actual_metric"],
            "fired_all_rules": ";".join(fired_all), "fired_live": ";".join(fired_live),
            "missing": ";".join(missing), "false_fire": ";".join(false_fire),
            "suppressed": ";".join(suppressed), "co_fired": ";".join(unexpected),
            "status_live": cell.split("]")[0].lstrip("[") if cell.startswith("[") else "",
            "cell_live": cell[:200],
        })

    path = out_dir / "verify.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    fired_all_ids = sorted({s for r in rows for s in r["fired_all_rules"].split(";") if s})
    fired_live_ids = sorted({s for r in rows for s in r["fired_live"].split(";") if s})
    print(f"\n[검증] 전 룰 enabled 평가 {full['elapsed']}s / 현재 룰 평가 {live['elapsed']}s "
          f"· item {total}개")
    print(f"  겨냥한 룰이 의도대로     : {stat['ok']}/{total}")
    print(f"  2~5단계인데 미발화(누락) : {stat['miss']}건  ← 0 이어야 정상")
    print(f"  1단계인데 발화(오발화)   : {stat['false_fire']}건  ← 0 이어야 정상")
    print(f"  조건은 맞았으나 억제됨   : {stat['suppressed']}건 (suppressed_by — 정상 동작)")
    print(f"  정상군 오탐(item)        : {stat['normal_fp']}건  ← 0 이어야 정상")
    print(f"  UNKNOWN(미분류) 표식     : {stat['unknown']}건 — 아무 룰도 안 뜬 케이스")
    print(f"  겨냥 밖 동반발화         : {stat['cofire']}건 (지표가 상관된 룰은 구조상 함께 뜬다)")
    print(f"  전 룰 기준 발화 signature: {', '.join(fired_all_ids) or '(없음)'}")
    print(f"  현재 룰(운영) 발화       : {', '.join(fired_live_ids) or '(없음)'}")
    off = sorted({s for r in answer_rows for s in r["intent"].split(";")
                  if s and s not in enabled_now})
    print(f"  현재 꺼져 있는 룰        : {', '.join(off) or '(없음)'} "
          f"→ 이 항목들은 운영 화면에서 '미분류'로 보인다")
    bad = [r for r in rows if r["missing"] or r["false_fire"]]
    for r in bad[:10]:
        print(f"    ! {r['item'][:44]:46} L{r['level']} miss={r['missing'] or '-'} "
              f"오발화={r['false_fire'] or '-'} 실측={r['actual_metric']}")
    if cofire:
        top = sorted(cofire.items(), key=lambda kv: -kv[1])[:8]
        print("  동반발화 상위: " + " | ".join(f"{k}×{v}" for k, v in top))
    if observed_n:
        # 관찰군 — 정답이 없으므로 "맞았나" 가 아니라 "무엇이라 판정했나" 를 본다.
        print(f"\n[관찰군] 무작위 item {observed_n}개에 대한 **현재 룰** 판정 분포")
        for sig, cnt in sorted(observed.items(), key=lambda kv: -kv[1]):
            print(f"    {sig:22} {cnt:3}건 ({cnt / observed_n:.0%})")
        print(f"    {'(발화 없음)':22} {observed_silent:3}건 "
              f"({observed_silent / observed_n:.0%}) — UNKNOWN 만 붙는다")
    # 미분류군 — "아무 룰도 안 걸림" 이 목표라 성공 여부를 item 단위로 보여 준다.
    unk = [r for r in rows if r["group"] == "unknown"]
    if unk:
        hit = [r for r in unk
               if not [s for s in r["fired_live"].split(";") if s and s != UNKNOWN_ID]]
        print(f"\n[미분류군] UNKNOWN 겨냥 {len(unk)}개 중 {len(hit)}개 성공 "
              f"(현재 룰 기준 다른 발화 0건)")
        for r in unk:
            other = [s for s in r["fired_live"].split(";") if s and s != UNKNOWN_ID]
            print(f"    {r['item'][:26]:28} {'OK' if not other else '실패'} "
                  f"발화={';'.join(other) or 'UNKNOWN 만'} · {r['status_live'] or '-'}")
    for sig, why in UNFIRABLE.items():
        print(f"  [발화불가] {sig}: {why}")
    return rows


def make_session_local(files, manifest):
    """서버 프로세스 없이 **이 PC 의 report DB 에 직접** 세션을 만든다.

    업로드 라우트가 하는 일(web_report.ingest_webreport)을 그대로 호출한다 — 서버가
    떠 있지 않아도(개발 PC 처럼) 세션이 만들어지고, 서버를 켜면 목록에 그대로 보인다.
    저장 경로는 서버와 같은 config 기본값(DB/pe/report/report.db · uploads/)이다.
    """
    from database import report_db
    from web_report import ingest as wr_ingest
    from config import REPORT_UPLOAD_DIR

    report_db.init_report_db()
    payload = [{"name": s["name"], "filename": s["file_name"],
                "data": p.read_bytes()} for s, p in zip(manifest["sources"], files)]
    return wr_ingest.ingest_webreport(
        manifest, payload, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
        client_ip="127.0.0.1", user_agent="Mozilla/5.0 HoneyUser/evaltest")


def upload(base_url: str, files, manifest):
    """multipart/form-data 로 POST /pe/report/upload_webreport (requests 의존 없음)."""
    import urllib.request

    boundary = "----evaltest" + str(int(time.time()))
    body = bytearray()

    def _part(headers, payload: bytes):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(headers.encode())
        body.extend(b"\r\n\r\n")
        body.extend(payload)
        body.extend(b"\r\n")

    _part('Content-Disposition: form-data; name="manifest"',
          json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    for i, path in enumerate(files):
        _part(f'Content-Disposition: form-data; name="webreport_{i}"; '
              f'filename="{path.name}"\r\nContent-Type: application/octet-stream',
              path.read_bytes())
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        base_url.rstrip("/") + "/pe/report/upload_webreport", data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "Mozilla/5.0 HoneyUser/evaltest",
                 "X-Honey-Agent": "1"})
    with urllib.request.urlopen(req, timeout=1800) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="eval_analyzer 디버깅용 web_report 테스트 데이터 생성")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "out"))
    ap.add_argument("--items", type=int, default=500, help="test item 총 개수 (기본 500)")
    ap.add_argument("--random-items", type=int, default=RANDOM_COUNT,
                    help="관찰용 무작위 item 수 — 정답 기대 없이 엔진 판정만 본다 (기본 30)")
    ap.add_argument("--unknown-items", type=int, default=UNKNOWN_COUNT,
                    help="미분류(UNKNOWN) 겨냥 item 수 — 어떤 룰도 안 걸리는 항목 (기본 5)")
    ap.add_argument("--radius", type=int, default=0,
                    help="웨이퍼 반경(die) — 행 수 ≈ πr² (기본 28 ≈ 2,460행)")
    ap.add_argument("--seed", type=int, default=20260812)
    ap.add_argument("--product-type", default="PMIC", choices=["MDDI", "PDDI", "PMIC",
                                                               "SECURITY", "TCON"])
    ap.add_argument("--family", default="PMIC_ETC")
    ap.add_argument("--product", default="EVALTEST")
    ap.add_argument("--lot", default="EVALTEST_LOT1")
    ap.add_argument("--file-name", default="eval_testdata")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--upload", default="", metavar="URL",
                    help="생성 후 업로드할 서버 (예: http://127.0.0.1:8080)")
    ap.add_argument("--upload-only", action="store_true",
                    help="생성을 건너뛰고 --out 의 기존 파일을 그대로 올린다/세션으로 만든다")
    ap.add_argument("--make-session", action="store_true",
                    help="서버 없이 이 PC 의 report DB 에 직접 세션 생성 (개발 PC 용)")
    ap.add_argument("--single-csv", default="", metavar="PATH",
                    help="7-meta honeyform CSV 1장만 만든다 (직접 업로드용)")
    ap.add_argument("--_eval-dump", default="", help=argparse.SUPPRESS)   # 내부 자식 모드
    args = ap.parse_args()

    # 웨이퍼 크기는 필요한 CSV 장수를 거의 바꾸지 못한다(fail 이 비율로 늘기 때문) —
    # 파일이 작아지도록 2,453행(반경 28)을 기본으로 쓴다.
    # 단일 CSV 는 반경 40(≈5,024 chip). fail 은 chip 을 서로 배타적으로 쓰므로(FAILTNO 는
    # chip 당 하나) 관찰군이 붙은 뒤로는 반경 22(1,517)로 예산이 한참 모자란다 — 예산이
    # 90% 를 넘으면 뒤쪽 item 이 통째로 밀려나고, 표본이 적은 항목은 쓸 chip 자체가 없어져
    # fail 0 = 평가 제외가 된다. 관찰군 수를 크게 올릴 땐 반경도 같이 올릴 것.
    args.radius = args.radius or (40 if args.single_csv else 28)

    out_dir = Path(args.out)
    if getattr(args, "_eval_dump"):
        run_eval_pass(out_dir, args, Path(getattr(args, "_eval_dump")))
        return

    if args.single_csv:
        make_single_csv(args)
        return

    if args.upload_only:
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        files = [out_dir / s["file_name"] for s in manifest["sources"]]
        missing = [str(p) for p in files if not p.exists()]
        if missing:
            raise SystemExit(f"parquet 이 없습니다: {missing[:3]} … 먼저 생성하세요")
        print(f"[업로드 전용] {out_dir} 의 parquet {len(files)}개 사용 (재생성 없음)")
        _deliver(files, manifest, args)
        return

    wafer = build_wafer(args.radius)
    n_rows = wafer[0].size
    if n_rows < 2000:
        print(f"[경고] source 당 행 수 {n_rows} < 2000 — --radius 를 28 이상으로 두세요")

    _check_coord_limits(args)
    print(f"[1/4] item 카탈로그 생성 (목표 {args.items}개) …")
    specs = build_catalog(args.items, args.seed)
    groups: dict[str, int] = {}
    for s in specs:
        groups[s["group"]] = groups.get(s["group"], 0) + 1
    print(f"      item {len(specs)}개 — " + ", ".join(f"{k} {v}" for k, v in groups.items()))

    print(f"[2/4] fail 원가 산출 · source 패킹 (source 당 {n_rows}행, fail 행은 item 간 배타) …")
    specs = prepare_specs(specs, n_rows)
    packed = pack_sources(specs, n_rows)
    print(f"      source {len(packed)}개 (item/source 최대 {max(len(p) for p in packed)}개)")

    print("[3/4] honeyform 생성 · parquet 인코딩 …")
    sources = [build_source_df(sp, wafer, args.seed + i * 101) for i, sp in enumerate(packed)]
    files, manifest = write_outputs(out_dir, sources, args)
    answer_rows = write_answer_key(out_dir, sources)
    total_bytes = sum(p.stat().st_size for p in files)
    print(f"      parquet {len(files)}개 · {total_bytes / 2**20:.1f}MB → {out_dir}")
    print(f"      answer_key.csv ({len(answer_rows)}행) · metrics_detail.json · manifest.json")

    if not args.no_verify:
        print("[4/4] 엔진 검증 (web_report.ai_comment → eval_engine, 2패스) …")
        verify(out_dir, answer_rows, args)
    else:
        print("[4/4] 검증 생략 (--no-verify)")

    _deliver(files, manifest, args)


def versioned_path(path: Path) -> Path:
    """이미 있으면 `_v2`, `_v3` … 로 새 버전 파일명을 만든다 (기존 raw data 를 덮지 않는다)."""
    if not path.exists() and not list(path.parent.glob(f"{path.stem}_w*{path.suffix}")):
        return path
    stem, suffix = path.stem, path.suffix
    stem = stem.rsplit("_v", 1)[0] if stem.rsplit("_v", 1)[-1].isdigit() else stem
    v = 2
    while (path.with_name(f"{stem}_v{v}{suffix}").exists()
           or list(path.parent.glob(f"{stem}_v{v}_w*{suffix}"))):
        v += 1
    return path.with_name(f"{stem}_v{v}{suffix}")


# CSV 1장에 **구조적으로 못 담는** signature — 발화 조건 자체가 "웨이퍼의 상당수가 fail"
# 이라 다른 항목이 쓸 chip 이 남지 않는다(chip 1개는 FAILTNO 를 하나만 갖는다).
#   GROSS_FAIL      수율 <50% 가 발화 조건
#   CONSTANT_VALUE  spec 밖 상수 = 전량 fail       BIDIR_TAIL      양쪽 margin<1σ = 분포가 spec 밖으로
#   TAIL_RISK       spec_margin_min<1σ 조건상 꼬리가 spec 을 넘어야 한다
CSV_EXCLUDE = {"GROSS_FAIL", "CONSTANT_VALUE", "BIDIR_TAIL", "TAIL_RISK"}


def single_csv_specs(n_rows: int, random_count: int = RANDOM_COUNT, seed: int = 20260812,
                     unknown_count: int = UNKNOWN_COUNT):
    """CSV 1장(웨이퍼 1장)에 담을 item 목록 — 단독 세트 + 관찰군 + 미분류군.

    분포를 spec 안에 가둬(`bounded`) fail 은 레벨 사다리(FAIL_N)만큼만 나오게 했으므로
    한 장에 들어간다. 그래도 예산(행의 90%)을 넘으면 비싼 것부터 뺀다.
    반환: (담은 specs, 제외 [(item, 필요 fail 수)], 사용한 fail 행 수)
    """
    pool = [s for s in single_specs() if not (set(s["intent"]) & CSV_EXCLUDE)]
    pool += random_specs(random_count, random.Random(seed), salt=str(seed))
    # 미분류군은 관찰군과 **다른 난수열**을 쓴다 — 같은 rng 를 이어 쓰면 관찰군 개수를
    # 바꿀 때마다 미분류군 표본까지 통째로 바뀌어 재현이 어긋난다.
    pool += unknown_specs(unknown_count, random.Random(seed + 1), salt=str(seed))
    specs = assign_tno(pool)
    prepare_specs(specs, n_rows)
    budget = int(n_rows * 0.9)
    kept, dropped, used = [], [], 0
    for s in sorted(specs, key=lambda s: (fail_budget(s, n_rows), s["name"])):
        need = fail_budget(s, n_rows)
        if used + need > budget:
            dropped.append((s["name"], need))
            continue
        kept.append(s)
        used += need
    # 공간 패턴 item 이 먼저 행을 고르게 한다(뒤로 밀리면 남은 행이 편중돼 패턴이 깨진다).
    # 그 다음이 미분류군이다 — fail 을 밀어 만들지 않고 **값이 넘긴 chip 그 자리**를 써야
    # 하므로, 그 chip 을 앞 item 이 먼저 가져가면 fail 이 통째로 사라진다(실측: 뒤로 밀면
    # 자연 fail 9개가 1개로 줄었다). 밀어 만드는 item 들은 아무 여유 chip 이나 쓰면 된다.
    kept.sort(key=lambda s: (s["fails"].get("pattern") not in SPATIAL_PATTERNS,
                             s["group"] != "unknown", s["name"]))
    return kept, dropped, used


# 제품군별 좌표 상한 — 실데이터에서 넘을 수 없는 값(도메인 규약, 2026-08-12).
COORD_MAX = {"PMIC": {"y": 200}}


def _check_coord_limits(args):
    """생성할 좌표가 제품군 규약을 넘지 않는지 확인 — 넘으면 실데이터가 아니게 된다.

    좌표는 0-based 양수라 최대값이 곧 `2 × 반경` 이다(PMIC 은 Y ≤ 200).
    """
    limit = (COORD_MAX.get(args.product_type) or {}).get("y")
    span = 2 * args.radius
    if limit and span > limit:
        raise SystemExit(f"{args.product_type} 는 YPOS 가 {limit} 을 넘을 수 없습니다 — "
                         f"--radius {args.radius} 는 Y 최대 {span}. --radius 를 "
                         f"{limit // 2} 이하로 두세요")


def make_single_csv(args):
    """7-meta honeyform **CSV 1장**을 만든다 (사람이 직접 web_report 로 올리는 용도)."""
    import shutil
    import tempfile

    _check_coord_limits(args)
    wafer = build_wafer(args.radius)
    n_rows = wafer[0].size
    specs, dropped, used = single_csv_specs(n_rows, args.random_items, args.seed,
                                            args.unknown_items)
    n_random = sum(1 for s in specs if s["group"] == "random")
    n_unknown = sum(1 for s in specs if s["group"] == "unknown")
    print(f"[1/3] item {len(specs)}개 (겨냥 {len(specs) - n_random - n_unknown} + "
          f"관찰용 무작위 {n_random} + 미분류 {n_unknown}) "
          f"· chip {n_rows}개/item · fail chip {used}개 ({used / n_rows:.0%} 사용)")
    if dropped:
        print("      예산 초과 제외: " + ", ".join(f"{n}({k})" for n, k in dropped))
    print("      CSV 1장에 못 담는 signature(별도 파일 필요): "
          + ", ".join(sorted(CSV_EXCLUDE)))

    df, summary = build_source_df(specs, wafer, args.seed)
    out_csv = versioned_path(Path(args.single_csv))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")     # Excel 용 BOM
    print(f"[2/3] CSV → {out_csv} · {out_csv.stat().st_size / 2 ** 20:.1f}MB "
          f"· {df.shape[0] - len(META_ROW_LABELS)}행 × item {df.shape[1] - len(META_COLUMNS)}개")

    tmp = Path(tempfile.mkdtemp(prefix="evalcsv_"))
    try:
        write_outputs(tmp, [(df, summary)], args)
        answer_rows = write_answer_key(tmp, [(df, summary)])
        shutil.copy(tmp / "answer_key.csv", out_csv.with_name(out_csv.stem + "_answer.csv"))
        print(f"      정답표 → {out_csv.with_name(out_csv.stem + '_answer.csv')}")
        if not args.no_verify:
            print("[3/3] 엔진 검증 (2패스) …")
            verify(tmp, answer_rows, args)
            shutil.copy(tmp / "verify.csv", out_csv.with_name(out_csv.stem + "_verify.csv"))
            print(f"      검증표 → {out_csv.with_name(out_csv.stem + '_verify.csv')}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _deliver(files, manifest, args):
    """생성 결과를 세션으로 만든다 — HTTP 업로드(--upload) 또는 로컬 DB 직접(--make-session)."""
    if args.upload:
        print(f"\n[업로드] {args.upload} …")
        res = upload(args.upload, files, manifest)
        print(f"  응답: {json.dumps(res, ensure_ascii=False)[:400]}")
        sid = res.get("session_id")
        if sid:
            print(f"  세션: {args.upload.rstrip('/')}/pe/report/view/{sid}")
    if args.make_session:
        print("\n[세션 생성] 이 PC 의 report DB 에 직접 (서버 프로세스 불필요) …")
        t0 = time.perf_counter()
        res = make_session_local(files, manifest)
        sid = res.get("session_id")
        print(f"  session_id = {sid}  ({time.perf_counter() - t0:.1f}s)")
        print(f"  서버 기동 후: /pe/report/view/{sid}")


if __name__ == "__main__":
    main()

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
LEVEL_KO = {1: "정상범위(미발화)", 2: "임계값 소폭 초과", 3: "초과", 4: "크게 초과", 5: "심각"}

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
    "edge_region_pct": 0.8, "center_region_pct": 0.3, "quadrant_imbalance_warn": 1.0,
    "mean_shift_warn": 0.30, "site_cpk_delta_warn": 0.5, "gross_yield_bad": 0.5,
    "code_edge_hit_warn": 0.05, "kurtosis_warn": 2.0, "spatial_fail_count_min": 5,
    "severe_outlier_count_min": 5, "ring_fail_ratio_warn": 2.0,
    "gradient_norm_warn": 0.3, "subpop_n_min": 50, "subpop_outlier_ratio_max": 0.03,
    "subpop_density_gap_warn": 0.3, "subpop_density_gap_strong": 0.5,
    "subpop_value_gap_warn": 0.3, "subpop_minor_mass_min": 0.05,
}

# 현 룰셋에서 **구조적으로 발화할 수 없는** signature 와 이유 (README §4 와 같은 내용).
UNFIRABLE = {
    "EQUIPMENT_SUSPECT": "raw_df 경로는 site 를 항상 None 으로 채운다(ingest._ingest_raw_df) "
                         "→ site_cpk_delta 가 영구 None",
    "TAIL_RISK": "skewness = (mean-median)/stdev (비모수 왜도)는 수학적 상한이 1.0 이라 "
                 "skew_warn=1.0 을 넘을 수 없다",
    "RING_FAIL": "ring 영역(반경 0.3~0.8)이 die 의 55% 라 ring_fail_ratio 상한이 "
                 "1/0.55≈1.82 — 임계값 2.0 에 도달 불가",
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


def _hist_peaks(v):
    if v.size < 8:
        return None
    hist, _ = np.histogram(v, bins=min(20, max(5, v.size // 5)))
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

def build_wafer(radius: int):
    """반경 radius 안의 정수격자 die 목록 → (x, y, rnorm) 배열."""
    xs, ys = [], []
    for y in range(-radius, radius + 1):
        for x in range(-radius, radius + 1):
            if x * x + y * y <= radius * radius:
                xs.append(x)
                ys.append(y)
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    r = np.sqrt(x ** 2 + y ** 2)
    return x, y, r / r.max()


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
                   share: float, rng: np.random.Generator):
    """free(아직 다른 item 이 안 쓴 행 인덱스)에서 패턴에 맞는 fail 행 count 개 선택."""
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

    edge = rnorm >= TH["edge_region_pct"]
    center = rnorm <= TH["center_region_pct"]
    ring = (~edge) & (~center)

    if pattern in ("edge", "center", "ring", "quadrant"):
        if pattern == "edge":
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

def spec(name, sig, level, *, values, fails=None, unit="V", lsl=LSL, usl=USL, bin_=2,
         metric="", target=None, group="", note="", fail_values="keep", expect=None):
    """test item 1건. expect: 겨냥한 룰이 'fire'(2~5단계) 인지 'not_fire'(1단계) 인지."""
    return {
        "name": name, "intent": list(sig), "level": level, "group": group,
        "expect": expect or ("fire" if level > 1 else "not_fire"),
        "unit": unit, "lsl": lsl, "usl": usl, "bin": bin_,
        "values": values, "fails": fails or {"pattern": "random", "count": 12},
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
SPREAD_T = [0.11, 0.20, 0.30, 0.50, 0.90]          # spread_norm (warn 0.18)
SEVOUT_T = [0.000, 0.060, 0.100, 0.200, 0.330]     # outlier_ratio (bad 0.05)
OUTWARN_T = [0.000, 0.022, 0.030, 0.040, 0.048]    # outlier_ratio (warn 0.02)
# SPEC_TOO_TIGHT 은 spread_norm(<0.18) 로 세기를 매긴다 — 목표 spread 에서 cpk = 1/(6·s)
# 이므로 [1.67, 1.28, 1.11, 0.99, 0.95] 로 함께 내려간다(둘 다 조건이라 한 손잡이로 움직인다).
SPECTIGHT_T = [0.100, 0.130, 0.150, 0.168, 0.175]  # spread_norm (warn 0.18 미만 유지)
LOWCPK_T = [1.50, 1.20, 0.90, 0.60, 0.30]          # cpk
MEANSHIFT_T = [0.15, 0.35, 0.50, 0.70, 0.90]       # |center_bias| (warn 0.30)
BIDIR_T = [1.20, 0.90, 0.60, 0.35, 0.15]           # min spec margin (warn 1.0, 작을수록 나쁨)
KURT_T = [1.0, 2.5, 5.0, 15.0, 40.0]               # excess kurtosis (warn 2.0)
RAIL_T = [0.02, 0.07, 0.15, 0.35, 0.60]            # limit_hit_ratio (warn 0.05)
EDGE_T = [1.0, 2.1, 2.4, 2.65, 2.78]               # edge_fail_ratio (warn 2.0, 상한 2.78)
CENTER_T = [1.0, 2.2, 4.0, 7.0, 10.5]              # center_fail_ratio (warn 2.0, 상한 11)
RING_T = [1.0, 1.4, 1.6, 1.75, 1.82]               # ring_fail_ratio (상한 1.82 < warn 2.0)
CLUSTER_T = [0.5, 1.1, 2.0, 3.0, 3.9]              # quadrant_imbalance (warn 1.0, 상한 4)
GRAD_T = [0.15, 0.35, 0.55, 0.75, 0.95]            # gradient_norm (warn 0.3, radial 상한 1.0)
YIELD_T = [0.90, 0.48, 0.35, 0.15, 0.02]           # yield (bad 0.5)
NSAMPLE_T = [40, 19, 12, 6, 2]                     # n_dut (n_min 20)
SITEDELTA_T = [0.2, 0.6, 1.0, 2.0, 4.0]            # site_cpk_delta (warn 0.5) — 미발화
SKEW_T = [0.05, 0.15, 0.25, 0.35, 0.45]            # |비모수 왜도| (warn 1.0 — 도달 불가)
# SUBPOP 세기는 **모드 간 분리폭(성분 σ 배수)** 으로 매긴다. density_gap 을 목표로 잡으면
# 약한 분리(0.35 등)에서 bimodality_score 가 임계 미달이라 발화 자체가 안 돼 사다리가
# 무너진다(2·3단계가 같은 값으로 수렴). L5 는 3봉(다봉).
SUBPOP_SEP_SD = [0.0, 5.0, 7.0, 10.0, 7.0]         # 모드 간 거리 / 성분 σ
CONST_V = [None, 1.0, 1.25, 1.5, 1.75]             # 2진수로 정확히 표현되는 값만 쓴다


def _quadrant_share(imbalance):
    """quadrant_imbalance 목표 → 1사분면에 몰아줄 fail 비율 s. v=(16s-4)/3."""
    return min(1.0, max(0.25, (3 * imbalance + 4) / 16))


def _wide_plan(lv):
    return {"kind": "normal", "mean": CENTER, "sigma": SPREAD_T[lv] * WIDTH}


def _spike_plan(p, base_sigma=0.02):
    plan = {"kind": "normal", "mean": CENTER, "sigma": base_sigma}
    if p > 0:
        plan["spike"] = {"p": p, "z": 10.0, "neg_p": 0.5}
    return plan


def _sevout_plan(lv, base_sigma=0.02):
    return _spike_plan(SEVOUT_T[lv], base_sigma)


def _outwarn_plan(lv, base_sigma=0.02):
    return _spike_plan(OUTWARN_T[lv], base_sigma)


def _spectight_plan(lv):
    """spread_norm 은 임계값(0.18) **아래**로 두면서 cpk 만 떨어뜨린다 — 두 조건이 모두
    성립해야 발화하므로 목표는 spread_norm 이고, 표본 잡음으로 0.18 을 넘지 않도록 역산한다.
    """
    plan = {"kind": "normal", "mean": CENTER, "sigma": SPECTIGHT_T[lv] * WIDTH}
    return with_tune(plan, lambda pl, s: pl.update(sigma=s),
                     lambda v: m_spread_norm(v, LSL, USL), SPECTIGHT_T[lv],
                     WIDTH * 0.02, WIDTH * 0.5)


def _lowcpk_plan(lv):
    return {"kind": "normal", "mean": CENTER, "sigma": WIDTH / (6 * LOWCPK_T[lv])}


def _meanshift_plan(lv, sigma=0.01):
    return {"kind": "normal", "mean": CENTER - MEANSHIFT_T[lv] * WIDTH / 2, "sigma": sigma}


def _bidir_plan(lv):
    return {"kind": "normal", "mean": CENTER, "sigma": (WIDTH / 2) / BIDIR_T[lv]}


def _heavytail_plan(lv):
    """spike 비율은 outlier warn(2%) 아래로 고정하고 z 를 튜닝해 kurtosis 목표를 맞춘다."""
    return _kurt_plan(KURT_T[lv])


def _kurt_plan(target, p=0.015, sigma=0.05):
    plan = {"kind": "normal", "mean": CENTER, "sigma": sigma,
            "spike": {"p": p, "z": 8.0, "neg_p": 0.5}}
    return with_tune(plan, lambda pl, z: pl["spike"].update(z=z),
                     m_kurtosis, target, 0.5, 60.0)


def _coderail_plan(lv, p=None):
    """CODE 항목 — 레일(0/63) 값이 outlier 컷(4.5 robust σ) 안에 들도록 산포를 잡는다.

    폭이 좁으면 레일 값이 통째로 outlier 로 잡혀 SEVERE_OUTLIER 가, 너무 넓으면
    spread_norm 이 커져 WIDE_DISTRIBUTION 이 대신 뜬다.
    """
    return {"kind": "normal", "mean": 31.5, "sigma": 8.0, "quantize": 1.0,
            "rail": {"p": RAIL_T[lv] if p is None else p}}


def _subpop_plan(lv, sd=None, center=None):
    """이봉/다봉 — density_gap 목표에 맞춰 모드 간 분리폭을 생성 시점에 역산한다.

    좌우 대칭·동일 가중이라 median 이 골 한가운데 떨어져 MAD 가 커진다 → 소수 무리가
    outlier 로 잡혀 SUBPOP 게이트(outlier_ratio<3%)에 걸리는 것을 피한다.
    (가중을 한쪽으로 기울이면 소수 무리가 그대로 outlier 가 되어 발화가 막힌다.)
    """
    sd = sd or 0.05
    mid = CENTER if center is None else center
    if lv == 0:
        return {"kind": "normal", "mean": mid, "sigma": sd}

    def apply(pl, sep):
        # 중심은 **적용 시점의 plan["mean"]** 을 따른다 — 뒤에 온 MEAN_SHIFT 가 옮겨 둔
        # 중심을, 생성 시점 재역산(resolve_tuning)이 원래 자리로 되돌리지 않게.
        c = pl.get("mean", mid)
        if lv == 4:                                # 다봉(3봉)
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

    plan = {"kind": "mixture", "mean": mid, "comps": [(1, mid, sd)]}
    apply(plan, SUBPOP_SEP_SD[lv] * sd)
    # 분리폭은 위에서 이미 정해졌다. 역산은 안전망 — 표본 사정으로 modality 판정이 서지
    # 않으면 ensure 가 분리폭을 키운다(목표는 현재 값 그대로).
    return with_tune(plan, apply, m_density_gap, 0.0, SUBPOP_SEP_SD[lv] * sd,
                     SUBPOP_SEP_SD[lv] * sd, ensure=ok)


def _skew_plan(lv):
    """비모수 왜도(mean-median)/std 목표. 소수쪽 무리를 outlier 로 잡히지 않게 넓게."""
    p, sd = 0.25, 0.13

    def apply(pl, d):
        pl["comps"] = [(1 - p, CENTER - 0.1, sd), (p, CENTER - 0.1 + d, sd)]

    plan = {"kind": "mixture", "comps": [(1, CENTER - 0.1, sd)]}
    return with_tune(plan, apply, m_skew_np, SKEW_T[lv], 0.0, 2.0)


def _constant_plan(lv):
    """상수값 항목. **2진수로 정확히 표현되는 값만** 쓴다 — 1.4 같은 값은 numpy 표본
    표준편차가 2e-16 으로 떠서 `stdev <= 0` 조건이 성립하지 않는다(README §4 부동소수 함정).
    """
    if lv == 0:
        return {"kind": "normal", "mean": CENTER, "sigma": 1e-4}
    return {"kind": "constant", "value": CONST_V[lv]}


def _site_plan(lv):
    """DUT 마다 산포가 다른 항목 — site 가 전달되면 EQUIPMENT_SUSPECT 를 낼 데이터."""
    d = SITEDELTA_T[lv]
    return {"kind": "mixture", "comps": [(1, CENTER, 0.02), (1, CENTER, 0.02 + d * 0.02),
                                         (1, CENTER, 0.02 + d * 0.04), (1, CENTER, 0.02 + d * 0.06)]}


SINGLE_BUILDERS = {
    "WIDE_DISTRIBUTION": lambda lv: (_wide_plan(lv), None, "spread_norm", SPREAD_T[lv], {}),
    "SEVERE_OUTLIER": lambda lv: (_sevout_plan(lv), None, "outlier_ratio", SEVOUT_T[lv], {}),
    "OUTLIER_WARN": lambda lv: (_outwarn_plan(lv), None, "outlier_ratio", OUTWARN_T[lv], {}),
    "SPEC_TOO_TIGHT": lambda lv: (_spectight_plan(lv), None, "spread_norm",
                                  SPECTIGHT_T[lv], {}),
    "LOW_CPK": lambda lv: (_lowcpk_plan(lv), None, "cpk", LOWCPK_T[lv], {}),
    "MEAN_SHIFT": lambda lv: (_meanshift_plan(lv), None, "center_bias", MEANSHIFT_T[lv], {}),
    "BIDIR_TAIL": lambda lv: (_bidir_plan(lv), None, "spec_margin_min", BIDIR_T[lv], {}),
    "HEAVY_TAIL": lambda lv: (_heavytail_plan(lv), None, "kurtosis", KURT_T[lv], {}),
    "SUBPOP_GAP": lambda lv: (_subpop_plan(lv), None, "density_gap", None,
                              {"note": f"모드 분리 {SUBPOP_SEP_SD[lv]}σ"
                                       + (" · 3봉(다봉)" if lv == 4 else "")}),
    "TAIL_RISK": lambda lv: (_skew_plan(lv), None, "skewness", SKEW_T[lv], {}),
    "CONSTANT_VALUE": lambda lv: (_constant_plan(lv), None, "stdev", 0.0, {}),
    "EQUIPMENT_SUSPECT": lambda lv: (_site_plan(lv), None, "site_cpk_delta", SITEDELTA_T[lv], {}),
    "CODE_RAIL": lambda lv: (_coderail_plan(lv), None, "code_edge_hit", RAIL_T[lv],
                             {"unit": "CODE", "lsl": 0.0, "usl": 63.0}),
    "MISSING_LIMIT": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.05}, None, "limit_missing",
        [0, 1, 1, 1, 1][lv],
        [{}, {"usl": None}, {"lsl": None}, {"lsl": None, "usl": None},
         {"lsl": None, "usl": None, "unit": ""}][lv]),
    "LOW_SAMPLE_UNCERTAIN": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.05, "n_valid": NSAMPLE_T[lv]},
        None, "n_dut", NSAMPLE_T[lv], {}),
    "EDGE_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.04},
        {"pattern": "edge", "count": 60, "share": min(1.0, EDGE_T[lv] * (1 - TH["edge_region_pct"] ** 2))},
        "edge_fail_ratio", EDGE_T[lv], {}),
    "CENTER_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.04},
        {"pattern": "center", "count": 60, "share": min(1.0, CENTER_T[lv] * TH["center_region_pct"] ** 2)},
        "center_fail_ratio", CENTER_T[lv], {}),
    "RING_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.04},
        {"pattern": "ring", "count": 60,
         "share": min(1.0, RING_T[lv] * (TH["edge_region_pct"] ** 2 - TH["center_region_pct"] ** 2))},
        "ring_fail_ratio", RING_T[lv], {}),
    "CLUSTER_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.04},
        {"pattern": "quadrant", "count": 80, "share": _quadrant_share(CLUSTER_T[lv])},
        "quadrant_imbalance", CLUSTER_T[lv], {}),
    "WAFER_GRADIENT": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.04},
        {"pattern": "grad_r", "count": 0, "share": GRAD_T[lv]},
        "gradient_norm_abs_max", GRAD_T[lv], {}),
    "GROSS_FAIL": lambda lv: (
        {"kind": "normal", "mean": CENTER, "sigma": 0.05},
        {"pattern": "random", "fraction": 1 - YIELD_T[lv]}, "yield", YIELD_T[lv], {}),
}


def single_specs():
    """단독 세트 — signature 21종 × 5단계."""
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

    if name == "WIDE":
        plan["kind"] = plan.get("kind", "normal")
        plan["sigma"] = SPREAD_T[i] * width
        if plan.get("comps"):                      # 이미 mixture 면 성분 폭을 함께 넓힌다
            plan["comps"] = [(w, mu, SPREAD_T[i] * width) for w, mu, _sd in plan["comps"]]
    elif name in ("SEVOUT", "OUTWARN"):
        p = (SEVOUT_T if name == "SEVOUT" else OUTWARN_T)[i]
        if p > 0:
            plan["spike"] = {"p": p, "z": 10.0, "neg_p": 0.5}
    elif name == "SPECTIGHT":
        plan["sigma"] = SPECTIGHT_T[i] * width
        with_tune(plan, lambda pl, s: pl.update(sigma=s), lambda v: m_spread_norm(v, lo, hi),
                  SPECTIGHT_T[i], width * 0.02, width * 0.5)
    elif name == "MEANSHIFT":
        _shift(center - MEANSHIFT_T[i] * width / 2 - plan.get("mean", center))
    elif name == "HEAVYTAIL":
        base = _kurt_plan(KURT_T[i], sigma=plan.get("sigma", 0.05))
        plan["spike"] = base["spike"]
        plan.setdefault("sigma", base["sigma"])
        plan.setdefault("tune", []).extend(base["tune"])   # 앞 재료의 역산을 지우지 않는다
    elif name == "SUBPOP":
        # 앞선 재료(WIDE 등)가 정한 산포를 모드 폭으로, 현재 중심을 모드 중심으로 물려받는다.
        plan.update(_subpop_plan(i, sd=plan.get("sigma", 0.05),
                                 center=plan.get("mean", center)))
    elif name == "CODERAIL":
        meta.update(unit="CODE", lsl=0.0, usl=63.0)
        plan.update(_coderail_plan(i))
    elif name == "CONSTANT":
        plan.clear()
        plan.update(_constant_plan(i))
    elif name == "MISSLIMIT":
        meta.update(lsl=None if i >= 2 else LSL, usl=None if i >= 1 else USL)
    elif name == "EDGE":
        fails.update(pattern="edge", count=60,
                     share=min(1.0, EDGE_T[i] * (1 - TH["edge_region_pct"] ** 2)))
    elif name == "CENTER":
        fails.update(pattern="center", count=60,
                     share=min(1.0, CENTER_T[i] * TH["center_region_pct"] ** 2))
    elif name == "CLUSTER":
        fails.update(pattern="quadrant", count=80, share=_quadrant_share(CLUSTER_T[i]))
    elif name == "GRAD":
        fails.update(pattern="grad_r", count=0, share=GRAD_T[i])
    elif name == "GROSS":
        fails.update(pattern="random", fraction=1 - YIELD_T[i])
    else:
        raise KeyError(name)


ING_SIG = {"WIDE": "WIDE_DISTRIBUTION", "SEVOUT": "SEVERE_OUTLIER",
           "OUTWARN": "OUTLIER_WARN", "SPECTIGHT": "SPEC_TOO_TIGHT",
           "MEANSHIFT": "MEAN_SHIFT", "HEAVYTAIL": "HEAVY_TAIL", "SUBPOP": "SUBPOP_GAP",
           "CODERAIL": "CODE_RAIL", "CONSTANT": "CONSTANT_VALUE",
           "MISSLIMIT": "MISSING_LIMIT", "EDGE": "EDGE_FAIL", "CENTER": "CENTER_FAIL",
           "CLUSTER": "CLUSTER_FAIL", "GRAD": "WAFER_GRADIENT", "GROSS": "GROSS_FAIL"}

# 재료 적용 순서 — 이름을 준 순서와 무관하게 이 순서로 얹는다. 순서에 따라 뒤 재료가 앞
# 재료의 결과를 지우면(예: 산포를 정한 뒤 mixture 로 갈아끼우면) 둘 다 미발화가 된다.
ING_ORDER = {"CONSTANT": 0, "CODERAIL": 1, "WIDE": 2, "SPECTIGHT": 2, "SUBPOP": 3,
             "MEANSHIFT": 4, "SEVOUT": 5, "OUTWARN": 5, "HEAVYTAIL": 5, "MISSLIMIT": 6,
             "EDGE": 7, "CENTER": 7, "CLUSTER": 7, "GRAD": 7, "GROSS": 7}
# fail 행 배치는 item 당 하나만 고를 수 있다 (패턴이 서로 덮어쓴다).
FAIL_ING = {"EDGE", "CENTER", "CLUSTER", "GRAD", "GROSS"}

COMBOS = [
    ("WIDE", "SEVOUT"), ("WIDE", "SUBPOP"), ("MEANSHIFT", "OUTWARN"),
    ("MEANSHIFT", "HEAVYTAIL"), ("EDGE", "SEVOUT"), ("CENTER", "WIDE"),
    ("CLUSTER", "MEANSHIFT"), ("SPECTIGHT", "EDGE"), ("CODERAIL", "MEANSHIFT"),
    ("SUBPOP", "CENTER"), ("HEAVYTAIL", "CLUSTER"), ("MISSLIMIT", "CLUSTER"),
    ("CONSTANT", "EDGE"), ("GROSS", "WIDE"),
    ("WIDE", "SEVOUT", "EDGE"), ("MEANSHIFT", "HEAVYTAIL", "CLUSTER"),
    ("SUBPOP", "WIDE", "CENTER"),
]


def combo_spec(names, lv, group="combo", seq=None):
    plan = {"kind": "normal", "mean": CENTER, "sigma": 0.03}
    fails = {"pattern": "random", "count": 12}
    meta = {"unit": "V", "lsl": LSL, "usl": USL, "bin_": 2}
    for nm in sorted(names, key=lambda n: ING_ORDER[n]):
        _ing(nm, lv, plan, fails, meta)
    label = "-".join(names)
    name = f"MIX{len(names)}_{label}_L{lv}" + (f"_{seq:03d}" if seq is not None else "")
    return spec(name, [ING_SIG[n] for n in names], lv, values=plan, fails=fails,
                metric="(조합)", target=None, group=group, **meta)


def combo_specs():
    return [combo_spec(names, lv) for names in COMBOS for lv in LEVELS]


# ── 축 변형 세트 ─────────────────────────────────────────────────────────────
# UNIT 원문 → 엔진 value_type. 마지막 둘은 "표에 없는 단위" 경로 확인용 —
# 엔진 UNIT_TO_VALUE_TYPE 은 정확일치 표라 모르는 표기는 조용히 PF(무판정)로 떨어진다.
UNIT_AXIS = [("v", "V"), ("ma", "A"), ("khz", "Hz"), ("ohm", "Ohm"), ("ms", "Sec"),
             ("CODE", "CODE"), ("PCT", "PCT"), ("", "PF"), ("DEGC", "PF(오분류)")]
AXIS_SCENARIOS = ["WIDE", "SEVOUT", "SPECTIGHT", "MEANSHIFT"]
BIN_AXIS = [3, 4, 5, 8, 18, 31]


def axis_specs():
    out = []
    # value_type 축 — 같은 시나리오를 UNIT 만 바꿔 룰 스코프(item_class)를 흔든다.
    for unit_raw, vt in UNIT_AXIS:
        for scen in AXIS_SCENARIOS:
            for lv in (2, 4):
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
        for scen in ("WIDE", "SEVOUT", "SUBPOP"):
            for lv in (2, 4):
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
    ("spread_norm", TH["spread_norm_warn"], [0.170, 0.176, 0.179, 0.181, 0.186, 0.200],
     lambda t: with_tune({"kind": "normal", "mean": CENTER, "sigma": t * WIDTH},
                         lambda pl, s: pl.update(sigma=s),
                         lambda v: m_spread_norm(v, LSL, USL), t, WIDTH * 0.02, WIDTH),
     "WIDE_DISTRIBUTION"),
    ("outlier_ratio_warn", TH["outlier_ratio_warn"], [0.014, 0.018, 0.0198, 0.021, 0.025, 0.030],
     lambda t: {"kind": "normal", "mean": CENTER, "sigma": 0.02,
                "spike": {"p": t, "z": 10.0, "neg_p": 0.5}}, "OUTLIER_WARN"),
    ("outlier_ratio_bad", TH["outlier_ratio_bad"], [0.040, 0.046, 0.049, 0.052, 0.060, 0.075],
     lambda t: {"kind": "normal", "mean": CENTER, "sigma": 0.02,
                "spike": {"p": t, "z": 10.0, "neg_p": 0.5}}, "SEVERE_OUTLIER"),
    ("cpk_warn", TH["cpk_warn"], [1.45, 1.38, 1.34, 1.32, 1.25, 1.10],
     # 산포(sigma)를 손잡이로 두고 -cpk 를 목표로 잡는다 — sigma 가 커질수록 -cpk 가
     # 커지므로(단조 증가) 이분법이 성립한다.
     lambda t: with_tune({"kind": "normal", "mean": CENTER, "sigma": WIDTH / (6 * t)},
                         lambda pl, s: pl.update(sigma=s),
                         lambda v: -(m_cpk(v, LSL, USL) or 0), -t, WIDTH * 0.02, WIDTH),
     "SPEC_TOO_TIGHT"),
    ("center_bias", TH["mean_shift_warn"], [0.26, 0.285, 0.298, 0.305, 0.33, 0.40],
     lambda t: {"kind": "normal", "mean": CENTER - t * WIDTH / 2, "sigma": 0.01}, "MEAN_SHIFT"),
    ("kurtosis", TH["kurtosis_warn"], [1.6, 1.85, 1.97, 2.05, 2.3, 3.0],
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
            out.append(spec(f"EDGEC_{metric.replace('_', '')}_{i}", [sig], 3 if worse else 1,
                            values=build(t), metric=metric, target=t, group="boundary",
                            expect="fire" if worse else "not_fire",
                            note=f"임계값 {th} 대비 {'초과' if worse else '이내'}", **over))
    # 부동소수 함정 — 같은 '상수값' 인데 2진수 표현 가능 여부로 CONSTANT_VALUE 판정이 갈린다.
    for value, fire in ((1.25, True), (1.5, True), (1.4, False), (1.8, False)):
        out.append(spec(f"EDGEC_constant_{str(value).replace('.', 'p')}", ["CONSTANT_VALUE"],
                        3 if fire else 1, values={"kind": "constant", "value": value},
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
                                "sigma": sigma},
                        fails={"pattern": "random", "count": rng.randint(5, 20)},
                        unit=unit_raw, bin_=rng.choice([2, 3, 4]),
                        metric="spread_norm", target=round(sigma / WIDTH, 4),
                        group="normal", note="발화 0건 기대(오탐 검사)"))
    return out


MIX_POOL = ["WIDE", "SEVOUT", "OUTWARN", "SPECTIGHT", "MEANSHIFT", "HEAVYTAIL", "SUBPOP",
            "EDGE", "CENTER", "CLUSTER"]

# **동시에 성립할 수 없는 재료 쌍** — 룰 조건이 서로 배타적이라 둘 다 발화시킬 수 없다.
#   SPECTIGHT 는 "좁고(spread<0.18)·중앙(|bias|<0.3)·단봉(BC<임계)인데 cpk 낮음" 이므로
#   WIDE/MEANSHIFT/SUBPOP 과 정면 충돌한다. SUBPOP 은 outlier_ratio<3% 게이트가 있어
#   outlier·heavy tail 계열과 같이 못 간다. OUTWARN 은 SEVOUT 에 억제된다(suppressed_by).
INCOMPATIBLE = [{"SPECTIGHT", "WIDE"}, {"SPECTIGHT", "MEANSHIFT"}, {"SPECTIGHT", "SUBPOP"},
                {"SUBPOP", "SEVOUT"}, {"SUBPOP", "OUTWARN"}, {"SUBPOP", "HEAVYTAIL"},
                {"SEVOUT", "OUTWARN"},
                # spike 손잡이(비율·크기)가 하나뿐이라 서로 덮어쓴다. 게다가 outlier 룰은
                # kurtosis 를 끌어올려 HEAVY_TAIL 을 어차피 동반발화시킨다.
                {"SEVOUT", "HEAVYTAIL"}, {"OUTWARN", "HEAVYTAIL"}]


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
        lv = rng.choice([2, 3, 4, 5])
        s = combo_spec(names, lv, group="mixed", seq=i)
        s["bin"] = rng.choice([2, 3, 4, 8])
        if set(names) <= FAIL_ING:
            # 현실형(fail chip 값을 limit 밖으로)은 **공간·수율 항목에만** 준다.
            # 값 분포를 겨냥한 항목에 주면 spec 밖 값들이 평균·값 범위를 흔들어
            # (히스토그램 구간이 넓어져) SUBPOP·SPEC_TOO_TIGHT 판정을 깨뜨린다.
            s["fail_values"] = "over_limit"
        out.append(s)
    return out


def build_catalog(total: int, seed: int):
    rng = random.Random(seed)
    specs = single_specs() + combo_specs() + axis_specs() + boundary_specs()
    fixed = len(specs)
    rest = max(0, total - fixed)
    n_normal = min(rest, max(0, int(round(rest * 0.4))))
    specs += normal_specs(n_normal, rng)
    specs += mixed_specs(rest - n_normal, rng)
    specs = specs[:total] if total < fixed else specs
    for i, s in enumerate(specs):                  # 이름 유일성 보장 + 제외규칙 회피
        s["name"] = f"{s['name']}_{i:03d}"
        assert "_CODE_" not in s["name"].upper(), f"exclusions.yaml 에 걸리는 이름: {s['name']}"
    return specs


# ──────────────────────────────────────────────────────────────────────────────
# source 로 패킹 → honeyform DataFrame
# ──────────────────────────────────────────────────────────────────────────────

def fail_budget(s, n_rows):
    f = s["fails"]
    if f.get("fraction"):
        return int(round(f["fraction"] * n_rows))
    if f.get("pattern") == "grad_r":
        return int(round(f.get("share", 0.3) * 0.67 * n_rows))
    if f.get("pattern") == "grad_x":
        return int(round(f.get("share", 0.3) * n_rows))
    return int(f.get("count", 12))


SPATIAL_PATTERNS = {"edge", "center", "ring", "quadrant", "grad_r", "grad_x"}


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


def build_source_df(specs, wafer, seed):
    """한 source 의 honeyform DataFrame + item 별 실측 요약 리스트."""
    x, y, rnorm = wafer
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

    for tno, s in enumerate(specs, start=1):
        lsl, usl = s["lsl"], s["usl"]
        # item 마다 고정 seed — 배치 순서가 바뀌어도 값이 재현되고, 튜닝(역산)한 파라미터가
        # 최종 생성값과 정확히 같은 난수열에서 나온다.
        item_seed = (seed * 1000003 + tno * 7919) % (2 ** 32)
        values = synth_values(resolve_tuning(s["values"], n, item_seed, lsl, usl), n,
                              np.random.default_rng(item_seed), lsl, usl)

        f = s["fails"]
        want = fail_budget(s, n)
        free_idx = np.flatnonzero(free)
        if f.get("pattern") == "grad_r" or f.get("pattern") == "grad_x":
            fail_idx = pick_fail_rows(f["pattern"], want, free_idx, rnorm, xn, yn,
                                      f.get("share", 0.3), rng)
        else:
            fail_idx = pick_fail_rows(f.get("pattern", "random"), want, free_idx,
                                      rnorm, xn, yn, f.get("share", 1.0), rng)
        if fail_idx.size == 0 and free_idx.size:   # 모든 item 은 fail 1개 이상이어야 한다
            fail_idx = rng.choice(free_idx, size=1, replace=False)

        free[fail_idx] = False
        bin_col[fail_idx] = int(s["bin"])
        failtno_col[fail_idx] = tno

        if s["fail_values"] == "over_limit" and usl is not None and lsl is not None:
            span = usl - lsl
            values[fail_idx] = usl + 0.02 * span * (1 + rng.random(fail_idx.size))

        v_ok = values[np.isfinite(values)]
        summary.append({
            "spec": s, "tno": tno, "fail_count": int(fail_idx.size),
            "n_dut": int(v_ok.size),
            "actual": measure(v_ok, lsl, usl, fail_idx, n, x, y, rnorm, xn, yn),
        })

        cols[s["name"]] = ["" if not np.isfinite(v) else f"{v:.6g}" for v in values]
        rows_meta["TSEQ"][s["name"]] = str(tno)
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
        "XPOS": [str(int(v)) for v in x],
        "YPOS": [str(int(v)) for v in y],
        "BIN": [str(int(b)) for b in bin_col],
        "FAILTNO": ["" if t == 0 else str(int(t)) for t in failtno_col],
    }
    body.update(cols)
    df = pd.concat([pd.DataFrame(head, columns=META_COLUMNS + item_names),
                    pd.DataFrame(body, columns=META_COLUMNS + item_names)],
                   ignore_index=True)
    return df, summary


def measure(v, lsl, usl, fail_idx, n_rows, x, y, rnorm, xn, yn):
    """생성된 값·fail 배치의 실측 지표 (정답표 기록용)."""
    out = {"n_dut": int(v.size), "fail_count": int(fail_idx.size),
           "yield": round(1 - fail_idx.size / n_rows, 4)}
    if v.size >= 2:
        out.update(
            spread_norm=_r(m_spread_norm(v, lsl, usl)), outlier_ratio=_r(m_outlier_ratio(v)),
            cpk=_r(m_cpk(v, lsl, usl)), center_bias=_r(m_center_bias(v, lsl, usl)),
            kurtosis=_r(m_kurtosis(v)), skewness=_r(m_skew_np(v)),
            bimodality=_r(m_bimodality(v)), density_gap=_r(m_density_gap(v)),
            modality_v2=m_modality_v2(v), limit_hit=_r(m_limit_hit(v, lsl, usl)),
            stdev=_r(m_std(v)),
            spec_margin_min=_r(_spec_margin_min(v, lsl, usl)),
            limit_missing=int(lsl is None or usl is None))
    if fail_idx.size:
        fm = np.zeros(n_rows, dtype=bool)
        fm[fail_idx] = True
        overall = fm.mean()
        edge, center = rnorm >= TH["edge_region_pct"], rnorm <= TH["center_region_pct"]
        ring = (~edge) & (~center)
        out["edge_fail_ratio"] = _r(fm[edge].mean() / overall) if edge.any() else None
        out["center_fail_ratio"] = _r(fm[center].mean() / overall) if center.any() else None
        out["ring_fail_ratio"] = _r(fm[ring].mean() / overall) if ring.any() else None
        rates = []
        for sx in (True, False):
            for sy in (True, False):
                q = ((x >= 0) == sx) & ((y >= 0) == sy)
                if q.any():
                    rates.append(fm[q].mean())
        mean_rate = float(np.mean(rates)) if rates else 0.0
        out["quadrant_imbalance"] = _r((max(rates) - min(rates)) / mean_rate) if mean_rate else None
        out["gradient_norm_abs_max"] = _r(max(abs(g) for g in
                                              (_grad(rnorm, fm), _grad(xn, fm), _grad(yn, fm))
                                              if g is not None))
    return out


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
                      "edge_fail_ratio": "edge_fail_ratio_warn",
                      "center_fail_ratio": "edge_fail_ratio_warn",
                      "ring_fail_ratio": "ring_fail_ratio_warn",
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
    필요하다(예: SPEC_TOO_TIGHT 가 뜨면 LOW_CPK 는 억제된다).
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
    for r in answer_rows:
        want = [s for s in r["intent"].split(";") if s]
        want_fireable = [s for s in want if s not in UNFIRABLE]
        fired_live = live["signatures"].get(r["item"], [])
        fired_all = full["signatures"].get(r["item"], [])
        suppressed = []
        if r["expect"] == "fire":
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
        unexpected = [s for s in fired_all if s not in want and s != UNKNOWN_ID]
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
    if cofire:
        top = sorted(cofire.items(), key=lambda kv: -kv[1])[:8]
        print("  동반발화 상위: " + " | ".join(f"{k}×{v}" for k, v in top))
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
    ap.add_argument("--radius", type=int, default=28,
                    help="웨이퍼 반경(die) — 행 수 ≈ πr² (기본 28 → 약 2460행)")
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
    ap.add_argument("--_eval-dump", default="", help=argparse.SUPPRESS)   # 내부 자식 모드
    args = ap.parse_args()

    out_dir = Path(args.out)
    if getattr(args, "_eval_dump"):
        run_eval_pass(out_dir, args, Path(getattr(args, "_eval_dump")))
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

    print(f"[1/4] item 카탈로그 생성 (목표 {args.items}개) …")
    specs = build_catalog(args.items, args.seed)
    groups: dict[str, int] = {}
    for s in specs:
        groups[s["group"]] = groups.get(s["group"], 0) + 1
    print(f"      item {len(specs)}개 — " + ", ".join(f"{k} {v}" for k, v in groups.items()))

    print(f"[2/4] source 패킹 (source 당 {n_rows}행, fail 행은 item 간 배타) …")
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

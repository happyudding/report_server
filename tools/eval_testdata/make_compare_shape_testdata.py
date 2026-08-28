"""Compare 산포 **모양** 차이 데이터 생성기 (before/after 쌍) — v2, 2026-08-28.

    server\\.venv\\Scripts\\python.exe tools\\eval_testdata\\make_compare_shape_testdata.py

산출물 4종 (기본 ``data/`` 아래):
    compare_shape_v2_before.csv   7-meta honeyform (Before)  — 약 1000 die × 202 item
    compare_shape_v2_after.csv    7-meta honeyform (After)
    compare_shape_v2_answer.csv   정답표 (item 별 유형·레벨·눈으로 본 차이 설명)
    compare_shape_v2_verify.csv   **서버 코드로 직접 돌린** 현행 지표 실측 + 미검출/과검출 판정

## v1(make_compare_testdata.py) 과 목적이 정반대다

v1 은 "현행 임계(Cpk<1.33 · |Δσ%|≥15%)가 기대대로 도는가"를 확인하는 **회귀 검증**용이라
검출되도록 설계된 데이터였다. 이 v2 는 그 임계 자체를 **다시 잡기 위한** 데이터다 —

    ① 눈으로 보면 명백히 다른데 현행 두 지표로는 **안 잡히는** 경우 (미검출)
    ② 눈으로 보면 차이가 없거나 오히려 좋아졌는데 **잡히는** 경우 (과검출)

를 동시에 최대한 많이 만든다.

그래서 **레벨(L1~L4)은 임계 초과 정도가 아니라 "눈으로 본 차이의 크기"** 다:

    L1 = 차이 거의 없음        L2 = 자세히 보면 다름
    L3 = 확실히 다름           L4 = 누가 봐도 완전히 다름

현행 검출 여부와 레벨은 **독립**이며, 그 어긋남이 곧 결론이다:

    blind_spot  = level≥3 인데 미검출  → 새 지표가 잡아야 할 목록
    over_detect = level≤2 인데 검출    → 현행 임계가 과하게 무는 목록

## 미검출(blind spot) 을 만드는 원리

현행 두 지표는 **모멘트 2개(μ, σ)만** 본다. 따라서 μ 와 σ 를 before/after 동일하게 고정한
채 **모양만** 바꾸면 Cpk% ≈ 100%, Δσ% ≈ 0% 인데 분포는 전혀 다른 항목이 만들어진다.
`_rescale` 이 표본 모멘트를 목표값으로 정확히 되돌려 이 고정을 보장한다 — 난수 노이즈로
σ 가 몇 % 흔들리면 미검출 케이스가 우연히 검출돼 버린다.

## 과검출(over detect) 을 만드는 원리

현행 판정(`compare._dist_focus`)의 구조적 약점 6가지를 각각 겨눈다:

    FP_LOWCPK_NOCHANGE  ② 경로(한쪽 Cpk<1.33)에는 **유의성 게이트가 없다** —
                          before/after 가 사실상 같아도 원래 Cpk 가 낮으면 무조건 검출
    FP_LOWCPK_IMPROVED  나빴다가 좋아졌는데 아직 1.33 밑이면 여전히 검출 (개선을 이슈로 봄)
    FP_SD_IMPROVED      |Δσ%| 가 **절대값**이라 산포가 줄어도(=개선) 검출
    FP_TINY_SIGMA       σ 가 아주 작으면 절대 변화는 무의미한데 **비율만** 커서 검출
    FP_DISCRETE_JITTER  이산 계단이 σ 보다 굵어 경계 die 이동만으로 σ 비율이 흔들림
    FP_OUTLIER_FEW      die 약 2% 의 이탈만으로 σ 급등 (분포 본체는 동일)

참고용으로 KS D / IQR 증가율 / median shift 도 실측표에 함께 싣는다(현행 payload 에 이미
있으나 **판정에는 안 쓰이는** 지표들이다 — 새 기준 후보로 얼마나 유효한지 보려는 것).

## 두 가지 결정적 제약 (v1 과 동일 — 어기면 데이터가 무의미해진다)

1. **모집단은 Bin1(양품) die 뿐이다** (`compare._bin1_frame`). 규격 밖 값을 만들면 그 die 가
   통계에서 빠져 "만든 분포"와 "보이는 분포"가 달라진다. 그래서 전 값을 spec 안으로
   클립하고 전 die 를 Bin1 로 둔다.
2. **XPOS/YPOS 는 항상 양수** (CLAUDE.md 규칙 #9).

pytest 미사용 — 생성 직후 서버 코드(`build_dist_shift`/`build_cpk_rows`)를 **그대로 호출해**
현행 지표를 실측한다(재구현 검증 금지 — 규칙 #13).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from web_report.honeyform import META_COLUMNS, META_ROW_LABELS, split_honeyform  # noqa: E402

try:                                              # 한국어 Windows 콘솔(cp949) 깨짐 방지
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── 공통 규격 ────────────────────────────────────────────────────────────────
LSL, USL = 0.0, 10.0          # 전 항목 공통 spec
CENTER = 5.0
UNIT = "V"
STEP = "P2"

# 현행 검출 임계 — compare.py 상수와 같아야 한다. **여기서 다시 판정하지 않는다**(표기용).
TH_CPK_LOW = 1.33
TH_STDEV_DELTA_PCT = 15.0
TH_ALPHA = 0.05
TH_CPK_HIGH = 100.0

LEVELS = (1, 2, 3, 4)
LEVEL_KO = {
    1: "L1 차이 거의 없음",
    2: "L2 자세히 보면 다름",
    3: "L3 확실히 다름",
    4: "L4 누가 봐도 완전히 다름",
}

# 기본 σ — Cpk 1.60 (= 여유 있는 정상 항목). Cpk = min(USL-μ, μ-LSL) / (3σ)
BASE_SD = (USL - CENTER) / (3.0 * 1.60)

WAFER_RADIUS = 18            # die ≈ 1009 (요청: raw 각 1000개)


# ── 값 합성 도구 ─────────────────────────────────────────────────────────────

def _rescale(v, mean, sd):
    """표본의 실제 평균·표준편차를 목표값으로 정확히 맞춘다(모양 보존).

    미검출 데이터의 핵심 장치다 — 이게 없으면 σ 가 난수 노이즈로 흔들려
    "μ·σ 동일, 모양만 다름" 케이스가 우연히 Δσ% 임계를 넘어 검출돼 버린다.
    """
    v = np.asarray(v, dtype=float)
    cur_sd = v.std(ddof=1)
    if cur_sd == 0:
        return np.full(v.size, mean, dtype=float)
    return (v - v.mean()) / cur_sd * sd + mean


def _quantize(v, step):
    """이산화 — 연속값을 step 격자에 올린다(code unit 계측기 모사).

    ⚠ 이산화는 σ 를 미세하게 바꾸므로 **호출부가 이산화 후 다시 _rescale 하지 않는다**
    (다시 하면 격자가 깨져 이산이 아니게 된다).
    """
    return np.round(np.asarray(v, dtype=float) / step) * step


def _clip_spec(v, lsl=LSL, usl=USL):
    """규격 안으로 클립 — 전 die 를 Bin1 로 유지(모집단 불변, 위 제약 ①)."""
    pad = (usl - lsl) * 0.001
    return np.clip(v, lsl + pad, usl - pad)


# ── 산포 모양 생성기 ─────────────────────────────────────────────────────────
# 각 함수는 (rng, n, level) → 표준화된 모양(평균 0·σ 1 근처)을 돌려준다.
# 최종 μ·σ 는 호출부의 _rescale 이 맞춘다. level 이 클수록 모양 차이가 커진다.

def _sh_normal(rng, n, level):
    """정규 — 기준 모양."""
    return rng.normal(0.0, 1.0, n)


def _sh_bimodal(rng, n, level):
    """쌍봉 — 두 무리로 갈라진다. level 이 클수록 봉우리 간격이 넓고 각 봉이 좁다."""
    gap = {1: 0.35, 2: 0.75, 3: 1.15, 4: 1.45}[level]
    inner = {1: 0.95, 2: 0.72, 3: 0.45, 4: 0.22}[level]
    half = n // 2
    a = rng.normal(-gap, inner, half)
    b = rng.normal(+gap, inner, n - half)
    return np.concatenate([a, b])


def _sh_bimodal_skew(rng, n, level):
    """비대칭 쌍봉 — 한쪽 봉이 작다(소수 모드 혼입). 소수 비율이 level 로 커진다."""
    frac = {1: 0.05, 2: 0.10, 3: 0.18, 4: 0.28}[level]
    gap = {1: 0.6, 2: 1.2, 3: 1.8, 4: 2.4}[level]
    k = max(1, int(n * frac))
    main = rng.normal(0.0, 1.0, n - k)
    minor = rng.normal(gap, 0.35, k)
    return np.concatenate([main, minor])


def _sh_trimodal(rng, n, level):
    """삼봉 — 세 무리(예: 3개 테스터/사이트 혼입)."""
    gap = {1: 0.3, 2: 0.7, 3: 1.1, 4: 1.5}[level]
    inner = {1: 0.9, 2: 0.6, 3: 0.35, 4: 0.18}[level]
    third = n // 3
    a = rng.normal(-gap, inner, third)
    b = rng.normal(0.0, inner, third)
    c = rng.normal(+gap, inner, n - 2 * third)
    return np.concatenate([a, b, c])


def _sh_tail_up(rng, n, level):
    """위쪽 한쪽 꼬리 — 소수 die 가 위로 늘어진다."""
    frac = {1: 0.02, 2: 0.04, 3: 0.07, 4: 0.12}[level]
    push = {1: 1.2, 2: 2.2, 3: 3.4, 4: 4.8}[level]
    v = rng.normal(0.0, 1.0, n)
    k = max(1, int(n * frac))
    idx = rng.choice(n, size=k, replace=False)
    v[idx] += rng.uniform(push * 0.6, push, k)
    return v


def _sh_tail_down(rng, n, level):
    """아래쪽 한쪽 꼬리 — 위와 방향만 반대.

    꼬리 방향 반전은 μ·σ 가 같아도 완전히 다른 분포다(현행 지표가 원리적으로 못 본다).
    """
    return -_sh_tail_up(rng, n, level)


def _sh_tail_both(rng, n, level):
    """양쪽 꼬리 — 좌우 대칭으로 늘어진다(σ 재정규화 후 몸통이 오히려 좁아진다)."""
    frac = {1: 0.02, 2: 0.04, 3: 0.07, 4: 0.11}[level]
    push = {1: 1.2, 2: 2.2, 3: 3.4, 4: 4.6}[level]
    v = rng.normal(0.0, 1.0, n)
    k = max(1, int(n * frac))
    idx = rng.choice(n, size=2 * k, replace=False)
    v[idx[:k]] += rng.uniform(push * 0.6, push, k)
    v[idx[k:]] -= rng.uniform(push * 0.6, push, k)
    return v


def _sh_uniform(rng, n, level):
    """균등 — 몸통이 평평하다(정규와 σ 가 같아도 모양이 전혀 다르다)."""
    w = {1: 0.35, 2: 0.65, 3: 0.85, 4: 1.0}[level]
    u = rng.uniform(-1.0, 1.0, n)
    g = rng.normal(0.0, 1.0, n)
    return w * u * 1.732 + (1.0 - w) * g          # w=1 이면 완전 균등


def _sh_plateau(rng, n, level):
    """고원형 — 가운데가 눌린 분포(중심에서 값이 빠져나감). level 이 크면 중앙이 비어 U 자."""
    dip = {1: 0.15, 2: 0.35, 3: 0.6, 4: 0.85}[level]
    v = rng.normal(0.0, 1.0, n)
    core = np.abs(v) < 0.6
    k = int(core.sum() * dip)
    if k > 0:
        idx = np.flatnonzero(core)[:k]
        v[idx] = np.sign(rng.normal(0.0, 1.0, k)) * rng.uniform(0.9, 1.6, k)
    return v


def _sh_skew_right(rng, n, level):
    """우측 왜곡 — 로그정규형. 몸통이 왼쪽에 몰리고 오른쪽으로 완만히 늘어진다."""
    s = {1: 0.15, 2: 0.35, 3: 0.6, 4: 0.9}[level]
    return np.exp(rng.normal(0.0, s, n))


def _sh_skew_left(rng, n, level):
    """좌측 왜곡 — 위 왜곡의 거울상."""
    return -_sh_skew_right(rng, n, level)


def _sh_outlier_cluster(rng, n, level):
    """이탈 무리 — 소수 die 가 몸통에서 멀찍이 떨어진 한 덩어리로 존재."""
    frac = {1: 0.01, 2: 0.02, 3: 0.04, 4: 0.07}[level]
    away = {1: 1.8, 2: 2.8, 3: 3.8, 4: 5.0}[level]
    v = rng.normal(0.0, 1.0, n)
    k = max(1, int(n * frac))
    idx = rng.choice(n, size=k, replace=False)
    v[idx] = away + rng.normal(0.0, 0.12, k)
    return v


def _sh_spike(rng, n, level):
    """스파이크 — 특정 한 값에 다수가 몰린다(클램프·포화 계측 모사)."""
    frac = {1: 0.05, 2: 0.15, 3: 0.30, 4: 0.50}[level]
    v = rng.normal(0.0, 1.0, n)
    k = max(1, int(n * frac))
    idx = rng.choice(n, size=k, replace=False)
    v[idx] = 0.0
    return v


def _sh_truncated(rng, n, level):
    """한쪽 절단 — 어떤 값 위쪽이 잘려 벽이 생긴다(스크리닝·리트라이 모사)."""
    cut = {1: 2.0, 2: 1.3, 3: 0.8, 4: 0.4}[level]
    v = rng.normal(0.0, 1.0, n)
    return np.minimum(v, cut)


def _sh_granular(rng, n, level):
    """거친 이산 — 몸통은 같은데 값 격자가 성기다.

    ⚠ 여기서는 모양만 만들고, 실제 이산화는 호출부(_shape_values)가 스펙의 quant 로 한다.
    """
    return rng.normal(0.0, 1.0, n)


def _sh_outlier_far(rng, n, level):
    """극소수(2~5 die) 원거리 이탈 — **본체는 그대로**인데 σ 만 뛴다.

    과검출 케이스 전용. σ 증가율 ≈ √(1 + k·away²/n) − 1 이라 n≈1000 에서 15% 를 넘기려면
    k·away² ≳ 330 이 필요하다.

    ⚠ 이탈 거리만 키워서는 안 된다 — 값이 규격(0~10) 밖으로 나가면 `_clip_spec` 이 잘라
    실제 이탈 폭이 σ 의 약 4.8배로 묶이고, σ 는 3% 밖에 안 오른다(실측). 그래서 거리는
    규격 경계까지만 두고 **die 수(약 2%)** 로 k·away² 를 채운다. 2% 는 여전히 "본체는
    그대로인데 소수 die 때문에 σ 가 뛴다" 는 성격을 유지하는 비율이다.

    ⚠ 이 모양만은 _rescale 로 σ 를 되돌리면 안 된다(되돌리면 만들려는 현상이 사라진다) —
    _shape_values 가 예외 처리한다.
    """
    k = max(2, int(n * {1: 0.016, 2: 0.022}.get(level, 0.022)))
    away = 4.6                      # σ 단위 — 규격 경계(중심에서 4.8σ) 바로 안쪽
    v = rng.normal(0.0, 1.0, n)
    idx = rng.choice(n, size=k, replace=False)
    v[idx] = np.sign(rng.normal(0.0, 1.0, k)) * away
    return v


SHAPE_FUNCS = {
    "normal": _sh_normal,
    "bimodal": _sh_bimodal,
    "bimodal_skew": _sh_bimodal_skew,
    "trimodal": _sh_trimodal,
    "tail_up": _sh_tail_up,
    "tail_down": _sh_tail_down,
    "tail_both": _sh_tail_both,
    "uniform": _sh_uniform,
    "plateau": _sh_plateau,
    "skew_right": _sh_skew_right,
    "skew_left": _sh_skew_left,
    "outlier_cluster": _sh_outlier_cluster,
    "spike": _sh_spike,
    "truncated": _sh_truncated,
    "granular": _sh_granular,
    "outlier_far": _sh_outlier_far,
}


def _shape_values(spec, n, rng, level):
    """spec(=한쪽의 모양 정의) → 최종 값 배열.

    spec: {"shape", "mu", "sd", "quant"(선택)}
      - shape 로 모양을 만들고
      - _rescale 로 μ·σ 를 목표값에 **정확히** 맞춘 뒤
      - quant 가 있으면 이산화하고 (이산화 후 재-rescale 금지 — 격자가 깨진다)
      - 규격 안으로 클립한다.

    예외: ``outlier_far`` 는 "본체는 그대로인데 σ 만 뛴다"가 곧 케이스의 정의라
    _rescale 로 σ 를 되돌리면 현상이 사라진다. 본체 스케일만 맞추고 이탈 die 가 σ 를
    끌어올리도록 둔다.
    """
    fn = SHAPE_FUNCS[spec["shape"]]
    raw = fn(rng, n, level)
    if spec["shape"] == "outlier_far":
        v = raw * spec["sd"] + spec["mu"]
    else:
        v = _rescale(raw, spec["mu"], spec["sd"])
    if spec.get("quant"):
        v = _quantize(v, spec["quant"])
    return _clip_spec(v)


# ── 케이스 정의 ──────────────────────────────────────────────────────────────
# 각 케이스는 "before 모양 → after 모양" 한 쌍이며, levels 에 적힌 레벨마다 item 1개가 난다.
#
# blind: μ·σ 를 양쪽 동일하게 고정해 **현행 지표가 원리적으로 못 잡는** 케이스.
# fp   : 현행 임계가 **과하게 무는** 케이스. 눈으로는 차이가 없거나 개선이라 레벨을 낮게 둔다.
#        (레벨을 L1~L2 로 제한하는 것이 핵심 — 그래야 over_detect 로 집계된다)

def _mu_for_cpk(cpk, sd):
    """목표 Cpk 를 만드는 평균(중심에서 위쪽으로)."""
    return USL - 3.0 * sd * cpk


# 이산 격자 — σ 대비 비율. 값이 클수록 성기다(= 눈에 띄게 이산).
GRAIN = {1: 0.25, 2: 0.5, 3: 1.0, 4: 2.0}

ALL_LV = (1, 2, 3, 4)
LOW_LV = (1, 2)          # 과검출 케이스 전용 — "눈으로는 차이 없음" 대역

CASES = [
    # ── A. 모양만 다름 (μ·σ 고정) — 현행 지표 원리적 미검출 ─────────────────
    dict(code="SHAPE_BIMODAL", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="bimodal", mu=CENTER, sd=BASE_SD),
         desc="정규 → 쌍봉 (μ·σ 동일)"),
    dict(code="SHAPE_BIMODAL_SKEW", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="bimodal_skew", mu=CENTER, sd=BASE_SD),
         desc="정규 → 비대칭 쌍봉(소수 모드 혼입)"),
    dict(code="SHAPE_TRIMODAL", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="trimodal", mu=CENTER, sd=BASE_SD),
         desc="정규 → 삼봉(사이트 혼입)"),
    dict(code="SHAPE_TAILFLIP", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="tail_up", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="tail_down", mu=CENTER, sd=BASE_SD),
         desc="꼬리 방향 반전 (위→아래, μ·σ 동일)"),
    dict(code="SHAPE_TAIL_ONESIDE", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="tail_up", mu=CENTER, sd=BASE_SD),
         desc="정규 → 한쪽 꼬리 (σ 는 재정규화로 동일)"),
    dict(code="SHAPE_TAIL_BOTH", n_case=1, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="tail_both", mu=CENTER, sd=BASE_SD),
         desc="정규 → 양쪽 꼬리(몸통 수축)"),
    dict(code="SHAPE_UNIFORM", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="uniform", mu=CENTER, sd=BASE_SD),
         desc="정규 → 균등(평평한 몸통)"),
    dict(code="SHAPE_PLATEAU", n_case=1, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="plateau", mu=CENTER, sd=BASE_SD),
         desc="정규 → 중앙 함몰(U 자화)"),
    dict(code="SHAPE_SKEW_R", n_case=1, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="skew_right", mu=CENTER, sd=BASE_SD),
         desc="정규 → 우왜곡(로그정규형)"),
    dict(code="SHAPE_SKEWFLIP", n_case=1, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="skew_right", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="skew_left", mu=CENTER, sd=BASE_SD),
         desc="왜곡 방향 반전 (우→좌, μ·σ 동일)"),
    dict(code="SHAPE_OUTLIER", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="outlier_cluster", mu=CENTER, sd=BASE_SD),
         desc="정규 → 이탈 무리 분리"),
    dict(code="SHAPE_SPIKE", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="spike", mu=CENTER, sd=BASE_SD),
         desc="정규 → 단일값 스파이크(포화·클램프)"),
    dict(code="SHAPE_TRUNC", n_case=1, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="truncated", mu=CENTER, sd=BASE_SD),
         desc="정규 → 한쪽 절단(벽 생성)"),

    # ── B. 이산(discrete) 케이스 ─────────────────────────────────────────────
    dict(code="DISC_GRAIN", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="granular", mu=CENTER, sd=BASE_SD,
                                quant=BASE_SD * GRAIN[1]),
         after=lambda lv: dict(shape="granular", mu=CENTER, sd=BASE_SD,
                               quant=BASE_SD * GRAIN[lv]),
         desc="이산 격자가 굵어짐 (해상도 저하, μ·σ 동일)"),
    dict(code="DISC_TO_CONT", n_case=1, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD,
                                quant=BASE_SD * GRAIN[lv]),
         after=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         desc="이산 → 연속 (계측 방식 변경)"),
    dict(code="DISC_BIMODAL", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD,
                                quant=BASE_SD * 0.5),
         after=lambda lv: dict(shape="bimodal", mu=CENTER, sd=BASE_SD,
                               quant=BASE_SD * 0.5),
         desc="이산 정규 → 이산 쌍봉"),
    dict(code="DISC_LEVELSHIFT", n_case=1, levels=ALL_LV, blind=False,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD,
                                quant=BASE_SD * 1.0),
         after=lambda lv: dict(shape="normal", mu=CENTER + BASE_SD * lv * 0.5,
                               sd=BASE_SD, quant=BASE_SD * 1.0),
         desc="이산값 계단 하나만큼 평행 이동"),
    dict(code="DISC_SPARSE", n_case=1, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD,
                                quant=BASE_SD * 1.5),
         after=lambda lv: dict(shape="spike", mu=CENTER, sd=BASE_SD,
                               quant=BASE_SD * 1.5),
         desc="이산 + 한 계단에 몰림 (수준 편중)"),

    # ── C. 겹침/분리 — 두 분포의 상대 위치 ───────────────────────────────────
    dict(code="POS_SEPARATE", n_case=2, levels=ALL_LV, blind=False,
         before=lambda lv: dict(shape="normal", mu=CENTER - BASE_SD * lv * 0.9,
                                sd=BASE_SD * 0.55),
         after=lambda lv: dict(shape="normal", mu=CENTER + BASE_SD * lv * 0.9,
                               sd=BASE_SD * 0.55),
         desc="완전 분리 방향으로 서로 멀어짐"),
    dict(code="POS_OVERLAP_SAME", n_case=3, levels=LOW_LV, blind=False,
         # 대조군 — before/after 가 같은 분포(난수만 다르다). **차이 없음이 정답**이라
         # 레벨을 L1~L2 로만 둔다(L3/L4 를 만들면 "심한 차이"라는 라벨 자체가 거짓이 된다).
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         desc="완전히 겹침 (대조군 — 차이 없음이 정답)"),
    dict(code="POS_SHIFT_SMALL", n_case=2, levels=ALL_LV, blind=False,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="normal",
                               mu=CENTER + BASE_SD * {1: 0.08, 2: 0.25,
                                                      3: 0.6, 4: 1.2}[lv],
                               sd=BASE_SD),
         desc="평균만 shift (σ 동일)"),

    # ── D. σ 변화 — 현행 Δσ% 가 잡는 축 (기준선 대조군) ──────────────────────
    dict(code="SD_UP", n_case=2, levels=ALL_LV, blind=False,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="normal", mu=CENTER,
                               sd=BASE_SD * {1: 1.04, 2: 1.16, 3: 1.45, 4: 2.0}[lv]),
         desc="산포만 커짐"),
    dict(code="SD_UP_MEAN_DOWN", n_case=1, levels=ALL_LV, blind=False,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="normal",
                               mu=CENTER - BASE_SD * {1: 0.1, 2: 0.3, 3: 0.7, 4: 1.2}[lv],
                               sd=BASE_SD * {1: 1.05, 2: 1.18, 3: 1.5, 4: 2.1}[lv]),
         desc="평균 이동 + 산포 확대 동시"),

    # ── E. 서로 다르게 늘어짐 — 양쪽 다 비정규인데 방식이 다르다 ─────────────
    dict(code="MIX_TAIL_VS_BIMODAL", n_case=2, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="tail_up", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="bimodal", mu=CENTER, sd=BASE_SD),
         desc="꼬리형 → 쌍봉형 (둘 다 비정규, 방식이 다름)"),
    dict(code="MIX_UNIFORM_VS_SPIKE", n_case=1, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="uniform", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="spike", mu=CENTER, sd=BASE_SD),
         desc="균등 → 스파이크 (양극단 모양)"),
    dict(code="MIX_OUTLIER_VS_TAIL", n_case=1, levels=ALL_LV, blind=True,
         before=lambda lv: dict(shape="outlier_cluster", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="tail_up", mu=CENTER, sd=BASE_SD),
         desc="이탈 무리 → 연속 꼬리 (같은 σ, 다른 위험)"),

    # ── F. 낮은 Cpk — 현행 ② 경로(절대 품질)가 잡는 축 ───────────────────────
    dict(code="CPK_LOW", n_case=1, levels=ALL_LV, blind=False,
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="normal",
                               mu=_mu_for_cpk({1: 1.5, 2: 1.35, 3: 1.1, 4: 0.7}[lv],
                                              BASE_SD),
                               sd=BASE_SD),
         desc="평균 이동으로 Cpk 하락"),

    # ── G. 과검출(false positive) — 현행 지표가 **과하게 잡는** 케이스 ────────
    # 전부 레벨 L1~L2 다. "눈으로는 차이가 없거나 오히려 좋아졌는데 검출" 이 곧 과검출의
    # 정의이며, verify 의 over_detect 컬럼이 그 목록이 된다.
    dict(code="FP_LOWCPK_NOCHANGE", n_case=3, levels=LOW_LV, blind=False, fp=True,
         # 현행 ② 경로에는 유의성 게이트가 없다 — before/after 가 사실상 동일해도
         # 원래부터 Cpk 가 낮으면 **무조건** 검출된다. "변화 없음"인데 이슈로 뜨는 1순위.
         before=lambda lv: dict(shape="normal", mu=_mu_for_cpk(1.10, BASE_SD), sd=BASE_SD),
         after=lambda lv: dict(shape="normal",
                               mu=_mu_for_cpk(1.10, BASE_SD) + BASE_SD * lv * 0.02,
                               sd=BASE_SD),
         desc="[과검출] 원래 Cpk 낮음 + 변화 없음 (유의성 게이트 부재)"),
    dict(code="FP_LOWCPK_IMPROVED", n_case=3, levels=LOW_LV, blind=False, fp=True,
         # 나빴던 항목이 **좋아졌는데도** After Cpk 가 아직 1.33 밑이면 검출된다.
         before=lambda lv: dict(shape="normal", mu=_mu_for_cpk(0.75, BASE_SD), sd=BASE_SD),
         after=lambda lv: dict(shape="normal",
                               mu=_mu_for_cpk(0.75 + 0.15 * lv, BASE_SD), sd=BASE_SD),
         desc="[과검출] 나빴다가 개선됐는데 아직 Cpk<1.33 이라 검출"),
    dict(code="FP_SD_IMPROVED", n_case=3, levels=LOW_LV, blind=False, fp=True,
         # |Δσ%| 는 **절대값**이라 산포가 줄어도(=개선) 검출된다.
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="normal", mu=CENTER,
                               sd=BASE_SD * {1: 0.83, 2: 0.78}[lv]),
         desc="[과검출] 산포가 줄어 개선됐는데 |Δσ%| 절대값이라 검출"),
    dict(code="FP_TINY_SIGMA", n_case=3, levels=LOW_LV, blind=False, fp=True,
         # σ 가 규격폭 대비 아주 작으면 절대 변화량은 무의미한데 **비율만** 크다.
         # (σ 0.08 → 0.10 = +25%, 규격폭 10 기준 die 하나도 위험해지지 않는다)
         # ⚠ σ 를 더 줄이면 Cpk 가 100 을 넘어 _dist_focus ① "여유 과대"로 **제외**된다 —
         #    현행 코드에 이미 있는 방어책이라, 그게 안 먹는 대역(Cpk<100)에 둬야
         #    "정말로 과검출되는" 사례가 된다.
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=(USL - LSL) * 0.008),
         after=lambda lv: dict(shape="normal", mu=CENTER,
                               sd=(USL - LSL) * 0.008 * {1: 1.20, 2: 1.30}[lv]),
         desc="[과검출] σ 가 아주 작아 절대 변화는 무의미한데 비율만 큼"),
    dict(code="FP_DISCRETE_JITTER", n_case=3, levels=LOW_LV, blind=False, fp=True,
         # 이산 계단이 σ 보다 굵으면 경계 die 가 한 계단 옮겨간 것만으로 σ 비율이 크게
         # 흔들린다 — 값의 종류는 2~3 개뿐이라 실제로는 "같은 수준"이다.
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD * 0.42,
                                quant=BASE_SD * 1.6),
         after=lambda lv: dict(shape="normal", mu=CENTER + BASE_SD * 0.34 * lv,
                               sd=BASE_SD * 0.42, quant=BASE_SD * 1.6),
         desc="[과검출] 이산 경계 die 이동만으로 σ 비율이 흔들림"),
    dict(code="FP_OUTLIER_FEW", n_case=3, levels=LOW_LV, blind=False, fp=True,
         # die 약 2% 의 이탈만으로 σ 가 15% 넘게 뛴다 — 분포 본체는 동일하다.
         before=lambda lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda lv: dict(shape="outlier_far", mu=CENTER, sd=BASE_SD),
         desc="[과검출] die 약 2% 이탈만으로 σ 급등 (본체는 동일)"),
]


def build_plan():
    """전 item 계획 — (이름, 유형, 레벨, before/after spec, 설계 의도 플래그)."""
    items = []
    tno = 1000
    for case in CASES:
        for i in range(case["n_case"]):
            for lv in case["levels"]:
                tno += 1
                items.append({
                    "name": f"{case['code']}_{i + 1:02d}_L{lv}",
                    "case": case["code"],
                    "level": lv,
                    "tno": tno,
                    "desc": case["desc"],
                    "blind_by_design": bool(case.get("blind")),
                    "fp_by_design": bool(case.get("fp")),
                    "before": case["before"](lv),
                    "after": case["after"](lv),
                })
    return items


# ── 웨이퍼 좌표 ──────────────────────────────────────────────────────────────

def build_wafer(radius: int):
    """반경 radius 안의 정수격자 die 좌표 (0-based 양수로 옮겨 반환).

    ⚠ XPOS/YPOS 는 실데이터에서 **항상 양수**다(CLAUDE.md 규칙 #9).
    """
    xs, ys = [], []
    for y in range(-radius, radius + 1):
        for x in range(-radius, radius + 1):
            if x * x + y * y <= radius * radius:
                xs.append(x + radius)
                ys.append(y + radius)
    return np.asarray(xs, dtype=int), np.asarray(ys, dtype=int)


# ── 프레임 조립 ──────────────────────────────────────────────────────────────

def build_frame(items, side, xs, ys, rng):
    """한쪽(before/after) 7-meta honeyform DataFrame.

    전 die 가 Bin1 이다 — dist_shift 모집단이 Bin1 뿐이라, 여기서 fail 을 만들면
    의도한 분포가 통계에서 부분적으로 빠진다(모듈 docstring 제약 ①).
    """
    n = xs.size
    meta_rows = {k: {} for k in META_ROW_LABELS}
    cols = {}
    for tseq, it in enumerate(items, start=1):
        v = _shape_values(it[side], n, rng, it["level"])
        cols[it["name"]] = [f"{x:.6f}" for x in v]
        meta_rows["TSEQ"][it["name"]] = str(tseq)
        meta_rows["TNO"][it["name"]] = str(it["tno"])
        meta_rows["STEP"][it["name"]] = STEP
        meta_rows["UNIT"][it["name"]] = UNIT
        meta_rows["HILIM"][it["name"]] = f"{USL:g}"
        meta_rows["LOLIM"][it["name"]] = f"{LSL:g}"

    names = [it["name"] for it in items]
    head = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row["SERIAL"] = label
        row.update({nm: meta_rows[label].get(nm, "") for nm in names})
        head.append(row)

    body = {
        "SERIAL": [f"C{i:06d}" for i in range(n)],
        "SHOT": [str(i // 4 + 1) for i in range(n)],
        "DUT": [str(i % 4 + 1) for i in range(n)],
        "XPOS": [str(int(v)) for v in xs],
        "YPOS": [str(int(v)) for v in ys],
        "BIN": ["1"] * n,
        "FAILTNO": [""] * n,
    }
    body.update(cols)
    return pd.concat([pd.DataFrame(head, columns=META_COLUMNS + names),
                      pd.DataFrame(body, columns=META_COLUMNS + names)],
                     ignore_index=True)


# ── 검증 (서버 코드 직접 호출 — 재구현 금지) ────────────────────────────────

def verify(df_before, df_after, items):
    """실제 서버 경로로 현행 지표를 실측한다. (rows, blind 수, over 수, counts) 반환.

    v1 과 달리 '기대와 일치하는가' 를 묻지 않는다 — 이 데이터의 목적은 **현행 지표가
    무엇을 놓치고 무엇을 과하게 무는지** 드러내는 것이라, 불일치가 실패가 아니라 결과물이다.
    """
    from web_report.tabs.compare import build_dist_shift
    from web_report.tabs.cpk import build_cpk_rows

    t_before = split_honeyform(df_before, source="WF_BEFORE", file_name="before.csv")
    t_after = split_honeyform(df_after, source="WF_AFTER", file_name="after.csv")
    stat_items = sorted(set(t_after.item_columns) | set(t_before.item_columns))
    cpk_rows = build_cpk_rows([t_after, t_before], stat_items)
    dist = build_dist_shift([t_after, t_before], cpk_rows)
    by_item = {r["subject"]: r for r in dist["rows"]}

    rows, blind, over = [], 0, 0
    for it in items:
        r = by_item.get(it["name"]) or {}
        focus = bool(r.get("focus"))
        # 미검출 = 눈으로 확실히 다른데(L3 이상) 현행 지표가 못 잡은 항목.
        is_blind = (it["level"] >= 3) and not focus
        # 과검출 = 눈으로는 차이가 없거나 개선(L1~L2)인데 현행 지표가 잡은 항목.
        is_over = (it["level"] <= 2) and focus
        if is_blind:
            blind += 1
        if is_over:
            over += 1
        b = r.get("before") or {}
        a = r.get("after") or {}
        rows.append({
            "item": it["name"], "case": it["case"], "level": it["level"],
            "level_ko": LEVEL_KO[it["level"]], "desc": it["desc"],
            "blind_by_design": int(it["blind_by_design"]),
            "fp_by_design": int(it["fp_by_design"]),
            "current_focus": int(focus),
            "blind_spot": int(is_blind),
            "over_detect": int(is_over),
            # 현행 판정에 실제로 쓰이는 2지표
            "cpk_ratio_pct": r.get("cpk_ratio_pct"),
            "stdev_delta_pct": r.get("stdev_delta_pct"),
            # 판정에 안 쓰이지만 payload 에 이미 있는 지표 (새 기준 후보)
            "ks_d": r.get("ks_d"),
            "meanshift_sigma": r.get("meanshift_sigma"),
            "median_shift": r.get("median_shift"),
            "iqr_delta_pct": r.get("iqr_delta_pct"),
            "p_mean": r.get("p_mean"), "p_stdev": r.get("p_stdev"),
            "before_avg": b.get("average"), "before_stdev": b.get("stdev"),
            "before_cpk": b.get("cpk"),
            "after_avg": a.get("average"), "after_stdev": a.get("stdev"),
            "after_cpk": a.get("cpk"),
        })
    counts = {"total": dist["summary"]["total"], "focus": dist["summary"]["focus"]}
    return rows, blind, over, counts


def answer_rows(items):
    out = []
    for it in items:
        b, a = it["before"], it["after"]
        out.append({
            "item": it["name"], "case": it["case"], "level": it["level"],
            "level_ko": LEVEL_KO[it["level"]], "desc": it["desc"],
            "blind_by_design": int(it["blind_by_design"]),
            "fp_by_design": int(it["fp_by_design"]),
            "tno": it["tno"], "unit": UNIT, "lsl": LSL, "usl": USL,
            "before_shape": b["shape"], "before_mean": b["mu"], "before_stdev": b["sd"],
            "before_quant": b.get("quant") or "",
            "after_shape": a["shape"], "after_mean": a["mu"], "after_stdev": a["sd"],
            "after_quant": a.get("quant") or "",
            "th_cpk_low": TH_CPK_LOW, "th_stdev_delta_pct": TH_STDEV_DELTA_PCT,
            "th_alpha": TH_ALPHA, "th_cpk_high": TH_CPK_HIGH,
        })
    return out


def summarize(rows):
    """case×level 요약 — "어떤 유형이 어느 레벨에서 어긋나는가"를 콘솔에 낸다."""
    df = pd.DataFrame(rows)
    piv = df.pivot_table(index="case", columns="level", values="current_focus",
                         aggfunc="mean")
    print("\n[현행 검출률] 행=유형, 열=레벨 (1.0=전부 검출, 0.0=전부 미검출, 빈칸=해당 레벨 없음)")
    print(piv.round(2).to_string())

    print("\n[미검출] L3~L4 인데 현행이 못 잡음 → 새 지표가 잡아야 할 목록")
    bad = df[df["blind_spot"] == 1]
    if bad.empty:
        print("  없음")
    else:
        print(bad.groupby("case").size().rename("건수").to_string())

    print("\n[과검출] L1~L2 인데 현행이 잡음 → 임계를 완화해야 할 목록")
    fp = df[df["over_detect"] == 1]
    if fp.empty:
        print("  없음")
    else:
        print(fp.groupby("case").size().rename("건수").to_string())

    # 설계 의도대로 나왔는지 — 의도했는데 재현 안 된 케이스를 짚어준다.
    miss_fp = df[(df["fp_by_design"] == 1) & (df["over_detect"] == 0)]
    if not miss_fp.empty:
        print("\n[주의] 과검출을 의도했으나 검출되지 않은 항목 "
              f"{len(miss_fp)}개 — 파라미터가 임계에 못 미쳤을 수 있다")
        print(miss_fp.groupby("case").size().rename("건수").to_string())


def main():
    ap = argparse.ArgumentParser(
        description="Compare 산포 모양 데이터 (현행 지표 미검출/과검출 발굴용)")
    ap.add_argument("--out-dir", default=str(_ROOT / "data"))
    ap.add_argument("--prefix", default="compare_shape_v2")
    ap.add_argument("--radius", type=int, default=WAFER_RADIUS)
    ap.add_argument("--seed", type=int, default=20260828)
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    xs, ys = build_wafer(args.radius)
    items = build_plan()

    df_before = build_frame(items, "before", xs, ys, rng)
    df_after = build_frame(items, "after", xs, ys, rng)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p_before = out_dir / f"{args.prefix}_before.csv"
    p_after = out_dir / f"{args.prefix}_after.csv"
    p_answer = out_dir / f"{args.prefix}_answer.csv"
    p_verify = out_dir / f"{args.prefix}_verify.csv"

    df_before.to_csv(p_before, index=False, encoding="utf-8-sig")   # Excel 용 BOM
    df_after.to_csv(p_after, index=False, encoding="utf-8-sig")
    pd.DataFrame(answer_rows(items)).to_csv(p_answer, index=False, encoding="utf-8-sig")

    print(f"[생성] die {xs.size} · item {len(items)} · 유형 {len(CASES)}")
    print(f"  {p_before}")
    print(f"  {p_after}")
    print(f"  {p_answer}")

    if args.no_verify:
        return 0

    rows, blind, over, counts = verify(df_before, df_after, items)
    pd.DataFrame(rows).to_csv(p_verify, index=False, encoding="utf-8-sig")
    print(f"  {p_verify}")
    print(f"\n[현행 지표 실측] 공통 항목 {counts['total']} · 검출 {counts['focus']} "
          f"({counts['focus'] / max(counts['total'], 1) * 100:.1f}%)")
    print(f"[미검출] L3~L4 인데 미검출: {blind} 개")
    print(f"[과검출] L1~L2 인데 검출:  {over} 개")
    summarize(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Compare 검출 **레벨 정렬** 데이터 생성기 (before/after 쌍) — v4, 2026-09-01.

    server\\.venv\\Scripts\\python.exe tools\\eval_testdata\\make_compare_level_testdata.py

산출물 4종 (기본 ``data/`` 아래):
    compare_level_v4_before.csv   7-meta honeyform (Before)
    compare_level_v4_after.csv    7-meta honeyform (After)
    compare_level_v4_answer.csv   정답표 (item 별 유형·레벨·기대 검출·역산된 강도)
    compare_level_v4_verify.csv   **서버 코드로 직접 돌린** 실측 대조 (ok=0 이 불일치)

## v2/v3(make_compare_shape_testdata.py) 와 목적이 다르다

v2/v3 는 **레벨과 검출이 독립**인 데이터였다 — 레벨은 "눈으로 본 차이의 크기"이고, 그것이
현행 검출과 어긋나는 곳(blind_spot·over_detect)을 드러내 **임계를 다시 잡는** 재료였다.
그 역할은 끝났다(v3 룰 개정이 그 데이터로 이뤄졌다).

이 v4 는 반대로 **레벨이 곧 검출 여부**인 데이터다(사용자 지시):

    L1, L2 → 현행 룰로 **검출되지 않아야** 한다
    L3, L4 → 현행 룰로 **검출되어야** 한다

용도는 회귀 검증이다 — Issue Table Compare 를 열어 item 명의 레벨로 정렬하면
**L1·L2 구간에 검출이 하나도 없고 L3·L4 구간이 전부 검출**이어야 한다. 어긋난 항목이
보이면 그것이 곧 룰의 결함 신고 재료가 된다.

## 어떻게 레벨을 검출에 맞추는가 — 목표 지표 역산

유형마다 "강도"를 뜻하는 파라미터가 제각각이라(봉우리 간격·꼬리 비율·격자 폭 …) 손으로
상수를 고르면 유형마다 검출 경계가 어긋난다. v3 실측이 그랬다 — 같은 L3 인데
`POS_SEPARATE` 는 ks_d 1.00, `SHAPE_TAIL_ONESIDE` 는 0.056 이었다.

그래서 v4 는 **모양 강도를 연속 파라미터 t(0~1)로 두고, 레벨별 목표 지표에 닿도록 t 를
이분탐색으로 역산한다**(`solve_t`). 목표는 현행 판정축 위에서 잡는다:

    L1: ks_d ≈ 0.05   (임계 0.15 에서 충분히 아래 — 미검출)
    L2: ks_d ≈ 0.10   (임계 바로 아래 — 미검출이되 "자세히 보면 다름")
    L3: ks_d ≈ 0.24   (임계 위 — 검출)
    L4: ks_d ≈ 0.42   (임계 훨씬 위 — 확실히 검출)

Cpk·σ 경로를 타는 유형(CPK_LOW·SD_UP)은 ks 가 아니라 **그 경로의 지표**를 목표로 잡는다
(`metric="after_cpk"` / `"sd_delta"`). 어느 경로로 잡히는지가 유형의 정체성이라, 전부
ks 로 맞추면 그 축의 회귀를 못 본다.

역산은 **서버의 `_ks_d` 를 그대로 호출**한다(재구현 금지 — 규칙 #13). 판정도 마찬가지로
`_dist_focus` 를 그대로 부른다.

## 왜 L2 를 임계 **바로 아래**에 두는가

경계를 넉넉히 비우면 "안 걸려야 할 게 안 걸린다"는 확인이 쉬워지지만, 룰이 조금 느슨해져도
드러나지 않는다. L2 를 임계 직하(0.10 vs 임계 0.15)에 두면 **임계가 흔들리는 순간 L2 가
먼저 걸려** 회귀를 조기에 잡는다. 반대로 L3 는 임계 직상이 아니라 여유를 두고(0.24) 잡아
표본 잡음으로 미검출이 되지 않게 한다 — 잡음으로 흔들리는 쪽은 "안 걸려야 할 것"이 아니라
"걸려야 할 것"이어야 오탐 신고가 나오지 않는다.

## 두 가지 결정적 제약 (v1~v3 과 동일 — 어기면 데이터가 무의미해진다)

1. **모집단은 Bin1(양품) die 뿐이다** (`compare._bin1_frame`). 규격 밖 값을 만들면 그 die 가
   통계에서 빠져 "만든 분포"와 "보이는 분포"가 달라진다. 전 값 spec 클립, 전 die Bin1.
2. **XPOS/YPOS 는 항상 양수** (CLAUDE.md 규칙 #9).

pytest 미사용 — 생성 직후 서버 코드(`build_dist_shift`/`build_cpk_rows`)를 **그대로 호출해**
기대와 대조하고, 불일치가 있으면 exit 1 로 끝난다.
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
from web_report.tabs.compare import _ks_d  # noqa: E402  (서버 정본 재사용 — 재구현 금지)

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

LEVELS = (1, 2, 3, 4)
LEVEL_KO = {
    1: "L1 미검출(여유)",
    2: "L2 미검출(임계 직하)",
    3: "L3 검출(임계 초과)",
    4: "L4 검출(크게 초과)",
}
# 레벨 → 기대 검출 여부. 이 데이터의 존재 이유 자체다.
EXPECT_FOCUS = {1: False, 2: False, 3: True, 4: True}

# 기본 σ — Cpk 1.60 (여유 있는 정상 항목). Cpk = min(USL-μ, μ-LSL) / (3σ)
BASE_SD = (USL - CENTER) / (3.0 * 1.60)

WAFER_RADIUS = 18            # die ≈ 1009

# ── 레벨별 목표 지표 ─────────────────────────────────────────────────────────
# 현행 임계(compare.py): ks_d 0.15 / |Δσ%| 20.0(위험군 15.0) / Cpk 1.33.
# 목표는 그 임계를 기준으로 L2 는 **직하**, L3 는 **여유 있게 위**로 잡는다(모듈 docstring).
TARGET_KS = {1: 0.05, 2: 0.10, 3: 0.24, 4: 0.42}
# Cpk 경로 유형 — After Cpk 목표. 임계 1.33 기준 L2 는 위(미검출), L3 는 아래(검출).
# ⚠ L2 를 1.42 로 잡으면 **④ 는 피해도 ⑥(형태)에 걸린다** — Cpk 를 떨어뜨리는 수단이
# 평균 이동이라 Cpk 1.42 는 이미 0.55σ 이동이고 그때 ks 가 0.21 로 임계 0.15 를 넘는다
# (v4 1차 실측). 미검출이어야 할 L2 를 지키려면 이동폭을 ks 임계 아래로 묶어야 하므로
# 목표 Cpk 를 1.33 바로 위(1.50)로 올린다. 판정 경로가 서로 겹치는 자리라, 한 경로만
# 보고 목표를 잡으면 다른 경로가 잡아채 간다.
TARGET_CPK = {1: 1.58, 2: 1.50, 3: 1.15, 4: 0.80}
# σ 경로 유형 — Δσ% 목표. 임계 20% 기준. ⚠ σ 가 커지면 After Cpk 도 떨어지므로 L1·L2 는
# Cpk 가 1.33 을 넘게(=④ 경로에 안 걸리게) 유지해야 한다 — BASE_SD 1.60 기준
# Δσ +16% 면 Cpk 1.38 로 아직 위다.
TARGET_SD = {1: 5.0, 2: 16.0, 3: 34.0, 4: 70.0}


# ── 값 합성 도구 ─────────────────────────────────────────────────────────────

def _rescale(v, mean, sd):
    """표본의 실제 평균·표준편차를 목표값으로 정확히 맞춘다(모양 보존).

    모양만 다른 케이스의 핵심 장치다 — 이게 없으면 σ 가 난수 노이즈로 흔들려 "μ·σ 동일,
    모양만 다름" 이 우연히 Δσ% 경로로 검출돼 레벨↔검출 대응이 깨진다.
    """
    v = np.asarray(v, dtype=float)
    cur_sd = v.std(ddof=1)
    if cur_sd == 0:
        return np.full(v.size, mean, dtype=float)
    return (v - v.mean()) / cur_sd * sd + mean


def _quantize(v, step):
    """이산화 — 연속값을 step 격자에 올린다(code unit 계측기 모사).

    ⚠ 이산화 후 다시 _rescale 하지 않는다(격자가 깨져 이산이 아니게 된다).
    """
    return np.round(np.asarray(v, dtype=float) / step) * step


def _clip_spec(v, lsl=LSL, usl=USL):
    """규격 안으로 클립 — 전 die 를 Bin1 로 유지(모집단 불변, 제약 ①)."""
    pad = (usl - lsl) * 0.001
    return np.clip(v, lsl + pad, usl - pad)


def _fit_moments_in_spec(v, mean, sd, lsl=LSL, usl=USL, iters=40):
    """규격 안에 가두면서 μ·σ 를 목표에 맞춘다 — 클립↔재스케일 교대 반복.

    **왜 필요한가.** `_rescale` 로 σ 를 맞춰도 그 뒤의 `_clip_spec` 이 극단값을 잘라
    σ 를 도로 줄인다. 꼬리·왜곡·스파이크처럼 모양 자체가 극단값을 갖는 유형에서 이 손실이
    커서(실측 −20~−50%), After 의 Cpk 가 **올라간다**. 그러면 `_dist_focus` ③ 이
    "개선"으로 보고 제외해 버려, 모양이 완전히 달라진 L3·L4 가 통째로 미검출이 됐다
    (v4 1차 실측: SHAPE_SKEW_R·SPIKE·SKEWFLIP·DISC_SPARSE).

    클립과 재스케일을 번갈아 돌리면 두 조건(규격 안 · 목표 모멘트)을 동시에 만족하는 쪽으로
    수렴한다. 모양은 단조변환으로만 바뀌므로 순서(=분포의 형태)는 보존된다.

    수렴하지 않는 경우(목표 σ 가 규격폭에 비해 너무 커서 물리적으로 불가능)에는 마지막
    클립 결과를 그대로 돌려준다 — 그 항목은 solve_t 가 지표를 실측하므로 조용히 틀리지
    않고 verify 에 드러난다.
    """
    pad = (usl - lsl) * 0.001
    lo, hi = lsl + pad, usl - pad
    v = np.asarray(v, dtype=float)
    for _ in range(iters):
        v = np.clip(v, lo, hi)
        cur_sd = v.std(ddof=1)
        if cur_sd == 0:
            return np.full(v.size, mean, dtype=float)
        v = (v - v.mean()) / cur_sd * sd + mean
        if v.min() >= lo - 1e-12 and v.max() <= hi + 1e-12:
            return v                      # 규격 안이면서 모멘트도 맞음 — 완료
    return np.clip(v, lo, hi)


# ── 산포 모양 생성기 ─────────────────────────────────────────────────────────
# 각 함수는 (rng, n, t) → 표준화 전 모양을 돌려준다. **t 는 0 이상 연속 강도**이며
# solve_t 가 목표 지표에 닿도록 역산한다(레벨별 고정 상수를 쓰지 않는 이유는
# 모듈 docstring 참조). t=0 이면 before 와 사실상 같은 모양이어야 한다.
#
# ⚠ t 는 1 을 넘을 수 있다(탐색 상한 T_MAX). 유형마다 ks 가 t 에 반응하는 기울기가
# 달라서, 목표 0.42(L4)에 닿으려면 t 2~3 이 필요한 모양이 있다. 따라서 **die 비율을
# 쓰는 모양은 반드시 상한을 클램프**해야 한다 — 안 그러면 비율이 1 을 넘어
# `rng.choice(replace=False)` 가 터진다(v4 1차에서 실제로 겪었다).


def _frac(base, span, t, cap=0.48):
    """t → die 비율. 상한을 두어 t>1 에서도 표본 추출이 깨지지 않게 한다."""
    return float(min(base + span * t, cap))

def _sh_normal(rng, n, t):
    """정규 — 기준 모양(t 무관)."""
    return rng.normal(0.0, 1.0, n)


def _sh_bimodal(rng, n, t):
    """쌍봉 — t 가 클수록 봉우리가 멀고 각 봉이 좁다."""
    gap = 0.15 + 1.45 * t
    inner = 1.0 - 0.80 * t
    half = n // 2
    return np.concatenate([rng.normal(-gap, max(inner, 0.12), half),
                           rng.normal(+gap, max(inner, 0.12), n - half)])


def _sh_bimodal_skew(rng, n, t):
    """비대칭 쌍봉 — 소수 모드가 몸통에서 떨어져 나간다.

    소수 비율과 거리를 함께 키운다. 비율만 키우면 대칭 쌍봉이 되고, 거리만 키우면
    이탈 무리와 구분이 안 된다 — 둘의 조합이 이 모양의 정체성이다.
    """
    k = max(1, int(n * _frac(0.03, 0.32, t)))
    gap = 0.5 + 2.2 * t
    return np.concatenate([rng.normal(0.0, 1.0, n - k), rng.normal(gap, 0.35, k)])


def _sh_trimodal(rng, n, t):
    """삼봉 — 세 무리(예: 3개 테스터/사이트 혼입)."""
    gap = 0.12 + 1.55 * t
    inner = 1.0 - 0.85 * t
    third = n // 3
    return np.concatenate([rng.normal(-gap, max(inner, 0.10), third),
                           rng.normal(0.0, max(inner, 0.10), third),
                           rng.normal(+gap, max(inner, 0.10), n - 2 * third)])


def _sh_tail_up(rng, n, t):
    """위쪽 한쪽 꼬리 — 소수 die 가 위로 늘어진다."""
    push = 0.8 + 4.2 * t
    v = rng.normal(0.0, 1.0, n)
    k = max(1, int(n * _frac(0.01, 0.30, t)))
    idx = rng.choice(n, size=k, replace=False)
    v[idx] += rng.uniform(push * 0.6, push, k)
    return v


def _sh_tail_down(rng, n, t):
    """아래쪽 한쪽 꼬리 — 위와 방향만 반대.

    꼬리 방향 반전은 μ·σ 가 같아도 완전히 다른 분포다(모멘트 2개로는 원리적으로 못 본다 —
    ks 경로가 잡아야 하는 대표 케이스).
    """
    return -_sh_tail_up(rng, n, t)


def _sh_tail_both(rng, n, t):
    """양쪽 꼬리 — 좌우 대칭으로 늘어진다(σ 재정규화 후 몸통이 오히려 좁아진다)."""
    push = 0.8 + 4.0 * t
    v = rng.normal(0.0, 1.0, n)
    # 양쪽에서 각 k 개씩 = 2k 를 뽑으므로 상한이 다른 유형의 **절반**이다.
    k = max(1, int(n * _frac(0.01, 0.26, t, cap=0.24)))
    idx = rng.choice(n, size=2 * k, replace=False)
    v[idx[:k]] += rng.uniform(push * 0.6, push, k)
    v[idx[k:]] -= rng.uniform(push * 0.6, push, k)
    return v


def _sh_uniform(rng, n, t):
    """균등 — 몸통이 평평하다(정규와 σ 가 같아도 모양이 전혀 다르다).

    t=1 이면 완전 균등. ks 로 본 정규↔균등 거리는 구조적 상한이 약 0.06 밖에 안 되므로
    (두 ECDF 가 중앙에서 교차한다) 이 유형은 L3·L4 를 만들 수 없다 — CASES 에서
    L1·L2 만 쓴다(`levels=LOW_LV`).
    """
    u = rng.uniform(-1.0, 1.0, n)
    g = rng.normal(0.0, 1.0, n)
    return t * u * 1.732 + (1.0 - t) * g


def _sh_plateau(rng, n, t):
    """고원형 — 가운데가 눌린 분포(중심에서 값이 빠져나감). t 가 크면 중앙이 비어 U 자."""
    v = rng.normal(0.0, 1.0, n)
    core = np.abs(v) < 0.6
    k = int(core.sum() * min(t, 1.0))
    if k > 0:
        idx = np.flatnonzero(core)[:k]
        v[idx] = np.sign(rng.normal(0.0, 1.0, k)) * rng.uniform(0.9, 1.6, k)
    return v


def _sh_skew_right(rng, n, t):
    """우측 왜곡 — 로그정규형. 몸통이 왼쪽에 몰리고 오른쪽으로 완만히 늘어진다."""
    return np.exp(rng.normal(0.0, 0.05 + 1.15 * t, n))


def _sh_skew_left(rng, n, t):
    """좌측 왜곡 — 위 왜곡의 거울상."""
    return -_sh_skew_right(rng, n, t)


def _sh_outlier_cluster(rng, n, t):
    """이탈 무리 — 소수 die 가 몸통에서 멀찍이 떨어진 한 덩어리로 존재.

    ⚠ 거리(away)만 키워도 ks 는 거의 안 오른다 — ks 는 **비율**의 최대거리라 die 1%가
    아무리 멀리 가도 0.01 이 상한이다. 그래서 t 는 주로 **비율**을 키운다.
    """
    away = 1.8 + 2.2 * min(t, 1.0)
    v = rng.normal(0.0, 1.0, n)
    k = max(1, int(n * _frac(0.01, 0.42, t)))
    idx = rng.choice(n, size=k, replace=False)
    v[idx] = away + rng.normal(0.0, 0.12, k)
    return v


def _sh_spike(rng, n, t):
    """스파이크 — 특정 한 값에 다수가 몰린다(클램프·포화 계측 모사)."""
    v = rng.normal(0.0, 1.0, n)
    k = max(1, int(n * _frac(0.0, 1.0, t, cap=0.90)))
    idx = rng.choice(n, size=k, replace=False)
    v[idx] = 0.0
    return v


def _sh_truncated(rng, n, t):
    """한쪽 절단 — 어떤 값 위쪽이 잘려 벽이 생긴다(스크리닝·리트라이 모사)."""
    # 절단선은 0 밑으로 내려가지 않게 묶는다 — 더 내리면 분포의 절반 이상이 한 점에
    # 쌓여 σ 가 0 으로 붕괴하고(_rescale 이 상수 배열을 반환) 지표가 통째로 None 이 된다.
    cut = max(2.4 - 2.2 * t, 0.05)
    return np.minimum(rng.normal(0.0, 1.0, n), cut)


def _sh_granular(rng, n, t):
    """거친 이산 — 몸통은 같은데 값 격자가 성기다(실제 이산화는 spec 의 quant 가 한다)."""
    return rng.normal(0.0, 1.0, n)


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
}


def _shape_values(spec, n, rng, t):
    """spec(=한쪽의 모양 정의) + 강도 t → 최종 값 배열.

    shape 로 모양을 만들고 → μ·σ 를 목표에 맞추면서 규격 안에 가둔 뒤
    (`_fit_moments_in_spec` — 단순 클립은 σ 를 갉아먹는다) → quant 가 있으면 이산화한다.

    이산화는 **맨 마지막**이고 그 뒤에 재-rescale 하지 않는다(격자가 깨져 이산이 아니게
    된다). 이산화가 μ·σ 를 아주 조금 흔들지만 격자 폭이 σ 의 1/4 이라 영향이 미미하고,
    solve_t 가 최종 값으로 지표를 실측하므로 목표는 그대로 맞는다.
    """
    raw = SHAPE_FUNCS[spec["shape"]](rng, n, t)
    v = _fit_moments_in_spec(_rescale(raw, spec["mu"], spec["sd"]),
                             spec["mu"], spec["sd"])
    q = spec.get("quant")
    if q:
        v = _clip_spec(_quantize(v, q))
    return v


# ── 목표 지표 역산 ───────────────────────────────────────────────────────────

def _cpk(v):
    """단측 최소 Cpk — 서버 build_cpk_rows 와 같은 정의(min 여유 / 3σ)."""
    mu, sd = float(np.mean(v)), float(np.std(v, ddof=1))
    if sd <= 0:
        return None
    return min(USL - mu, mu - LSL) / (3.0 * sd)


def _measure(before_v, after_v, metric):
    """역산이 쫓는 지표 1개를 실측한다. ks 는 **서버 `_ks_d`** 를 그대로 쓴다."""
    if metric == "ks":
        return _ks_d(np.sort(after_v), np.sort(before_v))
    if metric == "after_cpk":
        return _cpk(after_v)
    if metric == "sd_delta":
        sb = float(np.std(before_v, ddof=1))
        if sb <= 0:
            return None
        return (float(np.std(after_v, ddof=1)) - sb) / sb * 100.0
    raise ValueError(metric)


T_MAX = 3.0      # 강도 탐색 상한. 유형마다 ks 가 t 에 반응하는 기울기가 달라, 목표
                 # 0.42(L4)에 닿으려면 t 2~3 이 필요한 모양이 있다(비대칭 쌍봉 등).
                 # 상한에 닿아도 목표에 못 미치는 유형은 구조적 상한이 있는 것이며,
                 # verify 가 그것을 미검출로 드러낸다(CASES 주석의 levels 조정 신호).


def solve_t(case, level, n, seed, target, metric, lo=0.0, hi=T_MAX, iters=24):
    """목표 지표에 닿는 강도 t 를 이분탐색으로 찾는다.

    유형마다 강도 파라미터의 의미가 달라(봉우리 간격·꼬리 비율·격자 폭 …) 고정 상수로는
    레벨↔검출 대응을 맞출 수 없다 — v3 실측에서 같은 L3 의 ks 가 0.056~1.00 으로 흩어진
    것이 그 증거다. 여기서는 **지표를 목표로 두고 강도를 역산**한다.

    이분탐색은 지표가 t 에 **단조**임을 전제한다. 대부분의 모양이 그렇지만 아닌 것도
    있어서(아래), 탐색 뒤 오차가 크면 **격자 스캔으로 대체**한다.

    ⚠ **비단조 유형이 실제로 있다.** 꼬리 방향 반전(tail_up↔tail_down)은 ks 가 t=1 에서
    0.25 로 정점을 찍고 t=2 에서 0.08 로 **되돌아온다** — 양쪽 꼬리가 함께 길어지면
    두 분포가 (거울상이라) 서로 다시 닮아가기 때문이다. 이분탐색만 쓰면 상한 t=3 에
    잘못 수렴해 목표를 한참 빗나간다(v4 2차 실측: 목표 0.24 → 실측 0.11, L3·L4 미검출).
    격자 스캔은 그런 봉우리 지형에서도 목표에 가장 가까운 t 를 고른다.

    **탐색과 최종 생성이 같은 seed 를 쓰는 것이 중요하다** — 다른 난수로 찾은 t 는
    최종 데이터에서 목표를 빗나간다.
    """
    def measure(t):
        rb = np.random.default_rng(seed)
        ra = np.random.default_rng(seed + 1)
        bv = _shape_values(case["before"](t, level), n, rb, t)
        av = _shape_values(case["after"](t, level), n, ra, t)
        m = _measure(bv, av, metric)
        return None if m is None else m

    def err(t):
        m = measure(t)
        return (float("inf"), None) if m is None else (abs(m - target), m)

    # 방향 판정 — Cpk 목표는 t 가 커질수록 **작아진다**(감소 단조).
    decreasing = metric == "after_cpk"
    a, b = lo, hi
    for _ in range(iters):
        mid = (a + b) / 2.0
        m = measure(mid)
        if m is None:                       # 값이 붕괴한 구간 — 약한 쪽으로 물러난다
            b = mid
            continue
        below = (m > target) if decreasing else (m < target)
        if below:
            a = mid
        else:
            b = mid
    best_t = (a + b) / 2.0
    best_err, best_m = err(best_t)

    # 상대오차가 10% 를 넘으면 단조 가정이 깨진 것으로 보고 격자로 다시 찾는다.
    tol = max(abs(target) * 0.10, 1e-6)
    if best_err > tol:
        for t in np.linspace(lo, hi, 61):
            e, m = err(float(t))
            if e < best_err:
                best_err, best_t, best_m = e, float(t), m
    return best_t, best_m


# ── 케이스 정의 ──────────────────────────────────────────────────────────────
# before/after 는 (t, level) → spec 함수다. t 는 solve_t 가 역산한 강도.
#
# metric: 이 유형이 **어느 판정 경로로** 잡혀야 하는가.
#   "ks"        → 형태/위치 차이(⑥). μ·σ 를 고정한 모양 변화가 대부분 여기다.
#   "after_cpk" → 절대 품질(④). 평균 이동으로 Cpk 를 떨어뜨린다.
#   "sd_delta"  → 산포 증가(⑤).
# 경로를 유형마다 갈라 두는 이유는, 전부 ks 로 맞추면 ④⑤ 경로의 회귀를 이 데이터로
# 볼 수 없기 때문이다.

ALL_LV = (1, 2, 3, 4)
LOW_LV = (1, 2)          # ks 상한이 낮아 L3·L4 를 만들 수 없는 유형 전용

# 이산 격자(σ 대비 비율) — 이산 유형의 고정 격자.
GRAIN_FINE = 0.25

CASES = [
    # ── A. 모양만 다름 (μ·σ 고정) — ks 경로 ──────────────────────────────────
    dict(code="SHAPE_BIMODAL", n_case=2, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="bimodal", mu=CENTER, sd=BASE_SD),
         desc="정규 → 쌍봉 (μ·σ 동일)"),
    dict(code="SHAPE_BIMODAL_SKEW", n_case=2, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="bimodal_skew", mu=CENTER, sd=BASE_SD),
         desc="정규 → 비대칭 쌍봉(소수 모드 혼입)"),
    dict(code="SHAPE_TRIMODAL", n_case=2, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="trimodal", mu=CENTER, sd=BASE_SD),
         desc="정규 → 삼봉(사이트 혼입)"),
    dict(code="SHAPE_TAILFLIP", n_case=2, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="tail_up", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="tail_down", mu=CENTER, sd=BASE_SD),
         desc="꼬리 방향 반전 (위→아래, μ·σ 동일)"),
    dict(code="SHAPE_TAIL_ONESIDE", n_case=2, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="tail_up", mu=CENTER, sd=BASE_SD),
         desc="정규 → 한쪽 꼬리 (σ 는 재정규화로 동일)"),
    dict(code="SHAPE_TAIL_BOTH", n_case=1, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="tail_both", mu=CENTER, sd=BASE_SD),
         desc="정규 → 양쪽 꼬리(몸통 수축)"),
    dict(code="SHAPE_PLATEAU", n_case=1, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="plateau", mu=CENTER, sd=BASE_SD),
         desc="정규 → 중앙 함몰(U 자화)"),
    dict(code="SHAPE_SKEW_R", n_case=1, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="skew_right", mu=CENTER, sd=BASE_SD),
         desc="정규 → 우왜곡(로그정규형)"),
    dict(code="SHAPE_SKEWFLIP", n_case=1, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="skew_right", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="skew_left", mu=CENTER, sd=BASE_SD),
         desc="왜곡 방향 반전 (우→좌, μ·σ 동일)"),
    dict(code="SHAPE_OUTLIER", n_case=2, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="outlier_cluster", mu=CENTER, sd=BASE_SD),
         desc="정규 → 이탈 무리 분리"),
    dict(code="SHAPE_SPIKE", n_case=2, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="spike", mu=CENTER, sd=BASE_SD),
         desc="정규 → 단일값 스파이크(포화·클램프)"),
    dict(code="SHAPE_TRUNC", n_case=1, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="truncated", mu=CENTER, sd=BASE_SD),
         desc="정규 → 한쪽 절단(벽 생성)"),
    # 균등화는 ks 상한이 0.06 근처라(두 ECDF 가 중앙에서 교차) L3·L4 를 만들 수 없다.
    # 억지로 강도를 올리면 σ 가 달라져 다른 경로로 잡히므로, 이 유형은 L1·L2 로만 둔다.
    dict(code="SHAPE_UNIFORM", n_case=2, levels=LOW_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="uniform", mu=CENTER, sd=BASE_SD),
         desc="정규 → 균등(평평한 몸통) — ks 상한이 낮아 L1·L2 만"),

    # ── B. 이산(discrete) — ks 경로 ──────────────────────────────────────────
    # ⚠ 이산 항목은 σ 가 조금만 변해도 계단이 밀려 ks 가 부푼다(compare.py
    # DIST_KS_SOLO_MAX_SD 주석). 그래서 격자는 양쪽 동일하게 두고 **모양만** 바꾼다.
    dict(code="DISC_BIMODAL", n_case=2, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD,
                                   quant=BASE_SD * GRAIN_FINE),
         after=lambda t, lv: dict(shape="bimodal", mu=CENTER, sd=BASE_SD,
                                  quant=BASE_SD * GRAIN_FINE),
         desc="이산 정규 → 이산 쌍봉 (격자 동일)"),
    dict(code="DISC_SPARSE", n_case=1, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD,
                                   quant=BASE_SD * GRAIN_FINE),
         after=lambda t, lv: dict(shape="spike", mu=CENTER, sd=BASE_SD,
                                  quant=BASE_SD * GRAIN_FINE),
         desc="이산 + 한 계단에 몰림 (수준 편중)"),

    # ── C. 위치 이동 — ks 경로 ───────────────────────────────────────────────
    # 평행 이동은 σ 가 그대로라 ⑤ 를 안 타고 ks 로 잡힌다. Cpk 도 함께 떨어지지만
    # L3(ks 0.24 ≈ 0.6σ 이동)에서는 Cpk 1.4 대라 ④ 에는 아직 안 걸린다.
    dict(code="POS_SHIFT", n_case=2, levels=ALL_LV, metric="ks",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="normal", mu=CENTER + BASE_SD * 2.2 * t,
                                  sd=BASE_SD),
         desc="평균만 평행 이동 (σ 동일)"),
    dict(code="POS_SEPARATE", n_case=1, levels=ALL_LV, metric="ks",
         # 양쪽이 서로 반대로 멀어진다. 봉이 좁아(σ 0.55배) 조금만 벌려도 ks 가 오르므로
         # L1·L2 를 미검출로 두려면 이동폭을 v3(0.9σ 고정)보다 훨씬 작게 잡아야 한다 —
         # 그 역산이 solve_t 의 몫이다(v3 는 L1 부터 ks 0.90 이라 전 레벨 검출이었다).
         before=lambda t, lv: dict(shape="normal", mu=CENTER - BASE_SD * 1.6 * t,
                                   sd=BASE_SD * 0.55),
         after=lambda t, lv: dict(shape="normal", mu=CENTER + BASE_SD * 1.6 * t,
                                  sd=BASE_SD * 0.55),
         desc="양쪽이 서로 반대로 이동(분리 방향)"),

    # ── D. 산포 증가 — Δσ% 경로(⑤) ──────────────────────────────────────────
    dict(code="SD_UP", n_case=2, levels=ALL_LV, metric="sd_delta",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="normal", mu=CENTER,
                                  sd=BASE_SD * (1.0 + 1.6 * t)),
         desc="산포만 커짐 (Δσ% 경로)"),

    # ── E. Cpk 하락 — 절대 품질 경로(④) ─────────────────────────────────────
    dict(code="CPK_LOW", n_case=2, levels=ALL_LV, metric="after_cpk",
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="normal", mu=CENTER + BASE_SD * 3.4 * t,
                                  sd=BASE_SD),
         desc="평균 이동으로 Cpk 하락 (절대 품질 경로)"),

    # ── F. 대조군 — 전 레벨 미검출이 정답 ────────────────────────────────────
    # 레벨은 붙이되 before/after 가 같은 분포다(난수만 다르다). L3·L4 도 **미검출**이
    # 정답인 유일한 유형이라, expect 를 레벨이 아니라 여기서 직접 False 로 못박는다.
    dict(code="CONTROL_SAME", n_case=4, levels=ALL_LV, metric=None, control=True,
         before=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         after=lambda t, lv: dict(shape="normal", mu=CENTER, sd=BASE_SD),
         desc="[대조군] before/after 동일 분포 — 전 레벨 미검출이 정답"),
]


def _stable_seed(name: str) -> int:
    """item 이름으로 고정 seed — 다른 item 이 추가돼도 기존 값이 흔들리지 않는다."""
    return abs(hash(name)) % (2 ** 31) if False else (
        int.from_bytes(name.encode("utf-8")[:8].ljust(8, b"\0"), "little") % (2 ** 31))


def build_plan(n_die, quiet=False):
    """전 item 계획 — 유형×case×레벨마다 강도 t 를 역산해 붙인다."""
    items = []
    tno = 1000
    for case in CASES:
        for i in range(case["n_case"]):
            for lv in case["levels"]:
                tno += 1
                name = f"{case['code']}_{i + 1:02d}_L{lv}"
                seed = _stable_seed(name)
                if case.get("control"):
                    t, achieved = 0.0, None
                else:
                    metric = case["metric"]
                    target = {"ks": TARGET_KS, "after_cpk": TARGET_CPK,
                              "sd_delta": TARGET_SD}[metric][lv]
                    t, achieved = solve_t(case, lv, n_die, seed, target, metric)
                    if not quiet:
                        print(f"  · {name:32s} {metric:9s} 목표 {target:7.3f} "
                              f"→ 실측 {achieved:7.3f} (t={t:.4f})")
                items.append({
                    "name": name, "case": case["code"], "level": lv, "tno": tno,
                    "desc": case["desc"], "metric": case["metric"] or "",
                    "control": bool(case.get("control")),
                    "t": t, "achieved": achieved, "seed": seed,
                    "expect_focus": False if case.get("control") else EXPECT_FOCUS[lv],
                    "before": case["before"](t, lv),
                    "after": case["after"](t, lv),
                    "_case": case,
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

def build_frame(items, side, xs, ys):
    """한쪽(before/after) 7-meta honeyform DataFrame.

    전 die 가 Bin1 이다 — dist_shift 모집단이 Bin1 뿐이라 여기서 fail 을 만들면 의도한
    분포가 통계에서 부분적으로 빠진다(모듈 docstring 제약 ①).

    ⚠ 난수는 **item 마다 고정 seed**(solve_t 와 같은 seed)로 뽑는다. 하나의 rng 를
    순서대로 흘려 쓰면 item 이 추가·삭제될 때 뒤 항목의 값이 전부 바뀌어, 역산해 둔 t 가
    가리키던 지표를 빗나간다.
    """
    n = xs.size
    meta_rows = {k: {} for k in META_ROW_LABELS}
    cols = {}
    for tseq, it in enumerate(items, start=1):
        rng = np.random.default_rng(it["seed"] + (0 if side == "before" else 1))
        v = _shape_values(it[side], n, rng, it["t"])
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
    """서버 경로로 검출을 계산해 **레벨 기대**와 대조. (rows, 불일치 수, counts) 반환."""
    from web_report.tabs.compare import build_dist_shift
    from web_report.tabs.cpk import build_cpk_rows

    t_before = split_honeyform(df_before, source="WF_BEFORE", file_name="before.csv")
    t_after = split_honeyform(df_after, source="WF_AFTER", file_name="after.csv")
    stat_items = sorted(set(t_after.item_columns) | set(t_before.item_columns))
    cpk_rows = build_cpk_rows([t_after, t_before], stat_items)
    dist = build_dist_shift([t_after, t_before], cpk_rows)
    by_item = {r["subject"]: r for r in dist["rows"]}

    rows, bad = [], 0
    for it in items:
        r = by_item.get(it["name"]) or {}
        focus = bool(r.get("focus"))
        expect = bool(it["expect_focus"])
        ok = focus == expect
        if not ok:
            bad += 1
        b, a = r.get("before") or {}, r.get("after") or {}
        rows.append({
            "item": it["name"], "case": it["case"], "level": it["level"],
            "level_ko": LEVEL_KO[it["level"]], "desc": it["desc"],
            "metric": it["metric"], "target_t": round(it["t"], 4),
            "solved_metric": (None if it["achieved"] is None
                              else round(float(it["achieved"]), 4)),
            "expect_focus": int(expect), "actual_focus": int(focus), "ok": int(ok),
            "note": "" if ok else ("미검출(기대: 검출)" if expect else "오검출(기대: 미검출)"),
            # 판정에 쓰이는 지표
            "cpk_ratio_pct": r.get("cpk_ratio_pct"),
            "stdev_delta_pct": r.get("stdev_delta_pct"),
            "ks_d": r.get("ks_d"), "meanshift_sigma": r.get("meanshift_sigma"),
            "p_mean": r.get("p_mean"), "p_stdev": r.get("p_stdev"),
            "before_avg": b.get("average"), "before_stdev": b.get("stdev"),
            "before_cpk": b.get("cpk"),
            "after_avg": a.get("average"), "after_stdev": a.get("stdev"),
            "after_cpk": a.get("cpk"),
        })
    counts = {"total": dist["summary"]["total"], "focus": dist["summary"]["focus"]}
    return rows, bad, counts


def answer_rows(items):
    out = []
    for it in items:
        b, a = it["before"], it["after"]
        out.append({
            "item": it["name"], "case": it["case"], "level": it["level"],
            "level_ko": LEVEL_KO[it["level"]], "desc": it["desc"],
            "expect_focus": int(it["expect_focus"]),
            "detect_path": it["metric"], "strength_t": round(it["t"], 4),
            "solved_metric": (None if it["achieved"] is None
                              else round(float(it["achieved"]), 4)),
            "tno": it["tno"], "unit": UNIT, "lsl": LSL, "usl": USL,
            "before_shape": b["shape"], "before_mean": round(b["mu"], 6),
            "before_stdev": round(b["sd"], 6), "before_quant": b.get("quant") or "",
            "after_shape": a["shape"], "after_mean": round(a["mu"], 6),
            "after_stdev": round(a["sd"], 6), "after_quant": a.get("quant") or "",
        })
    return out


def summarize(rows):
    """레벨×검출 요약 — 이 데이터의 계약이 지켜졌는지 한 화면에 보인다."""
    df = pd.DataFrame(rows)
    print("\n[레벨별 검출률] 1.0=전부 검출 / 0.0=전부 미검출")
    lv = df[~df["case"].str.startswith("CONTROL")]
    print(lv.groupby("level")["actual_focus"].agg(["mean", "count"]).round(3).to_string())

    print("\n[유형×레벨 검출률] 행=유형, 열=레벨")
    piv = df.pivot_table(index="case", columns="level", values="actual_focus",
                         aggfunc="mean")
    print(piv.round(2).to_string())

    bad = df[df["ok"] == 0]
    if not bad.empty:
        print(f"\n[불일치] {len(bad)}건")
        print(bad.groupby(["case", "level", "note"]).size().rename("건수").to_string())


def main():
    ap = argparse.ArgumentParser(
        description="Compare 검출 레벨 정렬 데이터 (L1·L2 미검출 / L3·L4 검출)")
    ap.add_argument("--out-dir", default=str(_ROOT / "data"))
    ap.add_argument("--prefix", default="compare_level_v4")
    ap.add_argument("--radius", type=int, default=WAFER_RADIUS)
    ap.add_argument("--quiet-solve", action="store_true", help="역산 로그 숨김")
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    xs, ys = build_wafer(args.radius)
    print(f"[역산] die {xs.size} — 레벨별 목표 지표에 닿는 강도 t 를 찾는다")
    items = build_plan(xs.size, quiet=args.quiet_solve)

    df_before = build_frame(items, "before", xs, ys)
    df_after = build_frame(items, "after", xs, ys)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p_before = out_dir / f"{args.prefix}_before.csv"
    p_after = out_dir / f"{args.prefix}_after.csv"
    p_answer = out_dir / f"{args.prefix}_answer.csv"
    p_verify = out_dir / f"{args.prefix}_verify.csv"

    df_before.to_csv(p_before, index=False, encoding="utf-8-sig")   # Excel 용 BOM
    df_after.to_csv(p_after, index=False, encoding="utf-8-sig")
    pd.DataFrame(answer_rows(items)).to_csv(p_answer, index=False, encoding="utf-8-sig")

    print(f"\n[생성] die {xs.size} · item {len(items)} · 유형 {len(CASES)}")
    print(f"  {p_before}")
    print(f"  {p_after}")
    print(f"  {p_answer}")

    if args.no_verify:
        return 0

    rows, bad, counts = verify(df_before, df_after, items)
    pd.DataFrame(rows).to_csv(p_verify, index=False, encoding="utf-8-sig")
    print(f"  {p_verify}")
    print(f"\n[실측] 공통 항목 {counts['total']} · 검출 {counts['focus']}")
    summarize(rows)
    if bad:
        print(f"\n[실패] 레벨 기대와 다른 항목 {bad}개 — {p_verify} 의 ok=0 행")
        return 1
    print(f"\n[성공] 전 {len(rows)}개 항목이 레벨 기대와 일치 "
          f"(L1·L2 미검출 / L3·L4 검출)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

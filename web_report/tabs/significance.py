"""2표본 유의성 검정 — scipy 없이 numpy/stdlib 만으로.

Compare 산포 비교(dist_shift)의 **노이즈 게이트**용이다. 이 저장소는 폐쇄망 wheelhouse 로
배포해(server/requirements.txt → wheelhouse 재생성 → 운영 PC 재설치) scipy 추가 비용이 커서,
필요한 분포 함수 하나(Student-t CDF)만 직접 구현한다. 검정 로직을 Compare 업무 로직에서
떼어낸 이유는 알려진 값으로 **독립 검증**하기 위해서다(tests/test_significance.py).

⚠ die 단위 p-value 의 해석 한계 — 이 모듈을 쓰는 쪽이 반드시 알아야 한다:
같은 wafer 안의 die 는 공간 상관이 있어 독립 표본이 아니다. 그래서 여기서 나오는 p 는
실제보다 **낙관적으로(작게)** 나온다. 게다가 pooled n 이 수천~수만이라 실무상 무의미한
차이도 거의 항상 유의하게 나온다. 따라서 "p 가 작다 → 진짜 차이" 로는 쓸 수 없고,
"p 가 커서 낙관적으로 봐도 유의하지 않다 → 노이즈" 라는 **한 방향으로만** 신뢰할 수 있다.
호출부(compare._dist_focus)가 p 를 관심 항목 **억제**에만 쓰고 포함 근거로는 쓰지 않는
이유가 이것이다.
"""
from __future__ import annotations

import math

import numpy as np

_MAXIT = 300
_EPS = 3.0e-16
_FPMIN = 1.0e-300


def _betacf(a, b, x):
    """정규화 불완전베타의 연분수 (수정 Lentz 알고리즘)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def betainc(a, b, x):
    """정규화 불완전베타 I_x(a, b) ∈ [0, 1]."""
    if not (math.isfinite(a) and math.isfinite(b) and math.isfinite(x)):
        return None
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log1p(-x))
    # 연분수는 x < (a+1)/(a+b+2) 에서 빨리 수렴한다 — 아니면 대칭식 I_x(a,b)=1−I_{1−x}(b,a).
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1.0 - x) / b


def t_sf_two_sided(t, df):
    """양측 p = P(|T| > |t|), T ~ t(df). 항등식 I_{df/(df+t²)}(df/2, 1/2)."""
    if df is None or t is None or df <= 0:
        return None
    if not (math.isfinite(t) and math.isfinite(df)):
        return None
    return betainc(0.5 * df, 0.5, df / (df + t * t))


def welch_p(m1, s1, n1, m2, s2, n2):
    """Welch 2표본 t-test 양측 p (등분산 가정 없음). 계산 불가면 None.

    입력이 결측이거나 어느 한쪽 n<2, 또는 양쪽 분산이 모두 0 이면 검정이 성립하지 않는다.
    """
    if None in (m1, s1, n1, m2, s2, n2):
        return None
    if n1 < 2 or n2 < 2:
        return None
    v1 = s1 * s1 / n1
    v2 = s2 * s2 / n2
    se2 = v1 + v2
    if se2 <= 0:
        return None                      # 양쪽 다 고정값 — 비교할 산포가 없다
    t = (m1 - m2) / math.sqrt(se2)
    denom = v1 * v1 / (n1 - 1) + v2 * v2 / (n2 - 1)
    if denom <= 0:
        return None
    df = se2 * se2 / denom               # Welch–Satterthwaite
    return t_sf_two_sided(t, df)


def spread_stats(sorted_values):
    """Brown-Forsythe 용 |x − median| 의 (mean, std ddof=1, n). n<3 이면 None.

    입력은 NaN 제외 **오름차순 정렬된** ndarray — 호출부가 KS 용으로 이미 만든 배열을
    그대로 재사용해 데이터 추가 순회를 피한다.
    """
    n = int(len(sorted_values))
    if n < 3:
        return None
    med = float(np.median(sorted_values))
    z = np.abs(sorted_values - med)
    return float(z.mean()), float(z.std(ddof=1)), n


def brown_forsythe_p(sorted_a, sorted_b):
    """산포 차이 유의성 — 2군 Brown-Forsythe = |x−median| 에 대한 Welch t.

    F-test 를 쓰지 않는 이유: F-test 는 정규성 이탈에 극도로 민감해 규격 절단·bimodal·
    이산(code unit) 분포에서 오경보율이 명목 5% 를 크게 웃돈다. 그러면 억제 게이트가
    거의 아무것도 억제하지 못해 무의미해진다. 중앙값 기준의 BF 는 그런 분포에서도
    유의수준을 지킨다. 2군일 때 BF 의 ANOVA F 는 z 에 대한 2표본 t 의 제곱과 동치라,
    표본 크기가 다를 때 더 안전한 Welch 변형을 쓴다.
    """
    za = spread_stats(sorted_a)
    zb = spread_stats(sorted_b)
    if za is None or zb is None:
        return None
    return welch_p(*za, *zb)

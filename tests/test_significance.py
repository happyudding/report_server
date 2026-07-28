"""유의성 검정 헬퍼 회귀 테스트 (2026-07-28).

실행:
    python tests/test_significance.py

scipy 없이 직접 구현한 분포 함수라, **닫힌 형태로 값을 아는 케이스**로 고정한다.
  - t(1) = Cauchy → P(|T|>1) = 0.5 (정확)
  - t(2) → P(T>t) = ½(1 − t/√(2+t²)) (정확)
  - t(4) → F(t) = ½ + ¼·u·(3 − u²)  (u = t/√(4+t²), 정확)
  - t=0 → p=1, 큰 df → 정규 근사(1.959964 에서 0.05)
  - betainc 대칭성 I_x(a,b) = 1 − I_{1−x}(b,a), I_x(1,1) = x

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.tabs.significance import (betainc, brown_forsythe_p,  # noqa: E402
                                          spread_stats, t_sf_two_sided, welch_p)


def _close(a, b, tol=1e-10):
    return a is not None and abs(a - b) <= tol


def _t2_exact(t):
    """t(2) 양측 정확값."""
    return 1.0 - t / math.sqrt(2.0 + t * t)


def _t4_exact(t):
    """t(4) 양측 정확값 — F(t) = ½ + ¼·u·(3 − u²), u = t/√(4+t²) (u→1 에서 F→1)."""
    u = t / math.sqrt(4.0 + t * t)
    return 1.0 - 0.5 * u * (3.0 - u * u)


def test_betainc_known_values():
    """I_x(1,1) = x, 대칭식, 경계."""
    for x in (0.01, 0.25, 0.5, 0.75, 0.99):
        assert _close(betainc(1.0, 1.0, x), x), (x, betainc(1.0, 1.0, x))
    # I_x(a,b) = 1 − I_{1−x}(b,a) — 연분수의 두 분기를 교차 검증한다.
    for a, b, x in ((0.5, 3.0, 0.2), (5.0, 0.5, 0.7), (2.5, 7.5, 0.4), (500.0, 0.5, 0.99)):
        assert _close(betainc(a, b, x), 1.0 - betainc(b, a, 1.0 - x), 1e-12), (a, b, x)
    assert betainc(2.0, 3.0, 0.0) == 0.0
    assert betainc(2.0, 3.0, 1.0) == 1.0
    assert betainc(2.0, 3.0, -0.5) == 0.0


def test_t_two_sided_exact_cases():
    """닫힌 형태를 아는 df 에서 정확히 일치해야 한다."""
    # t(1) = Cauchy: P(|T|>1) = 0.5
    assert _close(t_sf_two_sided(1.0, 1.0), 0.5), t_sf_two_sided(1.0, 1.0)
    assert _close(t_sf_two_sided(-1.0, 1.0), 0.5), t_sf_two_sided(-1.0, 1.0)
    for t in (0.5, 1.0, 2.0, 3.5, 10.0):
        assert _close(t_sf_two_sided(t, 2.0), _t2_exact(t)), (t, t_sf_two_sided(t, 2.0))
        assert _close(t_sf_two_sided(t, 4.0), _t4_exact(t)), (t, t_sf_two_sided(t, 4.0))
    # t=0 이면 어떤 df 에서도 p=1
    for df in (1.0, 2.5, 30.0, 5000.0):
        assert _close(t_sf_two_sided(0.0, df), 1.0), df
    # df→∞ 는 정규분포 — 1.959964 에서 양측 0.05
    assert _close(t_sf_two_sided(1.959964, 5_000_000.0), 0.05, 1e-6), \
        t_sf_two_sided(1.959964, 5_000_000.0)
    # 널리 쓰이는 표값 (df=10, t=2.0 → 0.0734)
    assert _close(t_sf_two_sided(2.0, 10.0), 0.073388, 1e-6), t_sf_two_sided(2.0, 10.0)
    # 단조성 — |t| 가 커지면 p 는 줄어든다
    ps = [t_sf_two_sided(t, 9.0) for t in (0.5, 1.0, 2.0, 4.0, 8.0)]
    assert all(ps[i] > ps[i + 1] for i in range(len(ps) - 1)), ps
    assert t_sf_two_sided(1.0, 0.0) is None and t_sf_two_sided(float("nan"), 5.0) is None


def test_welch_p_basics():
    """동일 표본 → p=1, 큰 차이 → p≈0, 계산 불가 조건 → None."""
    assert _close(welch_p(10.0, 2.0, 50, 10.0, 2.0, 50), 1.0), welch_p(10.0, 2.0, 50, 10.0, 2.0, 50)
    # 등분산·동일 n 이면 Welch = Student, df = 2n−2 로 손계산과 맞아야 한다.
    m1, m2, s, n = 11.0, 10.0, 2.0, 26
    t = (m1 - m2) / math.sqrt(s * s / n + s * s / n)
    assert _close(welch_p(m1, s, n, m2, s, n), t_sf_two_sided(t, 2.0 * n - 2.0), 1e-9)
    # 표본이 커질수록 같은 효과크기의 p 는 작아진다(= 큰 n 에서 게이트가 무동작인 이유).
    small = welch_p(10.2, 1.0, 10, 10.0, 1.0, 10)
    big = welch_p(10.2, 1.0, 5000, 10.0, 1.0, 5000)
    assert small > 0.5 and big < 1e-10, (small, big)
    assert welch_p(10.0, 1.0, 1, 10.0, 1.0, 30) is None      # n<2
    assert welch_p(10.0, 0.0, 30, 10.0, 0.0, 30) is None     # 양쪽 고정값
    assert welch_p(None, 1.0, 30, 10.0, 1.0, 30) is None


def test_spread_and_brown_forsythe():
    """BF 는 평균이 같아도 산포 차이를 잡고, 산포가 같으면 안 잡는다."""
    assert spread_stats(np.array([1.0, 2.0])) is None        # n<3
    mean_z, std_z, n = spread_stats(np.array([8.0, 9.0, 10.0, 11.0, 12.0]))
    assert n == 5 and _close(mean_z, 1.2, 1e-12), (mean_z, n)   # |x−10| 평균

    base = np.sort(np.array([8.0, 9.0, 10.0, 11.0, 12.0] * 30))   # n=150
    same = base.copy()
    wide = np.sort(10.0 + (base - 10.0) * 1.2)                    # 평균 동일, σ +20%
    assert _close(brown_forsythe_p(same, base), 1.0), brown_forsythe_p(same, base)
    p_wide = brown_forsythe_p(wide, base)
    assert p_wide < 0.05, p_wide                                  # n=150 이면 유의
    # 같은 +20% 라도 n=15 면 추정 노이즈와 구분되지 않는다 — 게이트가 존재하는 이유.
    small_b = np.sort(np.array([8.0, 9.0, 10.0, 11.0, 12.0] * 3))
    small_a = np.sort(10.0 + (small_b - 10.0) * 1.2)
    p_small = brown_forsythe_p(small_a, small_b)
    assert p_small > 0.4, p_small
    # 평균만 다르고 산포가 같으면 BF 는 반응하지 않는다(평균 검정과 역할 분리).
    shifted = np.sort(base + 5.0)
    assert _close(brown_forsythe_p(shifted, base), 1.0), brown_forsythe_p(shifted, base)
    assert brown_forsythe_p(np.array([]), base) is None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_betainc_known_values, test_t_two_sided_exact_cases,
               test_welch_p_basics, test_spread_and_brown_forsythe):
        fn()
        checks += 1
    print(f"PASS: test_significance ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

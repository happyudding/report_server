"""L1 metrics 단독 테스트 — CODE_TO_PORT §2 공식 검증."""
import math

import pytest

from eval_engine.pipeline import metrics


def test_cpk_summary_basic():
    # values=[10,12,14,16,18], lsl=5, usl=25
    # mean=14, std(ddof=1)=sqrt(10)=3.16228
    out = metrics.cpk_summary([10, 12, 14, 16, 18], 5, 25)
    assert out["n"] == 5
    assert out["mean"] == pytest.approx(14.0)
    assert out["stdev"] == pytest.approx(math.sqrt(10))
    assert out["cp"] == pytest.approx(20 / (6 * math.sqrt(10)))
    assert out["cpl"] == pytest.approx(9 / (3 * math.sqrt(10)))
    assert out["cpu"] == pytest.approx(11 / (3 * math.sqrt(10)))
    assert out["cpk"] == pytest.approx(min(out["cpl"], out["cpu"]))


def test_cpk_summary_single_value():
    out = metrics.cpk_summary([7.0], 0, 10)
    assert out["n"] == 1
    assert out["stdev"] is None
    assert out["cpk"] is None  # n<=1 → 불가


def test_cpk_summary_zero_std():
    out = metrics.cpk_summary([5, 5, 5, 5], 0, 10)
    assert out["cpk"] is None  # std==0 → 불가


def test_cpk_summary_no_limits():
    out = metrics.cpk_summary([1, 2, 3, 4], None, None)
    assert out["cpk"] is None  # lsl/usl 없음 → 불가
    assert out["mean"] == pytest.approx(2.5)  # 통계는 계산됨


def test_cpk_summary_empty():
    out = metrics.cpk_summary([], 0, 10)
    assert out["n"] == 0
    assert out["mean"] is None


def test_bimodality_bimodal_gt_unimodal():
    unimodal = [10, 10.1, 9.9, 10.2, 9.8, 10.0, 10.1, 9.9, 10.0, 10.05]
    bimodal = [0, 0.1, 0.2, 0.1, 0, 10, 10.1, 9.9, 10.0, 10.2]
    bc_uni = metrics._bimodality_coefficient(unimodal)
    bc_bi = metrics._bimodality_coefficient(bimodal)
    assert bc_bi > bc_uni
    assert bc_bi > 0.555  # Sarle 임계


def test_compute_yield_from_fail_mask():
    case = {"values": [1.0, 2.0, 3.0, 4.0, 5.0],
            "fail_mask": [False, False, True, False, True],
            "lsl": 0, "usl": 10}
    m = metrics.compute(case)
    assert m["total_count"] == 5
    assert m["fail_count"] == 2
    assert m["yield"] == pytest.approx(0.6)
    assert m["cpk"] is not None


def test_compute_degrade_passthrough():
    # values 없는 degrade 모드 — yield/fail_count 그대로
    case = {"values": [], "fail_mask": [],
            "yield": 0.68, "fail_count": 3, "total_count": 280,
            "lsl": 1.0, "usl": 1.4}
    m = metrics.compute(case)
    assert m["yield"] == 0.68
    assert m["fail_count"] == 3
    assert m["total_count"] == 280
    assert m["cpk"] is None  # raw 없음
    assert m["bimodality"] is None

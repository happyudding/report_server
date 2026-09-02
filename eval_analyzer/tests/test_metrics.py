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


def test_compute_yield_uses_full_dut_denominator():
    """ingest 가 넣은 total_count/fail_count(전체 DUT 기준)가 있으면 그것이 분모/분자.

    item 셀 파싱 성공분(len(values))으로 재면 item 마다 분모가 달라져 비교가 왜곡된다.
    """
    case = {"values": [1.0, 2.0, 3.0],          # 파싱 성공 3개뿐
            "fail_mask": [False, True, False],
            "total_count": 10, "fail_count": 4,  # 전체 행 기준
            "lsl": 0, "usl": 10}
    m = metrics.compute(case)
    assert m["total_count"] == 10
    assert m["fail_count"] == 4
    assert m["yield"] == pytest.approx(0.6)


def test_cpk_uses_bin1_population_only():
    """cpk 계열은 Bin1(양품) die 만으로 낸다 (2026-09-02).

    report_server 의 CPK 탭/Issue Table 이 Bin1 기준이라(web_report/tabs/cpk.py) 엔진이
    전 die 로 재면 같은 항목의 cpk 가 두 값으로 갈린다 — 화면은 1.05 인데 LOW_CPK 가
    안 뜨는 "미분류" 의 원인이었다.
    """
    vals = [5.0] * 10 + [1.0, 9.0]          # 뒤 2개가 non-Bin1
    case = {"values": vals, "lsl": 0.0, "usl": 10.0,
            "bin1_mask": [True] * 10 + [False, False],
            "fail_mask": [False] * 10 + [True, True]}
    m = metrics.compute(case)
    bin1_only = metrics.cpk_summary([5.0] * 10, 0.0, 10.0)
    all_die = metrics.cpk_summary(vals, 0.0, 10.0)
    assert m["cpk"] == bin1_only["cpk"]
    assert m["cpk"] != all_die["cpk"]        # 두 모집단이 실제로 다른 값을 낸다
    # 분모(yield 계열)는 여전히 전 die — cpk 만 모집단이 다르다.
    assert m["total_count"] == 12


def test_cpk_falls_back_to_all_die_without_mask():
    """bin1_mask 가 없는 입력(레거시 raw_table·degrade)은 종전대로 전 die 기준."""
    vals = [5.0] * 10 + [1.0, 9.0]
    m = metrics.compute({"values": vals, "lsl": 0.0, "usl": 10.0,
                         "fail_mask": [False] * 12})
    assert m["cpk"] == metrics.cpk_summary(vals, 0.0, 10.0)["cpk"]


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

"""UNKNOWN 미분류 명시 발화 — "모든 fail 은 signature 로 설명된다" 의 하한선.

발화 0건인 fail 이 status=OK 로 새던 구멍을 L3 에서 막는다(2026-08-12).
사유 코드(NO_STATS_PF/NO_LIMIT/LOW_SAMPLE/NO_MATCH)는 "무엇을 고쳐야 unknown 이
줄어드는가" 를 가르는 값이라 함께 검증한다 — 서버 커버리지 집계가 이 코드를 센다.
"""
import pytest

from eval_engine.pipeline import _rules, signatures, status

UNKNOWN = signatures._UNKNOWN_ID


def _case(**kw):
    """어떤 룰도 겨냥하지 않는 중립 case_ctx (fail 1개 = UNKNOWN 발화 대상)."""
    c = {"product_type": None, "item_class": None, "bin": 99,
         "lsl": 0.0, "usl": 10.0, "value_type": "V", "unit": "V", "fail_count": 1}
    c.update(kw)
    return c


def _quiet_features(**kw):
    """모든 조건을 비껴가는 features — 이 상태에서 발화하면 그 룰의 임계값이 잘못된 것."""
    f = {"spread_norm": 0.05, "skewness": 0.1, "kurtosis": 0.0, "outlier_ratio": 0.0,
         "spec_margin_low": 5.0, "spec_margin_high": 5.0, "site_cpk_delta": 0.0,
         "edge_fail_ratio": 1.0, "center_fail_ratio": 1.0, "quadrant_imbalance": 0.0,
         "bimodality_score": 0.1, "n_dut": 100}
    f.update(kw)
    return f


def _reason(sig_result):
    """발화 목록에서 UNKNOWN 의 사유 코드. 없으면 None."""
    for s in sig_result["signatures"]:
        if s["id"] == UNKNOWN:
            return s.get("unknown_reason")
    return None


def test_unclassified_fail_fires_unknown_and_blocks_ok():
    """fail 인데 아무 룰도 안 뜨면 UNKNOWN 발화 + status 는 OK 가 아니다."""
    case, feats = _case(), _quiet_features()
    raw = {"yield": 0.99, "cpk": 2.0}
    sig = signatures.evaluate(case, feats, raw)
    assert [s["id"] for s in sig["signatures"]] == [UNKNOWN]
    assert _reason(sig) == "NO_MATCH"
    verdict = status.decide(case, feats, sig)
    assert verdict["primary_signature"] == UNKNOWN
    assert verdict["status"] != "OK"          # 설명 못 한 fail 을 정상 확정하지 않는다


def test_no_fail_stays_ok_without_unknown():
    """fail 이 0 이면 UNKNOWN 을 붙이지 않는다 — 정상 확정(OK) 경로는 그대로다."""
    case, feats = _case(fail_count=0), _quiet_features()
    sig = signatures.evaluate(case, feats, {"yield": 1.0, "cpk": 2.0})
    assert sig["signatures"] == []
    assert status.decide(case, feats, sig)["status"] == "OK"


def test_unknown_not_added_when_another_rule_fires():
    """다른 룰이 하나라도 뜨면 UNKNOWN 은 붙지 않는다(중복 표시 방지)."""
    case = _case()
    feats = _quiet_features(outlier_ratio=0.10)     # > outlier_ratio_bad
    sig = signatures.evaluate(case, feats, {"yield": 0.95, "cpk": 1.5})
    ids = [s["id"] for s in sig["signatures"]]
    assert "SEVERE_OUTLIER" in ids and UNKNOWN not in ids


def test_unknown_skipped_when_fail_unknown():
    """fail 수를 알 수 없으면(키 자체 부재) 발화하지 않는다 — 모름을 fail 로 읽지 않는다."""
    case = _case()
    case.pop("fail_count")
    sig = signatures.evaluate(case, _quiet_features(), {"yield": None, "cpk": 2.0})
    assert UNKNOWN not in [s["id"] for s in sig["signatures"]]


def test_pf_case_fires_unknown_with_unit_in_note():
    """PF(통계 없음) fail 은 UNKNOWN + UNIT 원문이 사유에 실린다 — 단위표 등록 대상 식별용."""
    sig = signatures.evaluate(_case(value_type="PF", unit="LSB"), _quiet_features(),
                              {"yield": 0.99, "cpk": None})
    assert _reason(sig) == "NO_STATS_PF"
    note = next(e["note"] for s in sig["signatures"] if s["id"] == UNKNOWN
                for e in s["evidence"])
    assert "LSB" in note


@pytest.mark.parametrize("kw, feats_kw, expect", [
    ({"value_type": "PF", "lsl": None, "usl": None}, {"n_dut": 3}, "NO_STATS_PF"),
    ({"lsl": None, "usl": None}, {"n_dut": 3}, "NO_LIMIT"),
    ({}, {"n_dut": 3}, "LOW_SAMPLE"),
    ({}, {}, "NO_MATCH"),
])
def test_unknown_reason_priority(kw, feats_kw, expect):
    """사유는 우선순위대로 하나만 — PF > limit 없음 > 표본 부족 > 조건 미달.

    사유 판정 함수를 직접 부른다 — MISSING_LIMIT·LOW_SAMPLE_UNCERTAIN 룰이 켜져 있으면
    그 케이스는 애초에 UNKNOWN 까지 오지 않으므로(그게 정상), evaluate 경유로는 배포
    on/off 에 따라 결과가 갈린다.
    """
    th = _rules.thresholds_for(_case(**kw))
    code, _ = signatures._unknown_reason(_case(**kw), _quiet_features(**feats_kw),
                                         {"cpk": None}, th)
    assert code == expect


def test_excluded_case_has_no_unknown():
    """평가 제외 목록에 걸린 item 은 UNKNOWN 도 붙이지 않는다(완전 제외)."""
    case = _case(item_raw="VDD_CODE_TRIM", item_canonical="VDD_CODE_TRIM")
    sig = signatures.evaluate(case, _quiet_features(), {"yield": 0.99, "cpk": 2.0})
    assert sig["excluded"]
    assert sig["signatures"] == []


def test_low_cpk_does_not_bury_its_cause():
    """cpk 는 **결과** 지표다 — 원인 룰이 떠 있으면 primary 자리를 그쪽에 내준다.

    2026-08-13 부터 LOW_CPK 는 목록에서 사라지지 않고 **primary 만 양보**한다 —
    "cpk 도 낮고 outlier 도 있다" 를 둘 다 보여 달라는 요구 때문. 대표가 원인 룰이어야
    한다는 원칙은 그대로다(안 그러면 "무엇을 고쳐야 하나" 가 사라진다).
    """
    case = _case()
    # 멀리 떨어지고(4 MAD 초과) 몸통과 끊긴(1.5σ 초과) fail = cpk 하락의 원인
    feats = _quiet_features(fail_mad_min=10.0, fail_pass_gap_sigma=3.0)
    sig = signatures.evaluate(case, feats, {"yield": 0.95, "cpk": 1.0})
    ids = [s["id"] for s in sig["signatures"]]
    assert {"OUTLIER", "LOW_CPK"} <= set(ids)
    assert status.decide(case, feats, sig)["primary_signature"] == "OUTLIER"

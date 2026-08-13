"""signature `suppressed_by` — **primary 양보** 회귀 (2026-08-13 의미 변경).

배경: 원인 룰과 결과 룰이 함께 뜰 때(예: OUTLIER 가 stdev 를 부풀려 LOW_CPK 도 뜬다)
대표(primary)는 원인 쪽이어야 "무엇을 고쳐야 하나" 가 보인다. 종전에는 결과 룰을 발화
목록에서 **지웠는데**, 그러면 "cpk 도 낮고 outlier 도 있다" 가 한 줄로만 보여 사용자가
나머지를 영영 볼 수 없었다. 지금은 목록에 남기고 primary 후보에서만 뺀다.
"""
import pytest

from eval_engine.pipeline import signatures, status


def _fired(ids, hint="MAJOR"):
    return [{"id": i, "status_hint": hint, "score": None, "evidence": []} for i in ids]


def test_demoted_stays_in_list():
    """지목한 룰이 함께 떴으면 **목록에는 남고** `demoted_by` 표시가 붙는다."""
    kept, demoted = signatures._apply_suppression(
        _fired(["OUTLIER", "HEAVY_TAIL"]),
        {"OUTLIER": [], "HEAVY_TAIL": ["OUTLIER"]})
    assert [s["id"] for s in kept] == ["OUTLIER", "HEAVY_TAIL"]
    assert demoted == [{"id": "HEAVY_TAIL", "by": ["OUTLIER"]}]
    assert next(s for s in kept if s["id"] == "HEAVY_TAIL")["demoted_by"] == ["OUTLIER"]


def test_not_demoted_when_suppressor_absent():
    """지목한 룰이 안 떴으면 표시도 없다 — 양보는 동반발화 때만 의미가 있다."""
    kept, demoted = signatures._apply_suppression(
        _fired(["HEAVY_TAIL"]), {"HEAVY_TAIL": ["OUTLIER"]})
    assert [s["id"] for s in kept] == ["HEAVY_TAIL"]
    assert demoted == [] and "demoted_by" not in kept[0]


def test_no_declaration_is_identity():
    """선언이 없으면 목록을 그대로 돌려준다(기존 룰 전부 무영향)."""
    fired = _fired(["A", "B"])
    kept, demoted = signatures._apply_suppression(fired, {"A": [], "B": []})
    assert kept is fired and demoted == []


def test_one_pass_not_transitive():
    """전이하지 않는다 — B 가 A 에 양보해도 C 는 B 의 **원본** 발화만 본다.

    체인을 따라가면 순환에서 무한루프가 나므로 1패스로 고정한 설계다.
    """
    kept, demoted = signatures._apply_suppression(
        _fired(["A", "B", "C"]), {"A": [], "B": ["A"], "C": ["B"]})
    assert [s["id"] for s in kept] == ["A", "B", "C"]      # 아무도 사라지지 않는다
    assert [d["id"] for d in demoted] == ["B", "C"]


def test_primary_goes_to_the_cause():
    """같은 severity 면 양보하지 않은 쪽이 primary — 이것이 선언의 실효다."""
    case = {"product_type": None, "family_product": None, "item_class": None,
            "bin": 99, "lsl": 0.0, "usl": 10.0, "value_type": "V"}
    fired = _fired(["OUTLIER", "LOW_CPK"])
    fired, demoted = signatures._apply_suppression(
        fired, {"OUTLIER": [], "LOW_CPK": ["OUTLIER"]})
    verdict = status.decide(case, {"n_dut": 100, "edge_fail_ratio": 1.0},
                            {"signatures": fired, "suppressed": demoted,
                             "severity_bias": 0.0, "raw_metrics_snapshot": {}})
    assert verdict["primary_signature"] == "OUTLIER"
    assert "LOW_CPK" in verdict["secondary_signatures"]


def test_severity_is_not_lost_by_demotion():
    """양보해도 그 룰의 severity 는 status 에 그대로 반영된다.

    목록에서 지우던 시절에는 MAJOR 결과 룰이 사라지면서 status 가 MINOR 로 내려가는
    부작용이 있었다(원인 룰이 MINOR 일 때). 남겨 두면 그 일이 없다.
    """
    case = {"product_type": None, "family_product": None, "item_class": None,
            "bin": 99, "lsl": 0.0, "usl": 10.0, "value_type": "V"}
    fired = _fired(["MEAN_SHIFT"], hint="MINOR") + _fired(["LOW_CPK"], hint="MAJOR")
    fired, demoted = signatures._apply_suppression(
        fired, {"MEAN_SHIFT": [], "LOW_CPK": ["MEAN_SHIFT"]})
    verdict = status.decide(case, {"n_dut": 100, "edge_fail_ratio": 1.0},
                            {"signatures": fired, "suppressed": demoted,
                             "severity_bias": 0.0, "raw_metrics_snapshot": {}})
    assert verdict["status"] == "MAJOR"                    # LOW_CPK 의 severity 유지
    assert verdict["primary_signature"] == "LOW_CPK"       # 같은 rank 후보가 자기뿐


def test_suppressor_ids_accepts_scalar():
    """yaml 에 문자열 1개로 써도 목록으로 정규화된다."""
    assert signatures._suppressor_ids({"suppressed_by": "OUTLIER"}) == ["OUTLIER"]
    assert signatures._suppressor_ids({}) == []


@pytest.mark.parametrize("mad_min, gap, expect_outlier", [
    (10.0, 3.0, True),        # 멀고 끊겼다 → OUTLIER
    (16.0, 0.4, False),       # 멀지만 이어졌다 → 꼬리(HEAVY_TAIL 축)
])
def test_evaluate_end_to_end(mad_min, gap, expect_outlier):
    """엔진 evaluate 를 통과시켰을 때 배포 yaml 선언대로 동작하는지."""
    case = {"product_type": None, "family_product": None, "item_class": None,
            "bin": 99, "lsl": 0.0, "usl": 10.0, "value_type": "V"}
    # cpk 는 raw_metrics 쪽 값이다 — features 에 None 으로 넣으면 그게 덮어써 LOW_CPK 가 죽는다.
    features = {"n_dut": 200, "fail_mad_min": mad_min, "fail_pass_gap_sigma": gap}
    result = signatures.evaluate(case, features, {"cpk": 0.9, "yield": 0.99})
    fired = {s["id"] for s in result["signatures"]}
    assert ("OUTLIER" in fired) is expect_outlier
    # 결과 룰(LOW_CPK)은 어느 쪽이든 **목록에 남는다** — 양보는 primary 만 바꾼다.
    assert "LOW_CPK" in fired


def test_deployed_rules_declare_no_cycle():
    """배포 signatures.yaml 의 suppressed_by 가 실재 id 를 가리키고 상호 참조가 없다."""
    docs = signatures.signatures_doc().get("signatures") or []
    ids = {s.get("id") for s in docs}
    table = {s.get("id"): signatures._suppressor_ids(s) for s in docs}
    for sig_id, targets in table.items():
        for target in targets:
            assert target in ids, f"{sig_id} → 없는 id {target}"
            assert sig_id not in table.get(target, []), f"{sig_id} ↔ {target} 상호 참조"

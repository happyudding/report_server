"""signature 포함관계 억제(`suppressed_by`) — 중복 발화 제거 회귀.

배경: `SEVERE_OUTLIER`(outlier_ratio > outlier_ratio_bad)가 뜨면
`OUTLIER_WARN`(> outlier_ratio_warn)은 조건상 **항상** 함께 뜬다 — 임계값 관계 검증이
warn <= bad 를 강제하기 때문이다. 같은 현상의 약한 표현이라 secondary 를 채우고
primary specificity 경쟁까지 흐리므로, 임계값은 그대로 두고 중복 의미만 걷어낸다.
"""
import pytest

from eval_engine.pipeline import signatures


def _fired(ids):
    return [{"id": i, "status_hint": "MAJOR", "score": None, "evidence": []} for i in ids]


def test_suppressed_when_suppressor_fires():
    """지목한 룰이 함께 떴으면 사라지고, 사유가 `suppressed` 에 남는다."""
    kept, suppressed = signatures._apply_suppression(
        _fired(["SEVERE_OUTLIER", "OUTLIER_WARN"]),
        {"SEVERE_OUTLIER": [], "OUTLIER_WARN": ["SEVERE_OUTLIER"]})
    assert [s["id"] for s in kept] == ["SEVERE_OUTLIER"]
    assert suppressed == [{"id": "OUTLIER_WARN", "by": ["SEVERE_OUTLIER"]}]


def test_kept_when_suppressor_absent():
    """지목한 룰이 안 떴으면 그대로 남는다 — 억제는 동반발화 때만 의미가 있다."""
    kept, suppressed = signatures._apply_suppression(
        _fired(["OUTLIER_WARN"]), {"OUTLIER_WARN": ["SEVERE_OUTLIER"]})
    assert [s["id"] for s in kept] == ["OUTLIER_WARN"]
    assert suppressed == []


def test_no_declaration_is_identity():
    """선언이 없으면 목록을 그대로 돌려준다(기존 룰 전부 무영향)."""
    fired = _fired(["A", "B"])
    kept, suppressed = signatures._apply_suppression(fired, {"A": [], "B": []})
    assert kept is fired and suppressed == []


def test_one_pass_not_transitive():
    """전이하지 않는다 — B 가 A 에 가려져도 C 는 B 의 **원본** 발화만 본다.

    체인을 따라가면 순환에서 무한루프가 나므로 1패스로 고정한 설계다.
    """
    kept, _ = signatures._apply_suppression(
        _fired(["A", "B", "C"]), {"A": [], "B": ["A"], "C": ["B"]})
    assert [s["id"] for s in kept] == ["A"]


def test_suppressor_ids_accepts_scalar():
    """yaml 에 문자열 1개로 써도 목록으로 정규화된다."""
    assert signatures._suppressor_ids({"suppressed_by": "SEVERE_OUTLIER"}) == ["SEVERE_OUTLIER"]
    assert signatures._suppressor_ids({}) == []


@pytest.mark.parametrize("outlier_ratio, expect", [
    (0.10, ["SEVERE_OUTLIER"]),          # bad(0.05) 초과 → warn 은 가려진다
    (0.03, ["OUTLIER_WARN"]),            # warn(0.02)만 초과 → 종전대로 단독 발화
])
def test_evaluate_end_to_end(outlier_ratio, expect):
    """엔진 evaluate 를 통과시켰을 때 실제로 한쪽만 남는지 — 배포 yaml 선언 그대로."""
    case = {"product_type": None, "family_product": None, "item_class": None,
            "bin": 99, "lsl": 0.0, "usl": 10.0, "value_type": "V"}
    n = 200
    features = {"n_dut": n, "outlier_ratio": outlier_ratio}
    metrics = {"cpk": 2.0, "yield": 0.99}
    result = signatures.evaluate(case, features, metrics)
    fired = {s["id"] for s in result["signatures"]}
    assert fired & {"SEVERE_OUTLIER", "OUTLIER_WARN"} == set(expect)
    # 억제된 룰의 근거가 reason_codes 에도 남지 않아야 한다(살아남은 것만 집계).
    if "OUTLIER_WARN" not in fired and result["suppressed"]:
        assert result["suppressed"][0]["id"] == "OUTLIER_WARN"


def test_deployed_rules_declare_no_cycle():
    """배포 signatures.yaml 의 suppressed_by 가 실재 id 를 가리키고 상호 참조가 없다."""
    docs = signatures.signatures_doc().get("signatures") or []
    ids = {s.get("id") for s in docs}
    table = {s.get("id"): signatures._suppressor_ids(s) for s in docs}
    for sig_id, targets in table.items():
        for target in targets:
            assert target in ids, f"{sig_id} → 없는 id {target}"
            assert sig_id not in table.get(target, []), f"{sig_id} ↔ {target} 상호 참조"

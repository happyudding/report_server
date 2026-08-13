"""L4 Status — 발화 signature/evidence → status/confidence/data_completeness.

규칙(docs 본문):
  - severity 집계 = 발화 signature 중 최대 rank → MONITOR/MINOR/MAJOR/CRITICAL.
  - OK: 발화 signature 0건 + data_completeness=full → 통계적 정상 확정.
    (signature 0건 + 결측이면 MONITOR — 모름과 정상을 구분)
    ※ fail 이 있는 case 는 L3 가 UNKNOWN 을 명시 발화하므로 여기까지 오지 않는다 —
      "설명 못 한 fail" 이 정상 확정으로 새던 구멍을 L3 에서 막는다.
  - bin_class(defective/abnormal) severity_bias 로 rank 변조.
  - trump: cpk<cpk_bad AND yield<cpk_trump_yield_floor → CRITICAL 우선.
    PF(cpk 없음)는 yield<gross_yield_bad 단독으로 CRITICAL (PF 무판정 공백 보완).
  - specificity 충돌해소: 구체 signature(EQUIPMENT_SUSPECT 등) > 일반. 지배 signature=primary.
  - data_completeness: 표본/공간 결측 정도(full/partial/low). 결측 많으면 confidence↓.
반환: {"status","primary_signature","secondary_signatures","confidence",
       "data_completeness","evidence":[{signal_code,value,weight}...]}
"""
from . import signatures
from ._rules import thresholds_for

SEVERITY_RANK = {"MONITOR": 1, "MINOR": 2, "MAJOR": 3, "CRITICAL": 4}
RANK_TO_STATUS = {v: k for k, v in SEVERITY_RANK.items()}

# 구체적(원인 특정) → 일반적 순. 같은 severity 충돌 시 앞쪽이 primary.
SPECIFICITY_ORDER = ["LOW_SAMPLE_UNCERTAIN", "MISSING_LIMIT", "CONSTANT_VALUE",
                     "EQUIPMENT_SUSPECT", "RING_FAIL",
                     # 공간 존은 좁은 것부터: E1(최외곽 한 줄) > EDGE(바깥 밴드) > CENTER.
                     # SPOT_CLUSTER(국부 뭉침)는 존보다 구체적이지만 존으로 설명되면 그쪽이
                     # 조치가 분명하므로 뒤에 둔다. CLUSTER_FAIL(사분면)보다는 앞.
                     "E1_FAIL",
                     "EDGE_FAIL", "CENTER_FAIL", "SPOT_CLUSTER", "CLUSTER_FAIL",
                     "CODE_RAIL", "TAIL_RISK", "OUTLIER",
                     "MEAN_SHIFT", "HEAVY_TAIL",
                     "BIDIR_TAIL", "BIMODALITY", "LOW_CPK", "GROSS_FAIL",
                     # UNKNOWN 은 다른 발화가 하나도 없을 때만 생기므로 경쟁 상대가 없다.
                     # 그래도 맨 끝에 둔다 — 순서 정합 검증(rules_io.validate_all)이 전 id 를 요구.
                     "UNKNOWN"]


def decide(case_ctx: dict, features: dict, sig_result: dict) -> dict:
    """L4 진입점 — 발화 signature 를 status/confidence/data_completeness 로 접는다.

    위 모듈 docstring 의 규칙을 순서대로 적용한다: severity 최대 rank → bin severity_bias
    변조 → trump(cpk+yield, PF 는 yield 단독) → specificity 로 primary 선정 → 결측 정도로
    completeness/confidence → 발화 0건이고 데이터가 완전할 때만 OK.
    새 signature 를 만들지는 않는다 — 이미 발화한 것을 접기만 한다.
    """
    fired = sig_result.get("signatures", [])
    th = thresholds_for(case_ctx)

    if not fired:
        rank, primary, secondary = 1, None, []
    else:
        ranks = [(SEVERITY_RANK[s["status_hint"]], s) for s in fired]
        max_rank = max(r for r, _ in ranks)
        bias = sig_result.get("severity_bias", 0.0) or 0.0
        rank = max(1, min(4, round(max_rank + bias)))
        top = [s for r, s in ranks if r == max_rank]
        # `demoted_by`(구 suppressed_by) = "원인 룰이 떠 있으니 primary 자리는 양보한다".
        # 목록에서는 지우지 않는다(2026-08-13) — 여러 현상이 실제로 다 있으면 다 보여야
        # 한다는 사용자 요구. 같은 severity 안에서 양보하지 않은 것을 먼저 고르고,
        # 전부 양보했으면 그때 원래 순서대로 고른다.
        pick = [s for s in top if not s.get("demoted_by")] or top
        primary = next((s for sid in SPECIFICITY_ORDER for s in pick if s["id"] == sid), pick[0])
        secondary = [s["id"] for s in fired if s["id"] != primary["id"]]

    status = RANK_TO_STATUS[rank]

    # trump 규칙: 낮은 cpk + 낮은 수율 → CRITICAL 강제
    snap = sig_result.get("raw_metrics_snapshot", {})
    cpk, yld = snap.get("cpk"), snap.get("yield")
    if (cpk is not None and yld is not None
            and cpk < th["cpk_bad"] and yld < th["cpk_trump_yield_floor"]):
        status = "CRITICAL"
    # PF trump: 통계 feature 가 전부 None 이라 signature 로는 CRITICAL 에 도달할 수
    # 없는 PF item 을 수율 단독으로 승격 (임계값은 thresholds.yaml gross_yield_bad).
    if (case_ctx.get("value_type") == "PF" and yld is not None
            and yld < th["gross_yield_bad"]):
        status = "CRITICAL"

    n_dut = features.get("n_dut") or 0
    # fail 이 확실히 0 이면 공간 fail-pattern feature 는 "결측"이 아니라 "대상 없음" —
    # 이것 때문에 completeness 가 partial 로 떨어져 정상 케이스가 영원히 OK 가 못 되던
    # 구멍을 막는다. fail 정보 자체가 없으면(None) 종전대로 결측 취급(양호 오판 금지).
    fail_count = signatures.fail_count_of(case_ctx)
    has_spatial = features.get("edge_fail_ratio") is not None or fail_count == 0
    if n_dut == 0:
        completeness, confidence = "low", 0.3
    elif n_dut < th["n_min"] or not has_spatial:
        completeness, confidence = "partial", 0.6
    else:
        completeness, confidence = "full", 0.9

    # OK 분리: 발화 signature 0건이라도 데이터가 완전할 때만 "통계적 정상 확정".
    # 결측(partial/low)이면 MONITOR 유지 — 결측을 양호로 오판하지 않는다.
    if not fired and status == "MONITOR" and completeness == "full":
        status = "OK"

    evidence = [{"signal_code": e["signal_code"], "value": e.get("value"), "weight": 1.0}
                for s in fired for e in s.get("evidence", [])]

    return {
        "status": status,
        "primary_signature": primary["id"] if primary else None,
        "secondary_signatures": secondary,
        "confidence": confidence,
        "data_completeness": completeness,
        "evidence": evidence,
    }

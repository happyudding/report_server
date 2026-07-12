"""L4 Status — 발화 signature/evidence → status/confidence/data_completeness.

규칙(docs 본문):
  - severity 가중합 → 구간 매핑(MONITOR/MINOR/MAJOR/CRITICAL) 기본.
  - OK: 발화 signature 0건 + data_completeness=full → 통계적 정상 확정.
    (signature 0건 + 결측이면 MONITOR — 모름과 정상을 구분)
  - bin_class(defective/abnormal) severity_bias 로 rank 변조.
  - trump: cpk<cpk_bad AND yield<cpk_trump_yield_floor → CRITICAL 우선.
  - specificity 충돌해소: 구체 signature(EQUIPMENT_SUSPECT 등) > 일반. 지배 signature=primary.
  - data_completeness: 표본/공간 결측 정도(full/partial/low). 결측 많으면 confidence↓.
반환: {"status","primary_signature","secondary_signatures","confidence",
       "data_completeness","evidence":[{signal_code,value,weight}...]}
"""
from ._rules import thresholds_for

SEVERITY_RANK = {"MONITOR": 1, "MINOR": 2, "MAJOR": 3, "CRITICAL": 4}
RANK_TO_STATUS = {v: k for k, v in SEVERITY_RANK.items()}

# 구체적(원인 특정) → 일반적 순. 같은 severity 충돌 시 앞쪽이 primary.
SPECIFICITY_ORDER = ["EQUIPMENT_SUSPECT", "EDGE_FAIL", "CENTER_FAIL", "CLUSTER_FAIL",
                     "CODE_RAIL", "SUBPOP_GAP", "SEVERE_OUTLIER", "OUTLIER_WARN",
                     "MEAN_SHIFT", "TAIL_RISK", "HEAVY_TAIL",
                     "BIDIR_TAIL", "WIDE_DISTRIBUTION", "LOW_CPK", "SPEC_TOO_TIGHT",
                     "GROSS_FAIL"]


def decide(case_ctx: dict, features: dict, sig_result: dict) -> dict:
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
        primary = next((s for sid in SPECIFICITY_ORDER for s in top if s["id"] == sid), top[0])
        secondary = [s["id"] for s in fired if s["id"] != primary["id"]]

    status = RANK_TO_STATUS[rank]

    # trump 규칙: 낮은 cpk + 낮은 수율 → CRITICAL 강제
    snap = sig_result.get("raw_metrics_snapshot", {})
    cpk, yld = snap.get("cpk"), snap.get("yield")
    if (cpk is not None and yld is not None
            and cpk < th["cpk_bad"] and yld < th["cpk_trump_yield_floor"]):
        status = "CRITICAL"

    n_dut = features.get("n_dut") or 0
    has_spatial = features.get("edge_fail_ratio") is not None
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

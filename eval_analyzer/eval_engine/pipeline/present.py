"""L6 Present — 결과 직렬화 + eval.db 적재.

persist: raw_metrics/features/evaluation/eval_evidence/case_signature/eval_precedent 저장(store CRUD).
  ※ raw(per-DUT)는 저장 안 함 — m/f 의 계산값만.
to_result: RunResult.cases[i] dict (docs/INTEGRATION_CONTRACT §4).
"""
from .. import store
from ._rules import issue_category_for, thresholds_for


def should_store(case_ctx, metrics, sig_result) -> bool:
    """rule 계산 후 DB 저장 여부 판단. 지금: yield fail 또는 cpk<cpk_warn.

    향후 all-rule 로 넓힐 때 아래 return 을
      `return yield_fail or bool(sig_result.get("signatures"))` 한 줄로 교체.
    (sig_result 는 지금 미사용이지만 그 교체를 위해 시그니처에 포함.)
    """
    th = thresholds_for(case_ctx)
    yield_fail = (metrics.get("fail_count") or 0) > 0
    cpk = metrics.get("cpk")
    low_cpk = cpk is not None and cpk < th["cpk_warn"]
    return yield_fail or low_cpk


def persist(run_ctx, case_ctx, raw_metrics, features, verdict, sig_result, comment,
            engine_version, model_version, precedents=None):
    run_id = run_ctx.get("run_id")
    case_id = case_ctx["case_id"]
    with store.get_conn() as conn:
        # fail_case/run_case 는 저장 대상(should_store 통과)에만 여기서 upsert (ingest 에서 이관).
        store.upsert_fail_case(case_id, case_ctx["product_name"], case_ctx["lot_id"],
                               case_ctx["wafer_number"], case_ctx["item_id"],
                               case_ctx["bin"], case_ctx["revision"],
                               case_ctx["item_class"], conn=conn)
        store.link_run_case(run_id, case_id, conn=conn)
        store.save_raw_metrics(case_id, run_id, raw_metrics, conn=conn)
        store.save_features(case_id, run_id, engine_version, features, conn=conn)
        eval_id = store.save_evaluation(
            case_id, run_id, engine_version, model_version, verdict["status"],
            verdict["confidence"], verdict["data_completeness"], comment, conn=conn)
        store.save_eval_evidence(eval_id, verdict.get("evidence", []), conn=conn)
        sig_rows = []
        if verdict.get("primary_signature"):
            sig_rows.append({"id": verdict["primary_signature"], "role": "primary", "score": 1.0})
        sig_rows += [{"id": sid, "role": "secondary", "score": None}
                     for sid in verdict.get("secondary_signatures", [])]
        store.save_case_signature(eval_id, sig_rows, conn=conn)
        store.save_eval_precedents(eval_id, precedents or [], conn=conn)


def to_result(case_ctx, verdict, sig_result, comment, precedents) -> dict:
    primary_id = verdict["primary_signature"]
    sig_breakdown = [
        {"id": s["id"], "role": "primary" if s["id"] == primary_id else "secondary",
         "evidence": s.get("evidence", []), "action_ko": s.get("action_ko")}
        for s in sig_result.get("signatures", [])
    ]
    return {
        "case_id": case_ctx["case_id"],
        "item_canonical": case_ctx["item_canonical"],
        "item_raw": case_ctx.get("item_raw"),          # 원본 item명 (Issue Table join 키)
        "item_class": case_ctx["item_class"],
        "bin": case_ctx["bin"],
        "issue_category": issue_category_for(verdict["primary_signature"]),  # YIELD|CPK|ETC
        "status": verdict["status"],
        "primary_signature": verdict["primary_signature"],
        "secondary_signatures": verdict["secondary_signatures"],
        "confidence": verdict["confidence"],
        "data_completeness": verdict["data_completeness"],
        "comment": comment,
        "evidence": [{"signal_code": e["signal_code"], "value": e.get("value"),
                      "weight": e.get("weight")} for e in verdict.get("evidence", [])],
        "signatures": sig_breakdown,
        "precedents": [{"action": p.get("action"), "result": p.get("result"),
                        "comment": p.get("human_comment"),
                        "product_name": p.get("product_name"),
                        "family_product": p.get("family_product")} for p in precedents],
    }

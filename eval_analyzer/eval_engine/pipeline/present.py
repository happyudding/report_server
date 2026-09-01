"""L6 Present — 결과 직렬화 + eval.db 적재.

persist: raw_metrics/features/evaluation/eval_evidence/case_signature/eval_precedent 저장(store CRUD).
  ※ raw(per-DUT)는 저장 안 함 — m/f 의 계산값만.
to_result: RunResult.cases[i] dict (docs/INTEGRATION_CONTRACT §4).
"""
from .. import store
from ._rules import issue_category_for, thresholds_for


def should_store(case_ctx, metrics, sig_result) -> bool:
    """rule 계산 후 DB 저장 여부 판단: yield fail / cpk<cpk_warn / signature 발화.

    signature 발화분을 포함하는 이유 — 수율·cpk 는 정상인데 분포만 이상한 케이스
    (BIMODALITY 이봉, OUTLIER 등)가 여기서 걸러지면 코멘트가 아예 생성되지
    않아 룰 디버깅이 불가능하다. report_server 는 이 부류를 Issue Table ETC 섹션에
    올린다(web_report/ai_comment.py etc_auto_items).
    """
    # 평가 제외 목록(rules/exclusions.yaml) 매칭 — 발화 차단에 더해 저장도 차단해야
    # yield/cpk 사유로 코멘트가 만들어지는 우회를 막는다(완전 제외).
    if (sig_result or {}).get("excluded"):
        return False
    th = thresholds_for(case_ctx)
    yield_fail = (metrics.get("fail_count") or 0) > 0
    cpk = metrics.get("cpk")
    low_cpk = cpk is not None and cpk < th["cpk_warn"]
    fired = bool((sig_result or {}).get("signatures"))
    return yield_fail or low_cpk or fired


def persist(run_ctx, case_ctx, raw_metrics, features, verdict, sig_result, comment,
            engine_version, model_version, precedents=None, db_path=None):
    """L6 적재 — case 1건의 raw_metrics/features/evaluation/evidence/signature/precedent 저장.

    ⚠ raw(per-DUT)는 저장하지 않는다(불변 규칙 3) — L1/L2 계산값만 남긴다.
    fail_case/run_case upsert 를 L0 ingest 가 아니라 여기서 하는 이유는 `should_store` 를
    통과한 case 만 마스터에 남기기 위해서다. 커넥션 하나를 열어 전 CRUD 에 넘기므로
    한 case 의 적재는 단일 트랜잭션이 된다.
    `db_path` 는 적재 대상 DB(기본 `config.DB_PATH`) — `store.get_conn` docstring 참조.
    ⚠ 동시 쓰기 주의: api.evaluate 가 ThreadPoolExecutor 로 case 를 돌리므로, persist 를
    켜는 호출자는 워커를 1로 줄여야 한다(api.evaluate 가 `max_workers` 로 강제한다).
    """
    run_id = run_ctx.get("run_id")
    case_id = case_ctx["case_id"]
    with store.get_conn(db_path) as conn:
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


# 선례 1건에 실어 보내는 **당시 수치** — store.search_precedents 가 최신 run 기준으로
# 붙여 준 컬럼들. 코멘트만 주면 "그때와 지금이 얼마나 닮았나"를 소비자가 판단할 수 없어
# 2026-08-28 에 함께 내보내기로 했다(AI Comment 프롬프트가 과거/현재를 대조한다).
# 값이 없는 선례(CSV 적재분 등)는 키가 None 이다 — 키 자체는 항상 있다(계약 안정).
_PRECEDENT_METRIC_KEYS = ("cpk", "cpl", "cpu", "cp", "mean", "stdev", "min", "max",
                          "yield", "fail_count", "total_count", "bimodality")
_PRECEDENT_FEATURE_KEYS = ("spread_norm", "outlier_ratio", "bimodality_score",
                           "limit_hit_ratio", "edge_fail_ratio", "center_fail_ratio",
                           "ring_fail_ratio", "fail_spread_norm", "tail_mass_3s",
                           "value_gap_ratio")


def _precedent_result(p: dict) -> dict:
    """선례 1건 → 계약 dict. 종전 5키 + 식별/당시 수치.

    종전 키(action/result/comment/product_name/family_product)는 **이름·의미 불변**이다 —
    기존 소비자(testbench, report_server)가 그대로 동작한다. 뒤의 키들은 추가분이다.
    """
    out = {
        "action": p.get("action"), "result": p.get("result"),
        "comment": p.get("human_comment"),
        "product_name": p.get("product_name"),
        "family_product": p.get("family_product"),
        # 식별 — 어느 lot/item 의 사례인지 알아야 현재와 대조가 된다
        "case_id": p.get("case_id"), "lot_id": p.get("lot_id"),
        "item_canonical": p.get("item_canonical"), "bin": p.get("bin"),
        "unit": p.get("unit"), "value_type": p.get("value_type"),
        # 당시 판정
        "status": p.get("status"), "signature": p.get("signature"),
        "similarity": p.get("similarity"),
    }
    out["metrics"] = {k: p.get(k) for k in _PRECEDENT_METRIC_KEYS
                      if p.get(k) is not None}
    out["features"] = {k: p.get(k) for k in _PRECEDENT_FEATURE_KEYS
                       if p.get(k) is not None}
    return out


def to_result(case_ctx, verdict, sig_result, comment, precedents) -> dict:
    """L6 직렬화 — RunResult.cases[i] dict 조립 (docs/INTEGRATION_CONTRACT §4).

    DB 적재분과 별개로 **호출자에게 돌려주는 계약 형태**다. `item_raw` 는 report_server
    Issue Table join 키이고, `issue_category` 는 signature 택소노미를 모르는 호출자를 위한
    편의 버킷(YIELD|CPK|ETC).

    `precedents[]` 는 `_precedent_result` 계약 — 종전 5키에 식별·당시 수치가 더해졌다.
    """
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
        # YIELD|CPK|ETC — 제품군 오버레이가 issue_category 를 바꿨으면 그 값을 따른다
        "issue_category": issue_category_for(verdict["primary_signature"], case_ctx),
        "status": verdict["status"],
        "primary_signature": verdict["primary_signature"],
        "secondary_signatures": verdict["secondary_signatures"],
        "confidence": verdict["confidence"],
        "data_completeness": verdict["data_completeness"],
        "comment": comment,
        "evidence": [{"signal_code": e["signal_code"], "value": e.get("value"),
                      "weight": e.get("weight")} for e in verdict.get("evidence", [])],
        "signatures": sig_breakdown,
        "precedents": [_precedent_result(p) for p in precedents],
    }

"""공개 진입점 evaluate(). 6단계 파이프라인 오케스트레이션.

계약: docs/INTEGRATION_CONTRACT.md (입력 run_input, 출력 RunResult).
report_server 가 파일 1회 run 시 이 함수를 호출한다. eval_analyzer 는 report_server 를 import 안 함.
"""
import logging
import time

from . import config, store
from .pipeline import ingest, metrics, features, signatures, status, recommend, present

# 라이브러리 로거 — 핸들러/레벨 설정은 host(report_server)에 맡긴다(핸들러 부착 금지).
logger = logging.getLogger(__name__)


def evaluate(run_input: dict, *, engine_version: str | None = None,
             model_version: str | None = None, persist: bool = True) -> dict:
    """한 세션의 fail item 들을 평가.

    흐름 (docs/5STAGE_COLUMNS, DB_SCHEMA):
      L0 ingest   run_input → run_id + fail_case 들 (마스터 upsert, item base/phase/canonical/class)
      L1 metrics  per fail item: raw 에서 cpk/mean/stdev/yield/... 계산 (raw 미저장)
      L2 features robust 산포/spec margin/공간 feature 계산 (engine_version)
      L3 signatures rules(thresholds/signatures yaml) + bin_taxonomy context → 발화 signature
      L4 status   severity 집계 + trump + specificity → status/confidence/data_completeness
      L5 recommend 룰 골격 + 선례(precedent) + LLM 합성 → comment
      L6 present  결과 dict (+ persist 시 eval.db 적재)
    """
    engine_version = engine_version or config.ENGINE_VERSION
    t0 = time.perf_counter()
    meta = run_input.get("meta", {})
    logger.info("evaluate 시작 product=%s lot=%s wafer=%s persist=%s engine=%s",
                meta.get("product_name"), meta.get("lot_id"), meta.get("wafer_number"),
                persist, engine_version)
    if persist:
        store.init_db()

    # L0
    run_ctx = ingest.ingest(run_input, persist=persist)   # → {run_id, cases:[case_ctx...]}

    results = []
    n_precedent_hits = 0
    for case in run_ctx["cases"]:
        m = metrics.compute(case)                          # L1 raw_metrics
        f = features.compute(case, m, engine_version)      # L2 features
        sig = signatures.evaluate(case, f, m)              # L3 발화 signature 들
        verdict = status.decide(case, f, sig)              # L4 status/confidence
        if not present.should_store(case, m, sig):         # 저장 판단(rule 계산 후): yield fail | cpk<cpk_warn
            continue
        preced = recommend.find_precedents(case, sig)      # 선례 검색 (DB_SCHEMA §9)
        n_precedent_hits += len(preced)
        comment = recommend.make_comment(case, verdict, sig, preced,
                                         model_version=model_version)  # L5
        if persist:
            present.persist(run_ctx, case, m, f, verdict, sig, comment, engine_version,
                            model_version, precedents=preced)
        results.append(present.to_result(case, verdict, sig, comment, preced))

    n_candidates = len(run_ctx["cases"])
    logger.info("evaluate 완료 run_id=%s candidates=%d stored=%d gated=%d precedent_hits=%d %.1fms",
                run_ctx.get("run_id"), n_candidates, len(results), n_candidates - len(results),
                n_precedent_hits, (time.perf_counter() - t0) * 1000)
    return {
        "run_id": run_ctx.get("run_id"),
        "engine_version": engine_version,
        "model_version": model_version,
        "cases": results,
    }

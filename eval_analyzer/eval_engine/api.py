"""공개 진입점 evaluate(). 6단계 파이프라인 오케스트레이션.

계약: docs/INTEGRATION_CONTRACT.md (입력 run_input, 출력 RunResult).
report_server 가 파일 1회 run 시 이 함수를 호출한다. eval_analyzer 는 report_server 를 import 안 함.
"""
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from . import config, store
from .pipeline import (ingest, metrics, features, signatures, status, recommend,
                       present, _rules)

_MAX_WORKERS = 3

# 라이브러리 로거 — 핸들러/레벨 설정은 host(report_server)에 맡긴다(핸들러 부착 금지).
logger = logging.getLogger(__name__)


# 룰 파일은 이 호출 1회 동안 1회만 읽는다(run 단위 스냅샷 — _rules.rules_scope 참조).
# @contextmanager 산출물은 ContextDecorator 라 호출마다 새 스코프가 만들어진다.
@_rules.rules_scope()
def evaluate(run_input: dict, *, engine_version: str | None = None,
             model_version: str | None = None, persist: bool = True,
             db_path=None, generate_comment: bool = True,
             thresholds_override: dict | None = None) -> dict:
    """한 세션의 fail item 들을 평가.

    흐름 (docs/5STAGE_COLUMNS, DB_SCHEMA):
      L0 ingest   run_input → run_id + fail_case 들 (마스터 upsert, item base/phase/canonical/class)
      L1 metrics  per fail item: raw 에서 cpk/mean/stdev/yield/... 계산 (raw 미저장)
      L2 features robust 산포/spec margin/공간 feature 계산 (engine_version)
      L3 signatures rules(thresholds/signatures yaml) + bin_taxonomy context → 발화 signature
      L4 status   severity 집계 + trump + specificity → status/confidence/data_completeness
      L5 recommend 룰 골격 + 선례(precedent) + LLM 합성 → comment
      L6 present  결과 dict (+ persist 시 eval.db 적재)

    선택 인자 2개 (기본값은 종전 동작 그대로 — 하위호환):
      `db_path`          persist 대상 DB 파일. 지정하면 그 파일에만 쓰고 엔진 기본
                         `config.DB_PATH` 는 건드리지 않는다. report_server 가 자기 소유
                         DB(REPORT_EVAL_DB_PATH)에 스냅샷을 적재할 때 쓴다 — 전역 대입이
                         아니라 인자인 이유는 `store.get_conn` docstring 참조.
      `generate_comment` False 면 L5(선례검색 + 코멘트 합성)를 통째로 건너뛴다. 판단 근거
                         (L1~L4)만 쌓는 백그라운드 수집용이며 **LLM·선례 DB 조회 비용이
                         0** 이 된다. 그 대신 결과·적재분의 `comment` 는 None 이고
                         `precedents` 는 빈 목록이다.
      `thresholds_override` {임계값 키: 값} — 이 run 에만 적용할 임계값. 파일 스코프 병합
                         (default → product_type → 오버레이 트리 → item_class) **맨 뒤에**
                         얹히므로 가장 구체적인 스코프가 된다. 호출자(report_server)가 세션
                         단위 민감도 게이지로 만든 구체값을 넣는다 — 게이지 단계표는 서버가
                         해석하고 엔진은 최종 숫자만 받는다(엔진에 UI 개념을 들이지 않는다).
                         ⚠ 스코프 전역이 아니라 **case 마다 스탬프**한다. `_rules._scope` 는
                         모듈 전역이라, 서로 다른 override 를 가진 evaluate 가 동시에 돌면
                         전역 주입은 서로를 덮어쓴다(서버는 컴퓨트 워커 여럿이 병렬로 부른다).
    """
    engine_version = engine_version or config.ENGINE_VERSION
    t0 = time.perf_counter()
    meta = run_input.get("meta", {})
    logger.info("evaluate 시작 product=%s lot=%s wafer=%s persist=%s engine=%s comment=%s",
                meta.get("product_name"), meta.get("lot_id"), meta.get("wafer_number"),
                persist, engine_version, generate_comment)
    if persist:
        store.init_db(db_path)

    # L0
    t_ingest = time.perf_counter()
    run_ctx = ingest.ingest(run_input, persist=persist, db_path=db_path)
    ms_ingest = (time.perf_counter() - t_ingest) * 1000

    if thresholds_override:
        # digest 는 `_rules.thresholds_for` 스코프 캐시 키의 일부다 — 같은 run 안에서
        # override 별로 병합 결과가 갈리게 한다(값 자체를 키로 쓰면 dict 라 해시 불가).
        _ovr = dict(thresholds_override)
        _digest = hashlib.sha256(
            json.dumps(_ovr, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
        logger.info("thresholds_override %d keys digest=%s", len(_ovr), _digest)
        for _case in run_ctx["cases"]:
            _case["_th_override"] = _ovr           # 전 case 공유(읽기 전용)
            _case["_th_override_digest"] = _digest

    # 선례검색 SQL 결과의 **run 단위 공유 캐시** (2026-09-02). 그 쿼리의 파라미터는
    # value_type·family·exclude 세션뿐이라 item 이 달라도 결과가 같은데, 종전에는 case 마다
    # DB 를 새로 열고 같은 쿼리를 되풀이했다(실측 L5 가 L2 의 5배 — 선례 0건인데도).
    # `_th_override` 와 같은 방식으로 case 에 실어 보낸다(전역 상태 금지 — 동시 evaluate 가
    # 서로를 덮어쓰지 않게). generate_comment=False 면 L5 자체를 건너뛰므로 무해하다.
    precedent_cache = {}
    for _case in run_ctx["cases"]:
        _case["_precedent_cache"] = precedent_cache

    # 레벨별 누적 소요(ms) — 종전에는 총소요 1줄뿐이라 "L1/L2 가 느리다" 를 확인하거나
    # 최적화 효과를 증명할 수치가 엔진에 전혀 없었다(2026-08-19 계측 추가).
    # 워커 스레드에서 더하므로 GIL 아래 float 누적이다 — 관측용이라 정밀 동기화는 불필요.
    level_ms = {"L1": 0.0, "L2": 0.0, "L3L4": 0.0, "L5": 0.0, "L6": 0.0}

    def _process_case(case):
        """case 1건의 L1~L6. 저장 게이트를 못 넘으면 None, 넘으면 (result dict, 선례 수).

        `should_store` 를 L4 뒤에 두는 이유 — 저장 여부 판단에 signature 발화가 필요해서
        룰 계산을 먼저 끝내야 한다. 대신 걸러진 case 는 L5(선례검색·코멘트)를 건너뛰므로
        가장 비싼 단계는 아낀다. `generate_comment=False` 면 통과분도 L5 를 건너뛴다.
        """
        t = time.perf_counter()
        m = metrics.compute(case)                          # L1 raw_metrics
        t1 = time.perf_counter(); level_ms["L1"] += (t1 - t) * 1000
        f = features.compute(case, m, engine_version)      # L2 features
        t2 = time.perf_counter(); level_ms["L2"] += (t2 - t1) * 1000
        sig = signatures.evaluate(case, f, m)              # L3 발화 signature 들
        verdict = status.decide(case, f, sig)              # L4 status/confidence
        level_ms["L3L4"] += (time.perf_counter() - t2) * 1000
        if not present.should_store(case, m, sig):         # 저장 판단(rule 계산 후): yield fail | cpk<cpk_warn
            return None
        t3 = time.perf_counter()
        if generate_comment:
            preced = recommend.find_precedents(case, sig)  # 선례 검색 (DB_SCHEMA §9)
            comment = recommend.make_comment(case, verdict, sig, preced,
                                    model_version=model_version)  # L5
        else:
            preced, comment = [], None
        t4 = time.perf_counter(); level_ms["L5"] += (t4 - t3) * 1000
        if persist:
            present.persist(run_ctx, case, m, f, verdict, sig, comment, engine_version,
                            model_version, precedents=preced, db_path=db_path)
        out = present.to_result(case, verdict, sig, comment, preced), len(preced)
        level_ms["L6"] += (time.perf_counter() - t4) * 1000
        return out

    cases = run_ctx["cases"]
    # ⚠ persist 는 워커마다 SQLite 커넥션을 열어 같은 파일에 쓴다(VERIFY_CHECKLIST §2-2).
    # 적재 경로는 백그라운드라 지연이 무해하므로 워커를 1로 줄여 직렬화한다 — 병렬은
    # 계산만 하는 preview(persist=False) 경로에서만 쓴다.
    workers = 1 if persist else _MAX_WORKERS
    with ThreadPoolExecutor(max_workers=workers) as pool:
        processed = list(pool.map(_process_case, cases))

    results = []
    n_precedent_hits = 0
    for item in processed:
        if item is None:
            continue
        result, n_hits = item
        results.append(result)
        n_precedent_hits += n_hits

    n_candidates = len(run_ctx["cases"])
    logger.info("evaluate 완료 run_id=%s candidates=%d stored=%d gated=%d precedent_hits=%d "
                "%.1fms [L0 %.0f L1 %.0f L2 %.0f L3L4 %.0f L5 %.0f L6 %.0f]",
                run_ctx.get("run_id"), n_candidates, len(results), n_candidates - len(results),
                n_precedent_hits, (time.perf_counter() - t0) * 1000, ms_ingest,
                level_ms["L1"], level_ms["L2"], level_ms["L3L4"], level_ms["L5"],
                level_ms["L6"])
    return {
        "run_id": run_ctx.get("run_id"),
        "engine_version": engine_version,
        "model_version": model_version,
        "cases": results,
    }

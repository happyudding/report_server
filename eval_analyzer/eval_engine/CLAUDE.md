# eval_engine — 분석 엔진 패키지 (블록 진입점)

`evaluate(run_input)` 하나로 fail-item 평가 6단계를 오케스트레이션하는 순수 라이브러리.
**서버·UI 없음. report_server 를 import 하지 않는다**(의존 방향은 report_server → eval_engine 한 방향).

> 최상위 규칙은 [../CLAUDE.md](../CLAUDE.md). 설계 정본은 [../docs/](../docs/).
> 이 파일은 "이 디렉터리에서 무엇을 어디서 만지나"를 위한 작업 지도.

## 파일 지도
| 파일 | 역할 | 상태 |
|---|---|---|
| `api.py` | `evaluate()` 진입점 — L0~L6 순서대로 호출(아래 흐름). 얇은 오케스트레이터. | 완성 |
| `config.py` | DB 경로 / rules 파일 경로 / LLM·선례 백엔드 설정. **전부 env override**. | 완성 |
| `store.py` | eval.db 스키마(DDL 17테이블) + CRUD + `make_case_id` + `search_precedents`(SQL 선례). | 완성 |
| `pipeline/` | L0~L6 실제 로직. → [pipeline/CLAUDE.md](pipeline/CLAUDE.md) | 완성 |
| `rules/` | thresholds/signatures/taxonomy yaml(임계값·룰 선언형). → [rules/CLAUDE.md](rules/CLAUDE.md) | 완성 |
| `precedent_client.py` | 선례검색 어댑터 경계(sql 기본 \| rag 교체). `_rag_search` 는 스텁. | sql 완성 / rag 미구현 |
| `llm_client.py` | 교체형 LLM 어댑터. `is_enabled()` + `complete()`. 모델 하드코딩 금지. | `complete()` 미구현 |
| `calibrate.py` | 오프라인 분위수 보정 → thresholds.yaml item_class 갱신 + engine_version 등록. | 분위수 보정 완성 / comment 채굴·검증 후속 |
| `cli.py` | 얇은 테스트/보정 CLI. init/run/seed/calibrate. | 완성 |

## evaluate() 흐름 (api.py)
```
L0 ingest    run_input → run_id + fail_case 들 (마스터 upsert, item_class, case_id)
L1 metrics   per fail item: raw(메모리)에서 cpk/mean/stdev/yield/bimodality (raw 미저장)
L2 features  robust 산포/spec margin/공간 feature (engine_version 별)
L3 signatures rules 평가 → 발화 signature + reason_codes + bin context
L4 status    severity 집계 + trump + specificity → status/confidence/data_completeness
L5 recommend 선례검색 + 템플릿/LLM 합성 → comment
L6 present   결과 dict 직렬화 (+ persist 시 eval.db 적재, raw 는 저장 안 함)
```

## 이 블록의 불변 규칙 (최상위 규칙의 하위 세부)
- **raw(per-DUT) 저장 금지.** L1/L2 계산값(요약통계·feature)만 DB 로.
- **임계값·모델·endpoint 하드코딩 금지.** 임계값은 `rules/*.yaml`(+ `pipeline/_rules.py` 로더),
  LLM·선례 백엔드는 `config.EVAL_*` 로만.
- **DB 에 JSON 컬럼 금지.** 다중값은 정규화 child(eval_evidence / case_signature 등).
- `make_case_id` = 자연키 sha256(product_name, lot_id, wafer, item_id, bin, revision) → **재업로드 idempotent**.
  바꾸면 과거 case 와 선례 매칭이 깨진다.

## 실행 / 검증
```
python -m eval_engine.cli init          # eval.db 생성(17테이블 + bin_taxonomy 시드 + user_version)
python -m eval_engine.cli run <csv>     # degrade / df_honey(raw_table) CSV E2E
python -m eval_engine.cli calibrate     # 누적 features 분위수 → thresholds.yaml item_class 갱신
python -m pytest -q                     # 전체 테스트
```
> 신규 정본 raw_df 포맷 E2E 는 CLI 가 아니라 [tools/testbench_eval.py](../tools/CLAUDE.md) 사용.

## 미구현(후속 작업)
- `llm_client.complete()` — 사용자 지정 endpoint 로 HTTP POST(OpenAI 호환 shape). 실패 시 상위에서 템플릿 fallback.
- `calibrate` comment 채굴(label/outcome 군집) + 룰 precision/recall 검증 — 분위수 보정은 구현됨.
- `precedent_client._rag_search()` — RAG 선례 백엔드(상대측). 계약: [../docs/PRECEDENT_RAG_HANDOFF.md](../docs/PRECEDENT_RAG_HANDOFF.md).

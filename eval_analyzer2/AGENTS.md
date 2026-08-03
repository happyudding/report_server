# eval_analyzer — Codex 진입점

반도체 Fail-Item 평가 분석 엔진. 엔지니어의 fail 판단(status 판정 + 분석방향 comment)을
코드로 옮긴다. **report_server 와 완전 독립**.

> **세션 시작 규칙**: 구현 전 반드시 `docs/` 를 먼저 읽어라. 특히 아래 4개는 필수.
> - [docs/DB_SCHEMA.md](docs/DB_SCHEMA.md) — eval.db 전체 DDL·grain·선례검색 (저장 구조의 정본)
> - [docs/CODE_TO_PORT.md](docs/CODE_TO_PORT.md) — cpk/ECDF/fail/feature **정확 공식** (재구현용)
> - [docs/INTEGRATION_CONTRACT.md](docs/INTEGRATION_CONTRACT.md) — `evaluate()` 입출력 계약
> - [docs/5STAGE_COLUMNS.md](docs/5STAGE_COLUMNS.md) — 컬럼 의미 사전
> 그 외: [docs/REPORT_SERVER_CONTEXT.md](docs/REPORT_SERVER_CONTEXT.md)(데이터 출처),
> [docs/HANDOFF_TO_REPORT_SERVER.md](docs/HANDOFF_TO_REPORT_SERVER.md)(상대측 작업).

## 블록 지도 (오케스트레이션)
각 블록 디렉터리에 작업용 `AGENTS.md`(무엇을 어디서 만지나)를 둔다. 그 블록에서 작업하는 세션은
해당 파일 하나만 먼저 읽으면 된다(Codex 가 하위 AGENTS.md 를 자동 로드). 설계 정본은 항상 `docs/`.

| 블록 | 진입 문서 | 역할 |
|---|---|---|
| 엔진 패키지 | [eval_engine/AGENTS.md](eval_engine/AGENTS.md) | `evaluate()` + config/store/cli/어댑터. 파일 지도·미구현 목록. |
| 판단 파이프라인 | [eval_engine/pipeline/AGENTS.md](eval_engine/pipeline/AGENTS.md) | L0~L6 단계별 함수·raw_df 파싱·결측 규칙·status 판정. |
| 룰/임계값 | [eval_engine/rules/AGENTS.md](eval_engine/rules/AGENTS.md) | thresholds/signatures/taxonomy yaml 스키마·스코프. |
| 선례 적재기 | [db_input/AGENTS.md](db_input/AGENTS.md) | 과거 사례 CSV → 제품군별 선례 DB. |
| 도구/testbench | [tools/AGENTS.md](tools/AGENTS.md) | 샘플 생성·콘솔 testbench(+ ⚠ 레이아웃 드리프트). |
| 테스트 | [tests/AGENTS.md](tests/AGENTS.md) | 스위트 지도·DB 격리 규칙. |

- `docs/` — 설계·계약·핸드오프 정본(위 세션 시작 규칙). `seeds/`·`samples/` — 예시 데이터.
- `../plotly_sqlite/` — **별도 대시보드 앱**(독립 프로젝트, 자체 requirements). 엔진과 결합 아님, 참고만.

## 불변 규칙 (반드시 준수)
1. **report_server 코드를 import 하지 않는다.** 필요한 계산은 직접 구현하거나 함수만 복사(vendor).
   알고리즘은 docs/CODE_TO_PORT.md 에 공식으로 있음. 의존 방향은 report_server → eval_analyzer 한 방향만.
2. **자체 DB(eval.db, SQLite)를 직접 관리.** report.db 는 무시(전면 개편 예정).
3. **raw(per-DUT) 저장 금지.** 최초 1회 계산값(요약통계/feature)만 저장. 산포 다운샘플 금지.
4. **DB 에 JSON 컬럼 금지.** 다중값은 정규화 child 테이블(eval_evidence, case_signature 등).
5. **룰 임계값 하드코딩 금지.** rules/*.yaml + calibration 분위수. 룰 스코프 = item_class(category_major|value_type|bin).
6. **LLM 모델 하드코딩 금지.** config.EVAL_LLM_* 로 사용자 지정(없으면 템플릿 fallback).
7. **입력 df 포맷 고정(최종).** `evaluate({"meta":.., "raw_df": df})` 의 df 레이아웃은 아래로 **불변**.
   컬럼 순서·메타행 위치 변경 금지(바꾸면 `pipeline/ingest.py:_ingest_raw_df` 파서가 깨짐).
   상세·필드사전은 [docs/REPORT_GENERATOR_DATA_REQUEST.md](docs/REPORT_GENERATOR_DATA_REQUEST.md).
   ```
   columns: SERIAL, SHOT, DUT, XPOS, YPOS, BIN, FAILTNO, TESTITEM1, TESTITEM2, ...  (meta 7개 + item)
   row0 TSEQ / row1 TNO / row2 STEP / row3 UNIT / row4 HILIM(USL) / row5 LOLIM(LSL) / row6+ 측정
   ```
   - 사용: XPOS/YPOS→공간, BIN→bin, UNIT→value_type, HILIM/LOLIM→usl/lsl, TESTITEM→측정값, FAILTNO/TNO→fail.
   - 미사용: SERIAL/SHOT/DUT/TSEQ/STEP(P1/P2/P3, 보류).
   - **fail 식별 = FAILTNO(serial이 fail한 test의 TNO) == item의 TNO** → fail item, 그 serial BIN=fail bin.
     FAILTNO 공란/0/NaN = pass. (limit 위반 재판정 아님)
   - (레거시) 초기 `raw_table`(중립 dict)/`items`(degrade) 경로도 하위호환 지원.

## 구조
```
eval_engine/        엔진 패키지 (서버·UI 없음)
  api.py            evaluate(run_input) 진입점 — 6단계 오케스트레이션
  config.py         DB 경로 / LLM / rules 경로 / 임계 상수
  store.py          eval.db DDL(완성) + CRUD(일부 TODO) + make_case_id + search_precedents(TODO)
  pipeline/         L0 ingest → L1 metrics → L2 features → L3 signatures → L4 status → L5 recommend → L6 present
                    (각 모듈 docstring 에 구현 TODO + docs 참조)
  llm_client.py     교체형 LLM 어댑터
  calibrate.py      분위수 보정 + comment 채굴
  cli.py            테스트/보정 CLI (python -m eval_engine.cli init/run/calibrate/seed)
  rules/            thresholds.yaml / signatures.yaml / bin_taxonomy.yaml / item_alias.yaml
seeds/              background seed 예시(과거 라벨/결과)
data/               eval.db (런타임 생성, gitignore)
```

## 구현 순서 (권장)
1. `store.py` CRUD 완성 (마스터 upsert / fail_case / raw_metrics / features / evaluation / label / outcome / precedent 검색).
2. `pipeline/ingest.py` — run_input → fail_case (item 파싱·item_class·case_id, ingest_run).
3. `pipeline/metrics.py` — CODE_TO_PORT §2 cpk_summary.
4. `pipeline/features.py` — CODE_TO_PORT §5 robust 산포/spec/공간.
5. `pipeline/signatures.py` + `status.py` — rules/*.yaml 평가 + status.
6. `pipeline/recommend.py` — 선례(DB_SCHEMA §9) + 템플릿/LLM 코멘트.
7. `cli.py run/seed` 로 seeds/background_seed_example.csv + 샘플 raw 1개 E2E 검증.

## 검증
`python -m eval_engine.cli init` → 16개 테이블 + bin_taxonomy 시드 + `PRAGMA user_version` 확인.
이후 `... run <sample.csv>` 로 evaluate() E2E. `python -m pytest -q` 로 전체 테스트.
현재 상태: L0~L6 파이프라인·store CRUD·선례검색(SQL) 구현 완료(테스트 통과).
미구현: calibrate.recalibrate(분위수 보정), llm_client.complete(HTTP 호출), RAG 선례 백엔드(상대측).

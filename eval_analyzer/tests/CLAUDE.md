# tests — 테스트 스위트 (블록 진입점)

`python -m pytest -q` (현재 **163 통과**). DB 테스트는 전부 tmp 격리 — 운영 `eval.db` 오염 없음.
상위 규칙 [../CLAUDE.md](../CLAUDE.md).

## 파일 지도
| 파일 | 커버 대상 |
|---|---|
| `conftest.py` | `fresh_db` fixture — `config.DB_PATH/DATA_DIR` 를 tmp 로 monkeypatch 후 `init_db`. |
| `test_metrics.py` | L1 `cpk_summary`/yield/bimodality 공식(CODE_TO_PORT §2). |
| `test_features.py` | L2 robust 산포·spec margin·공간 feature. |
| `test_signatures_status.py` | L3 signature 발화 + L4 status/trump/specificity. |
| `test_ingest_raw_df.py` | L0 정본 raw_df(6-메타행) 파싱·fail 매핑. **정본 레이아웃 기준선**. |
| `test_store.py` | store CRUD + `make_case_id` idempotent + `search_precedents` + 스키마 v4(eval_precedent/updated_at) + `save_features` 가 파생키(DB 미저장)를 무시하는지. |
| `test_precedent_client.py` | L5 선례검색 어댑터 — case_ctx 의 자기 세션/analysis_key·발화 signature·top-k 상한이 **store 로 실제 전달되는지**(배선). store 쪽 동작은 test_store 담당. |
| `test_rules_integrity.py` | 배포 `rules/*.yaml` 자체 정합성 — 조건·**코드**(`th["키"]`)가 참조하는 임계값이 thresholds.yaml default 에 있는지, SPECIFICITY_ORDER 1:1, 어휘·`enabled` 타입. 룰 파일만 고쳐도 깨지는 부류를 잡는다. |
| `test_e2e.py` | `evaluate()` 전 구간 E2E + 입력키 검증(raw_df/raw_table/items 부재 시 ValueError). |
| `test_calibrate.py` | `recalibrate()` 분위수 → thresholds item_class 갱신 + 버전 등록 (**thresholds 는 tmp 복사본으로 격리**). |
| `test_db_input_import.py` | db_input **레거시 20컬럼** label(human_status/root_cause)+case_outcome 적재·idempotent. |
| `test_db_input_simple_format.py` | db_input **단순 5컬럼** 포맷 — 헤더 감지·unit alias(VOLTS/HERTZ/AMPS)·사전 전수검사(부분 적재 금지)·코멘트 수정 재적재. |
| `test_db_input_json_mode.py` | db_input `--dry-run`/`--json` 계약(stdout JSON·종료코드 0/2·플래그 파싱) + unit 부분일치(UNIT_STEMS)·`%` 어휘. **report_server 의 Honey 'DB Input' 이 이 계약에 의존** → ../../docs/13 §10. |
| `integration/test_df_honey_eval.py` | df_honey → run_input 어댑터 경로(raw_table) 대량 평가. |
| `integration/adapter.py` | `df_honey_to_run_input` — report_server 쪽 어댑터 모사(eval_engine import 안 함). |

## 규칙·주의
- **DB 격리 필수**: DB 를 건드리는 테스트는 `fresh_db` fixture 사용. 직접 `config.DB_PATH` 쓰지 말 것.
- `test_ingest_raw_df.py` 의 메타행 6개(TSEQ/TNO/**STEP**/UNIT/HILIM/LOLIM)가 정본 레이아웃 기준선 —
  파서 변경 시 이 테스트가 먼저 깨져야 정상. (tools 의 5-메타행 드리프트와 대비 → [../tools/CLAUDE.md](../tools/CLAUDE.md))
- 새 signature/feature 추가 시: 해당 단계 테스트에 발화/결측 케이스 둘 다 추가.
- **단위 함수만 테스트하지 말 것.** 이 스위트가 놓쳐 온 부류는 계산이 아니라 **배선**이다 —
  인자를 안 넘기거나(precedent_client), meta 를 case 에 안 싣거나(ingest), 임계값 키 이름이
  어긋나도 단위 테스트는 전부 초록이었다. 새 값이 A→B 로 흘러야 한다면 그 흐름을 직접 본다.
- 임계값을 새로 읽는 코드를 쓰면 `th["키"]` 형태를 유지할 것 — `test_rules_integrity` 가
  그 문자열을 훑어 thresholds.yaml 과 대조한다(변수로 우회하면 검사에서 빠진다).

## 실행
```
python -m pytest -q            # 전체
python -m pytest tests/test_ingest_raw_df.py -q   # 단일 파일
```
설정: `../pytest.ini`.

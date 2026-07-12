# tests — 테스트 스위트 (블록 진입점)

`python -m pytest -q` (현재 **76 통과**). DB 테스트는 전부 tmp 격리 — 운영 `eval.db` 오염 없음.
상위 규칙 [../CLAUDE.md](../CLAUDE.md).

## 파일 지도
| 파일 | 커버 대상 |
|---|---|
| `conftest.py` | `fresh_db` fixture — `config.DB_PATH/DATA_DIR` 를 tmp 로 monkeypatch 후 `init_db`. |
| `test_metrics.py` | L1 `cpk_summary`/yield/bimodality 공식(CODE_TO_PORT §2). |
| `test_features.py` | L2 robust 산포·spec margin·공간 feature. |
| `test_signatures_status.py` | L3 signature 발화 + L4 status/trump/specificity. |
| `test_ingest_raw_df.py` | L0 정본 raw_df(6-메타행) 파싱·fail 매핑. **정본 레이아웃 기준선**. |
| `test_store.py` | store CRUD + `make_case_id` idempotent + `search_precedents` + 스키마 v4(eval_precedent/updated_at). |
| `test_e2e.py` | `evaluate()` 전 구간 E2E + 입력키 검증(raw_df/raw_table/items 부재 시 ValueError). |
| `test_calibrate.py` | `recalibrate()` 분위수 → thresholds item_class 갱신 + 버전 등록 (**thresholds 는 tmp 복사본으로 격리**). |
| `test_db_input_import.py` | db_input label(human_status/root_cause)+case_outcome 적재·idempotent. |
| `integration/test_df_honey_eval.py` | df_honey → run_input 어댑터 경로(raw_table) 대량 평가. |
| `integration/adapter.py` | `df_honey_to_run_input` — report_server 쪽 어댑터 모사(eval_engine import 안 함). |

## 규칙·주의
- **DB 격리 필수**: DB 를 건드리는 테스트는 `fresh_db` fixture 사용. 직접 `config.DB_PATH` 쓰지 말 것.
- `test_ingest_raw_df.py` 의 메타행 6개(TSEQ/TNO/**STEP**/UNIT/HILIM/LOLIM)가 정본 레이아웃 기준선 —
  파서 변경 시 이 테스트가 먼저 깨져야 정상. (tools 의 5-메타행 드리프트와 대비 → [../tools/CLAUDE.md](../tools/CLAUDE.md))
- 새 signature/feature 추가 시: 해당 단계 테스트에 발화/결측 케이스 둘 다 추가.

## 실행
```
python -m pytest -q            # 전체
python -m pytest tests/test_ingest_raw_df.py -q   # 단일 파일
```
설정: `../pytest.ini`.

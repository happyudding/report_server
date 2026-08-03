# tools — 샘플 생성·testbench·데모 선례 (블록 진입점)

`evaluate()` 를 사람이 눈으로 검증하기 위한 오프라인 도구 모음. 운영 코드 아님.
상위 규칙 [../CLAUDE.md](../CLAUDE.md).

## 파일 지도
| 파일 | 역할 | 실행 |
|---|---|---|
| `testbench_eval.py` | 정본 raw_df CSV → `evaluate()` → 콘솔 리포트(status/signature/comment/선례/분포). | `python tools/testbench_eval.py <csv> [--meta m.json] [--persist]` |
| `run_testbench.py` | tkinter 파일선택 GUI → testbench_eval 실행. `../run_testbench.bat` 더블클릭용. | bat 더블클릭 |
| `make_sample_raw_df.py` | 샘플 raw_df CSV + 사이드카 meta.json 생성(아키타입별 fail 주입). | `python tools/make_sample_raw_df.py` |
| `seed_demo_precedents.py` | 데모용 가짜 선례 seed(lot_id=`LOT_DEMO_PAST`). **운영 판단 사용 금지.** | `python tools/seed_demo_precedents.py` |

- `testbench_eval` 기본은 `persist=False`(preview, DB 미접근). `--persist` 시 `data/eval_testbench.db`.
- meta 우선순위: `--meta` > 사이드카 `<csv>.meta.json` > 내장 DEFAULT_META.

## ⚠ 알려진 드리프트 (미수정 — 문서화만)
`make_sample_raw_df.py` 와 `testbench_eval.read_raw_df` 는 **구 5-메타행 레이아웃**
(TSEQ/TNO/UNIT/HILIM/LOLIM, STEP 없음)을 가정한다. 그러나 정본 파서
[`pipeline/ingest.py:_ingest_raw_df`](../eval_engine/pipeline/ingest.py#L208) 와 테스트
`tests/test_ingest_raw_df.py` 는 **6-메타행(STEP 행 포함)** 을 기대한다
(`df.iloc[3]=UNIT, iloc[4]=HILIM, iloc[5]=LOLIM, iloc[6:]=측정`).

- 결과: 이 생성기의 CSV 를 정본 파서에 넣으면 UNIT↔HILIM↔LOLIM 이 한 행씩 밀려 오파싱된다.
- 테스트가 통과하는 이유: 테스트는 `raw_table` 경로(`tests/integration/adapter.py`)를 쓰거나
  6-메타행 df 를 직접 만들기 때문. 이 tools 는 테스트 경로에 없다.
- **수정하려면**: 두 tools 를 6-메타행(STEP 행 추가)으로 맞추거나, 파서를 5-메타행으로 되돌린다.
  정본은 6-메타행(최상위 CLAUDE.md·docs·테스트 기준)이므로 tools 쪽을 맞추는 것이 정합적.

## 관련 문서
- 정본 raw_df 포맷·필드사전 [../docs/REPORT_GENERATOR_DATA_REQUEST.md](../docs/REPORT_GENERATOR_DATA_REQUEST.md).

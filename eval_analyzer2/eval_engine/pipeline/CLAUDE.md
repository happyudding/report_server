# pipeline — L0~L6 판단 파이프라인 (블록 진입점)

`evaluate()` 의 실제 로직. 각 단계는 **순수 함수**(입력 dict → 출력 dict), `api.py` 가 순서대로 호출.
상위 규칙 [../../CLAUDE.md](../../CLAUDE.md), 엔진 개요 [../CLAUDE.md](../CLAUDE.md), 공식 정본 [../../docs/CODE_TO_PORT.md](../../docs/CODE_TO_PORT.md).

## 단계별 파일 지도
| 파일 | 단계 | 핵심 함수 | 무엇을 하나 |
|---|---|---|---|
| `ingest.py` | L0 | `ingest()` | run_input → fail_case 들. 마스터 upsert, item 파싱, item_class, case_id, 측정 시리즈 메모리 첨부. |
| `metrics.py` | L1 | `compute()`, `cpk_summary()` | raw(메모리)에서 cpk/cpl/cpu/cp/mean/stdev/min/max/yield/bimodality. |
| `features.py` | L2 | `compute()` | robust 산포(MAD)/spec margin/공간(edge·center·quadrant·gradient)/site_cpk_delta. |
| `signatures.py` | L3 | `evaluate()` | `signatures.yaml` when_metric 평가 → 발화 signature + evidence + bin context. |
| `status.py` | L4 | `decide()` | severity 집계 + severity_bias + trump + specificity → status/confidence/completeness. |
| `recommend.py` | L5 | `find_precedents()`, `make_comment()` | 선례검색(어댑터 위임) + 템플릿/LLM comment. |
| `present.py` | L6 | `persist()`, `to_result()` | eval.db 적재(raw 제외) + RunResult dict 직렬화. |
| `_rules.py` | 공용 | `thresholds_for()`, `signatures_doc()`, `bin_taxonomy_for()`, `validate_outcome()` | rules/*.yaml 로더(lru_cache). **임계값은 여기서만 읽는다.** |

## L0 ingest — 입력 3경로 (핵심 주의점)
`_build_cases` 가 run_input 키로 분기:
- `raw_df`(**정본, DataFrame**) → `_ingest_raw_df`. 진리값 모호 회피 위해 `is not None` 분기.
- `raw_table`(레거시 중립 dict, df_honey 어댑터) → `_ingest_raw_table`.
- `items`(degrade, 요약통계 직접) → `_ingest_degrade`.

### ⚠ raw_df 레이아웃은 6-메타행 (STEP 포함) — 절대 순서
```
columns: SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO, <item...>   (meta 7 + item)
row0 TSEQ  row1 TNO  row2 STEP(P1/P2/P3, 미사용)  row3 UNIT  row4 HILIM(USL)  row5 LOLIM(LSL)  row6+ 측정
```
파서는 `df.iloc[1]=TNO, df.iloc[3]=UNIT, df.iloc[4]=HILIM, df.iloc[5]=LOLIM, df.iloc[6:]=측정`
([ingest.py:220-223](ingest.py#L220-L223))으로 **고정**. row/컬럼 순서 바꾸면 파서가 깨진다.
- **fail 식별** = serial 의 `FAILTNO` == item 의 `TNO` → 그 item·그 serial 의 `BIN` = fail bin.
  FAILTNO 공란/0/NaN = pass. (limit 재판정 아님)
- `tools/` 의 생성기·testbench 는 **구 5-메타행(STEP 없음)** 을 가정 — 파서와 불일치.
  → [../../tools/CLAUDE.md](../../tools/CLAUDE.md) 의 ⚠ 참조.

## 결측·표본 처리 규칙 (양호 오판 금지)
- feature 가 None 이면 해당 signature `applies=False` (조건 False 처리). 결측을 "양호"로 읽지 않는다.
- `n_dut < n_min`(thresholds) → 고차모멘트 의존 signature(skewness/kurtosis/bimodality) 비활성화.
- 좌표/site 없으면 공간·site feature None → data_completeness ↓ → confidence ↓.

## status 판정 요점 (status.py)
- severity rank: MONITOR<MINOR<MAJOR<CRITICAL. bin `severity_bias` 로 rank 변조.
- **OK**: 발화 signature 0건 + data_completeness=full → OK(정상 확정). 결측이면 MONITOR 유지.
- **trump**: `cpk<cpk_bad AND yield<cpk_trump_yield_floor` → CRITICAL 강제.
- **specificity**: 같은 severity 충돌 시 구체 signature(EQUIPMENT_SUSPECT/EDGE_FAIL…) > 일반(LOW_CPK) 가 primary.
  순서는 `SPECIFICITY_ORDER` 리스트 — signatures.yaml 에 signature 추가 시 이 리스트도 갱신.

## preview(persist=False) 주의
DB 미접근 — `item_id` 를 canonical 해시로 대체하므로 이때 `case_id` 는 persist 재실행 시 달라질 수 있음(미리보기 전용).

## 관련 문서
- 컬럼 의미 사전 [../../docs/5STAGE_COLUMNS.md](../../docs/5STAGE_COLUMNS.md)
- feature/cpk/ECDF 정확 공식 [../../docs/CODE_TO_PORT.md](../../docs/CODE_TO_PORT.md)
- 저장 컬럼(raw_metrics/features) [../../docs/DB_SCHEMA.md](../../docs/DB_SCHEMA.md)

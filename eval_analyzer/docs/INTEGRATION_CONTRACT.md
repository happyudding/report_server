# report_server ↔ eval_analyzer 연동 계약

> 별도 프로젝트지만 **같이 움직인다**: client 가 파일을 1회 run 할 때마다 eval_analyzer 가 작동.
> 의존 방향은 **report_server → eval_analyzer 한 방향**(report_server 가 eval_analyzer 를 import 해 호출).
> **eval_analyzer 는 report_server 를 절대 import 하지 않는다.**

---

## 1. 트리거 지점
- report_server 의 `report_generator` 파이프라인이 raw 측정을 처리해 리포트를 만드는 시점
  (df_honey 로 raw 가 메모리에 올라온 직후, 한 파일/세션당 1회).
- 그 시점에 report_server 가 `eval_analyzer.evaluate(run_input)` 를 호출.
- eval_analyzer 는 결과를 **자체 eval.db** 에 적재하고 결과 dict 를 반환. report_server 는
  반환값을 UI 표시에 쓰거나 무시 가능(저장 주인은 eval_analyzer).

## 2. evaluate() 시그니처
```python
# eval_engine/api.py
def evaluate(run_input: dict, *, engine_version: str | None = None,
             model_version: str | None = None, persist: bool = True) -> dict:
    """한 세션(파일 1회 run)의 fail item 들을 평가.
    - run_input: §3 형식 (meta + raw_table). report_server 가 구성해 넘김.
    - persist=True 면 eval.db 에 ingest_run/fail_case/raw_metrics/features/evaluation 적재.
    - 반환: §4 RunResult dict.
    """
```

## 3. 입력 — run_input
**(정본) `raw_df` — honey_parse 정규화 df 를 그대로 전달.** 상세·필드사전은
[REPORT_GENERATOR_DATA_REQUEST.md](REPORT_GENERATOR_DATA_REQUEST.md).
```python
run_input = {"meta": {...}, "raw_df": df}   # df = pandas.DataFrame (아래 레이아웃)
# columns: SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO, TESTITEM1, TESTITEM2, ...
# row0 TSEQ / row1 TNO / row2 STEP / row3 UNIT / row4 HILIM(USL) / row5 LOLIM(LSL) / row6+ 측정
# fail 식별 = FAILTNO(serial이 fail한 test의 TNO) == 그 item의 TNO → fail item, 그 serial BIN=fail bin
# 사용: XPOS/YPOS/BIN/UNIT/HILIM/LOLIM/측정값/FAILTNO/TNO. 미사용: SERIAL/SHOT/DUT/TSEQ.
```

**(레거시) `raw_table` — 중립 dict.** 초기 계약. 여전히 지원(하위호환)하지만 신규 결합은 `raw_df` 사용.
report_server 가 df 에서 아래 **중립 dict** 로 변환해 전달(eval_analyzer 는 내부에서
자체 numpy 로 재구성).
```python
run_input = {
  "meta": {
     "product_name": "S5E_XXXX_13",     # PARTID 13자리
     "product_type": "PMIC",            # MDDI/PDDI/PMIC/SECURITY/TCON
     "family_product": "SOC",           # product_type 별 1:1 허용값(미매칭 시 ValueError)
     "pkg_type": "FCCSP",
     "process": "BCD1370F", "revision": 0.1,   # FLOAT (0/0.1/1.0/2.1…)
     "inch": 12, "gross_die": 280, "fab_line": "L1",
     "tester": "T01", "para": "P01",
     "lot_id": "LOT001", "wafer_number": 3,     # INTEGER
     "edm_link": "http://edm/...",       # EDM Link
     "temperature": 25,                  # [req0] INTEGER, 입력만
     "corner": "NN",                     # [req0] NN/SS/FF
     "source_file": "....csv",
     "ingested_by": "honey_client",
     "analysis_key": null                # (선택) report.db 링크 — 없어도 됨
  },
  # raw_table: df_honey 레이아웃을 dict 로 (per-DUT 행)
  "raw_table": {
     "meta_columns": ["DUT", "XCoord", "YCoord", "Bin", "Serial"],
     "item_columns": ["VREF_TRIM", "IDDQ_INIT", "..."],
     "units":       {"VREF_TRIM": "V", "IDDQ_INIT": "A"},   # → value_type 매핑
     "lower_limit": {"VREF_TRIM": 1.0,  "IDDQ_INIT": null},
     "upper_limit": {"VREF_TRIM": 1.4,  "IDDQ_INIT": 15.0},
     "rows": [
        {"DUT": 1, "XCoord": -3, "YCoord": 5, "Bin": 1,  "Serial": "...",
         "VREF_TRIM": 1.21, "IDDQ_INIT": 12.3},
        {"DUT": 2, "XCoord": -3, "YCoord": 6, "Bin": 18, "Serial": "...",
         "VREF_TRIM": 1.55, "IDDQ_INIT": 12.1},
        # ... per-DUT
     ]
  }
}
```
- **value_type(unit계열)** 는 units 값에서 매핑(V/A/Hz/CODE/P_F/Ohm/Sec). category_major(TRIM/NON_TRIM)
  는 item_name 에 'TRIM' 포함 여부로 판정(또는 meta 로 명시 가능).
- **degrade 입력**: raw_table 없이 집계만 줄 수도 있음(아래). 그러면 yield-only.
```python
# degrade 형식 (raw 없을 때)
run_input = { "meta": {...},
  "items": [ {"item_name":"X","bin":18,"unit":"V","yield":0.68,"fail_count":3,"total_count":280,
              "lsl":1.0,"usl":1.4}, ... ] }
```

## 4. 출력 — RunResult
```python
{
  "run_id": 123,
  "engine_version": "ev1", "model_version": "user-model-x",
  "cases": [
    {
      "case_id": "<sha256>",
      "item_canonical": "vref_trim", "item_raw": "VREF_TRIM",
      "item_class": "TRIM|V|18", "bin": 18,
      "issue_category": "ETC",
      "status": "MAJOR",
      "primary_signature": "SEVERE_OUTLIER",
      "secondary_signatures": ["TAIL_RISK"],
      "confidence": 0.8, "data_completeness": "full",
      "comment": "site 3 에서만 튐, golden unit 재측정 권장 (과거 retest→정상 이력)",
      "evidence": [ {"signal_code":"OUTLIER_RATIO","value":0.06,"weight":1.0}, ... ],
      "signatures": [
        {"id":"SEVERE_OUTLIER","role":"primary",
         "evidence":[{"signal_code":"OUTLIER_RATIO","value":0.06,"note":"outlier_ratio 0.06"}],
         "action_ko":"golden unit 재측정 권장"},
        {"id":"TAIL_RISK","role":"secondary", "evidence":[...], "action_ko":"..."}
      ],
      "precedents": [ {"action":"retest","result":"recovered_normal","comment":"..."} ]
    },
    ...
  ]
}
```
- **signatures**: 발화한 rule(signature)별 evidence/action_ko 세분화(evidence는 전부 rule 단위로 묶여
  primary/secondary 구분 없이 합쳐지는 evidence 필드와 달리 어떤 값이 어느 rule 근거인지 식별 가능).
- **item_raw**: 원본 item명(정규화 전). report_server Issue Table 의 `Item`/subject 컬럼과 **join 키**
  = `(item_raw, bin)`. (`item_canonical` 은 정규화명 — 내부 매칭/선례용.)
- **issue_category**: `YIELD | CPK | ETC`. primary_signature 기준 버킷(GROSS_FAIL→YIELD,
  LOW_CPK/SPEC_TOO_TIGHT→CPK, 그 외→ETC). report_server 가 signature 택소노미를 몰라도 Issue Table
  Yield/CPK/ETC 카테고리로 분류 가능(특히 지금 수기인 ETC 자동 채움). 표시라벨 매핑은 report_server 몫.
- **cases scope(저장 기준)**: ①yield fail(FAILTNO==TNO) item×fail bin **∪** ②yield fail 은 없지만
  cpk<cpk_warn(1.33) 인 marginal item(bin=PASS_BIN=1). 저장 판단은 rule(L3) 계산 뒤(`present.should_store`)
  — 반환 `cases` == eval.db 저장분. (향후 "전체 rule 위반 시 저장" 으로 판단식만 확장 예정.)

## 5. 서로가 필요한 것 (상호 의존 정리)
**report_server → eval_analyzer 에 줘야 할 것:**
- 트리거(파일 run 시 evaluate 호출).
- run_input.meta (product/product_type/family_product/pkg_type/lot/wafer/process/revision/inch/gross_die/fab_line/tester/para/edm_link/temperature/corner).
- run_input.raw_df (honey_parse 정규화 df: 측정 + HILIM/LOLIM + XPOS/YPOS + BIN + FAILTNO/TNO)
  — cpk/산포/공간 계산의 원재료. ※ 현재 report_server 는 이걸 *버린다*(REPORT_SERVER_CONTEXT §5)
  → 결합 시 메모리로 넘겨주는 게 핵심. (레거시 raw_table 도 지원)

**eval_analyzer → report_server 에 주는 것:**
- RunResult(§4) — status/signature/comment/confidence. report_server UI 가 표시(후속).
- 저장은 eval_analyzer 가 자체 eval.db 에. report_server DB 변경 불요(독립).

**충족 안 될 때(raw 미전달):** eval_analyzer 는 yield-only degrade
(LOW_YIELD/GROSS_FAIL 만, cpk/산포 signature 휴면, data_completeness="low").

## 6. 결합 시 코드 위치 (report_server 측, 다른 담당)
- report_server 가 `pip install -e eval_analyzer` 또는 경로 추가로 `eval_engine` 을 import.
- report_generator 파이프라인 끝에서:
  ```python
  from eval_engine import evaluate
  result = evaluate(build_run_input(df_honey_group, meta))
  # result 를 UI/응답에 첨부 (저장은 eval_analyzer 가 함)
  ```
- `build_run_input` 어댑터는 **report_server 쪽**에 둔다(eval_analyzer 는 중립 dict 만 받음).

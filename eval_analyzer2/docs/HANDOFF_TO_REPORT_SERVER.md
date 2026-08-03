# report_server 담당자 전달 — eval_analyzer 연동에 필요한 것

> 그대로 담당자/담당 AI 에게 전달 가능. report_server 의 DB·파이프라인을 **개편(rebuild)** 하는
> 관점에서, 별도 개발 중인 **eval_analyzer**(fail-item 평가 엔진)가 동작하려면 무엇이 필요한지 정리.
>
> 전제: report.db 는 전면 개편 예정. eval_analyzer 는 **자체 DB(eval.db)를 따로 소유**하므로
> report_server 가 평가결과를 저장할 의무는 없다. 핵심은 **"raw 입력을 eval_analyzer 로 흘려주는 것"**.

---

## 0. 한 줄
client 가 파일을 1회 run 할 때마다 eval_analyzer 가 작동해야 한다. 그러려면 report_generator
파이프라인에서 **per-DUT raw 측정(+limit+좌표+bin+site)을 버리지 말고 eval_analyzer.evaluate() 로
넘겨야** 한다. 현재는 업로드 시 집계 텍스트만 남기고 raw 를 버린다.

## 1. 지금 "덜 계산/덜 보존되는" 데이터 (under-computed / dropped)
| 항목 | 현재 상태 | 필요(왜) |
|---|---|---|
| per-DUT **raw 측정값** | 업로드 시 버림(집계 텍스트만 저장) | cpk·산포·outlier·tail·공간 전부 여기서 나옴 |
| **cpk/mean/stdev/lsl/usl** | report_analysis_summary 컬럼 있으나 **전부 NULL** | 공정능력·spec margin 판정 |
| 측정 **mean** | summary.mean_val 은 *yield avg*(측정 mean 아님) | 혼동 주의 — 실제 측정 mean 필요 |
| **x/y 좌표(XCoord/YCoord)** | 서버 미보존 | wafer 공간 패턴(edge/center/radial) |
| **site(=DUT)** | 서버 미보존 | site간 편차(EQUIPMENT_SUSPECT) |
| 산포·wafer 패턴 지표 | 미계산 | 산포/공간 signature |
| **temperature / corner(NN/SS/FF)** | 미수집 | 측정 조건(조건의존 fail) |
| item **category(TRIM 여부)/value_type(V/A/Hz/CODE…)** | 미분류 | ★eval_analyzer 룰 인덱스(item_class) |
| **family_product**(상위 제품군) | 미관리 | cross-product 이력 비교(모과제) |
| limit(lsl/usl) **revision별 이력** | 미보존 | spec 변경 추적 |

## 2. 담당자가 해야 할 것 (개편 관점)
### A. (필수) 결합 지점에서 raw 를 eval_analyzer 로 전달
- report_generator 가 df_honey 로 raw 를 메모리에 올린 직후, 어댑터 `build_run_input(df, meta)` 로
  run_input(아래 §3)을 만들어 `eval_engine.evaluate(run_input)` 호출.
- **정본 입력은 `raw_df`**(§3-A, 6-메타행 DataFrame). samples/*.csv 가 그 기대 레이아웃이며,
  **레퍼런스 어댑터**는 [tests/integration/adapter.py](../tests/integration/adapter.py) 의
  `sample_csv_to_run_input` (eval_engine import 안 함 — 그대로 복사/참고 가능). 레거시 `raw_table`(§3-B)도 지원.
- eval_analyzer 는 그 raw 에서 cpk/산포/공간을 계산하고 **자체 eval.db 에 저장**. report_server 는 결과만 받음.
- ※ eval_analyzer 는 report_server 코드를 import 하지 않는다. 어댑터(build_run_input)는 **report_server 쪽**에 둔다.

### B. (권장) 세션 입력에 temperature / corner 받기
- 업로드/실행 단위(세션)당 `temperature`(수치), `corner`(NN/SS/FF) 입력 UI/필드 추가 → run_input.meta 로 전달.

### C. (권장) item 메타 분류 제공
- item 원본명에서 `category_major`(TRIM 포함 여부), `value_type`(단위→V/A/Hz/CODE/P_F/Ohm/Sec) 분류.
  (eval_analyzer 도 fallback 추정하지만, 정확한 분류를 주면 룰 정확도↑)
- `family_product`(제품→상위군) 매핑 테이블 제공.

### D. (개편 시) 무엇을 DB 에 새로 쌓을지
- report.db 를 새로 설계한다면, **최소한 raw per-DUT(또는 그 compact 요약통계)·좌표·site·limit·
  temperature·corner** 가 보존/전달 가능해야 eval_analyzer 가 풀 기능. (raw 전체 저장이 부담이면
  결합 시 메모리 전달만으로 충분 — eval_analyzer 가 계산 후 compact 만 자기 DB 에 저장.)

## 3. run_input 형식 (eval_analyzer 가 받는 것)
meta 는 두 경로 공통. raw 는 **A(정본 raw_df)** 또는 **B(레거시 raw_table)** 중 하나.
```python
meta = {"product_name","family_product","product_type","process","revision","inch",
        "gross_die","fab_line","tester","para","lot_id","wafer_number",
        "temperature","corner","source_file","ingested_by","analysis_key?"}
```

### A. (정본) raw_df — 6-메타행 DataFrame
```python
run_input = {"meta": meta, "raw_df": df}   # df = pandas.DataFrame
# columns: SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO, <item1>, <item2>, ...   (meta 7 + item)
# iloc row0 TSEQ / row1 TNO / row2 STEP / row3 UNIT / row4 HILIM(USL) / row5 LOLIM(LSL) / row6+ 측정
# fail 식별 = 그 serial 의 FAILTNO == item 의 TNO → fail item, 그 serial 의 BIN = fail bin.
#            FAILTNO 공란/0/NaN = pass.
```
**dtype 계약(중요)**: 데이터행(row6+)의 **item 셀은 실제 숫자(float/int) 객체**여야 한다
(문자열이면 무시됨). 메타행(TNO/UNIT/HILIM/LOLIM)·XPOS/YPOS/BIN/FAILTNO 는 문자열이어도 파서가 변환.
CSV 에서 재구성할 때의 예시는 레퍼런스 어댑터 `sample_csv_to_run_input` 참조.

### B. (레거시) raw_table — 중립 dict
```python
run_input = {"meta": meta, "raw_table": {
     "meta_columns": ["DUT","XCoord","YCoord","Bin","Serial"],
     "item_columns": ["<item>...",],
     "units":       {"<item>":"V|A|Hz|CODE|P_F|Ohm|Sec"},
     "lower_limit": {"<item>": float|null},
     "upper_limit": {"<item>": float|null},
     "rows": [ {"DUT":1,"XCoord":-3,"YCoord":5,"Bin":1,"Serial":"...","<item>":<value>}, ... ]
}}
```
상세는 eval_analyzer/docs/INTEGRATION_CONTRACT.md.

## 4. analyzer 가 "놓치면 안 될 것" (보존·전달 우선순위)
1. **per-DUT 측정값 + 그 item 의 lsl/usl** (cpk·산포의 원재료) — 없으면 yield-only degrade.
2. **bin (per-DUT)** — fail 식별·분류(defective/abnormal 의미).
3. **XCoord/YCoord** — wafer 공간 진단.
4. **site/DUT id** — site간 편차 진단.
5. **meta**: product_name·lot_id·wafer_number·process·revision·temperature·corner.
6. **item 원본명**(가능하면 TRIM 여부·단위계열) — 룰 인덱스/선례 매칭.

## 5. report_server 가 굳이 안 해도 되는 것
- 평가결과/라벨 저장 (eval_analyzer 자체 DB 가 함).
- cpk/산포 계산 (eval_analyzer 가 함; report_server 는 raw 만 흘려주면 됨).
- → 즉 report_server 의 부담은 **"raw 를 버리지 않고 evaluate 로 넘기는 어댑터 + 세션 temperature/corner 입력"** 정도.

## 6. evaluate() 반환을 Issue Table 에 붙이기 (join 레시피)
> **지금 바로 붙일 필요 없음.** report_server Issue Table 포맷도 개편 예정이라, eval 은 **나중에 join 하기
> 쉽게 출력 포맷만** 맞춰뒀다(`item_raw`/`issue_category` 필드 추가). 아래는 개편 후 wiring 가이드.

방향은 **보강**(기존 Yield/CPK threshold scan 유지 + eval 로 comment/ETC 채움), 대체 아님.
- **join 키** = `(case.item_raw, case.bin)` ↔ Issue Table 행의 `(Item/subject, Bin)`.
  `item_raw` 는 정규화 전 원본 item명이라 그대로 매칭된다(`item_canonical` 은 내부용 정규화명).
- **comment 열** ← `case.comment` (분석방향 한 문장). 개발/PTE 코멘트 열은 사람이 그대로 사용.
- **카테고리 버킷** ← `case.issue_category` (`YIELD|CPK|ETC`). 특히 지금 **수기 입력인 ETC 를 eval 이 자동
  생성**(EDGE_FAIL/CLUSTER_FAIL/SUBPOP_GAP/OUTLIER 등 10종 signature + comment). 표시라벨(Yield/CPK/ETC)
  매핑·정렬(status 심각도)은 report_server 몫.
- eval 케이스는 **fail 난 item 만**(FAILTNO==TNO). cpk<1.33 이지만 fail 안 난 marginal item 은 기존 CPK
  threshold scan 이 계속 담당(그래서 "보강"). 반환 필드 상세는 INTEGRATION_CONTRACT §4.

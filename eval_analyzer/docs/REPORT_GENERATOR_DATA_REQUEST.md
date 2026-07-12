# report_generator 담당자 전달 — eval_analyzer 에 넘겨줄 데이터

> 그대로 포워딩 가능. **eval_analyzer**(fail-item 평가 엔진)가 DB(eval.db)를 쌓으려면,
> report_generator(honey_parse) 파이프라인에서 **parse 결과 df + 세션 meta** 를 넘겨주면 된다.
> eval_analyzer 가 그 df 에서 cpk/bimodality/산포/공간 지표를 **직접 계산**한다(중복 계산 감수).
> report_generator 는 **지표를 계산할 필요 없이 raw df 만** 넘기면 된다.

---

## 0. 한 줄
파일 1회 run 시, honey_parse 가 만든 df 를 버리지 말고 아래처럼 넘겨달라:
```python
from eval_engine import evaluate
evaluate({"meta": {...}, "raw_df": df})   # in-process 호출, 파일 1회 run당 1회
```
- 호출 시점 = honey_parse 로 df 를 메모리에 확보한 직후.
- 어댑터/변환 불필요 — **정규화된 df 를 그대로** 넘기면 됨. 저장·계산은 eval_analyzer 가 함.
- eval_analyzer 는 report_server 코드를 import 하지 않음(의존 방향 report_server → eval_analyzer 한 방향).

## 1. df 레이아웃 계약 (★ 이 형식을 지켜야 함)
```
columns: SERIAL, SHOT, DUT, XPOS, YPOS, BIN, FAILTNO, TESTITEM1, TESTITEM2, ...
         └─────────── meta 7개 (컬럼 0~6) ──────────┘ └──────── item 컬럼 (7~) ────────┘
row 0 : TSEQ    (test sequence)
row 1 : TNO     (item별 test 번호)
row 2 : STEP    (P1/P2/P3 — test step)
row 3 : UNIT    (item별 단위)
row 4 : HILIM   (item별 상한)
row 5 : LOLIM   (item별 하한)
row 6+: serial(=DUT) 별 측정 데이터
```
- **컬럼 순서 고정**: 앞 7개가 meta, 8번째부터 test item. item 개수는 가변.
- **메타행 6개 위치 고정**: row0~5 = TSEQ/TNO/STEP/UNIT/HILIM/LOLIM. row6 부터 실제 측정.
- item 컬럼의 메타행 셀에 그 item 의 TNO/단위/상한/하한이 들어간다.

### eval_analyzer 가 실제로 쓰는 것 / 안 쓰는 것
| 필드 | 사용? | 용도 |
|---|---|---|
| XPOS / YPOS | **사용** | wafer 공간 패턴(edge/center/radial/quadrant) |
| BIN | **사용** | fail bin 식별·분류 |
| FAILTNO | **사용** | fail item 식별(아래 §2) |
| TNO (row1) | **사용** | FAILTNO 매핑 키 |
| UNIT (row3) | **사용** | value_type(V/A/Hz/CODE/P_F/Ohm/Sec) 매핑 |
| HILIM (row4) | **사용** | 상한 = USL |
| LOLIM (row5) | **사용** | 하한 = LSL |
| TESTITEM 측정값 (row6+) | **사용** | cpk/bimodality/산포/outlier 계산 |
| SERIAL / SHOT / DUT / TSEQ (row0) / STEP (row2) | 미사용 | (존재해도 무방, 읽지 않음) |

## 2. fail item 식별 규칙 (★ FAILTNO/TNO 를 정확히 채워야 함)
- 각 측정행(serial)의 **FAILTNO** = 그 serial 이 **fail 한 test 의 TNO** (stop-on-fail, serial당 1개).
- eval_analyzer 는 TNO행(row1)에서 **같은 TNO 를 가진 item 컬럼 = fail item** 으로 잡고,
  그 serial 의 **BIN 을 fail bin** 으로 쓴다.
- 즉 fail case (item, bin) = `FAILTNO == 그 item 의 TNO` 인 serial 들의 BIN 별 집합.
- **pass serial 의 FAILTNO 는 공란/NaN/0** 으로 둔다(= 무fail 로 간주).
- ※ limit 위반으로 fail 을 잡지 않는다 — **FAILTNO/TNO/BIN 이 fail 판정의 정본**.

## 3. meta 필드 (df 밖 `meta` dict 로 전달)
| 필드 | 필수 | 타입 | 비고 |
|---|---|---|---|
| product_name | **필수** | TEXT (PARTID 13자리) | |
| product_type | **필수** | MDDI/PDDI/PMIC/SECURITY/TCON | 미허용값이면 ValueError |
| family_product | **필수** | product_type별 허용값 | ★불일치 시 ValueError — 아래 gotcha |
| revision | **필수** | FLOAT (0/0.1/1.0/2.1) | 문자열 금지 |
| lot_id | **필수** | TEXT | |
| wafer_number | **필수** | INTEGER | 문자열 금지 |
| pkg_type / process / inch / gross_die / fab_line / tester / para | 권장 | 각 타입 | 마스터 채움 |
| temperature | 권장 | INTEGER | 세션 입력(현재 저장만) |
| corner | 권장 | NN/SS/FF | 세션 입력 |
| edm_link / source_file / ingested_by / analysis_key | 선택 | TEXT | 링크·추적 |

`family_product` 허용값(product_type별): MDDI=MX/AQUA/CHINA/MDDI_ETC · PMIC=SOC/MEMORY/DISPLAY/IF/PMIC_ETC
· SECURITY=NFC_ESE/ESE/Contactless/SECU_ETC · PDDI=LCD/PDDI_IT/QDOLED/PDDI_ETC · TCON=TV/TCON_IT/TCON_ETC.

## 4. degrade (df 를 못 줄 때 — 집계만)
df 대신 item별 집계만 줄 수도 있다. 이 경우 yield 기반 판정만(cpk/산포/공간 signature 휴면):
```python
evaluate({"meta": {...}, "items": [
  {"item_name":"BUCK_SCAN","bin":40,"unit":"P_F","yield":0.3,
   "fail_count":196,"total_count":280,"lsl":None,"usl":None}, ...]})
```

## 5. 주의점 (gotcha)
- **family_product 매핑표 필요**: product → family_product(제품군) 매핑을 정확히 제공해야 함
  (허용값과 정확히 일치, 불일치 시 `evaluate()` 예외). rules/product_taxonomy.yaml 기준.
- **HILIM=USL(상한), LOLIM=LSL(하한)**. 한쪽만 있으면 그쪽만 채우고 반대는 공란(NaN).
- **FAILTNO 공란/0 = pass** 규칙을 지켜라(다른 규칙이면 알려달라).
- **컬럼 순서·메타행 위치 고정**(앞 7 meta, row0~5 메타행 TSEQ/TNO/STEP/UNIT/HILIM/LOLIM).
  바꾸면 eval_analyzer 파서가 깨진다.
- **다운샘플 금지** — 모든 serial 측정값을 그대로(산포/분포 정확도).
- 이 레이아웃엔 **Site 컬럼이 없어** site간 편차 지표(site_cpk_delta)는 비활성. 필요하면 협의.
- report_generator 는 **계산값이 아니라 df(raw)만** 넘긴다. 계산·저장은 eval_analyzer 책임.

## 6. eval_analyzer 가 굳이 안 받아도 되는 것
- 평가결과/라벨 저장(eval_analyzer 자체 DB).
- cpk/산포/공간 지표 계산(eval_analyzer 가 df 에서 직접 계산).
- → report_server 부담 = **"df 를 버리지 않고 evaluate 로 넘기기 + 세션 meta 채우기"** 정도.

> 상세 입출력 계약: [INTEGRATION_CONTRACT.md](INTEGRATION_CONTRACT.md). 계산 공식: [CODE_TO_PORT.md](CODE_TO_PORT.md).

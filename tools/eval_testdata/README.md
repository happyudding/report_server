# eval 디버깅용 합성 테스트 데이터 (L0~L6 트레이스 / signature 검증)

`make_eval_testdata.py` 는 **web_report 세션으로 바로 올릴 수 있는** 합성 데이터 한 벌을
만든다. 목적은 하나 — "이 룰이 이 세기에서 뜨는가"를 **정답을 아는 상태로** 확인하는 것.

```
# ① 7-meta CSV 1장 (직접 업로드용) — 파일이 있으면 _v2·_v3 … 로 자동 증가
server\.venv\Scripts\python.exe tools\eval_testdata\make_eval_testdata.py --single-csv data\eval_testdata_7meta.csv

# ② 전체 세트(parquet 30 source) 생성 + 세션까지
server\.venv\Scripts\python.exe tools\eval_testdata\make_eval_testdata.py --out data
server\.venv\Scripts\python.exe tools\eval_testdata\make_eval_testdata.py --out data --upload-only --upload http://127.0.0.1:8080
server\.venv\Scripts\python.exe tools\eval_testdata\make_eval_testdata.py --out data --upload-only --make-session
```

**버전 규칙**: `--single-csv` 는 같은 이름이 이미 있으면 `_v2`, `_v3` … 을 붙여 새로 만든다
(`versioned_path`). 기존 raw data 를 절대 덮지 않는다 — 이전 버전으로 재현·비교할 수 있어야
하기 때문.

**① CSV 1장** (`--single-csv`) — item 120개(겨냥 85 + **관찰용 무작위 30** + **미분류 5**)
× **chip 5,025개**(반경 40), 약 4.7MB:

| 파일 | 내용 |
|---|---|
| `<name>_vN.csv` | 7-meta honeyform CSV 1장 |
| `<name>_vN_answer.csv` | 정답표 — item·세기·목표/실측 지표·임계값·bin·fail 수 |
| `<name>_vN_verify.csv` | 실제 엔진 발화 대조 |

> **CSV 1장에 못 담는 signature 4종**(`CSV_EXCLUDE`) — 발화 조건 자체가 "웨이퍼의 상당수가
> fail" 이라 다른 항목이 쓸 chip 이 남지 않는다(chip 1개는 `FAILTNO` 를 하나만 갖는다):
> `GROSS_FAIL`(수율<50%) · `CONSTANT_VALUE`(spec 밖 상수 = 전량 fail) ·
> `BIDIR_TAIL`(양쪽 margin<1σ) · `TAIL_RISK`(spec_margin_min<1σ = 꼬리가 spec 을 넘어야 함).
> 이 넷은 전체 세트(`--out`, source 여러 개)에서 확인한다.
>
> ⚠ **`BIDIR_TAIL` 은 절반만 제외된다**(2026-08-19). 위 사유는 `_bidir_plan`(분포 자체가
> 넓어 양쪽 margin<1σ) 세트에만 해당한다. 같은 signature 를 **`replaces` 경로**로 겨냥하는
> `bidir_tails_specs()`(`BIDIR_TAILS_L1~L5` — 몸통은 멀쩡하고 양쪽 꼬리만 두꺼움)는 fail 이
> 자연 꼬리뿐이라 CSV 에 들어간다. 두 경로를 다 덮어야 룰이 실제로 검증된다 — v12 까지는
> 앞의 것만 있었고 그마저 CSV 에서 빠져 **BIDIR_TAIL 이 한 번도 검증되지 않았다**.
> (`WAFER_GRADIENT` 는 2026-08-13 룰 자체가 삭제돼 목록에서 빠졌다.)

> **웨이퍼가 커진 이유(반경 22→40)**: fail 은 chip 을 서로 배타적으로 쓰므로(`FAILTNO` 는
> chip 당 하나) 관찰군이 붙으면서 예산이 모자라 뒤쪽 item 이 통째로 밀려났다
> (표본이 적은 항목은 쓸 chip 이 없어져 fail 0 = 평가 제외가 된다).
> 관찰군을 `--random-items` 로 크게 늘릴 때는 `--radius` 도 같이 올릴 것 —
> 사용률이 90% 를 넘으면 생성기가 "예산 초과 제외" 로 항목을 버린다(콘솔에 찍힌다).

**② 전체 세트** (`--out DIR`) — parquet source 여러 개 × 2,453행 + `manifest.json` +
`answer_key.csv` + `verify.csv` + `metrics_detail.json`. item 500개.

`--items` `--random-items` `--radius` `--product-type` `--family` `--lot` `--seed` 로 조절한다.

---

## 1. 데이터 설계

### 1-1. 세기 5단계 (L1~L5)

각 signature 마다 그 룰의 판정 지표를 임계값에서 얼마나 벗어나게 할지로 5단계를 만든다.
**L1 = 미발화(정상, fail 0), L2부터 발화**하고 L5 까지 심해진다 — 즉 **fail 경계가 L1/L2
사이**다(2026-08-12 재편: 종전 사다리를 한 단계씩 내리고 L5 를 더 강하게 만들었다).

| 단계 | 의미 | `fail_mad_min`(OUTLIER, warn 4) | `kurtosis`(USL/LSL_TAIL, warn 10) | fail chip |
|---|---|---|---|---|
| L1 | 정상(fail 0) | 8.0 | 2.0 | **0** |
| L2 | **발화 시작** | 16.0 | 12.0 | 6 |
| L3 | 초과 | 22.0 | 15.0 | 15 |
| L4 | 크게 초과 | 32.0 | 19.0 | 30 |
| L5 | 심각 | 50.0 | 25.0 | 60 |

> OUTLIER L1 이 임계(4)의 2배인 8 인 것은 오타가 아니다 — 정규 몸통에서는 pass 최대거리가
> 구조적으로 ≈3.85σ 라, spike 를 spec 밖으로 내보내려면(= fail 을 만들려면) MAD 배수가
> 그 위에서 시작할 수밖에 없다. L1 은 spike 를 spec 안에 가두므로 fail 0 = 미발화다.

fail chip 수(`FAIL_N`)와 지표가 **함께** 올라간다. 그러려면 분포가 spec 밖으로 새면 안 되므로
값 기반 항목은 전부 **spec 안에 가둔다**(`bounded` — spec 밖으로 나간 값은 spec 안에서 재추출).
가두지 않으면 산포가 큰 항목은 chip 의 수십 %가 spec 밖 = fail 이 되어 웨이퍼 하나를 통째로
먹는다(§1-2 의 불변 법칙 때문).

> L1 은 **"겨냥한 그 룰"이 뜨지 않는다**는 뜻이다. 지표가 상관된 다른 룰
> (예: 산포가 넓으면 cpk 는 반드시 낮다)은 뜰 수 있고, 그건 오탐이 아니다.
> 아무것도 뜨면 안 되는 항목은 `NORMAL_*` (group=normal) 이다.

### 1-2. 불변 법칙 — **spec 밖 = fail**

이 엔진의 목적은 "fail 난 item 이 왜 죽었는지" 설명하는 것이므로, 데이터는 양방향으로
정합해야 한다. **spec 을 벗어난 chip 은 예외 없이 fail 이고, spec 안인 chip 은 fail 이 아니다.**

- fail chip 수는 **분포가 정한다** — 산포가 넓거나 중심이 치우칠수록 자동으로 늘어난다.
  고정 개수를 강요하지 않는다.
- 값만으로 fail 이 안 생기는 항목(좁은 분포·공간 룰·수율 룰)만 레벨 사다리(`FAIL_N`)만큼
  chip 을 골라 limit 밖으로 민다(`_push_out_of_spec`).
  ⚠ 미는 거리(`FAIL_MARGIN`, limit 대비 0.2~1.0%)를 키우면 밀려난 chip 이 **OUTLIER 판정선
  (거리 4 MAD **AND** 몸통~fail 구간의 빈 폭 비율 0.35)** 에 닿아 겨냥하지 않은 OUTLIER 가
  동반발화한다. 산포가 좁은 시나리오일수록 먼저 닿는다.
- **`fails={"mode": "natural"}` 은 이 밀어내기를 통째로 건너뛴다**(2026-08-14). fail 은
  분포가 스스로 spec 을 넘긴 chip 뿐이다. 밀어내기는 중간 꼬리의 chip 을 limit 밖으로
  **옮기므로** 그 자리에 구멍이 남고, 그 구멍이 곧 `fail_body_jump_ratio`(OUTLIER 끊김
  지표)라 어떤 겨냥이든 값 축에서 outlier 모양이 된다. 그래서 **꼬리 겨냥**
  (`USL_TAIL`/`LSL_TAIL`/`BIDIR_TAILS`)과 **미분류군(UNKNOWN)** 은 natural 모드를 쓴다. 대신 fail 수를 레벨 사다리로 강제할 수
  없으므로(분포가 정한다), 이 모드는 fail 수가 아니라 **분포 지표가 사다리인 유형**에만
  쓸 수 있다. 예: `LOW_CPK`(cpk 1.25)를 natural 로 바꾸면 자연 초과가 5025 chip 에
  0.5개꼴이라 항목이 통째로 사라진다.
- natural 모드는 chip 자리를 고를 수 없으므로(값이 넘긴 그 자리를 써야 한다)
  `single_csv_specs` 가 공간 패턴 다음 순서로 배치한다 — 뒤로 밀리면 그 chip 을 앞 item 이
  먼저 가져가 fail 이 사라진다(실측: 자연 fail 9개 → 1개).
  ⚠ 이 우선권은 **natural 모드 전체**에 준다(2026-08-19). 종전에는 미분류군에만 줬는데,
  꼬리 겨냥도 같은 성질이라 item 이 늘자 극단 chip 을 뺏겼다. chip 을 뺏기면 그 값은
  **몸통 안으로 당겨지고**(FAILTNO 는 chip 당 하나라 fail 로 못 찍는다), 최극단 몇 점이
  4제곱 지표를 지배하므로 한둘만 잃어도 판정이 뒤집힌다 — 실측으로 `USL_TAIL_L2` 의
  초과첨도가 목표 12.0 대비 **9.96** 으로 주저앉아 미발화했다.
- **L1 은 fail 0** — spec 밖 값이 나오면 안쪽으로 당겨 없앤다. fail 이 없으므로 서버
  평가 범위(`WEB_REPORT_EVAL_FAIL_ONLY=1`)에서 아예 빠진다 = "정상 item 은 발화하지 않는다"가
  구조적으로 보장된다.
- chip 하나는 `FAILTNO` 를 하나만 가지므로, **다른 item 이 이미 fail 로 쓴 chip 에서
  이 item 이 spec 을 벗어나면 그 값은 spec 안으로 당긴다**(실제 테스터도 처음 걸린 test
  하나로 귀속한다).
- 측정값이 없는(공란) chip 은 fail 로 찍지 않는다 — 값으로 설명 못 하는 fail 이 된다.

검증: **spec 안인데 fail = 0건, spec 밖인데 pass = 0건.**

> **예외 — 전 단계가 발화하는 세트**(2026-08-19 신설, `always_fires`). EDGE 상단/하단
> 세트는 세기 축이 "미발화 → 발화" 가 아니라 **"불량 영역이 얼마나 넓은가"**(부채꼴 각도)
> 라서 L1 에도 fail 이 있다. 그렇지 않으면 L1 이 fail 0 → 평가 대상에서 빠져 사다리의 첫
> 칸이 통째로 비어 버린다. 이 세트는 L1 부터 `(FAIL)` 이 붙고 expect 도 전부 `fire` 다.

**item 이름에 `(FAIL)`** — L2 이상(= 값이 죽어서 fail 난 항목)에만 붙는다.
`USL_TAIL_L5(FAIL)` 처럼 이름만으로 정답과 세기를 읽을 수 있다.

### 1-2-1. TNO 도 유형·레벨로 구분한다

`TNO = 유형(SIG_BIN)×1000 + 레벨×100 + 순번` (예: `19301` = USL_TAIL(19) L3 1번).
bin 규칙과 대칭이라 **번호만 보고 불량 유형·세기를 읽을 수 있고, source 를 넘어 전역
유일**하다. 종전에는 source 안 순번(1..N)이라 다른 source 의 다른 item 이 같은 TNO 를
가졌다 — fail 귀속이 `FAILTNO == TNO` 비교이므로 겹치면 fail 이 엉뚱한 item 에 붙는다.

### 1-3. fail 유형별 bin — Map Analysis 색 구분

**bin = 유형(SIG_BIN) × 10 + 레벨**. 예: `192` = USL_TAIL(19) L2, `205` =
BIMODALITY(20) L5. 십의 자리 이상으로 불량 유형이, 일의 자리로 세기가 갈린다 —
Map Analysis 에서 색이 유형별로 뭉치면서도 다양하게 나온다.
L1(fail 0)과 정상군은 일반 fail bin `2`, 관찰군(random)은 `41~49`.

| bin | signature | bin | signature | bin | signature |
|---|---|---|---|---|---|
| 12 | **OUTLIER**(구 SEVERE/WARN 통합) | 21 | TAIL_RISK | 29 | RING_FAIL |
| 15 | LOW_CPK(구 WIDE/SPEC_TIGHT 흡수) | 22 | CONSTANT_VALUE | 30 | **LSL_TAIL**(하한 쪽 꼬리) |
| 16 | MEAN_SHIFT | 23 | EQUIPMENT_SUSPECT | 33 | GROSS_FAIL |
| 17 | BIDIR_TAIL | 24 | CODE_RAIL | 34 | **E1_FAIL**(최외곽 1열) |
| 19 | **USL_TAIL**(상한 쪽 꼬리) | 25 | MISSING_LIMIT | 35 | **SPOT_FAIL**(좌표 몰림) |
| 20 | BIMODALITY(구 SUBPOP_GAP) | 26 | **EDGE_TOP**(상단 edge) | 36 | **EDGE_BOTTOM**(하단 edge) |
| | | 27 | EDGE_FAIL(E1 제외) | 2 / 41~49 | 일반(L1·정상군) / 관찰군 |
| | | 28 | CENTER_FAIL | | |

> **2026-08-19 재배치** — `CLUSTER_FAIL`(30)·`LOW_SAMPLE_UNCERTAIN`(26) 룰이 삭제되면서
> 그 값을 새 세트가 이어받았다(결번 재사용의 유일한 예외 — 삭제와 신설이 같은 커밋이라
> 과거 데이터와 섞일 여지가 없다). 19 는 구 `HEAVY_TAIL` 자리를 `USL_TAIL` 이 잇는다.
> `EDGE_TOP`/`EDGE_BOTTOM` 은 signature 가 아니라 **겨냥 세트 이름**이다 — 겨냥 룰은
> 둘 다 `EDGE_FAIL` 이고 bin 만 갈라 Map 에서 색으로 구분한다.
> ⚠ **상단/하단은 화면 기준이고 좌표 부호와 반대다**(2026-08-19 수정). 웨이퍼 맵은 y축을
> 뒤집어 그리므로(`static/webreport/wafer_charts.js` `autorange:"reversed"`) **YPOS 가
> 작을수록 화면 위**다. 종전에는 `edge_top` 을 +90°(y 큰 쪽)로 잡아 이름과 화면이 정확히
> 반대였다 — 사용자가 "이름이 서로 바뀌었다" 고 지적한 자리다.
> 검산: `EDGE_TOP_L*` 의 YPOS 평균이 5~16(작다), `EDGE_BOTTOM_L*` 이 60~75(크다).

> **결번 11·13·14·32** — 삭제된 룰(WIDE_DISTRIBUTION·SEVERE_OUTLIER·OUTLIER_WARN·
> SPEC_TOO_TIGHT·WAFER_GRADIENT)이 쓰던 값이다. **재사용하지 않는다** — 과거에 생성한
> CSV 의 bin 이 다른 유형을 가리키게 되면 옛 정답표와 대조가 어긋난다.

18(defective)·31(abnormal)은 `bin_taxonomy.yaml` 예약값이라 피했다 — 그 둘을 쓰면
severity_bias 가 얹혀 status 가 달라진다.

목표치는 **생성 시점에 역산**한다(`with_tune`) — 표본 잡음으로 0.179 가 0.182 가 되면
경계 검증이 성립하지 않기 때문. `answer_key.csv` 의 `target` ↔ `actual_metric` 을 비교하면
얼마나 정확히 맞췄는지 보인다(대부분 소수 셋째 자리까지 일치).

### 1-4. 구성 (전체 세트 500개)

| group | 내용 |
|---|---|
| `single` | signature × 5단계 — **한 룰만 겨냥** |
| `combo` | 2~3개 동시발화 조합 × 5단계 |
| `unit_axis` | 같은 시나리오를 UNIT 9종으로 (V/A/Hz/Ohm/Sec/CODE/PCT/PF/미등록) → `item_class` 스코프 |
| `bin_axis` | fail bin 6종(3·4·5·8·**18**·**31**) → `bin_taxonomy` severity_bias |
| `trim_axis` | item 명에 TRIM → `category_major=TRIM` |
| `boundary` | 임계값 바로 앞뒤 6점 스윕 + 상수값 부동소수 함정 4건 |
| `random` | **관찰군 30개**(`--random-items`) — 정답 기대 없음 |
| `unknown` | **미분류군 5개**(`--unknown-items`) — 어떤 룰도 안 걸려야 한다 (CSV 1장 모드) |
| `normal` | 정상 분포 — **발화 0건이어야 한다**(오탐 검사) |
| `mixed` | 무작위 조합(현실형) — 공간·수율 항목은 fail chip 값을 spec 밖으로 |

**관찰군(`random`)** 은 룰 사다리를 쓰지 않는다. **유형을 정해 놓고 만들지도 않는다**
(2026-08-13 재설계) — "wide/bimodal/spiky…" 목록에서 고르면 결국 우리가 아는 유형만 나와,
실데이터처럼 유형 사이 어딘가에 걸친 분포를 못 만들기 때문이다. 대신 **파라미터 공간에서
직접 뽑는다**: 모드 수 1~4(무게·중심·σ 각각 랜덤) · 양자화 25% · spike 30%(비율·거리·부호
랜덤) · rail 15% · 절단 60% · 결측 20%, unit·limit 도 랜덤(CODE/PCT/V/Hz…). fail 배치는
영역 편중을 연속값(share~Beta)으로 섞는다. **아무렇게나 들어온 데이터를 엔진이 어떻게
판정하는지** 보는 용도라, verify 는 이 그룹을 누락·오발화로 세지 않고 **발화 분포만 따로
요약**한다(`expect=observe`). 실제로 만든 파라미터는 `note` 에 남는다(정답이 아니라 관찰 기록).

**미분류군(`unknown`)** 은 2026-08-14 신설이다. 관찰군만으로는 "어떤 룰도 안 걸리는" 항목이
구조적으로 안 나왔다 — fail 을 밀어 만드니 늘 몸통과 끊겨 OUTLIER 조건에 닿았다(v9 실측
관찰군 30개 중 23개). 그래서 fail 을 자연 꼬리(`mode: "natural"`)로만 만들고, 모양은
**중심이 같은 좁은 몸통 + 넓은 소수 성분**으로 잡는다. 이 조합이라야 세 가지가 동시에
성립한다 — 넓은 성분이 limit 을 넘겨 fail 을 만들고(자연 꼬리), 몸통~limit 구간을 촘촘히
채워 끊김이 없고(OUTLIER 회피), 전체 σ 가 작아 cpk 가 1.33 위에 남는다(LOW_CPK 회피).
**단봉 정규분포로는 불가능**하다: cpk ≥ 1.33 이면 limit 이 4σ 밖이라 자연 초과가 5025 chip 에
0.3개꼴이다. 세 조건이 서로 밀고 당기므로 파라미터는 **cpk 목표에서 역산**한다
(`unknown_specs`). bin 은 50번대, TNO 유형은 97. verify 가 `[미분류군]` 절로 성공 수를 센다.

**재현성**: `--seed` 는 **관찰군·미분류군만** 바꾼다(`_stable_seed(name, salt)`). 겨냥 세트는 salt 없이
이름만으로 seed 를 잡아 값이 고정된다 — 룰 회귀를 비교할 때 기준선이 흔들리면 안 되기 때문.
⚠ 다만 한 웨이퍼를 공유하므로 관찰군이 바뀌면 남는 chip 이 달라져 **겨냥 세트의 fail 배치와
그 실측 지표는 일부 달라진다**(측정값 자체는 동일). 같은 seed 로 두 번 돌리면 완전히 같다.

**모든 item 은 fail item 이다** (FAILTNO == 그 item 의 TNO 인 chip 이 1개 이상).
서버 기본값 `WEB_REPORT_EVAL_FAIL_ONLY=1` 에서 전부가 평가 대상이 된다.

### 1-5. 왜 source 가 30개인가 (item 을 나눠 담는 이유)

chip 1행의 **`FAILTNO` 는 하나뿐**이다 → 한 source 안에서 item 들의 fail 행은 서로 겹칠 수
없다. GROSS_FAIL 5단계(수율 2%)는 혼자서 그 source 행의 98% 를 먹는다. 그래서 fail 예산에
맞춰 item 을 source 로 bin-packing 하고, 다음 두 제약을 건다.

- heavy(행의 15% 초과) item 은 source 당 1개, 공간 패턴 item 과 같이 두지 않는다.
- 영역(center/edge/ring/**spot**/edge_top/edge_bottom)별로 쓸 수 있는 행의 절반까지만 배정한다.

> **`spot` 패턴**(2026-08-13, SPOT_FAIL 용)은 `pick_fail_rows` 가 웨이퍼 위 한 점을
> 중심으로 반경 `SPOT_R_T`(0.60→0.06) 안에서만 fail 을 고른다. 중심을 **웨이퍼 중앙에서
> 비켜난 곳(0.55R, x축 위)** 에 두는 것이 의도다 — 중앙에 두면 `CENTER_FAIL` 이 함께 떠
> `SPOT_FAIL` 이 `hidden_by` 로 목록에서 빠지고, 그러면 이 항목이 무엇을 겨냥했는지
> 검증할 수 없다.

> **`edge_top`/`edge_bottom` 패턴**(2026-08-19)은 EDGE 밴드(E1 제외) 중 **상단/하단 반원의
> 부채꼴** 안에서만 fail 을 고른다. `share` 가 곧 반원(180°) 대비 각도 비율이고
> `EDGE_ARC_T`(0.2→1.0 = 36°→180°)가 사다리다. 판정 지표 `edge_fail_share` 는 전 단계
> 1.0 이므로 **L1 부터 `EDGE_FAIL` 이 뜬다** — 이 세트는 발화 경계가 아니라 "불량 area 가
> 중심에서 본 각도로 얼마나 퍼졌나" 를 재는 것이다(사용자가 그 축을 지정했다).
> ⚠ L1(36°)은 fail 24개가 좁은 호에 몰려 `SPOT_FAIL` 이 동반발화한다 — 사실 그대로이고
> primary 는 `SPECIFICITY_ORDER` 상 `EDGE_FAIL` 이다.

이걸 안 하면 앞 item 이 쓴 행 때문에 **뒤 item 의 공간 패턴이 망가진다**(실제로 겪었다:
gradient 목표 0.35 → 실측 0.08, 중앙 fail 0건).

### 1-6. 좌표 규약 (실데이터와 동일)

- **XPOS/YPOS 는 항상 양수**(0-based die 인덱스). 실데이터에 음수 좌표는 없다.
- **PMIC 은 YPOS ≤ 200** — `--radius` 가 이 상한을 넘기면 생성기가 거부한다(`_check_coord_limits`).
- 생성기는 내부적으로만 중심 정렬 좌표를 쓰고, 파일에는 좌하단을 (0,0)으로 옮겨 적는다.
- 엔진은 좌표 범위의 중앙을 웨이퍼 중심으로 잡으므로(2026-08-12 수정) 평행이동에 결과가
  변하지 않는다. 그 전에는 원점 기준이라 0-based 좌표에서 공간 룰이 통째로 어긋났다.

---

## 2. 어떻게 쓰나

1. **업로드** — `--upload http://<서버>:8080` (또는 `out/` 를 Honey 로 올린다).
   manifest 에 `ai_comment` + `ai_comment_optin` 이 들어 있어 세션 상세 Issue Table 에
   **Signature / AI Comment 컬럼**이 생긴다.
2. **세션 상세** — Issue Table 의 Signature 컬럼이 엔진 제안값이다. item 이름이 곧 정답
   (`HEAVY_TAIL_L3(FAIL)` = HEAVY_TAIL 3단계, 값이 죽어 fail 난 항목).
3. **L0~L6 트레이스** — `/pe/eval` → 트레이스 탭에서 이 세션을 지정. item 별로
   L1 통계 / L2 feature / L3 조건별 판정(applies·fired) / L4 status 가 그대로 보인다.
   `answer_key.csv` 의 `actual_metric` 과 트레이스 값이 일치해야 한다.
4. **임계값 튜닝** — `/pe/eval` 에서 임계값을 바꾸면 경계 스윕 항목(`EDGEC_*`)의 발화가
   어디서 갈리는지 바로 보인다. 저장 시 rev 가 올라 재평가된다.

세션을 만들지 않고 **로컬에서 정답 대조만** 하려면 그냥 스크립트를 실행하면 된다
(업로드 없이 `verify.csv` 가 나온다).

---

## 3. 자체 검증 (`verify.csv`)

생성 직후 **실제 운영 경로**(`web_report.ai_comment` → `eval_engine.evaluate`)로 두 번 평가한다.

- **① 현재 룰** — 지금 서버가 쓰는 signatures.yaml 그대로 (`fired_live`).
- **② 전 룰 enabled** — rules 폴더를 임시로 복사해 모든 signature 를 켠 사본
  (`fired_all_rules`). **운영 rules 파일은 건드리지 않는다**(복사본에만 쓴다).
  비활성 룰까지 "데이터가 조건을 맞췄는지" 확인하기 위한 경로다.

판정 4종 — 마지막 실행 결과(**v13**, 단일 CSV)는 **120/120**(겨냥 85 전부 의도대로 +
관찰군 30 + 미분류 5, 누락·오발화·정상군 오탐 모두 0):

| 지표 | 의미 | 기대 |
|---|---|---|
| `missing` | L2~L5 인데 겨냥한 룰이 안 뜸 | 0 |
| `false_fire` | L1 인데 겨냥한 룰이 뜸 | 0 |
| 정상군 오탐 | `NORMAL_*` 에서 룰 발화 | 0 |
| `suppressed` | 조건은 맞았지만 `suppressed_by` 로 **primary 를 양보** | 0 이어도 정상(목록에서 사라지지 않는다 — 2026-08-13 의미 변경) |
| `co_fired` | 겨냥 밖 동반발화 | 구조상 발생(§4-1) |

`UNKNOWN` 은 "아무 룰도 안 뜬 케이스" 표식이라 오탐으로 세지 않는다.
관찰군(`random`)은 위 4종 어디에도 안 들어가고 **발화 분포 요약**만 출력된다.

v9 관찰군 30개의 분포(참고값 — seed 를 바꾸면 달라진다): OUTLIER 40% · LOW_CPK 33% ·
MEAN_SHIFT 23% · HEAVY_TAIL 10% · CLUSTER_FAIL 10% · RING_FAIL/CODE_RAIL/BIMODALITY 각 3% ·
**발화 0건 17%**. (v13 실측 35개: OUTLIER 69% · LOW_CPK 34% · BIMODALITY 17% · MEAN_SHIFT 14% ·
RING_FAIL/LSL_TAIL/BIDIR_TAIL 각 3% · **발화 0건 14%**. v12 대비 OUTLIER 가 는 것은 끊김
임계가 0.35 → 0.30 으로 내려간 결과이고, BIMODALITY 가 는 것은 이봉 게이트 완화 결과다.) 한 유형에 쏠리면(예: 종전 관찰군 LOW_CPK 96%) 파라미터 공간이 좁다는
뜻이므로 `_sample_random_plan` 의 모드 중심·σ 범위를 다시 본다.

---

## 4. 데이터를 만들면서 확인된 룰 특성

만들다 보면 "이 값을 겨냥할 수 없다"가 곧 룰의 성질이다. 아래는 **임계값 튜닝 전에 알아야
하는 것들**이다.

### 4-1. 구조적으로 함께 뜨는 조합 (분리 불가)

| 관계 | 이유 |
|---|---|
| 산포 큼 ⟹ LOW_CPK | 대칭 limit 에서 `cpk = 1/(6·spread_norm)` → spread>0.18 이면 cpk<0.93 (구 WIDE_DISTRIBUTION 은 이 관계 때문에 LOW_CPK 로 흡수·삭제됐다) |
| OUTLIER ⟹ USL/LSL_TAIL | 멀리 튄 값 하나가 kurtosis 를 4제곱으로 밀어올린다 (그래서 `suppressed_by` — 목록에는 둘 다 남고 primary 만 OUTLIER). 반대로 **꼬리질량 밴드(1~5%)가 꼬리 룰을 지켜준다** — spike 몇 개는 질량이 1% 에 못 미쳐 조건 자체가 안 선다 |
| **cpk 넉넉 + fail 존재 ⟹ OUTLIER** | 정규 몸통은 최대 pass 거리가 ≈3.85σ 로 고정이라 `gap ≈ 3·cpk·(σ/robustσ) − 3.85` 다. cpk 가 1.8 을 넘으면 fail 이 하나만 있어도 gap 1.5 를 넘는다 — 맞는 판정이다(공정능력이 충분한데 죽었으면 산발 이상) |
| BIDIR_TAIL ⟹ LOW_CPK | 양쪽 margin<1σ 면 σ>폭/2 |
| BIMODALITY ⊻ outlier 계열 | `outlier_ratio ≥ 3%` 면 판정 보류(게이트) |
| BIMODALITY ⊻ 격자(CODE·정수) 단봉 | 계단 간격 데이터는 모드 사이 **빈 레벨 ≥2** 를 요구한다(양자화 오탐 차단) — 계단이 촘촘히 차 있으면 봉우리가 몇 개로 보여도 발화하지 않는다 |
| OUTLIER ⊻ 공간 룰 | OUTLIER 의 spike 는 **위치와 무관하게** spec 밖 fail 이라, 섞이면 영역 점유율 95% 가 깨진다 |
| CENTER_FAIL ⟹ SPOT_FAIL | 중심부에 몰린 fail 은 좌표도 붙어 있다. 그래서 `SPOT_FAIL` 은 `hidden_by: [CENTER_FAIL]` 로 **목록에서 빠진다**(2026-08-19) — 겨냥 세트를 만들 때 두 패턴을 겹치지 말 것 |
| USL_TAIL + LSL_TAIL ⟹ BIDIR_TAIL | 대칭 꼬리(예: 방향 접기 없는 scale mixture)는 양쪽이 함께 떠 `replaces` 로 `BIDIR_TAIL` 하나가 된다. 한쪽 꼬리를 겨냥하려면 `tail_side` 로 3σ 밖을 한쪽으로 접어야 한다 |

### 4-1-2. 낮은 MAD 배수의 OUTLIER 는 정규 몸통으로 못 만든다

> ⚠ 이 절과 아래 `gap` 언급은 **2026-08-13~14 시점의 구 판정축**(`fail_pass_gap_sigma ≥ 1.5`)
> 기준이다. 현재 끊김 조건은 `fail_body_jump_ratio ≥ 0.35`(같은 쪽에서 몸통 3σ~최근접 fail
> 구간의 최대 빈 폭 비율)이다 — 양쪽 꼬리를 섞지 않으므로 아래의 "최대 pass 거리 3.85σ"
> 산술은 더 이상 그대로 성립하지 않는다. 사다리 상수를 다시 잡을 때 재검토할 것.

구 판정은 `fail_mad_min ≥ 4` **AND** `gap ≥ 1.5σ` 인데, 두 축은 정규 몸통에서 **양의 상관**이다 —
최대 pass 거리가 ≈3.85σ 라 gap 1.5 를 만족하려면 fail 이 최소 5.35σ(≈8 MAD) 밖이어야 한다.
그래서 단독 세트 사다리는 8 부터 시작하고, **임계 4 앞뒤 경계는 균등분포 몸통**으로 만든다
(균등분포는 최대 pass 거리가 1.35σ 뿐이라 4 MAD 에서도 gap 이 선다 — 반폭 h 를 손잡이로 두면
`mad_min ≈ 1/h`). 실데이터에서 이 구조로 나온 것이 "평평한 분포 + limit 밖 소수" 항목이었다.

`INCOMPATIBLE` 상수가 이 관계를 코드로 갖고 있다.

### 4-1-1. 동시에 만들 수 없는 조합 (생성기가 막는다)

분포를 spec 안에 가두는(`bounded`) 규칙 때문에 **물리적으로 함께 성립하지 않는 조합**이
있다. `INCOMPATIBLE` 이 이를 코드로 갖고 있고, 고정 조합 목록은 `combo_specs()` 시작에서
검사한다(어기면 그 항목은 영원히 미발화로 남는다).

| 조합 | 왜 불가능한가 |
|---|---|
| 산포 큼 × OUTLIER/HEAVYTAIL | 산포가 넓으면 spike 를 얼마나 멀리 둬도 robust σ 대비 거리가 임계에 못 미친다 |
| 산포 큼 × SUBPOP | 모드를 spec 안에 두면서 골(density_gap)까지 만들 폭이 없다 |
| SUBPOP × MEANSHIFT | 가둔 분포는 재추출이 치우침을 되돌려 center_bias 가 임계에 못 미친다 |
| CONSTANT × 공간 룰 | 전 chip 이 fail 이면 "특정 영역 집중" 이라는 개념이 성립하지 않는다(점유율 항상 1.0) |
| **OUTLIER × 공간 룰(E1/EDGE/CENTER/RING/CLUSTER/SPOT)** | OUTLIER 의 spike 는 spec 밖이라 위치와 무관하게 fail 이 된다 → 영역 점유율 95% 가 깨진다 |

### 4-2. 현 룰셋으로 **발화가 불가능한** 룰

| 룰 | 이유 |
|---|---|
| `EQUIPMENT_SUSPECT` | `raw_df` 경로는 site 를 항상 `None` 으로 채운다(`ingest._ingest_raw_df`) → `site_cpk_delta` 영구 결측. **켜도 절대 안 뜬다.** DUT 를 site 로 넘기면 살아난다 |

해소된 것 2건 — `TAIL_RISK` 는 지표를 모멘트 왜도로 갈아탔고(2026-08-12), `RING_FAIL` 은
판정이 점유율(`ring_fail_share`)로 바뀌어 밀도 배수 상한(1.82 < 임계 2.0) 문제가 사라졌다.
밀도 배수(`*_fail_ratio`)는 여전히 영역마다 상한이 다르다(edge 2.78 / center 11 / ring 1.82
) — 그 값을 임계로 쓰는 룰을 새로 만들 때는 상한을 먼저 확인할 것.

### 4-2-1. fail chip 을 limit 밖에 두면 좁은 분포는 반드시 heavy tail 이 된다

`fail = limit 위반` 규칙(§1-2) 때문에, 산포가 좁은 item 은 fail chip 이 곧 극단값이 되어
kurtosis 가 밀려 올라간다. 종전에는 이것만으로 꼬리 룰(kurtosis>8)이 동반발화했는데,
**꼬리질량 밴드(`tail_mass_3s` 1~5%)를 AND 로 걸면서 대부분 사라졌다**(2026-08-13) — 점 몇
개는 3σ 밖 질량이 1% 에 못 미치기 때문이다. 그래도 fail 이 수십 개인 항목에서는 질량이
밴드에 들어와 함께 뜰 수 있고, 그건 데이터의 결함이 아니라 **사실 그대로**다 —
"대부분 잘 나오는데 몇 %가 멀리 튀었다" 가 정확히 heavy tail 이다.

같은 이유로 **밀어낸 fail 이 OUTLIER 판정선(gap 1.5σ)에 닿는다.** 이쪽은 손잡이가 둘이다:
- `FAIL_MARGIN` — 얼마나 멀리 미느냐. limit 을 겨우 넘는 정도(0.002~0.010)로 둔다.
- 기준 σ — 좁을수록 같은 거리가 더 크게 보인다. 공간 룰 항목은 `SPATIAL_SIGMA` 0.10 이
  접점이다(더 좁으면 OUTLIER, 더 넓으면 fail 이 stdev 를 밀어올려 LOW_CPK).

⚠ **꼬리 룰 겨냥은 `_kurt_plan` 이 연속 꼬리(scale mixture)로 만든다** — 고정 오프셋 spike 는
전부 같은 거리에 뭉쳐 gap 이 1.4σ 까지 올라가 OUTLIER 판정선에 아슬아슬했다. 넓은 성분을
섞고 `bounded` 를 걸지 않으면 꼬리가 몸통에서 limit 까지 이어져 gap≈0 이 보장된다.
**여기에 더해 fail 도 `mode: "natural"` 로 만들어야 한다**(2026-08-14). 연속 꼬리를 만들어
놓고 레벨 사다리(`FAIL_N`)만큼 `_push_out_of_spec` 로 보충하면 중간 꼬리의 chip 을 limit
밖으로 **옮겨서** 그 자리에 구멍이 나고, 그 구멍이 곧 `fail_body_jump_ratio` 다
(실측: 보충 있을 때 L4 0.487 → OUTLIER 로 넘어감 / natural 로 바꾼 뒤 0.087~0.228).

⚠ **`build_source_df` 의 stuck 되돌리기는 몸통 안으로** 당긴다. limit 바로 안쪽으로 당기던
종전 방식은 σ=0.05 항목에서 중심 9.8σ 짜리 인공 pass 를 만들어 gap 을 통째로 메웠다
(OUTLIER 사다리 L2·L3 가 죽던 원인).

### 4-3. `CONSTANT_VALUE` 의 부동소수 함정

`stdev <= 0` 조건이라 **상수값이라도 2진수로 정확히 표현되지 않으면 안 뜬다**.
값 1.4 를 2,453개 넣으면 표본표준편차가 `2.2e-16` 으로 떠서 미발화, 1.25/1.5 는 정확히 0 →
발화. 경계 항목 `EDGEC_constant_*` 4건이 이 차이를 보여준다.
실데이터의 clamp 값이 1.4·1.8 류면 이 룰은 조용히 놓친다 — 상대 산포 기준(`stdev/폭 < 1e-9`)이 안전하다.

### 4-3-1. 양자화 값은 히스토그램이 가짜 봉우리를 만든다

계단형(CODE·PCT) 값에서 bin 폭이 계단 간격보다 좁으면 **빈 칸이 사이사이 끼어** 봉우리가
여러 개로 잡힌다 — 실측에서 step 0.125 인 **단봉** 데이터의 히스토그램이
`[28,0,23,0,87,0,573,0,1393,1797,0,…]` 이 되어 BIMODALITY 가 오발화했다(계단별 도수는
완벽한 단봉인데도). 엔진 `features._grid_step` 이 격자를 검출해 bin 경계를 계단에 맞추고,
생성기 `_grid_step_gen` 이 **같은 규칙을 복제**한다(갈라지면 정답표가 실제 판정과 어긋난다).

### 4-4. MAD=0 이면 outlier 거리에 상한이 생긴다

pass 값이 **전부 같은 값**이면 MAD=0 이라 modified z 가 meanAD 폴백을 탄다. 그 경우
z 의 상한이 `n/(1.2533 × fail수)` 로 눌린다 — 60 chip 중 8개가 fail 이면 아무리 멀리
떨어뜨려도 z 는 6.0(= MAD 배수 8.9)을 못 넘는다. 테스트 픽스처에서 정상 몸통에 미세한
잡음을 주는 이유다(실데이터는 측정 잡음이 있어 이 상황이 드물다).

### 4-4. UNIT 표기 → `value_type` (무판정 경로)

엔진 `UNIT_TO_VALUE_TYPE` 은 **정확일치 표**라 모르는 표기는 조용히 `PF` 로 떨어지고,
PF 는 L1/L2 가 측정 통계를 전부 `None` 으로 비운다 = **어떤 값 기반 룰도 발화하지 않는다**.
`UNITPFMISS_*` 항목(UNIT=`DEGC`)이 이 경로다 — 화면에서는 그냥 "미분류"로 보이므로
오탐·누락이 아니라 **분류 실패**라는 걸 트레이스로 확인해야 한다.

### 4-5. `separated` 라벨은 사실상 도달 불가

`modality_v2` 는 `n_modes==2 && BC≥임계` 를 **먼저** 보므로, 값 축이 완전히 갈린 분포도
대개 `bimodal` 로 분류된다. `separated` 로 가려면 BC 가 임계 미달이어야 하는데 그러려면
가중이 한쪽으로 쏠려야 하고, 그러면 소수 무리가 outlier 로 잡혀 게이트에 막힌다.
그래서 이 데이터의 SUBPOP 항목은 `bimodal`/`multimodal` 만 나온다(배지 `[이봉]`/`[다봉]`).

---

## 5. 재현성·주의

- 같은 `--seed` 면 항상 같은 데이터가 나온다(item 마다 고정 seed → 배치 순서 무관).
- 검증은 **별도 프로세스**로 돌린다 — `eval_engine.config` 가 import 시점에 `EVAL_RULES_DIR`
  를 읽어 고정하기 때문(전 룰 enabled 사본을 같은 프로세스에서 쓸 수 없다).
- 이 도구는 엔진을 직접 import 하지 않는다 — `web_report.ai_comment` / `eval_debug` 만
  거친다(eval_engine import 3곳 규약, docs/13 §2).
- 생성 데이터는 **운영 DB 와 무관**하다. 업로드하면 당연히 실제 세션이 생기므로,
  운영 서버에 올릴 거면 lot_id(`--lot`)를 알아볼 수 있게 두고 확인 후 지울 것.

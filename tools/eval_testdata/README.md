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

**① CSV 1장** (`--single-csv`) — item 85개(17 signature × 5단계) × **chip 1,517개**, 약 1MB:

| 파일 | 내용 |
|---|---|
| `<name>_vN.csv` | 7-meta honeyform CSV 1장 |
| `<name>_vN_answer.csv` | 정답표 — item·세기·목표/실측 지표·임계값·bin·fail 수 |
| `<name>_vN_verify.csv` | 실제 엔진 발화 대조 |

> **CSV 1장에 못 담는 signature 5종** — 발화 조건 자체가 "웨이퍼의 상당수가 fail" 이라
> 다른 항목이 쓸 chip 이 남지 않는다(chip 1개는 `FAILTNO` 를 하나만 갖는다):
> `GROSS_FAIL`(수율<50%) · `WAFER_GRADIENT`(fail 률 기울기) · `CONSTANT_VALUE`(spec 밖 상수
> = 전량 fail) · `BIDIR_TAIL`(양쪽 margin<1σ) · `TAIL_RISK`(spec_margin_min<1σ = 꼬리가
> spec 을 넘어야 함). 이 다섯은 전체 세트(`--out`, source 여러 개)에서 확인한다.

**② 전체 세트** (`--out DIR`) — parquet 30 source × 2,453행 + `manifest.json` +
`answer_key.csv` + `verify.csv` + `metrics_detail.json`. item 500개.

`--items` `--radius` `--product-type` `--family` `--lot` `--seed` 로 조절한다.

---

## 1. 데이터 설계

### 1-1. 세기 5단계 (L1~L5)

각 signature 마다 그 룰의 판정 지표를 임계값에서 얼마나 벗어나게 할지로 5단계를 만든다.
**L1·L2 = 미발화(정상, fail 0), L3부터 발화**하고 L5 까지 심해진다.

| 단계 | 의미 | `spread_norm`(warn 0.18) | fail chip |
|---|---|---|---|
| L1 | 정상(가장 여유) | 0.09 | **0** |
| L2 | 정상(임계값 근접) | 0.14 | **0** |
| L3 | **발화 시작** | 0.20 | 6 |
| L4 | 초과 | 0.24 | 15 |
| L5 | 심각 | 0.29 | 30 |

fail chip 수(`FAIL_N`)와 지표가 **함께** 올라간다. 그러려면 분포가 spec 밖으로 새면 안 되므로
값 기반 항목은 전부 **spec 안에 가둔다**(`bounded` — spec 밖으로 나간 값은 spec 안에서 재추출).
가두지 않으면 산포가 큰 항목은 chip 의 수십 %가 spec 밖 = fail 이 되어 웨이퍼 하나를 통째로
먹는다(§1-2 의 불변 법칙 때문).

> L1·L2 는 **"겨냥한 그 룰"이 뜨지 않는다**는 뜻이다. 지표가 상관된 다른 룰
> (예: 산포가 넓으면 cpk 는 반드시 낮다)은 뜰 수 있고, 그건 오탐이 아니다.
> 아무것도 뜨면 안 되는 항목은 `NORMAL_*` (group=normal) 이다.

### 1-2. 불변 법칙 — **spec 밖 = fail**

이 엔진의 목적은 "fail 난 item 이 왜 죽었는지" 설명하는 것이므로, 데이터는 양방향으로
정합해야 한다. **spec 을 벗어난 chip 은 예외 없이 fail 이고, spec 안인 chip 은 fail 이 아니다.**

- fail chip 수는 **분포가 정한다** — 산포가 넓거나 중심이 치우칠수록 자동으로 늘어난다
  (WIDE L3 22개 → L7 1,413개). 고정 개수를 강요하지 않는다.
- 값만으로 fail 이 안 생기는 항목(좁은 분포·공간 룰·수율 룰)만 레벨 사다리(`FAIL_N`)만큼
  chip 을 골라 limit 밖으로 민다(`_push_out_of_spec`).
- **L1·L2 는 fail 0** — spec 밖 값이 나오면 안쪽으로 당겨 없앤다. fail 이 없으므로 서버
  평가 범위(`WEB_REPORT_EVAL_FAIL_ONLY=1`)에서 아예 빠진다 = "정상 item 은 발화하지 않는다"가
  구조적으로 보장된다.
- chip 하나는 `FAILTNO` 를 하나만 가지므로, **다른 item 이 이미 fail 로 쓴 chip 에서
  이 item 이 spec 을 벗어나면 그 값은 spec 안으로 당긴다**(실제 테스터도 처음 걸린 test
  하나로 귀속한다).
- 측정값이 없는(공란) chip 은 fail 로 찍지 않는다 — 값으로 설명 못 하는 fail 이 된다.

검증(마지막 생성물, item 147개 · fail chip 46,235개):
**spec 안인데 fail = 0건, spec 밖인데 pass = 0건.**

**item 이름에 `(FAIL)`** — L3 이상(= 값이 죽어서 fail 난 항목)에만 붙는다.
`WIDE_DISTRIBUTION_L5(FAIL)` 처럼 이름만으로 정답과 세기를 읽을 수 있다.

### 1-2-1. TNO 도 유형·레벨로 구분한다

`TNO = 유형(SIG_BIN)×1000 + 레벨×100 + 순번` (예: `11301` = WIDE_DISTRIBUTION(11) L3 1번).
bin 규칙과 대칭이라 **번호만 보고 불량 유형·세기를 읽을 수 있고, source 를 넘어 전역
유일**하다. 종전에는 source 안 순번(1..N)이라 다른 source 의 다른 item 이 같은 TNO 를
가졌다 — fail 귀속이 `FAILTNO == TNO` 비교이므로 겹치면 fail 이 엉뚱한 item 에 붙는다.

### 1-3. fail 유형별 bin — Map Analysis 색 구분

**bin = 유형(SIG_BIN) × 10 + 레벨**. 예: `113` = WIDE_DISTRIBUTION(11) L3, `207` =
SUBPOP_GAP(20) L7. 십의 자리 이상으로 불량 유형이, 일의 자리로 세기가 갈린다 —
Map Analysis 에서 색이 유형별로 뭉치면서도 다양하게 나온다(실측 105종).
L1·L2(fail 0)와 정상군은 일반 fail bin `2`.

| bin | signature | bin | signature | bin | signature |
|---|---|---|---|---|---|
| 11 | WIDE_DISTRIBUTION | 19 | HEAVY_TAIL | 27 | EDGE_FAIL(E1 제외) |
| 12 | SEVERE_OUTLIER | 20 | SUBPOP_GAP | 28 | CENTER_FAIL |
| 13 | OUTLIER_WARN | 21 | TAIL_RISK | 29 | RING_FAIL |
| 14 | SPEC_TOO_TIGHT | 22 | CONSTANT_VALUE | 30 | CLUSTER_FAIL |
| 15 | LOW_CPK | 23 | EQUIPMENT_SUSPECT | 32 | WAFER_GRADIENT |
| 16 | MEAN_SHIFT | 24 | CODE_RAIL | 33 | GROSS_FAIL |
| 17 | BIDIR_TAIL | 25 | MISSING_LIMIT | 34 | **E1_FAIL**(최외곽 1열) |
|  |  | 26 | LOW_SAMPLE_UNCERTAIN | 2 | 일반(L1·L2·정상군) |

18(defective)·31(abnormal)은 `bin_taxonomy.yaml` 예약값이라 피했다 — 그 둘을 쓰면
severity_bias 가 얹혀 status 가 달라진다.

목표치는 **생성 시점에 역산**한다(`with_tune`) — 표본 잡음으로 0.179 가 0.182 가 되면
경계 검증이 성립하지 않기 때문. `answer_key.csv` 의 `target` ↔ `actual_metric` 을 비교하면
얼마나 정확히 맞췄는지 보인다(대부분 소수 셋째 자리까지 일치).

### 1-4. 구성 (전체 세트 500개)

| group | 개수 | 내용 |
|---|---|---|
| `single` | 147 | signature 21종 × 7단계 — **한 룰만 겨냥** |
| `combo` | 119 | 2~3개 동시발화 조합 17종 × 7단계 |
| `unit_axis` | 72 | 같은 시나리오를 UNIT 9종으로 (V/A/Hz/Ohm/Sec/CODE/PCT/PF/미등록) → `item_class` 스코프 |
| `bin_axis` | 36 | fail bin 6종(3·4·5·8·**18**·**31**) → `bin_taxonomy` severity_bias |
| `trim_axis` | 20 | item 명에 TRIM → `category_major=TRIM` |
| `boundary` | 46 | 임계값 바로 앞뒤 6점 스윕(7지표) + 상수값 부동소수 함정 4건 |
| `normal` | 54 | 정상 분포 — **발화 0건이어야 한다**(오탐 검사) |
| `mixed` | 82 | 무작위 조합(현실형) — 공간·수율 항목은 fail chip 값을 spec 밖으로 |

**모든 item 은 fail item 이다** (FAILTNO == 그 item 의 TNO 인 chip 이 1개 이상).
서버 기본값 `WEB_REPORT_EVAL_FAIL_ONLY=1` 에서 500개 전부가 평가 대상이 된다.

### 1-5. 왜 source 가 30개인가 (item 을 나눠 담는 이유)

chip 1행의 **`FAILTNO` 는 하나뿐**이다 → 한 source 안에서 item 들의 fail 행은 서로 겹칠 수
없다. GROSS_FAIL 5단계(수율 2%)는 혼자서 그 source 행의 98% 를 먹는다. 그래서 fail 예산에
맞춰 item 을 source 로 bin-packing 하고, 다음 두 제약을 건다.

- heavy(행의 15% 초과) item 은 source 당 1개, 공간 패턴 item 과 같이 두지 않는다.
- 영역(center/edge/ring/quadrant)별로 쓸 수 있는 행의 절반까지만 배정한다.

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
   (`WIDE_DISTRIBUTION_L3(FAIL)` = WIDE_DISTRIBUTION 3단계, 값이 죽어 fail 난 항목).
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

판정 4종 — 마지막 실행 결과는 단일 CSV **136/136**, 전체 세트 **500/500**:

| 지표 | 의미 | 기대 |
|---|---|---|
| `missing` | L3~L7 인데 겨냥한 룰이 안 뜸 | 0 |
| `false_fire` | L1·L2 인데 겨냥한 룰이 뜸 | 0 |
| 정상군 오탐 | `NORMAL_*` 에서 룰 발화 | 0 |
| `suppressed` | 조건은 맞았지만 `suppressed_by` 로 눌림 | 정상 동작(현재 1건: SPEC_TOO_TIGHT → LOW_CPK) |
| `co_fired` | 겨냥 밖 동반발화 | 구조상 발생(§4-1) |

`UNKNOWN` 은 "아무 룰도 안 뜬 케이스" 표식이라 오탐으로 세지 않는다.

---

## 4. 데이터를 만들면서 확인된 룰 특성

만들다 보면 "이 값을 겨냥할 수 없다"가 곧 룰의 성질이다. 아래는 **임계값 튜닝 전에 알아야
하는 것들**이다.

### 4-1. 구조적으로 함께 뜨는 조합 (분리 불가)

| 관계 | 이유 |
|---|---|
| WIDE_DISTRIBUTION ⟹ LOW_CPK | 대칭 limit 에서 `cpk = 1/(6·spread_norm)` → spread>0.18 이면 cpk<0.93 |
| SEVERE_OUTLIER ⟹ OUTLIER_WARN | 임계값 관계상 항상 (그래서 `suppressed_by` 로 정리돼 있다) |
| SEVERE/OUTLIER_WARN ⟹ HEAVY_TAIL | outlier 가 곧 kurtosis |
| BIDIR_TAIL ⟹ WIDE + LOW_CPK | 양쪽 margin<1σ 면 σ>폭/2 |
| SPEC_TOO_TIGHT ⊻ WIDE/MEAN_SHIFT/SUBPOP | 조건이 "좁고·중앙·단봉"이라 **동시 발화 불가** |
| SUBPOP_GAP ⊻ outlier 계열 | `outlier_ratio ≥ 3%` 면 판정 보류(게이트) |

`INCOMPATIBLE` 상수가 이 관계를 코드로 갖고 있다.

### 4-1-1. 동시에 만들 수 없는 조합 (생성기가 막는다)

분포를 spec 안에 가두는(`bounded`) 규칙 때문에 **물리적으로 함께 성립하지 않는 조합**이
있다. `INCOMPATIBLE` 이 이를 코드로 갖고 있고, 고정 조합 목록은 `combo_specs()` 시작에서
검사한다(어기면 그 항목은 영원히 미발화로 남는다).

| 조합 | 왜 불가능한가 |
|---|---|
| WIDE × SEVOUT/OUTWARN/HEAVYTAIL | outlier 로 잡히려면 spike 가 robust σ 의 4.5배 밖이어야 하는데 spec 안(≤0.47 폭)을 못 벗어난다 |
| WIDE × SUBPOP | 모드를 spec 안에 두면서 골(density_gap)까지 만들 폭이 없다 |
| WIDE/SUBPOP × MEANSHIFT | 가둔 분포는 재추출이 치우침을 되돌려 center_bias 가 임계에 못 미친다 |
| CONSTANT × 공간 룰 | 전 chip 이 fail 이면 "특정 영역 집중" 이라는 개념이 성립하지 않는다(비율 항상 1.0) |
| SPECTIGHT × WIDE/MEANSHIFT/SUBPOP | 룰 조건이 서로 배타(좁고·중앙·단봉) |

### 4-2. 현 임계값으로 **발화가 불가능한** 룰 3종

| 룰 | 이유 |
|---|---|
| `EQUIPMENT_SUSPECT` | `raw_df` 경로는 site 를 항상 `None` 으로 채운다(`ingest._ingest_raw_df`) → `site_cpk_delta` 영구 결측. **켜도 절대 안 뜬다.** DUT 를 site 로 넘기면 살아난다 |
| `TAIL_RISK` | `skewness = (mean-median)/stdev` (비모수 왜도)는 **수학적 상한이 1.0** → `skew_warn: 1.0` 초과 불가. 임계값을 0.3~0.5 로 낮추거나 모멘트 왜도로 바꿔야 한다 |
| `RING_FAIL` | ring 영역(반경 0.3~0.8)이 die 의 55% → `ring_fail_ratio` 상한 `1/0.55 ≈ 1.82` < 임계 2.0. 임계값을 1.5 근처로 낮춰야 의미가 생긴다 |

같은 방식의 상한이 다른 공간 룰에도 있다: `edge_fail_ratio` 상한 2.78(영역 36%),
`center_fail_ratio` 11(9%), `quadrant_imbalance` 4. 임계값을 상한 위로 두면 그 룰은
영원히 침묵한다.

### 4-2-1. fail chip 을 limit 밖에 두면 좁은 분포는 반드시 heavy tail 이 된다

`fail = limit 위반` 규칙(§1-2) 때문에, 산포가 좁은 item 은 fail chip 이 곧 극단값이 되어
`HEAVY_TAIL`(kurtosis>2)이 함께 뜬다. 데이터의 결함이 아니라 **사실 그대로**다 —
"대부분 잘 나오는데 몇 개만 멀리 튀었다" 가 정확히 heavy tail 이다. 기준 분포가 지나치게
좁아 이 동반발화가 과했던 항목(HEAVY_TAIL 자신·MEAN_SHIFT·공간 룰)은 σ 를 키워 완화했다.

### 4-3. `CONSTANT_VALUE` 의 부동소수 함정

`stdev <= 0` 조건이라 **상수값이라도 2진수로 정확히 표현되지 않으면 안 뜬다**.
값 1.4 를 2,453개 넣으면 표본표준편차가 `2.2e-16` 으로 떠서 미발화, 1.25/1.5 는 정확히 0 →
발화. 경계 항목 `EDGEC_constant_*` 4건이 이 차이를 보여준다.
실데이터의 clamp 값이 1.4·1.8 류면 이 룰은 조용히 놓친다 — 상대 산포 기준(`stdev/폭 < 1e-9`)이 안전하다.

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

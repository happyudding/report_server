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

**① CSV 1장** (`--single-csv`) — item 110개(겨냥 80 = 16 signature × 5단계 + **관찰용 무작위
30**) × **chip 5,025개**(반경 40), 약 4MB:

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

| 단계 | 의미 | `spread_norm`(warn 0.18) | fail chip |
|---|---|---|---|
| L1 | 정상(fail 0) | 0.10 | **0** |
| L2 | **발화 시작** | 0.20 | 6 |
| L3 | 초과 | 0.26 | 15 |
| L4 | 크게 초과 | 0.33 | 30 |
| L5 | 심각 | 0.40 | 60 |

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
  ⚠ 미는 거리(`FAIL_MARGIN`)를 키우면 밀려난 chip 이 **OUTLIER 판정선(중심에서 12 robust σ)**
  에 닿아 겨냥하지 않은 OUTLIER 가 동반발화한다. 산포가 좁은 시나리오일수록 먼저 닿는다.
- **L1 은 fail 0** — spec 밖 값이 나오면 안쪽으로 당겨 없앤다. fail 이 없으므로 서버
  평가 범위(`WEB_REPORT_EVAL_FAIL_ONLY=1`)에서 아예 빠진다 = "정상 item 은 발화하지 않는다"가
  구조적으로 보장된다.
- chip 하나는 `FAILTNO` 를 하나만 가지므로, **다른 item 이 이미 fail 로 쓴 chip 에서
  이 item 이 spec 을 벗어나면 그 값은 spec 안으로 당긴다**(실제 테스터도 처음 걸린 test
  하나로 귀속한다).
- 측정값이 없는(공란) chip 은 fail 로 찍지 않는다 — 값으로 설명 못 하는 fail 이 된다.

검증: **spec 안인데 fail = 0건, spec 밖인데 pass = 0건.**

**item 이름에 `(FAIL)`** — L2 이상(= 값이 죽어서 fail 난 항목)에만 붙는다.
`WIDE_DISTRIBUTION_L5(FAIL)` 처럼 이름만으로 정답과 세기를 읽을 수 있다.

### 1-2-1. TNO 도 유형·레벨로 구분한다

`TNO = 유형(SIG_BIN)×1000 + 레벨×100 + 순번` (예: `11301` = WIDE_DISTRIBUTION(11) L3 1번).
bin 규칙과 대칭이라 **번호만 보고 불량 유형·세기를 읽을 수 있고, source 를 넘어 전역
유일**하다. 종전에는 source 안 순번(1..N)이라 다른 source 의 다른 item 이 같은 TNO 를
가졌다 — fail 귀속이 `FAILTNO == TNO` 비교이므로 겹치면 fail 이 엉뚱한 item 에 붙는다.

### 1-3. fail 유형별 bin — Map Analysis 색 구분

**bin = 유형(SIG_BIN) × 10 + 레벨**. 예: `112` = WIDE_DISTRIBUTION(11) L2, `205` =
BIMODALITY(20) L5. 십의 자리 이상으로 불량 유형이, 일의 자리로 세기가 갈린다 —
Map Analysis 에서 색이 유형별로 뭉치면서도 다양하게 나온다.
L1(fail 0)과 정상군은 일반 fail bin `2`, 관찰군(random)은 `41~49`.

| bin | signature | bin | signature | bin | signature |
|---|---|---|---|---|---|
| 11 | WIDE_DISTRIBUTION(off) | 19 | HEAVY_TAIL | 27 | EDGE_FAIL(E1 제외) |
| 12 | **OUTLIER**(구 SEVERE/WARN 통합) | 20 | BIMODALITY(구 SUBPOP_GAP) | 28 | CENTER_FAIL |
| 13 | *(결번 — 통합)* | 21 | TAIL_RISK | 29 | RING_FAIL |
| 14 | SPEC_TOO_TIGHT(off) | 22 | CONSTANT_VALUE | 30 | CLUSTER_FAIL |
| 15 | LOW_CPK | 23 | EQUIPMENT_SUSPECT | 32 | WAFER_GRADIENT |
| 16 | MEAN_SHIFT | 24 | CODE_RAIL | 33 | GROSS_FAIL |
| 17 | BIDIR_TAIL | 25 | MISSING_LIMIT | 34 | **E1_FAIL**(최외곽 1열) |
|  |  | 26 | LOW_SAMPLE_UNCERTAIN | 2 / 41~49 | 일반(L1·정상군) / 관찰군 |

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

**재현성**: `--seed` 는 **관찰군만** 바꾼다(`_stable_seed(name, salt)`). 겨냥 세트는 salt 없이
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

판정 4종 — 마지막 실행 결과(v8, 단일 CSV)는 **110/110**:

| 지표 | 의미 | 기대 |
|---|---|---|
| `missing` | L2~L5 인데 겨냥한 룰이 안 뜸 | 0 |
| `false_fire` | L1 인데 겨냥한 룰이 뜸 | 0 |
| 정상군 오탐 | `NORMAL_*` 에서 룰 발화 | 0 |
| `suppressed` | 조건은 맞았지만 `suppressed_by` 로 **primary 를 양보** | 0 이어도 정상(목록에서 사라지지 않는다 — 2026-08-13 의미 변경) |
| `co_fired` | 겨냥 밖 동반발화 | 구조상 발생(§4-1) |

`UNKNOWN` 은 "아무 룰도 안 뜬 케이스" 표식이라 오탐으로 세지 않는다.
관찰군(`random`)은 위 4종 어디에도 안 들어가고 **발화 분포 요약**만 출력된다.

---

## 4. 데이터를 만들면서 확인된 룰 특성

만들다 보면 "이 값을 겨냥할 수 없다"가 곧 룰의 성질이다. 아래는 **임계값 튜닝 전에 알아야
하는 것들**이다.

### 4-1. 구조적으로 함께 뜨는 조합 (분리 불가)

| 관계 | 이유 |
|---|---|
| WIDE_DISTRIBUTION ⟹ LOW_CPK | 대칭 limit 에서 `cpk = 1/(6·spread_norm)` → spread>0.18 이면 cpk<0.93 |
| OUTLIER ⟹ HEAVY_TAIL | 멀리 튄 값 하나가 kurtosis 를 4제곱으로 밀어올린다 (그래서 `suppressed_by` — 목록에는 둘 다 남고 primary 만 OUTLIER) |
| **cpk 넉넉 + fail 존재 ⟹ OUTLIER** | 정규 몸통은 최대 pass 거리가 ≈3.85σ 로 고정이라 `gap ≈ 3·cpk·(σ/robustσ) − 3.85` 다. cpk 가 1.8 을 넘으면 fail 이 하나만 있어도 gap 1.5 를 넘는다 — 맞는 판정이다(공정능력이 충분한데 죽었으면 산발 이상) |
| BIDIR_TAIL ⟹ WIDE + LOW_CPK | 양쪽 margin<1σ 면 σ>폭/2 |
| SPEC_TOO_TIGHT ⊻ WIDE/MEAN_SHIFT/SUBPOP | 조건이 "좁고·중앙·단봉"이라 **동시 발화 불가** |
| BIMODALITY ⊻ outlier 계열 | `outlier_ratio ≥ 3%` 면 판정 보류(게이트) |
| OUTLIER ⊻ 공간 룰 | OUTLIER 의 spike 는 **위치와 무관하게** spec 밖 fail 이라, 섞이면 영역 점유율 95% 가 깨진다 |

### 4-1-2. 낮은 MAD 배수의 OUTLIER 는 정규 몸통으로 못 만든다

새 판정은 `fail_mad_min ≥ 4` **AND** `gap ≥ 1.5σ` 인데, 두 축은 정규 몸통에서 **양의 상관**이다 —
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
| WIDE × OUTLIER/HEAVYTAIL | 산포가 넓으면 spike 를 얼마나 멀리 둬도 robust σ 대비 거리가 12 에 못 미친다 |
| WIDE × SUBPOP | 모드를 spec 안에 두면서 골(density_gap)까지 만들 폭이 없다 |
| WIDE/SUBPOP × MEANSHIFT | 가둔 분포는 재추출이 치우침을 되돌려 center_bias 가 임계에 못 미친다 |
| CONSTANT × 공간 룰 | 전 chip 이 fail 이면 "특정 영역 집중" 이라는 개념이 성립하지 않는다(점유율 항상 1.0) |
| SPECTIGHT × WIDE/MEANSHIFT/SUBPOP | 룰 조건이 서로 배타(좁고·중앙·단봉) |
| **OUTLIER × 공간 룰(E1/EDGE/CENTER/RING/CLUSTER)** | OUTLIER 의 spike 는 spec 밖이라 위치와 무관하게 fail 이 된다 → 영역 점유율 95% 가 깨진다 |

### 4-2. 현 룰셋으로 **발화가 불가능한** 룰

| 룰 | 이유 |
|---|---|
| `EQUIPMENT_SUSPECT` | `raw_df` 경로는 site 를 항상 `None` 으로 채운다(`ingest._ingest_raw_df`) → `site_cpk_delta` 영구 결측. **켜도 절대 안 뜬다.** DUT 를 site 로 넘기면 살아난다 |

해소된 것 2건 — `TAIL_RISK` 는 지표를 모멘트 왜도로 갈아탔고(2026-08-12), `RING_FAIL` 은
판정이 점유율(`ring_fail_share`)로 바뀌어 밀도 배수 상한(1.82 < 임계 2.0) 문제가 사라졌다.
밀도 배수(`*_fail_ratio`)는 여전히 영역마다 상한이 다르다(edge 2.78 / center 11 / ring 1.82
/ quadrant 4) — 그 값을 임계로 쓰는 룰을 새로 만들 때는 상한을 먼저 확인할 것.

### 4-2-1. fail chip 을 limit 밖에 두면 좁은 분포는 반드시 heavy tail 이 된다

`fail = limit 위반` 규칙(§1-2) 때문에, 산포가 좁은 item 은 fail chip 이 곧 극단값이 되어
`HEAVY_TAIL`(kurtosis>8)이 함께 뜬다. 데이터의 결함이 아니라 **사실 그대로**다 —
"대부분 잘 나오는데 몇 개만 멀리 튀었다" 가 정확히 heavy tail 이다.

같은 이유로 **밀어낸 fail 이 OUTLIER 판정선(gap 1.5σ)에 닿는다.** 이쪽은 손잡이가 둘이다:
- `FAIL_MARGIN` — 얼마나 멀리 미느냐. limit 을 겨우 넘는 정도(0.002~0.010)로 둔다.
- 기준 σ — 좁을수록 같은 거리가 더 크게 보인다. 공간 룰 항목은 `SPATIAL_SIGMA` 0.10 이
  접점이다(더 좁으면 OUTLIER, 더 넓으면 fail 이 stdev 를 밀어올려 LOW_CPK).

⚠ **HEAVY_TAIL 은 `_kurt_plan` 이 연속 꼬리(scale mixture)로 만든다** — 고정 오프셋 spike 는
전부 같은 거리에 뭉쳐 gap 이 1.4σ 까지 올라가 OUTLIER 판정선에 아슬아슬했다. 넓은 성분을
섞고 `bounded` 를 걸지 않으면 꼬리가 몸통에서 limit 까지 이어져 gap≈0 이 보장된다.

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

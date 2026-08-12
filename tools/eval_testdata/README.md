# eval 디버깅용 합성 테스트 데이터 (L0~L6 트레이스 / signature 검증)

`make_eval_testdata.py` 는 **web_report 세션으로 바로 올릴 수 있는** 합성 데이터 한 벌을
만든다. 목적은 하나 — "이 룰이 이 세기에서 뜨는가"를 **정답을 아는 상태로** 확인하는 것.

```
server\.venv\Scripts\python.exe tools\eval_testdata\make_eval_testdata.py
server\.venv\Scripts\python.exe tools\eval_testdata\make_eval_testdata.py --upload http://127.0.0.1:8080
```

산출물은 `tools/eval_testdata/out/` (git 미추적):

| 파일 | 내용 |
|---|---|
| `source_NN.parquet` (30개) | 7-meta honeyform. 1개 = 1 source = 1 wafer, **각 2,453행** |
| `manifest.json` | 업로드용 manifest (`options.ai_comment`/`ai_comment_optin` = true) |
| `answer_key.csv` | **정답표** — item 마다 겨냥한 signature·세기(1~5)·목표 지표값·실측 지표값·임계값 |
| `verify.csv` | 실제 엔진 발화 결과와 정답표의 대조 (아래 §3) |
| `metrics_detail.json` | item 별 생성 시점 실측 지표 전체 |

기본 규모: **test item 500개 / source 30개 / source 당 2,453행** (총 fail chip 약 3.7만).
`--items` `--radius` `--product-type` `--family` `--lot` `--seed` 로 조절한다.

---

## 1. 데이터 설계

### 1-1. 세기 5단계

각 signature 마다 그 룰의 판정 지표를 임계값에서 얼마나 벗어나게 할지로 5단계를 만든다.

| 단계 | 의미 | 예 (WIDE_DISTRIBUTION, `spread_norm` warn 0.18) |
|---|---|---|
| 1 | 정상범위 — **겨냥한 룰 미발화** | 0.11 |
| 2 | 임계값 소폭 초과 | 0.20 |
| 3 | 초과 | 0.30 |
| 4 | 크게 초과 | 0.50 |
| 5 | 심각 | 0.90 |

> 1단계는 **"겨냥한 그 룰"이 뜨지 않는다**는 뜻이다. 지표가 상관된 다른 룰
> (예: 산포가 넓으면 cpk 는 반드시 낮다)은 뜰 수 있고, 그건 오탐이 아니다.
> 아무것도 뜨면 안 되는 항목은 `NORMAL_*` (group=normal) 54개다.

목표치는 **생성 시점에 역산**한다(`with_tune`) — 표본 잡음으로 0.179 가 0.182 가 되면
경계 검증이 성립하지 않기 때문. `answer_key.csv` 의 `target` ↔ `actual_metric` 을 비교하면
얼마나 정확히 맞췄는지 보인다(대부분 소수 셋째 자리까지 일치).

### 1-2. 구성 (500개)

| group | 개수 | 내용 |
|---|---|---|
| `single` | 105 | signature 21종 × 5단계 — **한 룰만 겨냥** |
| `combo` | 85 | 2~3개 동시발화 조합 17종 × 5단계 |
| `unit_axis` | 72 | 같은 시나리오를 UNIT 9종으로 (V/A/Hz/Ohm/Sec/CODE/PCT/PF/미등록) → `item_class` 스코프 |
| `bin_axis` | 36 | fail bin 6종(3·4·5·8·**18**·**31**) → `bin_taxonomy` severity_bias |
| `trim_axis` | 20 | item 명에 TRIM → `category_major=TRIM` |
| `boundary` | 46 | 임계값 바로 앞뒤 6점 스윕(7지표) + 상수값 부동소수 함정 4건 |
| `normal` | 54 | 정상 분포 — **발화 0건이어야 한다**(오탐 검사) |
| `mixed` | 82 | 무작위 조합(현실형) — 공간·수율 항목은 fail chip 값을 spec 밖으로 |

**모든 item 은 fail item 이다** (FAILTNO == 그 item 의 TNO 인 chip 이 1개 이상).
서버 기본값 `WEB_REPORT_EVAL_FAIL_ONLY=1` 에서 500개 전부가 평가 대상이 된다.

### 1-3. 왜 source 가 30개인가 (item 을 나눠 담는 이유)

chip 1행의 **`FAILTNO` 는 하나뿐**이다 → 한 source 안에서 item 들의 fail 행은 서로 겹칠 수
없다. GROSS_FAIL 5단계(수율 2%)는 혼자서 그 source 행의 98% 를 먹는다. 그래서 fail 예산에
맞춰 item 을 source 로 bin-packing 하고, 다음 두 제약을 건다.

- heavy(행의 15% 초과) item 은 source 당 1개, 공간 패턴 item 과 같이 두지 않는다.
- 영역(center/edge/ring/quadrant)별로 쓸 수 있는 행의 절반까지만 배정한다.

이걸 안 하면 앞 item 이 쓴 행 때문에 **뒤 item 의 공간 패턴이 망가진다**(실제로 겪었다:
gradient 목표 0.35 → 실측 0.08, 중앙 fail 0건).

### 1-4. 좌표계 주의

XPOS/YPOS 를 **웨이퍼 중심이 (0,0)** 이 되도록 만든다(음수 포함). 엔진은 반경을
`sqrt(XPOS²+YPOS²)` 로 원점 기준 계산하므로, 좌표가 0-based 양수면 edge/center 판정이
통째로 어긋난다. ⚠ **실데이터가 0-based 라면 EDGE/CENTER/RING/GRADIENT 룰은 지금도 잘못된
반경을 보고 있다** — 룰을 켜기 전에 확인할 것.

---

## 2. 어떻게 쓰나

1. **업로드** — `--upload http://<서버>:8080` (또는 `out/` 를 Honey 로 올린다).
   manifest 에 `ai_comment` + `ai_comment_optin` 이 들어 있어 세션 상세 Issue Table 에
   **Signature / AI Comment 컬럼**이 생긴다.
2. **세션 상세** — Issue Table 의 Signature 컬럼이 엔진 제안값이다. item 이름이 곧 정답
   (`WIDE_DISTRIBUTION_L3_002` = WIDE_DISTRIBUTION 3단계).
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

판정 4종 — 마지막 실행 결과는 **500/500**:

| 지표 | 의미 | 기대 |
|---|---|---|
| `missing` | 2~5단계인데 겨냥한 룰이 안 뜸 | 0 |
| `false_fire` | 1단계인데 겨냥한 룰이 뜸 | 0 |
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

### 4-2. 현 임계값으로 **발화가 불가능한** 룰 3종

| 룰 | 이유 |
|---|---|
| `EQUIPMENT_SUSPECT` | `raw_df` 경로는 site 를 항상 `None` 으로 채운다(`ingest._ingest_raw_df`) → `site_cpk_delta` 영구 결측. **켜도 절대 안 뜬다.** DUT 를 site 로 넘기면 살아난다 |
| `TAIL_RISK` | `skewness = (mean-median)/stdev` (비모수 왜도)는 **수학적 상한이 1.0** → `skew_warn: 1.0` 초과 불가. 임계값을 0.3~0.5 로 낮추거나 모멘트 왜도로 바꿔야 한다 |
| `RING_FAIL` | ring 영역(반경 0.3~0.8)이 die 의 55% → `ring_fail_ratio` 상한 `1/0.55 ≈ 1.82` < 임계 2.0. 임계값을 1.5 근처로 낮춰야 의미가 생긴다 |

같은 방식의 상한이 다른 공간 룰에도 있다: `edge_fail_ratio` 상한 2.78(영역 36%),
`center_fail_ratio` 11(9%), `quadrant_imbalance` 4. 임계값을 상한 위로 두면 그 룰은
영원히 침묵한다.

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

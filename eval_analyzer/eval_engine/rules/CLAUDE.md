# rules — 선언형 임계값·룰·어휘 (블록 진입점)

엔진의 "판단 기준"을 코드 밖 yaml 로 분리한 곳. **임계값·룰·어휘 하드코딩 금지**(최상위 규칙 5).
모든 파일은 `pipeline/_rules.py` 로더를 통해서만 읽힌다. 경로는 `config.py` 의 `*_FILE`.

## 파일 지도
| 파일 | 역할 | 로더 |
|---|---|---|
| `thresholds.yaml` | 룰 임계값. `default → product_type → item_class` 병합(구체값 우선). `calibration:` 섹션 = 보정 스펙, `item_class:` 는 calibrate 가 재작성(**파일 마지막 섹션 유지**). | `thresholds_for(case_ctx)` |
| `signatures.yaml` | Layer2 진단 signature 선언(feature 조합 → 고장모드)의 **기준값**. 제품군별 차이는 `signatures/<PT>/…` 오버레이 트리. | `signatures_for(case_ctx)` |
| `bin_taxonomy.yaml` | (product_type, bin) → bin_class/severity_bias. `store.init_db()` 가 DB 로 시드. | `bin_taxonomy_for()` / store 시드 |
| `product_taxonomy.yaml` | 허용 product_type ↔ family_product 조합. ingest 가 강제 검증(1:1 드롭다운 전제). | `_validate_product_meta()` |
| `outcome_taxonomy.yaml` | case_outcome 의 action/result 허용 어휘 + ko/group. | `outcome_label()` / `validate_outcome()` |
| `item_alias.yaml` | raw item명 → item_canonical 수동 별칭. | `_alias_map()` |
| `exclusions.yaml` | 평가 제외 목록(전 제품군 공통). `item_contains`(item명 부분일치)·`units`(UNIT 정확일치, 둘 다 대소문자 무시) 매칭 시 L3 발화 전체 차단 + L6 저장 차단(AI Comment 미생성). **⚠ 아래 경고 참조** — 2026-09-02 부터 **기본값은 빈 목록**이다. `/pe/eval` Signatures 탭에서 편집. | `exclusion_reason(case_ctx)` |
| `ai_prompt.yaml` | **AI Comment [제안] 지시문 + 금지 문구**(2026-09-02). `instructions` = LLM 프롬프트 base 지시문 **뒤**에 붙는 문장(엔진이 읽는 유일한 키). `deny_patterns` = **엔진은 안 읽는다** — 서버가 클라 push 를 받을 때 [제안]을 줄 단위로 거르는 정규식(`web_report/service.apply_ai_suggestions`). "사례를 버리는 문장을 쓰지 마라" 류 조건이 계속 늘어나 코드 대신 `/pe/eval` **AI 지시문** 탭에서 관리한다. ⚠ **지시문**을 고치면 프롬프트 sha 가 갈려 저장된 [제안]이 전량 폐기·재대행되고, **금지 문구**만 고치면 sha 불변(다음 push 부터). 상세 ../../../docs/23 | `ai_prompt_instructions()` |
| `sensitivity.yaml` | **민감도 게이지 1~5 단계표**(2026-08-28). signature 그룹 8개 × 키별 `[L1..L5]`. **엔진은 읽지 않는다** — 호출자(report_server)가 카탈로그로 노출하고, 사용자가 고른 단계를 구체값으로 굳혀 `evaluate(thresholds_override=…)` 로 넘긴다. | (서버 `eval_debug.sensitivity_catalog`) |

### ⚠ exclusions.yaml — 제외는 "설명 없음"이 아니라 "설명 안 함"이다

제외에 걸린 item 은 [`signatures.evaluate`](../pipeline/signatures.py) 가 **맨 앞에서**
`signatures: []` 로 early-return 한다 — `UNKNOWN` 명시 발화(같은 함수 끝부분)에도
**도달하지 못한다**. 그래서 서버 화면에서는 fail 이 있어도 Signature 가 `Unknown` 이
아니라 **"미분류"**(빈 목록의 표시 폴백)로 보이고 AI Comment 도 비어 있다.
사용자에게는 "엔진이 판단을 못 했다"로 읽혀, 실제(=일부러 안 봤다)와 정반대다.

- **2026-09-02**: `item_contains: [_CODE_]` 를 **제거**했다(사용자 지시 — "fail 이면
  무조건 발화해야 한다"). 그 전까지 이름에 `_CODE_` 가 든 항목은 fail 이어도 전부
  미분류였다. CODE 항목에는 [`CODE_RAIL`](signatures.yaml) 전용 룰이 이미 있고,
  아무 룰도 안 뜨면 이제 `UNKNOWN` 이 사유(`NO_LIMIT`/`NO_MATCH`…)와 함께 뜬다.
- 회귀 방지: `tests/test_unknown_signature.py` 의
  `test_code_items_are_not_excluded_by_default`. 제외 **메커니즘** 자체를 보는 테스트는
  배포 yaml 이 아니라 주입한 목록으로 검증한다(설정에 흔들리지 않게).
- 제외를 다시 넣을 때는 **"이 item 은 판정 자체가 무의미한가"** 로만 판단한다.
  "룰이 잘 안 맞는다" 는 제외가 아니라 룰·임계값으로 푼다.
- 편집은 `/pe/eval` Signatures 탭 — 저장이 백업 + `.rules_rev` +1(캐시 무효화)까지
  해 준다. 파일을 손으로 고쳤으면 `.rules_rev` 를 **직접 올려야** 기존 세션이 재평가된다.

## thresholds 스코프 우선순위
```
default (cold-start 표준 robust 시드)
  └─ product_type[<PT>]  override                      (thresholds.yaml 안의 레거시 섹션)
        └─ thresholds/<PT>/_default.yaml               (제품군 공통 오버레이 파일)
              └─ thresholds/<PT>/<FAMILY>.yaml         (family_product 오버레이 파일)
                    └─ item_class["<category>|<value_type>"]
                          └─ **세션 오버라이드**(evaluate thresholds_override) ← 최우선
```
- 마지막 단계는 **파일이 아니라 호출 인자**다(2026-08-28) — 세션 단위 민감도 게이지가
  구체값으로 얹힌다. `api.evaluate` 가 case 마다 `_th_override`/`_th_override_digest` 를
  스탬프하고 `thresholds_for` 가 캐시 키에 digest 를 넣는다. ⚠ 스코프 전역(`_scope`)에
  넣으면 서로 다른 민감도의 동시 evaluate 가 서로를 덮어쓴다 — 반드시 case 탑재.
  단계표는 `sensitivity.yaml`, 규약 전문은 ../../../docs/13 §17.
- ⚠ `item_class` 는 **2단**이다(2026-08-19 — 종전 3단의 마지막 조각 `bin` 이 빠졌다).
  case 가 item 당 1개가 되면서 식별 축에서 bin 을 뺀 결정과 같은 이유이고, 부수 효과로
  보정 모집단이 합쳐진다(실측: 버킷 34개 대부분 n<10 → 6개 전부 n≥10 — `min_n` 을 넘겨
  `calibrate` 가 실제로 동작할 수 있게 됐다). 구 3단 키가 남아 있으면 매칭되지 않고
  default 로 폴백한다(에러 아님).
- 오버레이 트리는 **파일이 없으면 통째로 skip** 이라, 트리를 안 만들면 종전과 100% 동일하다.
- 오버레이 파일은 flat 매핑(`cpk_warn: 1.2`)만 쓴다 — `calibration`/`item_class` 섹션 금지
  (calibrate 는 계속 thresholds.yaml 의 마지막 `item_class:` 섹션만 재작성한다).
- 파일/폴더 이름은 `product_taxonomy.yaml` 의 허용값 그대로. 편집은 관리자 화면
  `/pe/eval` 이 하고(검증·백업·`.rules_rev` 증가 포함), 손으로 고쳐도 된다.
- **캐시**: `load_yaml` 은 (경로, mtime) 키라 파일을 고치면 서버 재시작 없이 다음 호출에서
  반영된다(컴퓨트 워커 프로세스 포함).
- 임계값 키는 signatures.yaml 에서 **이름으로 참조**됨(예: `spread_norm: ">spread_norm_warn"`).
  thresholds 에서 키를 지우면 그 이름을 쓰는 signature 가 KeyError → **키 이름 변경 시 signatures.yaml 동시 수정**.

## signatures.yaml 스키마
```yaml
- id: WIDE_DISTRIBUTION           # status.py SPECIFICITY_ORDER 와 이름 일치해야 primary 정렬됨
  when_metric: { metric: "op" }   # 모든 조건 AND. op: ">key" "<key" "abs>key" ">0.5"(리터럴)
  status_hint: MAJOR              # MONITOR|MINOR|MAJOR|CRITICAL (bin severity_bias 로 변조)
  action_ko: "코멘트 골격 …"       # recommend 템플릿 base
  evidence: ["spread_norm {spread_norm}"]  # {키}=ctx_values(raw_metrics+features) 치환
  scope:                          # (선택) 적용 범위. 키 부재/빈 목록 = 전 제품 공통
    product_type: [PMIC]          # 이 제품군에서만 평가
    family_product: [SOC]         # 이 family 에서만 평가 (product_type 과 AND)
```
- 파생 컨텍스트 `spec_margin_min` / `center_bias` 는 signatures.py 가 계산해 주입(양방향 tail·중심 이탈용).
  조립 로직 정본은 `signatures.build_ctx_values()` — 관리자 트레이스가 같은 함수를 쓴다.
- `enabled: false` 를 넣으면 그 signature 는 평가에서 통째로 빠진다(키 부재 = 활성).
- **when_metric 을 쓰지 않는 특수분기 2개** — 조건을 고쳐도 효력이 없다(패널도 읽기 전용):
  `BIMODALITY`(features.modality_v2 로 판정) · `UNKNOWN`(fail 인데 다른 룰이 하나도 안
  뜨면 발화 — `signatures._evaluate_unknown`, 사유는 evidence `UNKNOWN_<코드>`).
- `scope` 는 하위호환용 필터로 남아 있다(`signatures.scope_matches()`, enabled 다음·SUBPOP
  특수분기보다 먼저). **제품군별 차이는 아래 오버레이 트리로 낸다** — 배포 룰 중 `scope` 를
  쓰는 것은 없고 `/pe/eval` 도 더 이상 편집 UI 를 제공하지 않는다(2026-08-04).

## signature 스코프 우선순위 (오버레이 트리)
```
signatures.yaml                                  ← 기준값(전 제품 공통)
  └─ signatures/<PT>/_default.yaml               (제품군 공통 오버레이)
        └─ signatures/<PT>/<FAMILY>.yaml         (family_product 오버레이)
```
```yaml
# signatures/MDDI/_default.yaml — 선언한 필드만 기준값을 덮는다(필드 단위 교체)
signatures:
  LOW_CPK:
    enabled: true
    status_hint: CRITICAL
```
- 로더는 `signatures_for(case_ctx)`. thresholds 트리와 규약이 같다 — **파일이 없으면 통째로
  skip** 이라 트리를 안 만들면 종전과 100% 동일하다.
- 덮을 수 있는 필드: `enabled` / `when_metric` / `status_hint` / `issue_category` /
  `phenomenon_ko` / `action_ko` / `evidence` (`scope` 는 제외 — 오버레이 자체가 적용 범위다).
- 편집은 `/pe/eval` Signatures 탭(제품군·Family 드롭다운). 상속값과 같은 필드는 파일에 쓰지
  않으므로 기준값을 고치면 따로 지정하지 않은 제품군은 따라간다.
- `signatures_doc()` 은 이제 **기준값 전용**이다 — 평가·코멘트 경로(signatures/recommend/
  present)는 전부 `signatures_for(case_ctx)` 를 쓴다.
- signature 추가 시 체크: (1) status.py `SPECIFICITY_ORDER` 에 id 추가, (2) 필요한 임계값 키를 thresholds 에 추가.
- **현상 5축 체계(2026-08-12)** — 중심 / 산포·여유 / 형태 / 공간 / 데이터품질. 축당 primary
  하나만 두고 같은 현상의 약한 통계는 `suppressed_by` 로 **primary 를 양보**한다
  (`LOW_CPK ← [MEAN_SHIFT, OUTLIER, BIMODALITY]`, `USL_TAIL`/`LSL_TAIL` `← [OUTLIER]`).
  결과 지표(cpk)는 원인 룰이 있으면 primary 가 되지 않는다. 배경은
  [../../../docs/13 §16](../../../docs/13_eval_analyzer_integration.md).
  ⚠ **`suppressed_by` 는 목록에서 지우지 않는다**(2026-08-13 의미 변경). 지우던 시절에는
  "cpk 도 낮고 outlier 도 있다" 가 한 줄로만 보여 나머지를 볼 수 없었다. 지금은
  `signatures._apply_suppression` 이 `demoted_by` 를 달고 `status.decide` 가 primary
  후보에서만 뺀다 — 발화 목록·Signature 컬럼에는 둘 다 남는다.
- **룰 사이의 관계는 4종**이다(2026-08-19 에 둘, 2026-08-20 에 하나 늘었다). 넷 다 yaml
  선언이고 엔진은 `signatures.evaluate` 에서 **단독 → 대체 → 제거 → 양보** 순으로 적용한다
  (순서를 바꾸면 이미 사라진 발화가 남은 발화를 눌러 아무도 primary 가 아닌 상태가 생기고,
  단독을 대체 뒤로 미루면 합성된 발화가 단독을 통과해 버린다):

  | 선언 | 뜻 | 배포 룰 |
  |---|---|---|
  | `exclusive: true` | 이 룰이 뜨면 **다른 발화를 전부 지우고 혼자 남는다**(상대 미지목) | `FUNC_FAIL` |
  | `suppressed_by: [A]` | A 가 함께 뜨면 **primary 만 양보**(목록에는 남는다) | `LOW_CPK` · `USL_TAIL`/`LSL_TAIL` |
  | `hidden_by: [A]` | A 가 함께 뜨면 **목록에서 통째로 제거** | `SPOT_FAIL ← [CENTER_FAIL]` |
  | `replaces: [A, B]` | A·B 가 **모두** 뜨면 그것들을 지우고 이 룰이 대신 발화 | `BIDIR_TAIL ← [USL_TAIL, LSL_TAIL]` |

  `exclusive` 만 상대를 지목하지 않는다 — "이 item 의 값은 측정량이 아니라 판정 코드라
  통계 해석이 통째로 성립하지 않는다" 는 **해석의 선점**이라, 지울 대상이 특정 룰이 아니라
  나머지 전부이기 때문이다. 그래서 상대를 지목하는 세 선언과 **같은 룰에 함께 쓰지 않는다**
  (`validate_all` 이 막는다).
  ⚠ `exclusive`/`hidden_by`/`replaces` 로 사라진 발화는 **화면 어디에도 남지 않는다** — 사유는
  `/pe/eval` 트레이스에만 있다(`eval_debug._signature_matrix` 의 branch_note). 새 선언을
  추가할 때는 "정말로 정보가 0 인가" 를 먼저 보라. 참조 무결성(없는 id·자기참조·상호참조)은
  `rules_io.validate_all` 이 검사한다.
- **`when_metric` 값은 문자열 하나 또는 조건 목록(AND)** 이다(2026-08-13). 같은 지표에
  상·하한을 함께 거는 밴드용 — `tail_mass_3s: [">=heavy_tail_mass_min", "<=heavy_tail_mass_max"]`.
  엔진 `_eval_condition` / 패널 `rules_io._validate_condition` / 트레이스
  `eval_debug._cond_rows` 세 곳이 같은 규약을 안다(하나만 고치면 화면이 갈린다).
- **삭제된 룰 5종(2026-08-13)** — `SPEC_TOO_TIGHT`·`SEVERE_OUTLIER`·`WIDE_DISTRIBUTION`·
  `OUTLIER_WARN`·`WAFER_GRADIENT`. `enabled:false` 보존을 그만두고 선언 자체를 지웠다
  (운영 DB 에 이 이름의 사람 라벨이 0건이라 마이그레이션 불필요였다).
  그 룰만 쓰던 임계값(`spread_norm_warn`·`outlier_ratio_*`·`gradient_norm_warn`·
  `*_fail_ratio_warn`·`severe_outlier_count_min`)도 함께 지웠다 —
  ⚠ 임계값을 지울 땐 `calibration.quantiles` 와 `rules_io` 의 KINDS/RELATIONS 도 같이 본다.
- **룰셋 재편(2026-08-12, 사용자 v5 검토 반영)** — 겹치는 이름을 지우고 판정축을 바꿨다:
  - `SEVERE_OUTLIER`+`OUTLIER_WARN` → **`OUTLIER`** 하나.
    판정축은 2026-08-13 에 다시 바뀌어 **거리 AND 끊김** 두 조건이다:
    `fail_mad_min ≥ 4`(중심에 가장 가까운 fail 의 MAD 배수) **AND**
    ~~`fail_pass_gap_sigma ≥ 1.5`~~ → **`fail_body_jump_ratio ≥ 0.35`**(2026-08-14 교체).
    구 지표는 `|z|` 라 **양쪽 꼬리를 한 자에 섞어** 반대쪽에 더 먼 pass 가 하나만 있어도
    음수가 됐다 — 사용자가 outlier 로 지목한 v9 관찰군 8건이 −3.4~+1.5 로 통째 미발화.
    새 지표는 `fail_mad_min` 을 만든 **그 쪽에서만**, 몸통 경계(`outlier_body_z` 3.0)부터
    최근접 fail 까지 구간의 "한 번에 비어 있는 최대 폭 / 구간 폭" 을 잰다.
    v9/v10 겨냥 실측: HEAVY_TAIL 0.09~0.29 / OUTLIER 0.94~1.00 으로 갈린다.
    ⚠ **겨냥 데이터의 fail 을 `_push_out_of_spec` 으로 밀어 만들면 이 지표가 오염된다** —
    중간 꼬리 chip 을 limit 밖으로 *옮기므로* 그 자리에 구멍이 생긴다. 그래서 HEAVY_TAIL
    겨냥은 `fails={"mode":"natural"}` 로 자연 꼬리만 쓴다(tools/eval_testdata).
    ⚠ 거리 하나로는 못 가른다 — 실측에서 z 13.2 가 heavy tail, z 8.5 가 outlier 였다.
    꼬리가 이어져 limit 을 넘었으면 HEAVY_TAIL, 몸통과 끊겼으면 OUTLIER 다.
    정규 몸통은 최대 pass 거리가 ≈3.85σ 로 고정이라 **낮은 MAD 배수(4~6)의 OUTLIER 를
    합성으로 만들 수 없다** — 그 구간은 균등분포 몸통(최대 1.35σ)으로만 재현된다.
  - `SPEC_TOO_TIGHT`·`WIDE_DISTRIBUTION` → `LOW_CPK` 로 통합(둘 다 off).
  - `SUBPOP_GAP` → **`BIMODALITY`** 개명(누적 DB 는 1회 마이그레이션으로 치환 —
    `server/tools/migrate_bimodality_rename.py`).
  - `kurtosis_warn` 2.0 → 8.0 (2.0 은 정상 산포에도 붙었다).
- **공간 존은 E1/EDGE/CENTER/RING + SPOT_FAIL** — `E1_FAIL` 은 최외곽
  1 chip line(각 줄의 양끝 die, `features._e1_mask`)이고 EDGE·RING 은 **E1 을 뺀** 영역이다.
  존 4종의 판정 기준은 **점유율** `*_fail_share ≥ region_fail_share_min`(0.95, 공용) —
  "전체 fail 중 그 영역이 몇 %를 가졌나". `*_fail_ratio`(밀도 배수)는 evidence 참고값이다.
  - `SPOT_FAIL`(2026-08-13 신설, 2026-08-19 개명 — 구 `SPOT_CLUSTER`)
    = `fail_spread_norm ≤ spot_fail_spread_max`(0.25) — fail 무게중심 기준 RMS
    거리/웨이퍼 반경. **위치·모양과 무관**하게 "서로 붙어 있나" 만 본다.
    ⚠ `CENTER_FAIL` 과 함께 뜨면 **목록에서 빠진다**(`hidden_by`) — 중심부 뭉침은 두 룰이
    구조적으로 같이 뜨는데 같은 사실을 두 번 말하는 셈이라 사용자가 CENTER 만 요구했다.
    ⚠ `RING_FAIL` 은 같은 임계로 **반대 부호** 조건(`> 0.25`)을 함께 건다(2026-08-14) —
    ring 밴드가 die 의 절반이라 국부 blob 이 거기 놓이면 점유율 1.0 이 되고, RING 이
    `SPECIFICITY_ORDER` 에서 앞이라 primary 까지 가져갔다(SPOT 겨냥 L2~L5 전부). 두 룰이
    **같은 키를 공유**해야 "둘 다 발화"/"둘 다 미발화" 틈이 안 생긴다.
    실측: RING 겨냥 0.593~0.619 / SPOT 겨냥 0.094~0.211.
  - (`CLUSTER_FAIL`(사분면 불균형)은 **2026-08-19 삭제**. 사분면 격자는 실제 결함 모양과
    무관한 인공 경계라 같은 blob 이 위치만 달라져도 값이 반토막 났고(축 경계 2.20 vs
    한가운데 4.00), 45° 격자 보완을 넣어도 원점 근처 뭉침은 다른 룰 몫이었다.
    임계값 `quadrant_imbalance_warn` 도 함께 지웠다 — 지표 `quadrant_imbalance` 는
    evidence·참고용으로 계속 계산·저장한다.)
- **DUT 축은 공간 축과 별개다** — `DUT_FAIL`(2026-09-01 신설)은 "웨이퍼 어디에서 났나"가
  아니라 **"어느 테스터 채널/소켓에서 났나"** 를 본다. 판정자는 공간 4종과 같은 **점유율**
  `dut_fail_share ≥ dut_fail_share_min`(0.50) **AND** `fail_count > dut_fail_count_min`(10)
  이고, 배수(`dut_fail_ratio` = share × 채널수)는 evidence 참고값이다 — 배수의 상한이
  채널 수라 DUT 8개짜리와 4개짜리가 임계값을 공유할 수 없다(밀도 배수형 공간 지표가
  겪은 것과 같은 함정).
  - 임계가 공간(0.95)보다 훨씬 낮은 이유는 **모집단 수가 다르기 때문**이다: 영역은 4개라
    0.95 가 "죄다 거기"지만, 채널은 보통 4~16개라 균등이어도 0.06~0.25 다.
  - **관계 선언을 두지 않는다**(exclusive/hidden_by/suppressed_by/replaces 전부 없음) —
    공간 룰과 함께 떠도 원인이 다르다(공정 vs 하드웨어). 둘 다 사실이고 둘 다 보여야 한다.
  - ⚠ **채널이 1개면 발화하지 않는다**(feature None). 점유율이 정의상 항상 1.0 이라
    "한 채널에 몰렸다" 가 아무 정보도 아니다.
  - 모집단은 **공간 축(전체 die)** 이다 — `spatial_dut` 를 `spatial_fail_mask` 와 짝으로
    읽는다. 측정값 축으로 재면 값이 빈 die 가 분모에서 빠진다(공간 룰 2026-08-28 과 동일).
  - `action_ko` 에 **`{dut_top}`(몇 번 DUT)** 이 들어간다 — 조치는 채널을 특정해야 시작된다.
    치환은 L3 발화 시점(`signatures._fill_action`)에 한 번만 하고, 하류(L5 코멘트·LLM
    프롬프트·`present` 의 signature 목록·서버 `ai_prompt.py`)는 채워진 문구를 쓴다.
    값이 없으면 `{키}` 를 **그대로 남긴다**(빈칸으로 지우면 누락이 안 보인다).
- **꼬리 판정의 자(尺)는 `tail_extent_high`/`_low` 다**(2026-08-20 — 사용자 지적 "Tail 로
  죽는 건 산포가 쭉 늘어지는 그림이어야 하는데 살짝만 늘어져도 발화된다"). 꼬리 끝(P99.5)이
  **몸통 robust σ 의 몇 배**까지 뻗었나이며(정규 ≈2.58, 임계 `tail_extent_min` 5.0),
  기준은 spec limit 이 아니라 **실측 데이터의 몸통**이다. 꼬리 룰 3종이 이 키를 공유한다.
  - ⚠ **MAD=0(과반 동일값)이면 None** — `_modified_z` 의 meanAD 폴백을 쓰지 않는 유일한
    지표다. 폴백은 자를 "모드에서 벗어난 값의 평균 이탈량" 으로 바꿔, 눈으로는 1자인 산포에서
    3σ 컷이 1 code unit 아래로 내려앉는다. 그러면 모드가 아닌 die 비율(1~3%)이 그대로 질량
    밴드에 들고 kurtosis 는 ≈1/p 로 폭등해 **USL+LSL 동시 발화 → BIDIR_TAIL(MAJOR)** 이
    됐다(v13 오탐의 기전). 몸통 폭이 0 이면 "몸통 대비 몇 배" 라는 질문 자체가 성립하지 않는다.
  - `kurtosis_warn` 은 **단방향 룰(USL/LSL)에만** 남은 보조 조건이다. `BIDIR_TAIL` 에서는
    뺐다 — 대칭 양측 꼬리는 초과첨도가 낮게 나와(실측 6~8 < 10) 게이트를 두면 정작 잡아야
    할 모양이 통째로 UNKNOWN 으로 떨어졌다.
  - extent(늘어진 정도)와 질량 하한(꼬리가 실재하나)은 **반드시 AND** 다. extent 만 보면
    튄 점 하나(OUTLIER 영역)가 통과하고, 질량만 보면 살짝 퍼진 몸통이 통과한다.
- **꼬리는 방향으로 갈린다** — `USL_TAIL`(상한 쪽) / `LSL_TAIL`(하한 쪽), 2026-08-19 에
  구 `HEAVY_TAIL` 을 나눈 것이다. **판정 밴드는 방향별 질량**(`tail_mass_3s_high`/`_low`,
  eval.db **v10** 컬럼)에 건다.
  ⚠ 같은 날 **한 번 뒤집힌 결정**이라 경위를 남긴다. 처음에는 밴드를 `tail_mass_3s`(양쪽
  합)에 걸었는데, 그러면 **양방향으로 꼬리가 두꺼운 항목이 통째로 미발화**한다 — 한쪽 3%
  씩이면 합이 6% 라 상한 5% 를 넘는다. 그 결과 `USL_TAIL`·`LSL_TAIL` 이 둘 다 안 떠
  `BIDIR_TAIL` 의 `replaces` 도 발동하지 못하고, 정작 BIDIR 로 불러야 할 모양이 UNKNOWN
  으로 떨어졌다(실측: 생성기 UNKNOWN 겨냥 5건 전부 — kurtosis 15~16, 방향 균형 0.48/0.52).
  방향별로 걸어도 상한의 취지("이건 꼬리가 아니라 몸통이 갈라진 것")는 그대로 산다.
  전 115 항목 시뮬레이션에서 단방향 겨냥 8건 무변화·오탐 0.
  `tail_side_share_high/low`(그 방향이 가진 꼬리 질량 몫, `build_ctx_values` 가 주입 —
  DB 미저장) ≥ `tail_side_share_min`(0.2)는 **보조 조건으로 남는다** — 반대쪽에 점 한둘이
  섞였을 뿐인 경우를 여전히 한쪽 꼬리로 읽게 해 준다.
- **BIMODALITY 게이트는 자기 컷(`subpop_outlier_sigma` 4.5)으로 잰다**(2026-08-19 분리).
  종전에는 표시용 `outlier_sigma` 를 함께 썼는데, 그 값을 4.5 → 2.5 로 내리자 outlier
  비율이 0.010 → 0.153 으로 뛰어 **이봉 판정이 조용히 죽었다**. 표시 감도("튄 값을 얼마나
  보여줄까")와 판정 게이트("산발이라 이봉으로 못 볼 정도인가")는 목적이 다르다.
  같은 날 판정선도 완화됐다(`subpop_outlier_ratio_max` 0.03→0.15 ·
  `subpop_density_gap_strong` 0.5→**0.20** · `subpop_minor_mass_min` 0.05→0.0003) —
  떨어져 나간 소수 무리를 분리로 보기 위해서다. ⚠ **실질 판별자는 `density_gap ≥ 0.20`
  하나**이고 minor_mass 하한은 사실상 꺼졌다. 되돌릴 때 둘을 함께 볼 것.
  ⚠ `subpop_density_gap_strong`(0.20)이 `subpop_density_gap_warn`(0.3)보다 **작다** —
  이름의 강약이 값의 대소를 함의하지 않는다(전자는 분리, 후자는 이봉/다봉 판정용).
- **이산(CODE) 값의 BIMODALITY 는 빈 계단 ≥2 를 요구한다**(2026-08-13) — 계단으로 그린
  정규분포는 이산이라 울퉁불퉁한 것이지 이봉이 아니다. 진짜로 갈라졌다면 **레벨 자체가
  비어 있는 구간**이 생긴다(`features._grid_empty_levels`).
- ⚠ **임계값을 그 지표의 상한 위로 두면 그 룰은 영원히 침묵한다.** 밀도 배수형 공간 지표의
  상한은 `1/영역면적비`(edge≈2.8, center≈11, ring≈1.8)라 룰마다 임계를 공유할 수
  없었고 ring 은 아예 도달 불가였다 — 공간 4종을 점유율로 갈아탄 이유다. 비모수
  왜도(`skewness`)의 상한 1.0 도 같은 부류(그래서 TAIL_RISK 는 `skewness_moment` 로 갈아탔다).
- ⚠ **점유율 판정은 fail 이 적으면 우연에 흔들린다** — fail 6개면 무작위 배치라도 한 영역에
  다 들어갈 확률이 몇 %나 된다(ring 은 0.55⁶≈3%). `spatial_fail_count_min` 을 10 으로 둔 이유.

## calibrate 와의 관계
`calibrate.recalibrate()`가 누적 features 분위수(`calibration:` 스펙, item_class 별 `min_n` 이상)로
`item_class:` 섹션을 재작성(기존 수동 항목 병합 보존)하고, 새 `engine_version` 을
`engine_version_registry`(파일 ref+sha256)에 등록한다. default 시드값은 건드리지 않는다.

## 관련 문서
- [../../docs/DB_SCHEMA.md](../../docs/DB_SCHEMA.md)(bin_taxonomy/engine_version_registry), [../../docs/5STAGE_COLUMNS.md](../../docs/5STAGE_COLUMNS.md)(feature 의미).

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
| `exclusions.yaml` | 평가 제외 목록(전 제품군 공통). `item_contains`(item명 부분일치)·`units`(UNIT 정확일치, 둘 다 대소문자 무시) 매칭 시 L3 발화 전체 차단 + L6 저장 차단(AI Comment 미생성). `/pe/eval` Signatures 탭에서 편집. | `exclusion_reason(case_ctx)` |

## thresholds 스코프 우선순위
```
default (cold-start 표준 robust 시드)
  └─ product_type[<PT>]  override                      (thresholds.yaml 안의 레거시 섹션)
        └─ thresholds/<PT>/_default.yaml               (제품군 공통 오버레이 파일)
              └─ thresholds/<PT>/<FAMILY>.yaml         (family_product 오버레이 파일)
                    └─ item_class["<category>|<value_type>|<bin>"]  ← 가장 구체, 최우선
```
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
  `SUBPOP_GAP`(features.modality_v2 로 판정) · `UNKNOWN`(fail 인데 다른 룰이 하나도 안
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
  하나만 남기고 같은 현상의 약한 통계는 `suppressed_by` 로 목록에만 둔다
  (`LOW_CPK ← [SPEC_TOO_TIGHT, WIDE_DISTRIBUTION, MEAN_SHIFT]`,
  `HEAVY_TAIL ← [SEVERE_OUTLIER, OUTLIER_WARN]`, `BIDIR_TAIL ← [WIDE_DISTRIBUTION]`).
  결과 지표(cpk)는 원인 룰이 있으면 primary 가 되지 않는다. 배경은
  [../../../docs/13 §16](../../../docs/13_eval_analyzer_integration.md).
- **공간 존은 E1/EDGE/CENTER** — `E1_FAIL` 은 최외곽 1 chip line(4-이웃 중 결손이 있는 die,
  `features._e1_mask`)이고 `edge_fail_ratio`·`ring_fail_ratio` 는 **E1 을 뺀** 영역이다.
- ⚠ **임계값을 그 지표의 상한 위로 두면 그 룰은 영원히 침묵한다.** 비율형 공간 지표의 상한은
  `1/영역면적비`(edge≈2.8, center≈11, ring≈1.8, quadrant≤4)이고, 비모수 왜도(`skewness`)는
  1.0 이다(그래서 TAIL_RISK 는 `skewness_moment` 로 갈아탔다).

## calibrate 와의 관계
`calibrate.recalibrate()`가 누적 features 분위수(`calibration:` 스펙, item_class 별 `min_n` 이상)로
`item_class:` 섹션을 재작성(기존 수동 항목 병합 보존)하고, 새 `engine_version` 을
`engine_version_registry`(파일 ref+sha256)에 등록한다. default 시드값은 건드리지 않는다.

## 관련 문서
- [../../docs/DB_SCHEMA.md](../../docs/DB_SCHEMA.md)(bin_taxonomy/engine_version_registry), [../../docs/5STAGE_COLUMNS.md](../../docs/5STAGE_COLUMNS.md)(feature 의미).

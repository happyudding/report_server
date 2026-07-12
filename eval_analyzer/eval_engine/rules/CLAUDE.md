# rules — 선언형 임계값·룰·어휘 (블록 진입점)

엔진의 "판단 기준"을 코드 밖 yaml 로 분리한 곳. **임계값·룰·어휘 하드코딩 금지**(최상위 규칙 5).
모든 파일은 `pipeline/_rules.py` 로더를 통해서만 읽힌다. 경로는 `config.py` 의 `*_FILE`.

## 파일 지도
| 파일 | 역할 | 로더 |
|---|---|---|
| `thresholds.yaml` | 룰 임계값. `default → product_type → item_class` 병합(구체값 우선). `calibration:` 섹션 = 보정 스펙, `item_class:` 는 calibrate 가 재작성(**파일 마지막 섹션 유지**). | `thresholds_for(case_ctx)` |
| `signatures.yaml` | Layer2 진단 signature 선언(feature 조합 → 고장모드). | `signatures_doc()` |
| `bin_taxonomy.yaml` | (product_type, bin) → bin_class/severity_bias. `store.init_db()` 가 DB 로 시드. | `bin_taxonomy_for()` / store 시드 |
| `product_taxonomy.yaml` | 허용 product_type ↔ family_product 조합. ingest 가 강제 검증(1:1 드롭다운 전제). | `_validate_product_meta()` |
| `outcome_taxonomy.yaml` | case_outcome 의 action/result 허용 어휘 + ko/group. | `outcome_label()` / `validate_outcome()` |
| `item_alias.yaml` | raw item명 → item_canonical 수동 별칭. | `_alias_map()` |

## thresholds 스코프 우선순위
```
default (cold-start 표준 robust 시드)
  └─ product_type[<PT>]  override
        └─ item_class["<category>|<value_type>|<bin>"]  override   ← 가장 구체, 최우선
```
- 임계값 키는 signatures.yaml 에서 **이름으로 참조**됨(예: `spread_norm: ">spread_norm_warn"`).
  thresholds 에서 키를 지우면 그 이름을 쓰는 signature 가 KeyError → **키 이름 변경 시 signatures.yaml 동시 수정**.

## signatures.yaml 스키마
```yaml
- id: WIDE_DISTRIBUTION           # status.py SPECIFICITY_ORDER 와 이름 일치해야 primary 정렬됨
  when_metric: { metric: "op" }   # 모든 조건 AND. op: ">key" "<key" "abs>key" ">0.5"(리터럴)
  status_hint: MAJOR              # MONITOR|MINOR|MAJOR|CRITICAL (bin severity_bias 로 변조)
  action_ko: "코멘트 골격 …"       # recommend 템플릿 base
  evidence: ["spread_norm {spread_norm}"]  # {키}=ctx_values(raw_metrics+features) 치환
```
- 파생 컨텍스트 `spec_margin_min` / `center_bias` 는 signatures.py 가 계산해 주입(양방향 tail·중심 이탈용).
- signature 추가 시 체크: (1) status.py `SPECIFICITY_ORDER` 에 id 추가, (2) 필요한 임계값 키를 thresholds 에 추가.

## calibrate 와의 관계
`calibrate.recalibrate()`가 누적 features 분위수(`calibration:` 스펙, item_class 별 `min_n` 이상)로
`item_class:` 섹션을 재작성(기존 수동 항목 병합 보존)하고, 새 `engine_version` 을
`engine_version_registry`(파일 ref+sha256)에 등록한다. default 시드값은 건드리지 않는다.

## 관련 문서
- [../../docs/DB_SCHEMA.md](../../docs/DB_SCHEMA.md)(bin_taxonomy/engine_version_registry), [../../docs/5STAGE_COLUMNS.md](../../docs/5STAGE_COLUMNS.md)(feature 의미).

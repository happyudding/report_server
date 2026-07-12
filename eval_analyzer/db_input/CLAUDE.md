# db_input — 수동 선례(precedent) 적재기 (블록 진입점)

과거 사례 CSV(엔지니어가 손으로 정리한 라벨/코멘트)를 **선례 DB** 로 적재하는 오프라인 유틸.
운영 파이프라인(`evaluate()`)과 별개지만, item 분류 규칙은 엔진 것을 재사용해 선례 fuzzy 매칭 일관성을 보장한다.

> 상위 규칙 [../CLAUDE.md](../CLAUDE.md). 선례검색 로직 자체는 `eval_engine/store.py:search_precedents`.

## 파일 지도
| 파일 | 역할 |
|---|---|
| `import_csv.py` | CSV → `output/<product_type>_<family_product>.db` upsert. 진입점 `python db_input/import_csv.py <csv>`. |
| `template_example.csv` | 입력 컬럼 예시. |
| `run_import.bat` / `select_csv.ps1` | Windows 더블클릭용 파일 선택 → import 래퍼. |
| `output/` | 생성된 선례 DB 들(제품군별로 파일 분리). |

## 입력 CSV 컬럼
```
product_name, product_type, family_product, lot_id, wafer_number, revision,
item_name, value_type, bin, USL, LSL, average, stdev, human_comment, session_id,
human_status, root_cause_category, outcome_action, outcome_condition, outcome_result
```
필수: `product_name, product_type, family_product, item_name, value_type, bin`.
- `human_status`/`root_cause_category` → label (calibrate 의 룰 검증 소비 대상).
- `outcome_*` → case_outcome (선례 action/result 표시. action/result 는
  rules/outcome_taxonomy.yaml 어휘로 검증 — 미정의 값이면 에러).

## 동작 요점
- **제품군별 파일 분리**: `(product_type, family_product, session_id)` 로 그룹핑 → 그룹마다
  `output/<product_type>_<family_product>.db` 생성/갱신. 한 CSV 에 여러 조합이 섞여도 자동 분리.
- **엔진 규칙 재사용**: `_alias_map` / `_canonicalize` / `_classify_category_major` / `_validate_product_meta`
  를 `eval_engine.pipeline.ingest` 에서 import(import 방향 db_input → eval_engine, 규칙 위반 아님).
- **idempotent**: `make_case_id` 자연키 upsert + 같은 (source_file, session_id) run 재사용 →
  재실행해도 중복 없이 갱신. label/case_outcome 은 case 당 1건만(중복 삽입 방지).
- `average`/`stdev`/USL/LSL 있으면 `_cpk_summary`(CODE_TO_PORT §2)로 cpk 계산해 raw_metrics 저장.

## ⚠ 주의
- `config.DATA_DIR`/`config.DB_PATH` 를 런타임에 `output/` 으로 **덮어써서** 별도 DB 에 쓴다.
  같은 프로세스에서 이후 운영 `eval.db` 접근이 필요하면 config 를 되돌려야 한다(현재는 스크립트 단발 실행 전제).
- `session_id` = report_server `report_session.session_id` 역참조용(선택). `analysis_key`(컨텐츠 해시)와 다름.

## 관련 문서
- 저장 스키마·선례검색 §9 [../docs/DB_SCHEMA.md](../docs/DB_SCHEMA.md).

# db_input — 수동 선례(precedent) 적재기 (블록 진입점)

과거 사례 CSV(엔지니어가 손으로 정리한 라벨/코멘트)를 **선례 DB** 로 적재하는 오프라인 유틸.
운영 파이프라인(`evaluate()`)과 별개지만, item 분류 규칙은 엔진 것을 재사용해 선례 fuzzy 매칭 일관성을 보장한다.

> 상위 규칙 [../CLAUDE.md](../CLAUDE.md). 선례검색 로직 자체는 `eval_engine/store.py:search_precedents`.

## 파일 지도
| 파일 | 역할 |
|---|---|
| `import_csv.py` | CSV → eval.db upsert. 진입점 `python db_input/import_csv.py <csv> [--to-eval-db]`. |
| `template_example.csv` | 단순 포맷 입력 예시 (UTF-8 BOM — Excel 한글). |
| `run_import.bat` / `select_csv.ps1` | Windows 더블클릭용 파일 선택 → import 래퍼. |
| `output/` | 생성된 선례 DB 들(제품군별로 파일 분리 — `--to-eval-db` 없이 실행했을 때). |

## 입력 CSV — 포맷 2종 (헤더로 자동 감지)

### 1) 단순 포맷 (정식)
```
Product type, Family Product, unit, Item, comment      ← 5컬럼 전부 필수
```
헤더는 대소문자·공백·순서에 유연하다(strip+소문자+공백→`_` 정규화 후 비교).
- **unit 은 원문 그대로** 적는다(VOLTS/HERTZ/AMPS/mA…). 엔진 어휘(V/A/Hz/CODE/Ohm/Sec/P_F)로
  매핑해 `item_master.value_type`·`unit` 에 저장한다. 매핑표 = 엔진 `UNIT_TO_VALUE_TYPE`
  (pipeline/ingest.py) + 이 파일의 `EXTRA_UNIT_ALIASES`(엔진에 없는 hertz/ampere/second 등).
  **모르는 단위가 하나라도 있으면 아무것도 적재하지 않고 중단**한다(행번호 + 원문 목록 출력) —
  `EXTRA_UNIT_ALIASES` 를 보강한 뒤 재실행. 빈 unit 은 엔진과 같이 `P_F`(에러 아님).
  값 정규화가 필수인 이유: `search_precedents` 가 `value_type` 을 **등호 하드필터**로 쓴다.
- **case 합성값**: lot/wafer/bin/limit/통계가 없는 요약 선례라 `product_name` =
  `<Product type>_<Family Product>`, `bin=0`, lot/wafer 없음, `revision=0.0` 으로 고정한다.
  ⚠ 따라서 같은 (product_type, family_product, item) 은 **하나의 case 로 접힌다** — CSV 안에
  같은 조합이 여러 번 나오면 뒤 행이 이기고, 재적재하면 코멘트가 갱신된다(관리자 탭 CSV
  왕복이 의도적으로 lossy 한 이유).

### 2) 레거시 포맷 (하위호환, 20컬럼)
```
product_name, product_type, family_product, lot_id, wafer_number, revision,
item_name, value_type, bin, USL, LSL, average, stdev, human_comment, session_id,
human_status, root_cause_category, outcome_action, outcome_condition, outcome_result
```
필수: `product_name, product_type, family_product, item_name, value_type, bin`
(= `REQUIRED_COLUMNS`, `ai_extract.py` 가 이 상수를 재사용).
- `value_type` 은 **이미 엔진 어휘여야 한다**(레거시 경로는 unit 매핑을 하지 않는다).
- `human_status`/`root_cause_category` → label (calibrate 의 룰 검증 소비 대상).
- `outcome_*` → case_outcome (선례 action/result 표시. action/result 는
  rules/outcome_taxonomy.yaml 어휘로 검증 — 미정의 값이면 에러).
- `session_id` = report_server `report_session.session_id` 역참조용(선택).
  `analysis_key`(컨텐츠 해시)와 다름.

## 동작 요점
- **적재 대상 DB**: 기본은 `(product_type, family_product, session_id)` 그룹별
  `output/<product_type>_<family_product>.db`. `--to-eval-db` 를 주면 `config.DB_PATH`
  (`EVAL_DB_PATH` env 존중) 하나로 통합 적재.
  `run_import.bat` 은 **report_server 안에 배치된 사본이면**(`..\..\server\config.py` 존재)
  `EVAL_DB_PATH` 를 서버 소유 `DB\pe\report\eval\eval.db` 로 잡고 자동으로 `--to-eval-db` 를
  붙인다 → 관리자 `/pe/admin-pte/` **Eval DB 탭**에 바로 보인다. 원본 저장소 단독 실행은
  기존 per-family 동작 그대로.
- **엔진 규칙 재사용**: `UNIT_TO_VALUE_TYPE` / `_alias_map` / `_canonicalize` /
  `_classify_category_major` / `_validate_product_meta` 를 `eval_engine.pipeline.ingest` 에서
  import(import 방향 db_input → eval_engine, 규칙 위반 아님).
- **사전 전수검사**: 단순 포맷은 적재 전에 전 행을 검사해(빈 값·미매핑 unit·taxonomy 조합)
  에러를 모아 한 번에 올린다 — **부분 적재 없음**. 레거시 포맷은 기존대로 행 단위 검증.
- **idempotent**: `make_case_id` 자연키 upsert + 같은 (source_file, session_id) run 재사용 →
  재실행해도 중복 없이 갱신. label 은 case 당 1건이고 **값이 들어온 컬럼만 UPDATE**
  (빈 컬럼은 기존 값을 지우지 않는다). case_outcome 도 case 당 1건.
- `average`/`stdev`/USL/LSL 있으면 `_cpk_summary`(CODE_TO_PORT §2)로 cpk 계산해 raw_metrics 저장.

## ⚠ 주의
- `config.DATA_DIR`/`config.DB_PATH` 를 런타임에 대상 DB 로 **덮어써서** 쓴다.
  같은 프로세스에서 이후 운영 `eval.db` 접근이 필요하면 config 를 되돌려야 한다(현재는 스크립트 단발 실행 전제).
- 서버 eval.db 에 적재할 때: 스키마 적용은 `store.init_db()` 가 멱등 처리하고(WAL +
  busy_timeout), 서버가 쓰는 라벨은 `labeler='web_report'` 라 여기서 넣는 `labeler='db_input'`
  라벨과 서로 지우지 않는다(세션 재적재 reconcile 대상 밖).

## 관련 문서
- 저장 스키마·선례검색 §9 [../docs/DB_SCHEMA.md](../docs/DB_SCHEMA.md).

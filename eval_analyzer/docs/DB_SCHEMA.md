# eval_analyzer DB 스키마 (eval.db, SQLite) — 구현 기준

> eval_analyzer 가 **직접 소유·관리**하는 DB. report.db 와 무관. `store.py` 가 아래 DDL 로
> 테이블을 생성(CREATE TABLE IF NOT EXISTS)하고 CRUD 를 제공한다.
> **원칙: raw(per-DUT) 저장 안 함. JSON 컬럼 금지(전부 정규화 child/스칼라). 모든 행은 case_id 축으로 조인.**

## 0. 공통 규약
- SQLite. 타임스탬프는 `INTEGER`(unix epoch sec). bool 은 `INTEGER`(0/1).
- enum 은 `TEXT` + 애플리케이션 검증(별도 CHECK 안 검). vocabulary 는 본 문서 §10.
- 쓰기는 단일 커넥션 컨텍스트매니저(자동 commit), `PRAGMA journal_mode=WAL`, `busy_timeout=5000`.
- case_id 는 자연키 해시(§3). 재업로드 idempotent.
- 스키마 버전은 `PRAGMA user_version`(현재 4). `store.init_db()` 가 버전을 읽어
  `_MIGRATIONS`(from_version → fn) 를 순차 적용 후 `SCHEMA_VERSION` 으로 갱신.
  스키마 변경 시 SCHEMA_VERSION +1 과 마이그레이션 단계 추가가 필수.
- 본 문서의 `-- FK ...` 주석은 **논리적 관계 표기**(무강제) — DDL 에 FOREIGN KEY 제약 없음,
  `PRAGMA foreign_keys` 미사용. 기존 테이블에 제약 추가는 SQLite 특성상 전체 재생성이 필요해
  하지 않는다(신규 eval_precedent 만 FK 선언 — 이 역시 PRAGMA off 라 강제되지 않는 문서용).
- **저장 게이팅(should_store)**: evaluate() 는 후보 case 중 `yield fail(fail_count>0) 또는
  cpk<cpk_warn` 인 case 만 fail_case 이하를 적재한다(pipeline/present.py). 나머지 후보는
  결과에도 DB 에도 남지 않는다 — "모든 candidate 가 축적된다"고 가정하지 말 것.

## 1. grain 요약 (row 1개의 의미)
| 테이블 | row 1개 = |
|---|---|
| product_master | 제품 1개(product_name) |
| item_master | item 정의 1개 |
| item_alias | (원본 이름 → item_id) 매핑 1개 |
| item_spec | (item, product, revision) 의 spec 1건 |
| bin_taxonomy | (product_type, bin) 의 의미 1건 |
| ingest_run | 업로드/실행(클라가 파일 1회 run) 1건 |
| run_case | (run, case) 접점 1건 |
| fail_case | fail 발생 instance 1건 |
| raw_metrics | (case, run) 표준 측정요약 1건 |
| features | (case, run, engine_version) 판단지표 1세트 |
| evaluation | (case, run, engine_version, model_version) 기계 판정 1건 |
| eval_evidence | (eval, signal) 근거 1건 |
| case_signature | (eval, signature) 1건 |
| label | 사람 라벨 이벤트 1건(case당 다중) |
| case_outcome | case 의 실제 조치·결과 1건 |
| eval_precedent | (eval, 참조한 선례 case) 1건 — L5 선례 사용 이력 |
| engine_version_registry | engine_version 1개 |

## 2. 마스터 / 기준정보 DDL
```sql
CREATE TABLE IF NOT EXISTS product_master (
    product_name   TEXT PRIMARY KEY,         -- PARTID 13자리
    product_type   TEXT,                     -- MDDI / PDDI / PMIC / SECURITY / TCON
    family_product TEXT,                     -- product_type 별 허용값(§10, 드롭다운 1:1). cross-product 이력 키
    pkg_type       TEXT,                     -- 패키지 타입
    process        TEXT,                     -- 공정 (BCD1370F ...)
    inch           INTEGER,                  -- 8 / 12
    gross_die      INTEGER,
    fab_line       TEXT,
    tester         TEXT,                     -- 제품 고정 속성
    para           TEXT,                     -- 제품 고정 속성
    updated_at     INTEGER
);

CREATE TABLE IF NOT EXISTS item_master (
    item_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name_raw  TEXT NOT NULL,            -- 원본 이름
    item_canonical TEXT NOT NULL,            -- 정규화(trim/lower/표기차 제거)
    item_base      TEXT,                     -- phase 뗀 본체 (family 키, 예: vref)
    item_phase     TEXT,                     -- init/code/trim/p2 ...
    category_major TEXT,                     -- 'TRIM' | 'NON_TRIM'   ← item_class 구성
    category_mid   TEXT,
    value_type     TEXT,                     -- V|A|Hz|CODE|TCODE|P_F  ← item_class 구성 (= 'unit계열')
    unit           TEXT,
    UNIQUE(item_canonical)
);
CREATE INDEX IF NOT EXISTS idx_item_master_value_type ON item_master(value_type);   -- §9 선례검색
CREATE INDEX IF NOT EXISTS idx_product_master_family ON product_master(family_product);

CREATE TABLE IF NOT EXISTS item_alias (
    raw_name       TEXT PRIMARY KEY,
    item_id        INTEGER NOT NULL          -- FK item_master.item_id
);

CREATE TABLE IF NOT EXISTS item_spec (
    item_id        INTEGER NOT NULL,
    product_name   TEXT NOT NULL,
    revision       REAL NOT NULL,            -- FLOAT (0, 0.1, 1.0, 2.1 ...)
    lsl            REAL,
    usl            REAL,
    updated_at     INTEGER,
    PRIMARY KEY (item_id, product_name, revision)
);

CREATE TABLE IF NOT EXISTS bin_taxonomy (
    product_type   TEXT NOT NULL,
    bin_number     INTEGER NOT NULL,
    bin_class      TEXT,                     -- defective | abnormal | parametric ...
    severity_bias  REAL,                     -- status 보정 가중
    description    TEXT,
    updated_at     INTEGER,
    PRIMARY KEY (product_type, bin_number)
);
```
> bin_taxonomy 의 소스는 `rules/bin_taxonomy.yaml` — `store.init_db()` 가 entries 를
> 이 테이블로 적재(idempotent)하고, 런타임 조회(L3 signatures)는 yaml 을 직접 읽는다
> (preview 모드 DB 미접근 보장). 값 수정은 yaml 에서.

## 3. 코어 — 업로드 / case
```sql
CREATE TABLE IF NOT EXISTS ingest_run (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name   TEXT,                     -- FK product_master
    lot_id         TEXT,
    wafer_number   INTEGER,                  -- WAFER
    source_file    TEXT,
    analysis_key   TEXT,                     -- report.db report_session.analysis_key 역참조(컨텐츠 해시, 있으면)
    session_id     TEXT,                     -- report.db report_session.session_id 역참조(업로드/실행 이벤트 ID, 있으면)
    edm_link       TEXT,                     -- EDM Link
    temperature    INTEGER,                  -- [req0] 세션 입력 (우선 입력만, 분석 미사용)
    corner         TEXT,                     -- [req0] NN / SS / FF ...
    ingested_by    TEXT,
    created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_case (        -- (run, case) 다대다 (재업로드 이력)
    run_id         INTEGER NOT NULL,
    case_id        TEXT NOT NULL,
    seen_at        INTEGER NOT NULL,
    PRIMARY KEY (run_id, case_id)
);

CREATE TABLE IF NOT EXISTS fail_case (
    case_id        TEXT PRIMARY KEY,         -- = sha256(product_name|lot_id|wafer_number|item_id|bin|revision)
    product_name   TEXT NOT NULL,
    lot_id         TEXT,
    wafer_number   INTEGER,
    item_id        INTEGER NOT NULL,         -- FK item_master
    bin            INTEGER,
    revision       REAL,                     -- FLOAT (0, 0.1, 1.0, 2.1 ...)
    item_class     TEXT,                     -- = category_major|value_type|bin  ← ★룰 스코프 키
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER,
    UNIQUE(product_name, lot_id, wafer_number, item_id, bin, revision)
);
CREATE INDEX IF NOT EXISTS idx_fail_case_item_class ON fail_case(item_class);
CREATE INDEX IF NOT EXISTS idx_fail_case_product ON fail_case(product_name);
CREATE INDEX IF NOT EXISTS idx_fail_case_item ON fail_case(item_id);            -- §9 JOIN 키
```
**case_id 생성** (store.py):
```python
import hashlib
key = "|".join(str(x) for x in
      [product_name, lot_id, wafer_number, item_id, bin, revision])
case_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
```

## 4. RAW MEASURE (계산값, raw 자체는 미저장)
```sql
CREATE TABLE IF NOT EXISTS raw_metrics (
    case_id      TEXT NOT NULL,
    run_id       INTEGER NOT NULL,
    cpk REAL, cpl REAL, cpu REAL, cp REAL,
    mean REAL, stdev REAL, min REAL, max REAL,
    yield REAL, fail_count INTEGER, total_count INTEGER,
    bimodality REAL,
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (case_id, run_id)            -- 재업로드 run별 보관
);
```

## 5. JUDGMENT METRIC (features, WIDE)
```sql
CREATE TABLE IF NOT EXISTS features (
    case_id        TEXT NOT NULL,
    run_id         INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    computed_at    INTEGER NOT NULL,
    -- 분포형
    spread_norm REAL, skewness REAL, kurtosis REAL, outlier_ratio REAL,
    modality TEXT, bimodality_score REAL, density_gap REAL, cdf_gap REAL,
    -- spec margin
    spec_margin_low REAL, spec_margin_high REAL, nearest_spec_side TEXT, limit_hit_ratio REAL,
    -- 공간(wafer)
    edge_fail_ratio REAL, center_fail_ratio REAL, radial_gradient REAL,
    quadrant_imbalance REAL, x_gradient REAL, y_gradient REAL, wafer_zone_signature TEXT,
    -- 기타
    n_dut INTEGER, site_cpk_delta REAL, code_edge_hit REAL,
    PRIMARY KEY (case_id, run_id, engine_version)
);
```

## 6. VERDICT (evaluation + 근거/시그니처 child — JSON 금지)
```sql
CREATE TABLE IF NOT EXISTS evaluation (
    eval_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id          TEXT NOT NULL,
    run_id           INTEGER NOT NULL,
    engine_version   TEXT NOT NULL,
    model_version    TEXT NOT NULL DEFAULT '',  -- LLM 모델 (코멘트 생성). ''=LLM 미사용.
                                             -- NULL 금지: NULL 은 UNIQUE 를 우회(중복 insert)
    status           TEXT,                   -- CRITICAL|MAJOR|MINOR|MONITOR|OK
    confidence       REAL,
    data_completeness TEXT,                  -- full|partial|low
    comment          TEXT,                   -- 엔진 생성 코멘트(재생성 가능 캐시)
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER,                -- 재판정(upsert 갱신) 시각. 최초 insert 는 NULL
    UNIQUE(case_id, run_id, engine_version, model_version)
);

-- [req2] evidence_json 대신 정규화 child
CREATE TABLE IF NOT EXISTS eval_evidence (
    eval_id     INTEGER NOT NULL,
    signal_code TEXT NOT NULL,               -- 예 'LOW_CPK','OUTLIER_RATIO'
    value       REAL,
    weight      REAL,
    note        TEXT,
    PRIMARY KEY (eval_id, signal_code)
);

CREATE TABLE IF NOT EXISTS case_signature (
    eval_id     INTEGER NOT NULL,
    signature   TEXT NOT NULL,               -- 예 EQUIPMENT_SUSPECT
    role        TEXT NOT NULL,               -- primary | secondary
    score       REAL,
    PRIMARY KEY (eval_id, signature)
);

CREATE TABLE IF NOT EXISTS eval_precedent (  -- L5 가 참조한 선례 이력(품질 피드백/감사)
    eval_id           INTEGER NOT NULL,      -- FK evaluation
    precedent_case_id TEXT NOT NULL,         -- FK fail_case (참조된 선례 case)
    rank              INTEGER,               -- 관련도 순위(1-based)
    similarity        REAL,                  -- item_canonical 유사도
    PRIMARY KEY (eval_id, precedent_case_id)
);
```

## 7. HUMAN (label + outcome)
```sql
CREATE TABLE IF NOT EXISTS label (
    label_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id               TEXT NOT NULL,
    eval_id               INTEGER,           -- 어떤 판정을 보고 달았나
    human_status          TEXT,
    root_cause_category   TEXT,              -- equipment|process|design|spec|unknown
    root_cause_detail     TEXT,
    engine_comment_accepted INTEGER,         -- 0/1
    comment_modified      INTEGER,           -- 0/1
    human_comment         TEXT,              -- 선례(RAG) 본체
    labeler               TEXT,
    reviewer              TEXT,
    label_quality         TEXT,
    created_at            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_label_case ON label(case_id);

CREATE TABLE IF NOT EXISTS case_outcome (
    outcome_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      TEXT NOT NULL,
    label_id     INTEGER,
    action       TEXT,                       -- retest|condition_change|spec_release|dev_feedback|trim_adjust|scrap|monitor
    condition    TEXT,                       -- 예 'UVLO_TEST_EN=H'
    result       TEXT,                       -- recovered_normal|confirmed_defective|improved|pending
    resolved_by  TEXT,
    resolved_at  INTEGER,
    note         TEXT,
    created_at   INTEGER                     -- 적재 시각(시계열 수집용). v4 이전 행은 NULL
);
CREATE INDEX IF NOT EXISTS idx_outcome_case ON case_outcome(case_id);
```

## 8. 버전 레지스트리 (JSON 금지 → 파일 ref)
```sql
CREATE TABLE IF NOT EXISTS engine_version_registry (
    engine_version  TEXT PRIMARY KEY,
    thresholds_ref  TEXT,                    -- rules/thresholds.yaml 의 경로
    thresholds_hash TEXT,                    -- 그 파일 sha256
    signatures_ref  TEXT,
    signatures_hash TEXT,
    taxonomy_ref    TEXT,
    taxonomy_hash   TEXT,
    created_at      INTEGER NOT NULL
);
```
> 버전 *내용*은 rules/ 의 yaml 파일로 보관(DB 엔 경로+해시만). 같은 engine_version 으로
> 재현 시 해당 yaml 을 읽는다. JSON blob 을 DB 에 넣지 않는다.

## 9. 선례(precedent) 검색 — [req1] (unit + item명 퍼지≥70%)
저장 테이블은 안 만들고(보류: materialize), **검색 함수**로 구현. 키:
- 동일 `value_type`(=unit계열)
- `item_canonical` 알파벳 유사도 ≥ 0.70 (예: `difflib.SequenceMatcher(None,a,b).ratio()` 또는 Levenshtein ratio)
- (보조 필터) `family_product`, `signature`
- **`bin` 은 매칭 조건에서 제외**(더 폭넓게 참고 — 커밋 4166cb1). fail bin 이 달라도 같은 item/unit 이면 선례로 본다.

검색 SQL 골격(파이썬에서 유사도 후처리):
```sql
SELECT fc.case_id, im.item_canonical, fc.bin, im.value_type, fc.product_name,
       pm.family_product, cs.signature, l.label_id,        -- label_id: 대표행 랭킹용
       l.root_cause_category, l.human_comment,
       co.action, co.condition, co.result
FROM fail_case fc
JOIN item_master im ON im.item_id = fc.item_id
JOIN product_master pm ON pm.product_name = fc.product_name
LEFT JOIN evaluation ev ON ev.case_id = fc.case_id
LEFT JOIN case_signature cs ON cs.eval_id = ev.eval_id AND cs.role='primary'
LEFT JOIN label l ON l.case_id = fc.case_id
LEFT JOIN case_outcome co ON co.case_id = fc.case_id
     AND (co.label_id IS NULL OR co.label_id = l.label_id)  -- outcome 은 자기 label 과만 짝
WHERE im.value_type = :value_type
  AND (:family_product IS NULL OR pm.family_product = :family_product);   -- bin 조건 없음
-- → 파이썬 후처리: item_canonical 유사도 ≥0.70 행만 채택,
--   case_id 당 1행(최신 label_id 대표행 — label×outcome 곱 제거),
--   정렬 = (human_comment 있는 선례 우선, 유사도 내림차순). DB 파일 없으면 [].
```
출력 → L5 recommend 에서 "retest→정상 / UVLO_TEST_EN H→정상 / spec release / 모과제 동일유형" 근거로 사용.

## 10. controlled vocabulary
```
status         : CRITICAL | MAJOR | MINOR | MONITOR | OK
                 (OK = 발화 signature 0건 + data_completeness=full — 통계적 정상 확정.
                  signature 0건 + 결측(partial/low)이면 MONITOR)
root_cause     : equipment | process | design | spec | unknown
disposition    : retest | trim | spec_review | monitor | scrap   (label 권장조치)
outcome.action : retest | condition_change | trim_adjust | spec_release | dev_feedback
                 | pa_feedback | false_fail | scrap | monitor | other
outcome.result : recovered_normal | improved | false_fail | confirmed_defective
                 | inconclusive | pending | other
                 (정본 = rules/outcome_taxonomy.yaml — ko/group 포함, insert_case_outcome
                  에서 validate_outcome 검증. 'other' = 이스케이프값(미정의 케이스 수용))
category_major : TRIM | NON_TRIM
value_type     : V | A | Hz | CODE | P_F | Ohm | Sec
corner         : NN | SS | FF | (기타 코너)
data_completeness : full | partial | low
product_type   : MDDI | PDDI | PMIC | SECURITY | TCON
family_product : (product_type 별 1:1 허용 — rules/product_taxonomy.yaml 에서 검증)
                 MDDI: MX|AQUA|CHINA|MDDI_ETC · PMIC: SOC|MEMORY|DISPLAY|IF|PMIC_ETC
                 SECURITY: NFC_ESE|ESE|Contactless|SECU_ETC · PDDI: LCD|PDDI_IT|QDOLED|PDDI_ETC
                 TCON: TV|TCON_IT|TCON_ETC
```

## 11. 보류(미구현 — 필요 시 확장)
- per-source 분해 테이블(raw_metrics_source) : site_cpk_delta drill-down / SOURCE_IMBALANCE 상세
- dist_digest(quantile/histogram) : raw 폐기해도 feature 소급 재계산(현재 forward-only)
- family_metrics(family_corr, phase_recovery_rate) : (product, lot, wafer, item_base) grain
- RAG embedding(comment_embedding) : 텍스트 임베딩 (현재는 §9 구조화 검색)
- CONDITION 측정축 : temperature/corner 는 입력만(req0), 측정조건별 fail 추적은 후속
- feature: clamp_ratio(값쏠림), trim_code_margin(TRIM headroom) + signature gradient(radial/x/y)
  / TRIM_INEFFECTIVE / RETEST_RECOVERY (BIDIR_TAIL·SUBPOP_GAP·CODE_RAIL·HEAVY_TAIL·OUTLIER_WARN 은 구현됨)
- precedent materialize

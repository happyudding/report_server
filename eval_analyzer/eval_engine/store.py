"""eval.db 스키마 + CRUD. eval_analyzer 가 자체 소유하는 SQLite DB.

DDL 은 docs/DB_SCHEMA.md 와 1:1. raw(per-DUT) 저장 안 함. JSON 컬럼 없음(정규화 child).
"""
import difflib
import hashlib
import sqlite3
import time
from contextlib import contextmanager

import yaml

from . import config

SCHEMA_VERSION = 10  # PRAGMA user_version. 스키마 변경 시 +1 하고 _MIGRATIONS 에 단계 추가.

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_master (
    product_name TEXT PRIMARY KEY, product_type TEXT, family_product TEXT, pkg_type TEXT,
    process TEXT, inch INTEGER, gross_die INTEGER, fab_line TEXT, tester TEXT, para TEXT,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS item_master (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT, item_name_raw TEXT NOT NULL,
    item_canonical TEXT NOT NULL, item_base TEXT, item_phase TEXT,
    category_major TEXT, category_mid TEXT, value_type TEXT, unit TEXT,
    UNIQUE(item_canonical)
);
CREATE INDEX IF NOT EXISTS idx_item_master_value_type ON item_master(value_type);
CREATE INDEX IF NOT EXISTS idx_product_master_family ON product_master(family_product);
CREATE TABLE IF NOT EXISTS item_alias (raw_name TEXT PRIMARY KEY, item_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS item_spec (
    item_id INTEGER NOT NULL, product_name TEXT NOT NULL, revision REAL NOT NULL,
    lsl REAL, usl REAL, updated_at INTEGER,
    PRIMARY KEY (item_id, product_name, revision)
);
CREATE TABLE IF NOT EXISTS bin_taxonomy (
    product_type TEXT NOT NULL, bin_number INTEGER NOT NULL, bin_class TEXT,
    severity_bias REAL, description TEXT, updated_at INTEGER,
    PRIMARY KEY (product_type, bin_number)
);
CREATE TABLE IF NOT EXISTS ingest_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, product_name TEXT, lot_id TEXT,
    wafer_number INTEGER, source_file TEXT, analysis_key TEXT, session_id TEXT, edm_link TEXT,
    temperature INTEGER, corner TEXT, ingested_by TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS run_case (
    run_id INTEGER NOT NULL, case_id TEXT NOT NULL, seen_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, case_id)
);
-- test_condition = 측정 조건 축. '' = 일반/미상(기본), 'TEMP' = 온도 평가(Issue Table Temp
-- 시트 유래), 'FF'/'SS'/'FS'/'SF' = corner 예약(현재 채우는 경로 없음).
-- 같은 item 이 조건만 달리해 평가되면 별개 case 여야 한다 — 안 그러면 label 이 서로 덮인다.
CREATE TABLE IF NOT EXISTS fail_case (
    case_id TEXT PRIMARY KEY, product_name TEXT NOT NULL, lot_id TEXT, wafer_number INTEGER,
    item_id INTEGER NOT NULL, bin INTEGER, revision REAL, item_class TEXT,
    test_condition TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL, updated_at INTEGER,
    UNIQUE(product_name, lot_id, wafer_number, item_id, bin, revision, test_condition)
);
CREATE INDEX IF NOT EXISTS idx_fail_case_item_class ON fail_case(item_class);
CREATE INDEX IF NOT EXISTS idx_fail_case_product ON fail_case(product_name);
CREATE INDEX IF NOT EXISTS idx_fail_case_item ON fail_case(item_id);
CREATE TABLE IF NOT EXISTS raw_metrics (
    case_id TEXT NOT NULL, run_id INTEGER NOT NULL,
    cpk REAL, cpl REAL, cpu REAL, cp REAL, mean REAL, stdev REAL, min REAL, max REAL,
    yield REAL, fail_count INTEGER, total_count INTEGER, bimodality REAL,
    created_at INTEGER NOT NULL, PRIMARY KEY (case_id, run_id)
);
CREATE TABLE IF NOT EXISTS features (
    case_id TEXT NOT NULL, run_id INTEGER NOT NULL, engine_version TEXT NOT NULL, computed_at INTEGER NOT NULL,
    spread_norm REAL, skewness REAL, kurtosis REAL, outlier_ratio REAL, modality TEXT,
    bimodality_score REAL, density_gap REAL, cdf_gap REAL,
    spec_margin_low REAL, spec_margin_high REAL, nearest_spec_side TEXT, limit_hit_ratio REAL,
    edge_fail_ratio REAL, center_fail_ratio REAL, radial_gradient REAL, quadrant_imbalance REAL,
    x_gradient REAL, y_gradient REAL, wafer_zone_signature TEXT,
    n_dut INTEGER, site_cpk_delta REAL, code_edge_hit REAL,
    shot_fail_ratio REAL,
    ring_fail_ratio REAL,
    radial_gradient_norm REAL, x_gradient_norm REAL, y_gradient_norm REAL,
    n_modes INTEGER, modality_v2 TEXT,
    -- v9 (2026-08-19, 사용자 승인): 룰 7종의 **판정 기준값**. 종전에는 발화한 case 의
    -- eval_evidence 에 4자리 반올림으로만 남아, 미발화 case 를 포함한 모집단이 없어
    -- 표본함 층화·임계값 what-if 가 구조적으로 불가능했다(docs/17 §4-6).
    fail_mad_min REAL, fail_body_jump_ratio REAL,
    fail_pass_gap_sigma REAL, fail_robust_z_max REAL,
    e1_fail_share REAL, edge_fail_share REAL, center_fail_share REAL, ring_fail_share REAL,
    fail_spread_norm REAL, tail_mass_3s REAL,
    rail_low_ratio REAL, rail_high_ratio REAL,
    value_gap_ratio REAL, value_gap_minor_mass REAL,
    -- v10 (2026-08-19, 사용자 승인): 꼬리 질량의 **방향 분해**. 구 HEAVY_TAIL 을
    -- USL_TAIL/LSL_TAIL 로 가르면서 이 둘이 판정 기준값이 됐다(tail_mass_3s 는 |z| 라
    -- 방향이 없다). 기존 행은 NULL — per-DUT 원본에서만 나오므로 소급 채움 불가.
    tail_mass_3s_high REAL, tail_mass_3s_low REAL,
    PRIMARY KEY (case_id, run_id, engine_version)
);
CREATE TABLE IF NOT EXISTS evaluation (
    eval_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, run_id INTEGER NOT NULL,
    engine_version TEXT NOT NULL, model_version TEXT NOT NULL DEFAULT '',
    status TEXT, confidence REAL,
    data_completeness TEXT, comment TEXT, created_at INTEGER NOT NULL, updated_at INTEGER,
    UNIQUE(case_id, run_id, engine_version, model_version)
);
CREATE TABLE IF NOT EXISTS eval_precedent (  -- L5 가 참조한 선례 이력 (선례 품질 피드백/감사용)
    eval_id INTEGER NOT NULL,
    precedent_case_id TEXT NOT NULL,
    rank INTEGER, similarity REAL,
    PRIMARY KEY (eval_id, precedent_case_id),
    FOREIGN KEY (eval_id) REFERENCES evaluation(eval_id),
    FOREIGN KEY (precedent_case_id) REFERENCES fail_case(case_id)
);
CREATE TABLE IF NOT EXISTS eval_evidence (
    eval_id INTEGER NOT NULL, signal_code TEXT NOT NULL, value REAL, weight REAL, note TEXT,
    PRIMARY KEY (eval_id, signal_code)
);
CREATE TABLE IF NOT EXISTS case_signature (
    eval_id INTEGER NOT NULL, signature TEXT NOT NULL, role TEXT NOT NULL, score REAL,
    PRIMARY KEY (eval_id, signature)
);
-- PK 가 (eval_id, signature) 라 "이 룰이 발화한 case 전부" 조회는 full scan 이 된다.
-- 표본함(/pe/eval 검수 큐)이 룰별로 뽑으므로 signature 선두 인덱스를 둔다.
CREATE INDEX IF NOT EXISTS idx_case_signature_sig ON case_signature(signature, role);
CREATE TABLE IF NOT EXISTS label (
    label_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, eval_id INTEGER,
    human_status TEXT, root_cause_category TEXT, root_cause_detail TEXT,
    engine_comment_accepted INTEGER, comment_modified INTEGER, human_comment TEXT,
    labeler TEXT, reviewer TEXT, label_quality TEXT, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_label_case ON label(case_id);
-- 사람이 지목한 **정답 signature**(label 1건에 여러 개, rank=1 이 1순위).
-- case_signature(엔진이 발화한 것)와 절대 섞지 않는다 — 그쪽은 role='primary' 를 전제로
-- 선례검색·채점·골든셋이 조회하므로, 사람 라벨을 끼워 넣으면 그 3곳이 함께 틀어진다.
-- FK cascade 는 SQLite 기본이 off 라 기대하지 않는다: label 삭제 시 여기도 같이 지운다
-- (delete_label_with_signatures).
CREATE TABLE IF NOT EXISTS label_signature (
    label_id INTEGER NOT NULL, signature TEXT NOT NULL, rank INTEGER,
    PRIMARY KEY (label_id, signature)
);
CREATE INDEX IF NOT EXISTS idx_label_signature_sig ON label_signature(signature);
CREATE TABLE IF NOT EXISTS case_outcome (
    outcome_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, label_id INTEGER,
    action TEXT, condition TEXT, result TEXT, resolved_by TEXT, resolved_at INTEGER, note TEXT,
    created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_outcome_case ON case_outcome(case_id);
CREATE TABLE IF NOT EXISTS engine_version_registry (
    engine_version TEXT PRIMARY KEY, thresholds_ref TEXT, thresholds_hash TEXT,
    signatures_ref TEXT, signatures_hash TEXT, taxonomy_ref TEXT, taxonomy_hash TEXT,
    created_at INTEGER NOT NULL
);
"""


def _now():
    """현재 시각(epoch 초, int). 모든 created_at/updated_at 이 이걸 쓴다."""
    return int(time.time())


def make_case_id(product_name, lot_id, wafer_number, item_id, bin_, revision, condition=""):
    """자연키 sha256 (재업로드 idempotent). docs/DB_SCHEMA §3.

    `condition`(fail_case.test_condition) 은 **빈 값이면 해시 재료에서 아예 빠진다** —
    기존 case_id 를 그대로 두기 위한 하위호환이다. 조건 축이 붙은 case 만 새 해시가 된다.
    """
    parts = (product_name, lot_id, wafer_number, item_id, bin_, revision)
    if condition:
        parts += (condition,)
    key = "|".join(str(x) for x in parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@contextmanager
def get_conn(db_path=None):
    """eval.db 커넥션 컨텍스트 — 정상 종료 시 commit, 예외면 롤백된 채 닫힌다.

    WAL + busy_timeout 5초로 연다. 예외가 나면 commit 을 건너뛰고 close 만 하므로
    한 with 블록이 곧 하나의 트랜잭션 경계다. row_factory=Row 라 컬럼명 접근이 된다.

    `db_path` 를 주면 그 파일을 연다(기본은 `config.DB_PATH`). report_server 가 자기
    소유 DB(REPORT_EVAL_DB_PATH)에 적재할 때 쓰는 경로이며, **`config.DB_PATH` 를
    전역 대입하지 않기 위한** 인자다 — 그 모듈은 장수명 Flask 프로세스에서 공유되므로
    전역을 갈면 같은 프로세스의 다른 호출자까지 오염된다(docs/13 §10 과 같은 이유).
    """
    conn = sqlite3.connect(str(db_path or config.DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_v1_to_v2(conn):
    """evaluation.model_version NULL 허용 → NOT NULL DEFAULT '' (NULL 은 '' 로 정규화).

    SQLite 는 NOT NULL 로 ALTER 불가 → 테이블 재생성 후 복사. 이미 v2 형태면 no-op.
    """
    notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(evaluation)")}
    if notnull.get("model_version"):
        return
    conn.executescript("""
        ALTER TABLE evaluation RENAME TO evaluation_v1;
        CREATE TABLE evaluation (
            eval_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, run_id INTEGER NOT NULL,
            engine_version TEXT NOT NULL, model_version TEXT NOT NULL DEFAULT '',
            status TEXT, confidence REAL,
            data_completeness TEXT, comment TEXT, created_at INTEGER NOT NULL,
            UNIQUE(case_id, run_id, engine_version, model_version)
        );
        INSERT INTO evaluation (eval_id,case_id,run_id,engine_version,model_version,
                                status,confidence,data_completeness,comment,created_at)
            SELECT eval_id,case_id,run_id,engine_version,COALESCE(model_version,''),
                   status,confidence,data_completeness,comment,created_at
            FROM evaluation_v1;
        DROP TABLE evaluation_v1;
    """)


def _migrate_v2_to_v3(conn):
    """ingest_run.session_id 추가 (report_server report_session.session_id 역참조용,
    analysis_key(컨텐츠 해시)와 별개 — 업로드/실행 이벤트마다 새로 생성되는 ID)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ingest_run)")}
    if "session_id" not in cols:
        conn.execute("ALTER TABLE ingest_run ADD COLUMN session_id TEXT")


def _migrate_v3_to_v4(conn):
    """v4: 선례검색 인덱스 3개 + evaluation.updated_at(재판정 시각) +
    case_outcome.created_at(시계열 수집용) + eval_precedent(선례 사용 이력).
    인덱스/테이블은 IF NOT EXISTS 로 SCHEMA 재실행과 겹쳐도 안전, 컬럼은 존재 검사 후 ALTER."""
    if "updated_at" not in {r[1] for r in conn.execute("PRAGMA table_info(evaluation)")}:
        conn.execute("ALTER TABLE evaluation ADD COLUMN updated_at INTEGER")
    if "created_at" not in {r[1] for r in conn.execute("PRAGMA table_info(case_outcome)")}:
        conn.execute("ALTER TABLE case_outcome ADD COLUMN created_at INTEGER")
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_item_master_value_type ON item_master(value_type);
        CREATE INDEX IF NOT EXISTS idx_product_master_family ON product_master(family_product);
        CREATE INDEX IF NOT EXISTS idx_fail_case_item ON fail_case(item_id);
        CREATE TABLE IF NOT EXISTS eval_precedent (
            eval_id INTEGER NOT NULL,
            precedent_case_id TEXT NOT NULL,
            rank INTEGER, similarity REAL,
            PRIMARY KEY (eval_id, precedent_case_id),
            FOREIGN KEY (eval_id) REFERENCES evaluation(eval_id),
            FOREIGN KEY (precedent_case_id) REFERENCES fail_case(case_id)
        );
    """)


def _migrate_v4_to_v5(conn):
    """v5: features 에 REAL 컬럼 5개 추가 — shot_fail_ratio, ring_fail_ratio,
    radial_gradient_norm, x_gradient_norm, y_gradient_norm. 이미 있으면 skip(idempotent)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
    for col in ("shot_fail_ratio", "ring_fail_ratio", "radial_gradient_norm",
                "x_gradient_norm", "y_gradient_norm"):
        if col not in cols:
            conn.execute(f"ALTER TABLE features ADD COLUMN {col} REAL")


def _migrate_v5_to_v6(conn):
    """v6: features 에 n_modes(INTEGER) + modality_v2(TEXT) 추가 — SUBPOP_GAP 판정 근거.
    이미 있으면 skip(idempotent)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
    if "n_modes" not in cols:
        conn.execute("ALTER TABLE features ADD COLUMN n_modes INTEGER")
    if "modality_v2" not in cols:
        conn.execute("ALTER TABLE features ADD COLUMN modality_v2 TEXT")


def _migrate_v6_to_v7(conn):
    """v7: label_signature(사람이 지목한 정답 signature) 테이블 + 인덱스 추가.

    기존 테이블·데이터는 건드리지 않는다 — 새 테이블만 만든다(2026-08-11, 사용자 승인).
    CREATE IF NOT EXISTS 라 SCHEMA 로 이미 만들어진 새 DB 에 다시 돌려도 안전하다.
    """
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS label_signature ("
        " label_id INTEGER NOT NULL, signature TEXT NOT NULL, rank INTEGER,"
        " PRIMARY KEY (label_id, signature));"
        "CREATE INDEX IF NOT EXISTS idx_label_signature_sig ON label_signature(signature);")


def _migrate_v7_to_v8(conn):
    """v8: fail_case.test_condition 추가 + UNIQUE 자연키에 편입 (2026-08-18, 사용자 승인).

    인라인 UNIQUE 는 ALTER 로 못 바꾸므로 테이블을 재구축한다. **순서 주의** — 구 테이블을
    RENAME 하면 eval_precedent 의 `REFERENCES fail_case(case_id)` 가 새 이름으로 재작성돼
    dangling FK 가 남는다(SQLite 3.25+ 기본). 그래서 새 테이블을 먼저 만들고 구 테이블을
    DROP 한 뒤 RENAME 한다. 인덱스는 DROP 과 함께 사라지므로 재생성한다.

    기존 행은 전부 test_condition='' — 소급 구분은 하지 않는다(원본은 report.db 편집 DB 에
    있고, 세션이 다시 export 될 때 자연 복구된다).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fail_case)")}
    if "test_condition" in cols:
        return
    conn.executescript("""
        CREATE TABLE fail_case_new (
            case_id TEXT PRIMARY KEY, product_name TEXT NOT NULL, lot_id TEXT, wafer_number INTEGER,
            item_id INTEGER NOT NULL, bin INTEGER, revision REAL, item_class TEXT,
            test_condition TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL, updated_at INTEGER,
            UNIQUE(product_name, lot_id, wafer_number, item_id, bin, revision, test_condition)
        );
        INSERT INTO fail_case_new (case_id,product_name,lot_id,wafer_number,item_id,bin,
                                   revision,item_class,test_condition,created_at,updated_at)
            SELECT case_id,product_name,lot_id,wafer_number,item_id,bin,
                   revision,item_class,'',created_at,updated_at FROM fail_case;
        DROP TABLE fail_case;
        ALTER TABLE fail_case_new RENAME TO fail_case;
        CREATE INDEX IF NOT EXISTS idx_fail_case_item_class ON fail_case(item_class);
        CREATE INDEX IF NOT EXISTS idx_fail_case_product ON fail_case(product_name);
        CREATE INDEX IF NOT EXISTS idx_fail_case_item ON fail_case(item_id);
    """)


_V9_FEATURE_COLS = ("fail_mad_min", "fail_body_jump_ratio", "fail_pass_gap_sigma",
                    "fail_robust_z_max", "e1_fail_share", "edge_fail_share",
                    "center_fail_share", "ring_fail_share", "fail_spread_norm",
                    "tail_mass_3s", "rail_low_ratio", "rail_high_ratio",
                    "value_gap_ratio", "value_gap_minor_mass")


def _migrate_v8_to_v9(conn):
    """v9: features 에 룰 판정지표 REAL 컬럼 14개 추가 (2026-08-19, 사용자 승인).

    값은 이미 L2 `features.compute()` 가 계산해 반환하고 있었다 — 저장 화이트리스트
    (`save_features` 의 cols)가 버렸을 뿐이라 계산 경로는 바뀌지 않는다.
    PK·UNIQUE 를 건드리지 않으므로 테이블 재구축 없이 ALTER 만 한다(기존 행 재작성 없음).
    기존 행은 새 컬럼이 NULL 로 남는다 — 소비자(review 층화·calibrate·signature_reason)는
    전부 None 을 "그 표본 제외"로 처리하므로 안전하고, 값은 per-DUT 원본에서만 나오므로
    소급 채움은 불가능하다(재수집해야 채워진다). 이미 있으면 skip(idempotent).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
    for col in _V9_FEATURE_COLS:
        if col not in cols:
            conn.execute(f"ALTER TABLE features ADD COLUMN {col} REAL")


_V10_FEATURE_COLS = ("tail_mass_3s_high", "tail_mass_3s_low")


def _migrate_v9_to_v10(conn):
    """v10: features 에 방향별 꼬리 질량 REAL 컬럼 2개 추가 (2026-08-19, 사용자 승인).

    구 `HEAVY_TAIL` 을 `USL_TAIL`/`LSL_TAIL` 로 가르면서 판정 기준값이 `tail_mass_3s`
    (|z|>3, 방향 없음) 에서 방향별 질량으로 바뀌었다. 기존 행은 NULL 이고 소급 채움은
    불가능하다(재수집해야 채워진다). 이미 있으면 skip(idempotent).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
    for col in _V10_FEATURE_COLS:
        if col not in cols:
            conn.execute(f"ALTER TABLE features ADD COLUMN {col} REAL")


_MIGRATIONS = {1: _migrate_v1_to_v2, 2: _migrate_v2_to_v3, 3: _migrate_v3_to_v4,
               4: _migrate_v4_to_v5, 5: _migrate_v5_to_v6,
               6: _migrate_v6_to_v7, 7: _migrate_v7_to_v8,
               8: _migrate_v8_to_v9,
               9: _migrate_v9_to_v10}  # {from_version: fn} — from → from+1


def _migrate(conn):
    """PRAGMA user_version 을 읽어 현재 버전부터 SCHEMA_VERSION 까지 순서대로 올린다.

    `_MIGRATIONS` 는 {from_version: fn} — 한 칸씩(from → from+1) 적용한다. 각 마이그레이션이
    idempotent 라 새로 만든 DB(SCHEMA 로 이미 최신 형태)에 다시 돌려도 안전하다.
    ⚠ 스키마를 바꾸려면 SCHEMA·마이그레이션·SCHEMA_VERSION 을 함께 올려야 하고, 운영
    eval.db 에 누적 데이터가 있으므로 **사용자 사전 승인 대상**이다(CLAUDE.md 불변 규칙 2).
    """
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    for v in range(max(ver, 1), SCHEMA_VERSION):
        _MIGRATIONS[v](conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _seed_bin_taxonomy(conn):
    """rules/bin_taxonomy.yaml entries → bin_taxonomy 테이블 적재(idempotent). 파일 없으면 skip."""
    try:
        with open(config.BIN_TAXONOMY_FILE, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return
    for e in doc.get("entries") or []:
        upsert_bin_taxonomy(e.get("product_type"), e.get("bin_number"), e.get("bin_class"),
                            e.get("severity_bias"), e.get("description"), conn=conn)


def init_db(db_path=None):
    """eval.db 생성 + 스키마 + 마이그레이션 + bin_taxonomy 시드. (상위 디렉토리 자동 생성)

    `db_path` 지정 시 그 파일을 대상으로 한다 — `config.DATA_DIR` 이 아니라 그 파일의
    부모를 만든다(엔진 기본 DB 를 건드리지 않기 위해). get_conn 과 같은 이유의 인자다.
    """
    from pathlib import Path
    (Path(db_path).parent if db_path else config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed_bin_taxonomy(conn)


# ── CRUD (docs/DB_SCHEMA 기준) ───────────────────────────────────────────────
@contextmanager
def _scope(conn):
    """conn 이 주어지면 그대로 사용(호출자가 커밋/종료 책임), 없으면 자체 커넥션."""
    if conn is not None:
        yield conn
    else:
        with get_conn() as c:
            yield c


def upsert_product_master(meta: dict, conn=None) -> None:
    """제품 마스터 upsert (product_name PK) — 재업로드 시 메타를 최신값으로 덮어쓴다."""
    sql = """INSERT INTO product_master
             (product_name,product_type,family_product,pkg_type,process,inch,gross_die,
              fab_line,tester,para,updated_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(product_name) DO UPDATE SET
               product_type=excluded.product_type, family_product=excluded.family_product,
               pkg_type=excluded.pkg_type, process=excluded.process, inch=excluded.inch,
               gross_die=excluded.gross_die, fab_line=excluded.fab_line,
               tester=excluded.tester, para=excluded.para, updated_at=excluded.updated_at"""
    with _scope(conn) as c:
        c.execute(sql, (meta.get("product_name"), meta.get("product_type"),
                        meta.get("family_product"), meta.get("pkg_type"),
                        meta.get("process"), meta.get("inch"), meta.get("gross_die"),
                        meta.get("fab_line"), meta.get("tester"), meta.get("para"), _now()))


def resolve_item_id(raw_name: str, conn=None):
    """원본 item 명 → item_alias 에 등록된 item_id. 처음 보는 이름이면 None."""
    with _scope(conn) as c:
        row = c.execute("SELECT item_id FROM item_alias WHERE raw_name=?", (raw_name,)).fetchone()
        return row["item_id"] if row else None


def upsert_item_master(item_canonical, item_name_raw, item_base, item_phase,
                       category_major, category_mid, value_type, unit, conn=None) -> int:
    """item 마스터 upsert 후 item_id 반환. 충돌 키는 item_canonical(정규화된 이름)이다.

    같은 측정을 제품·리비전마다 조금씩 다른 원본명으로 부르므로, canonical 하나에 여러
    raw name 이 item_alias 로 붙는 구조다. 여기서는 마지막에 본 raw name 을 남긴다.
    """
    sql = """INSERT INTO item_master
             (item_name_raw,item_canonical,item_base,item_phase,category_major,
              category_mid,value_type,unit)
             VALUES (?,?,?,?,?,?,?,?)
             ON CONFLICT(item_canonical) DO UPDATE SET
               item_name_raw=excluded.item_name_raw, item_base=excluded.item_base,
               item_phase=excluded.item_phase, category_major=excluded.category_major,
               category_mid=excluded.category_mid, value_type=excluded.value_type,
               unit=excluded.unit"""
    with _scope(conn) as c:
        c.execute(sql, (item_name_raw, item_canonical, item_base, item_phase,
                        category_major, category_mid, value_type, unit))
        row = c.execute("SELECT item_id FROM item_master WHERE item_canonical=?",
                        (item_canonical,)).fetchone()
        return row["item_id"]


def upsert_item_alias(raw_name, item_id, conn=None) -> None:
    """원본 item 명 → item_id 매핑 등록. 다음 ingest 부터 resolve_item_id 가 바로 찾는다."""
    with _scope(conn) as c:
        c.execute("INSERT OR REPLACE INTO item_alias (raw_name,item_id) VALUES (?,?)",
                  (raw_name, item_id))


def upsert_item_spec(item_id, product_name, revision, lsl, usl, conn=None) -> None:
    """spec limit(lsl/usl) 이력 upsert. 키는 (item_id, product_name, revision).

    revision 을 키에 넣는 이유 — limit 이 바뀌면 과거 판정의 근거가 달라지므로 덮어쓰지
    않고 리비전별로 남긴다.
    """
    sql = """INSERT INTO item_spec (item_id,product_name,revision,lsl,usl,updated_at)
             VALUES (?,?,?,?,?,?)
             ON CONFLICT(item_id,product_name,revision) DO UPDATE SET
               lsl=excluded.lsl, usl=excluded.usl, updated_at=excluded.updated_at"""
    with _scope(conn) as c:
        c.execute(sql, (item_id, product_name, revision, lsl, usl, _now()))


def upsert_bin_taxonomy(product_type, bin_number, bin_class, severity_bias,
                        description, conn=None) -> None:
    """bin 택소노미 upsert (product_type, bin_number 키) — bin_class + severity_bias.

    init_db 가 rules/bin_taxonomy.yaml 을 이 함수로 시드하므로 yaml 이 사실상 정본이다.
    """
    sql = """INSERT INTO bin_taxonomy
             (product_type,bin_number,bin_class,severity_bias,description,updated_at)
             VALUES (?,?,?,?,?,?)
             ON CONFLICT(product_type,bin_number) DO UPDATE SET
               bin_class=excluded.bin_class, severity_bias=excluded.severity_bias,
               description=excluded.description, updated_at=excluded.updated_at"""
    with _scope(conn) as c:
        c.execute(sql, (product_type, bin_number, bin_class, severity_bias,
                        description, _now()))


def get_bin_taxonomy(product_type, bin_number, conn=None):
    """(product_type, bin_number) 택소노미 1행 dict. 없으면 None.

    ⚠ L3 는 이 DB 조회가 아니라 `_rules.bin_taxonomy_for`(yaml 직독)를 쓴다 — 이건 CRUD 쪽 API.
    """
    with _scope(conn) as c:
        row = c.execute("SELECT * FROM bin_taxonomy WHERE product_type=? AND bin_number=?",
                        (product_type, bin_number)).fetchone()
        return dict(row) if row else None


def create_ingest_run(meta, conn=None) -> int:
    """업로드 1회 = ingest_run 1행 생성, run_id 반환. 매번 새 행이다(upsert 아님).

    session_id/analysis_key 는 report_server 세션 역참조용이자 선례검색의 **자기 세션
    제외** 조건이다(search_precedents) — 안 넣으면 방금 올린 데이터가 과거 사례로 돌아온다.
    """
    sql = """INSERT INTO ingest_run
             (product_name,lot_id,wafer_number,source_file,analysis_key,session_id,edm_link,
              temperature,corner,ingested_by,created_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?)"""
    with _scope(conn) as c:
        cur = c.execute(sql, (meta.get("product_name"), meta.get("lot_id"),
                              meta.get("wafer_number"), meta.get("source_file"),
                              meta.get("analysis_key"), meta.get("session_id"),
                              meta.get("edm_link"), meta.get("temperature"), meta.get("corner"),
                              meta.get("ingested_by"), _now()))
        return cur.lastrowid


def link_run_case(run_id, case_id, conn=None) -> None:
    """run ↔ case 다대다 링크. case_id 는 자연키라 여러 run 에 재등장하므로 INSERT OR IGNORE."""
    with _scope(conn) as c:
        c.execute("INSERT OR IGNORE INTO run_case (run_id,case_id,seen_at) VALUES (?,?,?)",
                  (run_id, case_id, _now()))


def upsert_fail_case(case_id, product_name, lot_id, wafer_number, item_id, bin_,
                     revision, item_class, condition="", conn=None) -> str:
    """fail_case upsert. 이미 있으면 updated_at 만 갱신 — 나머지 컬럼은 case_id 의 재료라 불변.

    case_id 가 자연키 sha256(make_case_id)이므로 같은 wafer/item/bin 을 재업로드해도 행이
    늘지 않는다(idempotent). `condition` 은 test_condition 컬럼이며 case_id 재료와 같은
    값을 줘야 한다(make_case_id 의 동명 인자).
    """
    sql = """INSERT INTO fail_case
             (case_id,product_name,lot_id,wafer_number,item_id,bin,revision,
              item_class,test_condition,created_at,updated_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(case_id) DO UPDATE SET updated_at=excluded.updated_at"""
    with _scope(conn) as c:
        c.execute(sql, (case_id, product_name, lot_id, wafer_number, item_id, bin_,
                        revision, item_class, condition or "", _now(), _now()))
        return case_id


def save_raw_metrics(case_id, run_id, m: dict, conn=None) -> None:
    """L1 계산값 upsert (case_id, run_id 키). DB_SCHEMA §4.

    이름이 raw_metrics 지만 **per-DUT raw 는 여기 없다** — 요약통계만이다(불변 규칙 3).
    """
    cols = ["cpk", "cpl", "cpu", "cp", "mean", "stdev", "min", "max", "yield",
            "fail_count", "total_count", "bimodality"]
    sql = f"""INSERT INTO raw_metrics (case_id,run_id,{','.join(cols)},created_at)
              VALUES (?,?,{','.join('?' * len(cols))},?)
              ON CONFLICT(case_id,run_id) DO UPDATE SET
              {','.join(f'{c}=excluded.{c}' for c in cols)}"""
    with _scope(conn) as c:
        c.execute(sql, (case_id, run_id, *[m.get(k) for k in cols], _now()))


def save_features(case_id, run_id, engine_version, f: dict, conn=None) -> None:
    """L2 계산값 upsert (case_id, run_id, engine_version 키). DB_SCHEMA §5.

    engine_version 이 키에 들어가는 이유 — 룰/공식이 바뀌면 같은 case 의 feature 도 달라져야
    하므로 버전별로 나란히 남긴다(과거 판정 재현 가능).
    ⚠ `shot_fail_ratio` 는 테이블·마이그레이션에는 있지만 features.py 에 계산 경로가 없고
    아래 cols 목록에도 없어 **항상 NULL** 이다(VERIFY_CHECKLIST §1-3, 미해결).
    v9(2026-08-19)부터 룰 판정지표 14종(`_V9_FEATURE_COLS`) + v10 의 방향별 꼬리 질량
    2종(`_V10_FEATURE_COLS`)도 저장한다 — 표본함 층화와
    임계값 what-if 의 모집단이 된다. **cols 에서 빠지면 컬럼만 있고 영원히 NULL 이 된다**
    (shot_fail_ratio 가 그 전례다).
    """
    cols = ["spread_norm", "skewness", "kurtosis", "outlier_ratio", "modality",
            "bimodality_score", "density_gap", "cdf_gap", "spec_margin_low",
            "spec_margin_high", "nearest_spec_side", "limit_hit_ratio",
            "edge_fail_ratio", "center_fail_ratio", "radial_gradient",
            "quadrant_imbalance", "x_gradient", "y_gradient", "wafer_zone_signature",
            "n_dut", "site_cpk_delta", "code_edge_hit",
            "ring_fail_ratio",
            "radial_gradient_norm", "x_gradient_norm", "y_gradient_norm",
            "n_modes", "modality_v2", *_V9_FEATURE_COLS, *_V10_FEATURE_COLS]
    sql = f"""INSERT INTO features (case_id,run_id,engine_version,computed_at,{','.join(cols)})
              VALUES (?,?,?,?,{','.join('?' * len(cols))})
              ON CONFLICT(case_id,run_id,engine_version) DO UPDATE SET
              {','.join(f'{c}=excluded.{c}' for c in cols)}"""
    with _scope(conn) as c:
        c.execute(sql, (case_id, run_id, engine_version, _now(),
                        *[f.get(k) for k in cols]))


def save_evaluation(case_id, run_id, engine_version, model_version, status,
                    confidence, data_completeness, comment, conn=None) -> int:
    """L4/L5 판정 결과 upsert 후 eval_id 반환. 키는 (case, run, engine_version, model_version).

    model_version 을 `''` 로 정규화하는 게 핵심이다 — SQLite 에서 NULL 은 UNIQUE 제약을
    우회해서, 그냥 두면 같은 판정이 계속 새 행으로 쌓인다(v2 마이그레이션의 이유).
    """
    model_version = model_version or ""  # NULL 은 UNIQUE 를 우회하므로 '' 로 정규화
    sql = """INSERT INTO evaluation
             (case_id,run_id,engine_version,model_version,status,confidence,
              data_completeness,comment,created_at)
             VALUES (?,?,?,?,?,?,?,?,?)
             ON CONFLICT(case_id,run_id,engine_version,model_version) DO UPDATE SET
               status=excluded.status, confidence=excluded.confidence,
               data_completeness=excluded.data_completeness, comment=excluded.comment,
               updated_at=excluded.created_at"""
    with _scope(conn) as c:
        c.execute(sql, (case_id, run_id, engine_version, model_version, status,
                        confidence, data_completeness, comment, _now()))
        row = c.execute("""SELECT eval_id FROM evaluation
                           WHERE case_id=? AND run_id=? AND engine_version=?
                             AND model_version=?""",
                        (case_id, run_id, engine_version, model_version)).fetchone()
        return row["eval_id"]


def save_eval_evidence(eval_id, evidence: list, conn=None) -> None:
    """판정 근거(signal_code + 값) 저장 — JSON 컬럼 금지라 정규화한 child 테이블(불변 규칙 4).

    ⚠ PK 가 (eval_id, signal_code)다. signal_code 는 영구 기록이므로 라벨과 값의 대응을
    바꾸면 과거 기록과 의미가 어긋난다(DENSITY_GAP 오라벨 사건, VERIFY_CHECKLIST §2-1).
    """
    with _scope(conn) as c:
        for e in evidence:
            c.execute("""INSERT OR REPLACE INTO eval_evidence
                         (eval_id,signal_code,value,weight,note) VALUES (?,?,?,?,?)""",
                      (eval_id, e["signal_code"], e.get("value"), e.get("weight"),
                       e.get("note")))


def save_case_signature(eval_id, signatures: list, conn=None) -> None:
    """발화 signature 를 role(primary/secondary)과 함께 저장. 선례검색이 primary 만 조인한다."""
    with _scope(conn) as c:
        for s in signatures:
            c.execute("""INSERT OR REPLACE INTO case_signature
                         (eval_id,signature,role,score) VALUES (?,?,?,?)""",
                      (eval_id, s["id"], s.get("role", "secondary"), s.get("score")))


def insert_label(case_id, eval_id, human_status, root_cause_category, root_cause_detail,
                 engine_comment_accepted, comment_modified, human_comment, labeler,
                 reviewer, label_quality, conn=None) -> int:
    """엔지니어 정답 라벨 1건 삽입, label_id 반환. 갱신이 아니라 **이력 누적**이다.

    human_comment 가 선례검색이 실제로 인용하는 유일한 텍스트다(DB_SCHEMA §9).
    """
    sql = """INSERT INTO label (case_id,eval_id,human_status,root_cause_category,
             root_cause_detail,engine_comment_accepted,comment_modified,human_comment,
             labeler,reviewer,label_quality,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""
    with _scope(conn) as c:
        cur = c.execute(sql, (case_id, eval_id, human_status, root_cause_category,
                              root_cause_detail, engine_comment_accepted, comment_modified,
                              human_comment, labeler, reviewer, label_quality, _now()))
        return cur.lastrowid


def save_label_signatures(label_id, signatures, conn=None) -> None:
    """사람이 지목한 정답 signature 저장 — 순서를 rank(1..N)로 보존한다.

    같은 label 의 기존 행은 먼저 지운다(라벨 1건 = 지목 목록 1개). 엔진 발화 기록
    (case_signature)과는 다른 테이블이다.
    """
    with _scope(conn) as c:
        c.execute("DELETE FROM label_signature WHERE label_id=?", (label_id,))
        for i, sig in enumerate(signatures or [], start=1):
            c.execute("""INSERT OR REPLACE INTO label_signature (label_id,signature,rank)
                         VALUES (?,?,?)""", (label_id, str(sig), i))


def delete_label_with_signatures(where_sql: str, params, conn=None) -> int:
    """label 삭제 + 자식 label_signature 정리. 삭제 건수 반환.

    SQLite 는 FK cascade 가 기본 off 라 자식이 고아로 남는다 — label 을 지우는 곳은
    **전부 이 함수를 거쳐** 자식부터 지운다. `where_sql` 은 label 테이블 기준 조건절
    (예 ``"case_id=? AND labeler=?"``).
    """
    with _scope(conn) as c:
        c.execute(f"DELETE FROM label_signature WHERE label_id IN "
                  f"(SELECT label_id FROM label WHERE {where_sql})", params)
        cur = c.execute(f"DELETE FROM label WHERE {where_sql}", params)
        return cur.rowcount or 0


def insert_case_outcome(case_id, label_id, action, condition, result, resolved_by,
                        resolved_at, note, conn=None) -> int:
    """조치·결과(action/result) 1건 삽입, outcome_id 반환.

    삽입 전에 `validate_outcome` 으로 어휘를 강제한다 — 자유 문자열을 허용하면 선례 통계가
    바로 무너진다. _rules import 는 순환 회피용 지연 import.
    """
    from .pipeline._rules import validate_outcome  # lazy: 순환 import 회피
    validate_outcome(action, result)
    sql = """INSERT INTO case_outcome (case_id,label_id,action,condition,result,
             resolved_by,resolved_at,note,created_at) VALUES (?,?,?,?,?,?,?,?,?)"""
    with _scope(conn) as c:
        cur = c.execute(sql, (case_id, label_id, action, condition, result,
                              resolved_by, resolved_at, note, _now()))
        return cur.lastrowid


def save_eval_precedents(eval_id, precedents: list, conn=None) -> None:
    """L5 가 참조한 선례 이력 저장 — rank(관련도 순위 1-based) + similarity.
    case_id 없는 행(RAG 백엔드 등)은 skip. 재평가 시 같은 (eval, case) 는 갱신."""
    with _scope(conn) as c:
        for rank, p in enumerate(precedents, start=1):
            if not p.get("case_id"):
                continue
            c.execute("""INSERT OR REPLACE INTO eval_precedent
                         (eval_id,precedent_case_id,rank,similarity) VALUES (?,?,?,?)""",
                      (eval_id, p["case_id"], rank, p.get("similarity")))


def upsert_engine_version_registry(engine_version, thresholds_ref=None, thresholds_hash=None,
                                   signatures_ref=None, signatures_hash=None,
                                   taxonomy_ref=None, taxonomy_hash=None, conn=None) -> None:
    """engine_version ↔ 그때의 rules 파일 해시 등록. calibrate 가 임계값을 갱신할 때 호출한다.

    features/evaluation 이 engine_version 을 키로 갖고 있으므로, 이 표가 있어야 "그 판정이
    어떤 임계값으로 나왔는지"를 나중에 되짚을 수 있다.
    """
    sql = """INSERT INTO engine_version_registry
             (engine_version,thresholds_ref,thresholds_hash,signatures_ref,signatures_hash,
              taxonomy_ref,taxonomy_hash,created_at) VALUES (?,?,?,?,?,?,?,?)
             ON CONFLICT(engine_version) DO UPDATE SET
               thresholds_ref=excluded.thresholds_ref, thresholds_hash=excluded.thresholds_hash,
               signatures_ref=excluded.signatures_ref, signatures_hash=excluded.signatures_hash,
               taxonomy_ref=excluded.taxonomy_ref, taxonomy_hash=excluded.taxonomy_hash"""
    with _scope(conn) as c:
        c.execute(sql, (engine_version, thresholds_ref, thresholds_hash, signatures_ref,
                        signatures_hash, taxonomy_ref, taxonomy_hash, _now()))


def search_precedents(value_type, item_canonical, family_product=None,
                      limit=None, exclude_case_id=None, exclude_session_id=None,
                      exclude_analysis_key=None, fired_signatures=None, conn=None) -> list:
    """DB_SCHEMA §9: 동일 value_type + item_canonical 유사도>=threshold (+ family_product).

    bin 은 매칭 조건에서 제외(더 폭넓게 참고). 후보를 SQL 로 좁힌 뒤
    difflib.SequenceMatcher.ratio 로 이름 유사도 후처리.
    exclude_case_id: 자기 자신(현재 평가 중인 case)은 선례에서 제외.
    exclude_session_id / exclude_analysis_key: **자기 세션(및 dedup 형제)의 사례 제외**
      — 같은 세션의 코멘트가 다른 case_id 로 적재돼 "과거 사례"인 척 돌아오는
      시간 누출을 막는다. ingest_run 경유(run_case ⨝ ingest_run). None 이면 no-op.
    fired_signatures: 현재 케이스에서 발화한 signature id 목록 — 선례의 primary
      signature 가 겹치면 정렬 부스트(하드필터 아님, 선례 DB 가 얕아도 회수 유지).
    **(제품, lot, item) 당 1행**(최신 label 기준, human_comment 있는 행 우선) — 2026-08-19
    까지는 case 당 1행이라 bin 별로 쪼개진 옛 case 가 같은 item 을 여러 줄로 채웠다.
    limit=None 이면 전체 반환(store 계약 — 상한은 호출측 precedent_client 가 건다).
    DB 파일이 없으면(preview 모드 등) 빈 목록 — 빈 파일 생성/크래시 방지.
    """
    if conn is None and not config.DB_PATH.exists():
        return []
    sql = """SELECT fc.case_id, im.item_canonical, fc.bin, im.value_type, fc.product_name,
                    fc.lot_id, pm.family_product, cs.signature, l.label_id,
                    l.root_cause_category, l.human_comment,
                    co.action, co.condition, co.result
             FROM fail_case fc
             JOIN item_master im ON im.item_id = fc.item_id
             JOIN product_master pm ON pm.product_name = fc.product_name
             LEFT JOIN evaluation ev ON ev.case_id = fc.case_id
             LEFT JOIN case_signature cs ON cs.eval_id = ev.eval_id AND cs.role='primary'
             LEFT JOIN label l ON l.case_id = fc.case_id
             LEFT JOIN case_outcome co ON co.case_id = fc.case_id
                  AND (co.label_id IS NULL OR co.label_id = l.label_id)
             WHERE im.value_type = ?
               AND (? IS NULL OR pm.family_product = ?)
               AND NOT EXISTS (
                   SELECT 1 FROM run_case rc
                   JOIN ingest_run ir ON ir.run_id = rc.run_id
                   WHERE rc.case_id = fc.case_id
                     AND ((? IS NOT NULL AND ir.session_id = ?)
                          OR (? IS NOT NULL AND ir.analysis_key = ?)))"""
    params = (value_type, family_product, family_product,
              exclude_session_id, exclude_session_id,
              exclude_analysis_key, exclude_analysis_key)
    with _scope(conn) as c:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]

    def _rank(r):  # 같은 case 의 여러 (label×outcome) 행 중 대표행: 최신 label > comment 있는 행
        """대표행 선정 정렬키 — (label_id, human_comment 유무). 큰 쪽이 이긴다."""
        return ((r["label_id"] or 0), r["human_comment"] is not None)

    fired = {s for s in (fired_signatures or []) if s}
    best = {}
    for r in rows:
        if exclude_case_id is not None and r["case_id"] == exclude_case_id:
            continue
        sim = difflib.SequenceMatcher(None, item_canonical, r["item_canonical"] or "").ratio()
        if sim < config.PRECEDENT_NAME_SIMILARITY:
            continue
        r["similarity"] = sim
        # dedup 은 **(제품, lot, item)** 단위다 — case_id 단위로 묶으면 과거에 bin 별로
        # 쪼개져 적재된 case(2026-08-19 이전 데이터·CSV 적재분)가 같은 item 인데도 각각
        # 살아남아 선례 목록을 채운다. 실측: 반환 행의 90~94% 가 같은 item 의 복제본이라
        # top-k 5칸이 사실상 item 1~2개로 채워졌다. 정렬 키(코멘트 유무·발화 겹침·유사도)가
        # 복제본끼리 전부 동률이라 정렬로도 걸러지지 않는다.
        key = (r["product_name"], r.get("lot_id"), r["item_canonical"])
        prev = best.get(key)
        if prev is None or _rank(r) > _rank(prev):
            best[key] = r
    out = sorted(best.values(),
                 key=lambda r: (r["human_comment"] is not None,
                                bool(fired) and r.get("signature") in fired,
                                r["similarity"]),
                 reverse=True)
    return out if limit is None else out[:limit]

def cases_for_runs(run_ids : list[int], conn = None) -> list[dict]:
    """여러 run 의 case 를 판정·수율과 함께 평평한 행 목록으로. cross_source 전용 조회.

    행마다 source_file(ingest_run) + fail_count/total_count(raw_metrics) + primary_signature
    (case_signature)가 붙어 있어, source 간 fail rate 비교를 이 한 번의 조회로 끝낸다.
    빈 run_ids 나 DB 파일 부재는 빈 목록(빈 파일 생성·크래시 방지).
    """
    if not run_ids:
        return []
    if conn is None and not config.DB_PATH.exists():
        return []
    qmarks = ",".join("?" * len(run_ids))
    sql = f"""SELECT fc.case_id, fc.item_id, im.item_canonical, fc.product_name, fc.lot_id,
                     fc.wafer_number, rc.run_id, ir.source_file,
                     rm.fail_count, rm.total_count,
                     ev.status, ev.engine_version, cs.signature AS primary_signature
               FROM run_case rc
               JOIN fail_case fc ON fc.case_id = rc.case_id
               JOIN item_master im ON im.item_id = fc.item_id
               JOIN ingest_run ir ON ir.run_id = rc.run_id
               LEFT JOIN raw_metrics rm ON rm.case_id = fc.case_id AND rm.run_id = rc.run_id
               LEFT JOIN evaluation ev ON ev.case_id = fc.case_id AND ev.run_id = rc.run_id
               LEFT JOIN case_signature cs ON cs.eval_id = ev.eval_id AND cs.role = 'primary'
               WHERE rc.run_id IN ({qmarks})"""
    with _scope(conn) as c:
        return [dict(r) for r in c.execute(sql, run_ids).fetchall()]

def update_evaluation_comment(case_id, run_id, engine_version, comment, conn=None) -> None:
    """이미 저장된 판정의 comment 만 덮어쓴다(status/confidence 는 건드리지 않음).

    cross_source 가 사후에 SOURCE_ONLY_FAIL 문구를 얹는 경로. 해당 행이 없으면 조용히 0행 갱신.
    """
    with _scope(conn) as c:
        c.execute("""UPDATE evaluation SET comment=?, updated_at=?
                     WHERE case_id=? AND run_id=? AND engine_version=?""",
                     (comment, _now(), case_id, run_id, engine_version))

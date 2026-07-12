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

SCHEMA_VERSION = 4  # PRAGMA user_version. 스키마 변경 시 +1 하고 _MIGRATIONS 에 단계 추가.

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
CREATE TABLE IF NOT EXISTS fail_case (
    case_id TEXT PRIMARY KEY, product_name TEXT NOT NULL, lot_id TEXT, wafer_number INTEGER,
    item_id INTEGER NOT NULL, bin INTEGER, revision REAL, item_class TEXT,
    created_at INTEGER NOT NULL, updated_at INTEGER,
    UNIQUE(product_name, lot_id, wafer_number, item_id, bin, revision)
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
CREATE TABLE IF NOT EXISTS label (
    label_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, eval_id INTEGER,
    human_status TEXT, root_cause_category TEXT, root_cause_detail TEXT,
    engine_comment_accepted INTEGER, comment_modified INTEGER, human_comment TEXT,
    labeler TEXT, reviewer TEXT, label_quality TEXT, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_label_case ON label(case_id);
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
    return int(time.time())


def make_case_id(product_name, lot_id, wafer_number, item_id, bin_, revision):
    """자연키 sha256 (재업로드 idempotent). docs/DB_SCHEMA §3."""
    key = "|".join(str(x) for x in (product_name, lot_id, wafer_number, item_id, bin_, revision))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@contextmanager
def get_conn():
    conn = sqlite3.connect(str(config.DB_PATH), timeout=10)
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


_MIGRATIONS = {1: _migrate_v1_to_v2, 2: _migrate_v2_to_v3,
               3: _migrate_v3_to_v4}  # {from_version: fn} — from → from+1


def _migrate(conn):
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


def init_db():
    """eval.db 생성 + 스키마 + 마이그레이션 + bin_taxonomy 시드. (config.DATA_DIR 자동 생성)"""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
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
    with _scope(conn) as c:
        row = c.execute("SELECT item_id FROM item_alias WHERE raw_name=?", (raw_name,)).fetchone()
        return row["item_id"] if row else None


def upsert_item_master(item_canonical, item_name_raw, item_base, item_phase,
                       category_major, category_mid, value_type, unit, conn=None) -> int:
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
    with _scope(conn) as c:
        c.execute("INSERT OR REPLACE INTO item_alias (raw_name,item_id) VALUES (?,?)",
                  (raw_name, item_id))


def upsert_item_spec(item_id, product_name, revision, lsl, usl, conn=None) -> None:
    sql = """INSERT INTO item_spec (item_id,product_name,revision,lsl,usl,updated_at)
             VALUES (?,?,?,?,?,?)
             ON CONFLICT(item_id,product_name,revision) DO UPDATE SET
               lsl=excluded.lsl, usl=excluded.usl, updated_at=excluded.updated_at"""
    with _scope(conn) as c:
        c.execute(sql, (item_id, product_name, revision, lsl, usl, _now()))


def upsert_bin_taxonomy(product_type, bin_number, bin_class, severity_bias,
                        description, conn=None) -> None:
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
    with _scope(conn) as c:
        row = c.execute("SELECT * FROM bin_taxonomy WHERE product_type=? AND bin_number=?",
                        (product_type, bin_number)).fetchone()
        return dict(row) if row else None


def create_ingest_run(meta, conn=None) -> int:
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
    with _scope(conn) as c:
        c.execute("INSERT OR IGNORE INTO run_case (run_id,case_id,seen_at) VALUES (?,?,?)",
                  (run_id, case_id, _now()))


def upsert_fail_case(case_id, product_name, lot_id, wafer_number, item_id, bin_,
                     revision, item_class, conn=None) -> str:
    sql = """INSERT INTO fail_case
             (case_id,product_name,lot_id,wafer_number,item_id,bin,revision,
              item_class,created_at,updated_at)
             VALUES (?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(case_id) DO UPDATE SET updated_at=excluded.updated_at"""
    with _scope(conn) as c:
        c.execute(sql, (case_id, product_name, lot_id, wafer_number, item_id, bin_,
                        revision, item_class, _now(), _now()))
        return case_id


def save_raw_metrics(case_id, run_id, m: dict, conn=None) -> None:
    cols = ["cpk", "cpl", "cpu", "cp", "mean", "stdev", "min", "max", "yield",
            "fail_count", "total_count", "bimodality"]
    sql = f"""INSERT INTO raw_metrics (case_id,run_id,{','.join(cols)},created_at)
              VALUES (?,?,{','.join('?' * len(cols))},?)
              ON CONFLICT(case_id,run_id) DO UPDATE SET
              {','.join(f'{c}=excluded.{c}' for c in cols)}"""
    with _scope(conn) as c:
        c.execute(sql, (case_id, run_id, *[m.get(k) for k in cols], _now()))


def save_features(case_id, run_id, engine_version, f: dict, conn=None) -> None:
    cols = ["spread_norm", "skewness", "kurtosis", "outlier_ratio", "modality",
            "bimodality_score", "density_gap", "cdf_gap", "spec_margin_low",
            "spec_margin_high", "nearest_spec_side", "limit_hit_ratio",
            "edge_fail_ratio", "center_fail_ratio", "radial_gradient",
            "quadrant_imbalance", "x_gradient", "y_gradient", "wafer_zone_signature",
            "n_dut", "site_cpk_delta", "code_edge_hit"]
    sql = f"""INSERT INTO features (case_id,run_id,engine_version,computed_at,{','.join(cols)})
              VALUES (?,?,?,?,{','.join('?' * len(cols))})
              ON CONFLICT(case_id,run_id,engine_version) DO UPDATE SET
              {','.join(f'{c}=excluded.{c}' for c in cols)}"""
    with _scope(conn) as c:
        c.execute(sql, (case_id, run_id, engine_version, _now(),
                        *[f.get(k) for k in cols]))


def save_evaluation(case_id, run_id, engine_version, model_version, status,
                    confidence, data_completeness, comment, conn=None) -> int:
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
    with _scope(conn) as c:
        for e in evidence:
            c.execute("""INSERT OR REPLACE INTO eval_evidence
                         (eval_id,signal_code,value,weight,note) VALUES (?,?,?,?,?)""",
                      (eval_id, e["signal_code"], e.get("value"), e.get("weight"),
                       e.get("note")))


def save_case_signature(eval_id, signatures: list, conn=None) -> None:
    with _scope(conn) as c:
        for s in signatures:
            c.execute("""INSERT OR REPLACE INTO case_signature
                         (eval_id,signature,role,score) VALUES (?,?,?,?)""",
                      (eval_id, s["id"], s.get("role", "secondary"), s.get("score")))


def insert_label(case_id, eval_id, human_status, root_cause_category, root_cause_detail,
                 engine_comment_accepted, comment_modified, human_comment, labeler,
                 reviewer, label_quality, conn=None) -> int:
    sql = """INSERT INTO label (case_id,eval_id,human_status,root_cause_category,
             root_cause_detail,engine_comment_accepted,comment_modified,human_comment,
             labeler,reviewer,label_quality,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""
    with _scope(conn) as c:
        cur = c.execute(sql, (case_id, eval_id, human_status, root_cause_category,
                              root_cause_detail, engine_comment_accepted, comment_modified,
                              human_comment, labeler, reviewer, label_quality, _now()))
        return cur.lastrowid


def insert_case_outcome(case_id, label_id, action, condition, result, resolved_by,
                        resolved_at, note, conn=None) -> int:
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
                      limit=None, exclude_case_id=None, conn=None) -> list:
    """DB_SCHEMA §9: 동일 value_type + item_canonical 유사도>=threshold (+ family_product).

    bin 은 매칭 조건에서 제외(더 폭넓게 참고). 후보를 SQL 로 좁힌 뒤
    difflib.SequenceMatcher.ratio 로 이름 유사도 후처리.
    exclude_case_id: 자기 자신(현재 평가 중인 case)은 선례에서 제외.
    case 당 1행(최신 label 기준, human_comment 있는 행 우선), 라벨 있는 선례 우선.
    limit=None 이면 전체 반환(호출측에서 매칭된 선례를 모두 사용하는 것을 전제).
    DB 파일이 없으면(preview 모드 등) 빈 목록 — 빈 파일 생성/크래시 방지.
    """
    if conn is None and not config.DB_PATH.exists():
        return []
    sql = """SELECT fc.case_id, im.item_canonical, fc.bin, im.value_type, fc.product_name,
                    pm.family_product, cs.signature, l.label_id,
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
               AND (? IS NULL OR pm.family_product = ?)"""
    with _scope(conn) as c:
        rows = [dict(r) for r in c.execute(
            sql, (value_type, family_product, family_product)).fetchall()]

    def _rank(r):  # 같은 case 의 여러 (label×outcome) 행 중 대표행: 최신 label > comment 있는 행
        return ((r["label_id"] or 0), r["human_comment"] is not None)

    best = {}
    for r in rows:
        if exclude_case_id is not None and r["case_id"] == exclude_case_id:
            continue
        sim = difflib.SequenceMatcher(None, item_canonical, r["item_canonical"] or "").ratio()
        if sim < config.PRECEDENT_NAME_SIMILARITY:
            continue
        r["similarity"] = sim
        prev = best.get(r["case_id"])
        if prev is None or _rank(r) > _rank(prev):
            best[r["case_id"]] = r
    out = sorted(best.values(),
                 key=lambda r: (r["human_comment"] is not None, r["similarity"]),
                 reverse=True)
    return out if limit is None else out[:limit]

"""DB 공통 기반: 스키마·마이그레이션·커넥션·analysis lock.

report_db facade 의 구현 분리(2026-07-11 Phase 4). 다른 database/ 모듈들은
여기의 get_conn/_now/_row 를 공유한다.
"""
import sqlite3
import time
from contextlib import contextmanager

from config import REPORT_DB_PATH, REPORT_LOCK_TTL_SEC

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_session (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT UNIQUE NOT NULL,
    analysis_key  TEXT,
    file_name     TEXT NOT NULL,
    file_path     TEXT,
    content_hash  TEXT,
    status        TEXT DEFAULT 'pending',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER,
    error_message TEXT,
    product_type  TEXT,
    family_product TEXT,
    process       TEXT,
    product       TEXT,
    part_id       TEXT,
    sub_part_id   TEXT,
    product_group TEXT,
    wf_size       TEXT,
    chip_size_x   TEXT,
    chip_size_y   TEXT,
    gross_die     TEXT,
    pkg_type      TEXT,
    e2f_fab_site  TEXT,
    step          TEXT,
    temperature   TEXT,
    equip         TEXT,
    para          TEXT,
    flat_zone     TEXT,
    revision      TEXT,
    edm_link      TEXT,
    dataset_id    TEXT,
    lot_id        TEXT,
    password      TEXT,
    is_debug      INTEGER DEFAULT 0,
    source        TEXT DEFAULT 'xlsx_upload',
    is_important  INTEGER DEFAULT 0,
    is_private    INTEGER DEFAULT 0,
    uploaded_by   TEXT,
    client_host   TEXT,
    webreport_options TEXT,
    mode          TEXT DEFAULT 'Normal',
    deleted_at    INTEGER,
    deleted_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_report_session_analysis_key
    ON report_session(analysis_key);
CREATE INDEX IF NOT EXISTS idx_report_session_status_created
    ON report_session(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_session_product_type
    ON report_session(product_type);
CREATE INDEX IF NOT EXISTS idx_report_session_deleted_at
    ON report_session(deleted_at);

CREATE TABLE IF NOT EXISTS report_analysis_summary (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_key  TEXT NOT NULL,
    session_id    TEXT,
    item_name     TEXT NOT NULL,
    bin_number    INTEGER,
    yield_percent REAL,
    fail_count    INTEGER,
    cpk_val       REAL,
    mean_val      REAL,
    stdev_val     REAL,
    lsl           REAL,
    usl           REAL,
    unit          TEXT,
    created_at    INTEGER NOT NULL,
    UNIQUE(analysis_key, item_name, bin_number)
);
CREATE INDEX IF NOT EXISTS idx_report_summary_analysis_key
    ON report_analysis_summary(analysis_key);

CREATE TABLE IF NOT EXISTS report_object_info (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_key  TEXT NOT NULL,
    object_type   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    options_json  TEXT NOT NULL,
    s3_bucket     TEXT,
    s3_key        TEXT NOT NULL,
    s3_uri        TEXT,
    created_at    INTEGER NOT NULL,
    last_accessed INTEGER,
    UNIQUE(analysis_key, object_type)
);
CREATE INDEX IF NOT EXISTS idx_report_object_content_hash
    ON report_object_info(content_hash);
CREATE INDEX IF NOT EXISTS idx_report_object_last_accessed
    ON report_object_info(last_accessed);

CREATE TABLE IF NOT EXISTS report_analysis_lock (
    analysis_key  TEXT PRIMARY KEY,
    owner         TEXT NOT NULL,
    locked_at     INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS report_csv_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_key TEXT NOT NULL,
    filename     TEXT NOT NULL,
    s3_key       TEXT NOT NULL,
    s3_uri       TEXT,
    file_size    INTEGER,
    uploaded_at  INTEGER NOT NULL,
    UNIQUE(analysis_key, filename)
);
CREATE INDEX IF NOT EXISTS idx_report_csv_analysis_key
    ON report_csv_files(analysis_key);

CREATE TABLE IF NOT EXISTS report_annotation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    analysis_key TEXT,
    target       TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_annotation_session
    ON report_annotation(session_id);

CREATE TABLE IF NOT EXISTS report_dashboard_comment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    item_key     TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   INTEGER NOT NULL,
    UNIQUE(dataset_id, kind, item_key)
);
CREATE INDEX IF NOT EXISTS idx_report_dashboard_dataset
    ON report_dashboard_comment(dataset_id, kind);

CREATE TABLE IF NOT EXISTS report_sheet_data (
    analysis_key TEXT NOT NULL,
    sheet_name   TEXT NOT NULL,
    data_json    TEXT NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (analysis_key, sheet_name)
);

CREATE TABLE IF NOT EXISTS report_audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    action         TEXT NOT NULL,        -- 'upload' | 'edit' | 'delete'
    session_id     TEXT,
    analysis_key   TEXT,
    -- 삭제 시 세션 행이 사라지므로 조회 가독성을 위해 메타 스냅샷을 함께 저장
    product_type   TEXT,
    product        TEXT,
    lot_id         TEXT,
    file_name      TEXT,
    changed_fields TEXT,                 -- edit 시 변경 필드명 콤마조인, 그 외 NULL
    client_ip      TEXT,
    user_agent     TEXT,
    client_user    TEXT,                 -- 클라이언트 신고 Windows 계정 (upload 만, 위조 가능)
    client_host    TEXT,                 -- 클라이언트 신고 PC 이름
    result         TEXT DEFAULT 'ok',    -- 'ok' | 'fail'
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_audit_created_at
    ON report_audit_log(created_at);
-- /pe/admin 대시보드 필터 조회용 (audit 행이 누적돼도 action/session_id 필터가 풀스캔 안 되게)
CREATE INDEX IF NOT EXISTS idx_report_audit_action
    ON report_audit_log(action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_audit_session_id
    ON report_audit_log(session_id);

CREATE TABLE IF NOT EXISTS report_user_favorite (
    user_id    TEXT NOT NULL,        -- 웹 사용자 신고 Windows ID (소문자 정규화, 위조 가능)
    session_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, session_id)
);

CREATE TABLE IF NOT EXISTS report_user (
    user_id       TEXT PRIMARY KEY,  -- 소문자 정규화된 로그인 ID
    password_hash TEXT NOT NULL,     -- werkzeug generate_password_hash 결과
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS report_session_editor (
    session_id  TEXT NOT NULL,       -- 편집 권한을 위임한 세션
    editor_user TEXT NOT NULL,       -- 권한을 받은 PC 계정 (소문자 정규화, _current_user 규칙)
    granted_by  TEXT,                -- 부여한 업로더 계정
    granted_at  INTEGER NOT NULL,
    PRIMARY KEY (session_id, editor_user)
);

CREATE TABLE IF NOT EXISTS report_web_visitor (
    user_id    TEXT PRIMARY KEY,     -- web_report 를 연 적 있는 Honey 사용자 (편집자 후보 풀)
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS report_user_important (
    user_id    TEXT NOT NULL,        -- 사용자별 개인 중요표시 (전역 is_important 와 별개)
    session_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, session_id)
);

-- web_report 편집 상태 (comment/override) — 세션 단위 저장 (2026-07-11).
-- manifest 는 업로드 시점 불변 스냅샷으로 강등되고, 편집은 이 테이블에만 기록된다.
-- dedup(동일 analysis_key) 세션 간 편집을 공유하지 않는다. 표시 순서(etc_item)는 rowid.
CREATE TABLE IF NOT EXISTS report_webreport_edit (
    session_id TEXT NOT NULL,
    kind       TEXT NOT NULL,        -- issue_comment | etc_item | trim_override | summary_engr
    item_key   TEXT NOT NULL,        -- kind 별 키 (web_report/edits.py 규약)
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    updated_by TEXT,
    PRIMARY KEY (session_id, kind, item_key)
);

-- 세션별 편집 rev (단조 증가) — REPORT/TRIM//full 캐시 키의 무효화 토큰.
CREATE TABLE IF NOT EXISTS report_webreport_edit_rev (
    session_id TEXT PRIMARY KEY,
    rev        INTEGER NOT NULL
);
"""

_PRODUCT_TYPE_NAMES = ("MDDI", "PDDI", "PMIC", "SECURITY", "TCON")


def _now():
    return int(time.time())


def _row(row):
    return None if row is None else dict(row)


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(conn, table_name, column_name):
    if not _table_exists(conn, table_name):
        return False
    return any(r[1] == column_name for r in conn.execute(f"PRAGMA table_info({table_name})"))


def _migrate_product_type_names(conn):
    for table_name in ("report_session", "report_audit_log"):
        if not _column_exists(conn, table_name, "product_type"):
            continue
        for name in _PRODUCT_TYPE_NAMES:
            conn.execute(
                f"UPDATE {table_name} SET product_type=? WHERE product_type=?",
                (name, name[:2]),
            )


def _migrate(conn):
    """기존 DB 스키마 업그레이드. 빈 DB(테이블 없음) 에서는 no-op — SCHEMA 가 새로 만든다."""

    # report_object_info: 옛 (analysis_key PK) → (id PK + UNIQUE(analysis_key, object_type))
    if _table_exists(conn, "report_object_info"):
        info = conn.execute("PRAGMA table_info(report_object_info)").fetchall()
        col_names = [r[1] for r in info]
        if col_names and "id" not in col_names:
            conn.execute("ALTER TABLE report_object_info RENAME TO _report_object_info_old")
            conn.execute("""
                CREATE TABLE report_object_info (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_key  TEXT NOT NULL,
                    object_type   TEXT NOT NULL,
                    content_hash  TEXT NOT NULL,
                    options_json  TEXT NOT NULL,
                    s3_bucket     TEXT,
                    s3_key        TEXT NOT NULL,
                    s3_uri        TEXT,
                    created_at    INTEGER NOT NULL,
                    last_accessed INTEGER,
                    UNIQUE(analysis_key, object_type)
                )
            """)
            conn.execute("""
                INSERT INTO report_object_info
                    (analysis_key, object_type, content_hash, options_json,
                     s3_bucket, s3_key, s3_uri, created_at, last_accessed)
                SELECT analysis_key, object_type, content_hash, options_json,
                       s3_bucket, s3_key, s3_uri, created_at, last_accessed
                FROM _report_object_info_old
            """)
            conn.execute("DROP TABLE _report_object_info_old")

    # report_session: 추가 컬럼들
    if _table_exists(conn, "report_session"):
        sess_info = conn.execute("PRAGMA table_info(report_session)").fetchall()
        sess_cols = {r[1] for r in sess_info}
        for col in (
            "analysis_key", "content_hash", "error_message",
            "product_type", "family_product", "process", "product", "revision", "edm_link",
            "dataset_id", "lot_id", "password", "uploaded_by", "client_host",
            "webreport_options",
            "part_id", "sub_part_id", "product_group", "wf_size", "chip_size_x",
            "chip_size_y", "gross_die", "pkg_type", "e2f_fab_site", "step",
            "temperature", "equip", "para", "flat_zone",
        ):
            if col not in sess_cols:
                conn.execute(f"ALTER TABLE report_session ADD COLUMN {col} TEXT")
        if "is_debug" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN is_debug INTEGER DEFAULT 0")
        if "source" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN source TEXT DEFAULT 'xlsx_upload'")
        if "is_important" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN is_important INTEGER DEFAULT 0")
        if "is_private" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN is_private INTEGER DEFAULT 0")
        if "mode" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN mode TEXT DEFAULT 'Normal'")
        # 휴지통(soft delete) 컬럼 — deleted_at 은 INTEGER(epoch), deleted_by 는 삭제자 계정.
        if "deleted_at" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN deleted_at INTEGER")
        if "deleted_by" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN deleted_by TEXT")

    if not _table_exists(conn, "report_sheet_data"):
        conn.execute("""
            CREATE TABLE report_sheet_data (
                analysis_key TEXT NOT NULL,
                sheet_name   TEXT NOT NULL,
                data_json    TEXT NOT NULL,
                updated_at   INTEGER NOT NULL,
                PRIMARY KEY (analysis_key, sheet_name)
            )
        """)

    # report_audit_log: 클라이언트 신고 신원 컬럼 (기존 DB 는 ALTER 필요)
    if _table_exists(conn, "report_audit_log"):
        audit_cols = {r[1] for r in conn.execute("PRAGMA table_info(report_audit_log)")}
        for col in ("client_user", "client_host"):
            if col not in audit_cols:
                conn.execute(f"ALTER TABLE report_audit_log ADD COLUMN {col} TEXT")

    # 편집 권한 위임 / web_report 방문자 / 사용자별 개인 중요표시 (기존 DB 에도 생성)
    if not _table_exists(conn, "report_session_editor"):
        conn.execute("""
            CREATE TABLE report_session_editor (
                session_id  TEXT NOT NULL,
                editor_user TEXT NOT NULL,
                granted_by  TEXT,
                granted_at  INTEGER NOT NULL,
                PRIMARY KEY (session_id, editor_user)
            )
        """)
    if not _table_exists(conn, "report_web_visitor"):
        conn.execute("""
            CREATE TABLE report_web_visitor (
                user_id    TEXT PRIMARY KEY,
                first_seen INTEGER NOT NULL,
                last_seen  INTEGER NOT NULL
            )
        """)
    if not _table_exists(conn, "report_user_important"):
        conn.execute("""
            CREATE TABLE report_user_important (
                user_id    TEXT NOT NULL,
                session_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, session_id)
            )
        """)
    # 웹 로그인 계정 (사번 + PIN 4자리). SCHEMA 에는 있으나 구 DB 에는 없을 수 있어
    # 여기서도 생성한다 — 일반 브라우저 로그인이 이 테이블을 사용한다.
    if not _table_exists(conn, "report_user"):
        conn.execute("""
            CREATE TABLE report_user (
                user_id       TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at    INTEGER NOT NULL
            )
        """)

    _migrate_product_type_names(conn)


def init_report_db():
    REPORT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(REPORT_DB_PATH) as conn:
        # PRAGMA 를 DDL 앞에 건다 — busy_timeout 없이 ALTER/CREATE INDEX 가 돌면 부팅 시
        # 다른 커넥션과 겹칠 때 대기 없이 "database is locked" 로 실패한다. journal_mode 는
        # 트랜잭션 중 변경 불가라 첫 DML 이전이 유일하게 안전한 위치.
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        _migrate(conn)
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(REPORT_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    # synchronous/temp_store 는 커넥션 단위 설정이라 init_report_db 만으로는 적용되지 않는다
    # (WAL 은 DB 파일 영속). 미설정 시 요청 커넥션이 synchronous=FULL 로 동작해 쓰기가 느려짐.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── analysis lock ─────────────────────────────────────────────────────────────

def try_acquire_analysis_lock(analysis_key, owner):
    now = _now()
    expires = now + REPORT_LOCK_TTL_SEC
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM report_analysis_lock WHERE expires_at <= ?", (now,)
        )
        try:
            conn.execute(
                "INSERT INTO report_analysis_lock "
                "(analysis_key, owner, locked_at, expires_at) VALUES (?, ?, ?, ?)",
                (analysis_key, owner, now, expires),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def release_analysis_lock(analysis_key, owner):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM report_analysis_lock WHERE analysis_key=? AND owner=?",
            (analysis_key, owner),
        )

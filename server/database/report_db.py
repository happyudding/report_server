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
    process       TEXT,
    product       TEXT,
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
    mode          TEXT DEFAULT 'Normal'
);
CREATE INDEX IF NOT EXISTS idx_report_session_analysis_key
    ON report_session(analysis_key);
CREATE INDEX IF NOT EXISTS idx_report_session_status_created
    ON report_session(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_session_product_type
    ON report_session(product_type);

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
"""

_SUMMARY_COLUMNS = (
    "analysis_key", "session_id", "item_name", "bin_number",
    "yield_percent", "fail_count", "cpk_val", "mean_val", "stdev_val",
    "lsl", "usl", "unit", "created_at",
)

_PRODUCT_TYPE_NAMES = ("MDDI", "PDDI", "PMIC", "SECURITY", "TCON")


def _now():
    return int(time.time())


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
            "product_type", "process", "product", "revision", "edm_link",
            "dataset_id", "lot_id", "password", "uploaded_by", "client_host",
            "webreport_options",
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

    _migrate_product_type_names(conn)


def init_report_db():
    REPORT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(REPORT_DB_PATH) as conn:
        _migrate(conn)
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA busy_timeout = 5000")


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


def _row(row):
    return None if row is None else dict(row)


# ── session ─────────────────────────────────────────────────────────────────

def create_session(session_id, file_name, file_path, product_type=None, dataset_id=None,
                   lot_id=None, password=None, is_debug=0, product=None,
                   process=None, revision=None, edm_link=None, source='xlsx_upload',
                   uploaded_by=None, client_host=None, mode='Normal'):
    now = _now()
    file_path_str = str(file_path) if file_path is not None else None
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_session "
            "(session_id, file_name, file_path, product_type, process, product, revision, "
            " edm_link, dataset_id, lot_id, password, is_debug, source, uploaded_by, client_host, "
            " mode, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (session_id, file_name, file_path_str, product_type, process, product, revision,
             edm_link, dataset_id, lot_id, password, is_debug, source, uploaded_by, client_host,
             mode or 'Normal', now, now),
        )


_SESSION_UPDATABLE = {"analysis_key", "content_hash", "status", "error_message", "file_path",
                      "is_important", "is_private", "webreport_options"}


def delete_session(session_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM report_annotation WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM report_session WHERE session_id=?", (session_id,))


def count_sessions_for_analysis_key(analysis_key, exclude_session_id=None):
    """analysis_key 를 참조하는 세션 수. 삭제 시 산출물 공유 여부 판단용.

    동일 데이터 재업로드는 같은 analysis_key 를 공유하므로, 산출물(S3/로컬 파일·메타 행)은
    마지막 참조 세션을 지울 때만 정리해야 한다.
    """
    with get_conn() as conn:
        if exclude_session_id:
            row = conn.execute(
                "SELECT COUNT(*) FROM report_session WHERE analysis_key=? AND session_id<>?",
                (analysis_key, exclude_session_id)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM report_session WHERE analysis_key=?",
                (analysis_key,)).fetchone()
    return int(row[0])


def delete_analysis_rows(analysis_key):
    """analysis_key 에 매달린 산출물 메타 행 삭제 (마지막 참조 세션 삭제 시에만 호출).

    report_audit_log 는 의도적으로 보존한다 (삭제 이력 추적용 메타 스냅샷 포함).
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM report_object_info WHERE analysis_key=?", (analysis_key,))
        conn.execute("DELETE FROM report_analysis_summary WHERE analysis_key=?", (analysis_key,))
        conn.execute("DELETE FROM report_sheet_data WHERE analysis_key=?", (analysis_key,))
        conn.execute("DELETE FROM report_csv_files WHERE analysis_key=?", (analysis_key,))


def update_session(session_id, **fields):
    fields = {k: v for k, v in fields.items() if k in _SESSION_UPDATABLE}
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    params = list(fields.values()) + [_now(), session_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE report_session SET {cols} WHERE session_id=?", params)


def get_session(session_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM report_session WHERE session_id=?", (session_id,)
        ).fetchone()
    return _row(row)


def _history_where(product_type=None, process=None, product=None, revision=None,
                   lot_id=None, source=None):
    """get_history / count_history 공용 WHERE 절 + 파라미터."""
    conditions = ["s.status IN ('done', 'reused')"]
    params = []
    if product_type:
        conditions.append("s.product_type = ?")
        params.append(product_type)
    if process:
        conditions.append("s.process = ?")
        params.append(process)
    if product:
        conditions.append("s.product = ?")
        params.append(product)
    if revision:
        conditions.append("s.revision = ?")
        params.append(revision)
    if lot_id:
        conditions.append("s.lot_id LIKE ?")
        params.append(f"%{lot_id}%")
    if source:
        conditions.append("s.source = ?")
        params.append(source)
    return " AND ".join(conditions), params


def get_history(product_type=None, process=None, product=None, revision=None, lot_id=None,
                source=None, limit=500, offset=0):
    where, params = _history_where(product_type, process, product, revision, lot_id, source)
    params.extend([limit, offset])
    # session_id 를 마지막 정렬키로 두어 offset 페이지 간 순서가 안정되게 한다
    sql = f"""
        SELECT s.session_id, s.file_name, s.product_type, s.process, s.product,
               s.revision, s.edm_link, s.lot_id, s.created_at, s.status, s.dataset_id,
               s.is_debug, s.source, s.uploaded_by, s.client_host,
               COALESCE(s.mode, 'Normal') AS mode,
               COALESCE(s.is_important, 0) AS is_important,
               COALESCE(s.is_private, 0) AS is_private,
               CASE WHEN s.password IS NOT NULL THEN 1 ELSE 0 END AS has_password,
               COALESCE(SUM(c.file_size), 0) AS total_file_size
        FROM report_session s
        LEFT JOIN report_csv_files c ON c.analysis_key = s.analysis_key
        WHERE {where}
        GROUP BY s.session_id
        ORDER BY COALESCE(s.is_important, 0) DESC, s.created_at DESC, s.session_id
        LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_history(product_type=None, process=None, product=None, revision=None,
                  lot_id=None, source=None):
    """get_history 와 동일 필터의 전체 세션 수 (서버 페이지네이션 total 용)."""
    where, params = _history_where(product_type, process, product, revision, lot_id, source)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM report_session s WHERE {where}", params).fetchone()
    return int(row[0]) if row else 0


# ── retention / cleanup ───────────────────────────────────────────────────────

def get_expired_sessions(cutoff_epoch):
    """created_at 이 cutoff 이전이고 중요표시가 없는 세션. 자동정리 대상.

    전역 is_important(legacy) 또는 사용자별 개인 중요표시(report_user_important)가
    하나라도 있으면 보존한다 — 누군가 중요하다고 표시한 데이터는 지우지 않는다."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, analysis_key, product_type, product, lot_id, "
            "       file_name, created_at "
            "FROM report_session "
            "WHERE created_at < ? AND COALESCE(is_important, 0) = 0 "
            "  AND status IN ('done', 'reused') "
            "  AND session_id NOT IN (SELECT session_id FROM report_user_important) "
            "ORDER BY created_at ASC",
            (cutoff_epoch,),
        ).fetchall()
    return [dict(r) for r in rows]


# 참고: analysis_key 공유 판단 count_sessions_for_analysis_key / 공유행 삭제
# delete_analysis_rows 는 이미 위에 정의돼 있어 자동정리도 그대로 재사용한다.


# ── audit log ─────────────────────────────────────────────────────────────────

_AUDIT_COLUMNS = (
    "action", "session_id", "analysis_key", "product_type", "product",
    "lot_id", "file_name", "changed_fields", "client_ip", "user_agent",
    "client_user", "client_host", "result", "created_at",
)


def log_audit(action, session_id=None, analysis_key=None, product_type=None,
              product=None, lot_id=None, file_name=None, changed_fields=None,
              client_ip=None, user_agent=None, client_user=None, client_host=None,
              result="ok"):
    """업로드/수정/삭제 감사 기록 1행 추가. user_agent 는 과도하게 길면 잘라 저장."""
    if user_agent and len(user_agent) > 500:
        user_agent = user_agent[:500]
    values = (
        action, session_id, analysis_key, product_type, product,
        lot_id, file_name, changed_fields, client_ip, user_agent,
        client_user, client_host, result, _now(),
    )
    placeholders = ", ".join("?" for _ in _AUDIT_COLUMNS)
    cols = ", ".join(_AUDIT_COLUMNS)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO report_audit_log ({cols}) VALUES ({placeholders})",
            values,
        )


def get_audit_logs(action=None, session_id=None, q=None, limit=200, offset=0):
    """감사 로그 조회. action/session_id 필터 + q(파일명/product/lot_id 부분일치)."""
    conditions = []
    params = []
    if action:
        conditions.append("action = ?")
        params.append(action)
    if session_id:
        conditions.append("session_id = ?")
        params.append(session_id)
    if q:
        conditions.append("(file_name LIKE ? OR product LIKE ? OR lot_id LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    params.extend([limit, offset])
    sql = f"""
        SELECT id, action, session_id, analysis_key, product_type, product,
               lot_id, file_name, changed_fields, client_ip, user_agent,
               client_user, client_host, result, created_at
        FROM report_audit_log
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def purge_audit_logs(cutoff_epoch):
    """created_at 이 cutoff 이전인 감사 로그 행 삭제 (롤오프). 삭제 행 수 반환.

    report_audit_log 는 세션 삭제 시에도 의도적으로 보존되어 무한 증가하므로,
    cleanup 스케줄러가 REPORT_AUDIT_RETENTION_DAYS 기준으로 주기 호출한다.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM report_audit_log WHERE created_at < ?", (int(cutoff_epoch),))
        return cur.rowcount


# ── user favorites (검색결과 즐겨찾기, ID 별) ─────────────────────────────────

def get_user_favorites(user_id):
    """user_id 의 즐겨찾기 session_id 목록. 삭제된 세션의 잔존 행은 조회에서 무해."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id FROM report_user_favorite "
            "WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [r["session_id"] for r in rows]


def set_user_favorite(user_id, session_id, favorite):
    """즐겨찾기 on/off. INSERT OR IGNORE 로 중복 토글에도 안전."""
    with get_conn() as conn:
        if favorite:
            conn.execute(
                "INSERT OR IGNORE INTO report_user_favorite "
                "(user_id, session_id, created_at) VALUES (?, ?, ?)",
                (user_id, session_id, _now()),
            )
        else:
            conn.execute(
                "DELETE FROM report_user_favorite WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            )


# ── 세션 편집 권한 위임 (업로더 → 특정 사용자) ────────────────────────────────

def list_session_editors(session_id):
    """세션에 편집 권한을 위임받은 사용자 목록. 최근 부여순."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT editor_user, granted_by, granted_at FROM report_session_editor "
            "WHERE session_id=? ORDER BY granted_at DESC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def is_session_editor(session_id, user_id):
    """user_id 가 이 세션의 위임 편집자인지."""
    if not user_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM report_session_editor WHERE session_id=? AND editor_user=?",
            (session_id, user_id),
        ).fetchone()
    return row is not None


def add_session_editor(session_id, editor_user, granted_by):
    """편집 권한 부여. INSERT OR IGNORE 로 중복 부여에도 안전."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO report_session_editor "
            "(session_id, editor_user, granted_by, granted_at) VALUES (?, ?, ?, ?)",
            (session_id, editor_user, granted_by, _now()),
        )


def remove_session_editor(session_id, editor_user):
    """편집 권한 회수."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM report_session_editor WHERE session_id=? AND editor_user=?",
            (session_id, editor_user),
        )


# ── web_report 방문자 (편집자 후보 풀) ────────────────────────────────────────

def record_web_visitor(user_id):
    """web_report 세션을 연 Honey 사용자 기록 (UPSERT). first_seen 유지, last_seen 갱신."""
    if not user_id:
        return
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_web_visitor (user_id, first_seen, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
            (user_id, now, now),
        )


def search_web_visitors(q="", limit=50):
    """방문자 검색. q 가 있으면 부분일치, 없으면 최근 방문순 전체. user_id 목록 반환."""
    q = (q or "").strip().lower()
    with get_conn() as conn:
        if q:
            rows = conn.execute(
                "SELECT user_id FROM report_web_visitor WHERE user_id LIKE ? "
                "ORDER BY last_seen DESC LIMIT ?",
                (f"%{q}%", int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT user_id FROM report_web_visitor ORDER BY last_seen DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    return [r["user_id"] for r in rows]


# ── 사용자별 개인 중요표시 (전역 is_important 와 별개) ────────────────────────

def is_user_important(user_id, session_id):
    """user_id 가 이 세션을 개인 중요표시했는지."""
    if not user_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM report_user_important WHERE user_id=? AND session_id=?",
            (user_id, session_id),
        ).fetchone()
    return row is not None


def set_user_important(user_id, session_id, important):
    """개인 중요표시 on/off. INSERT OR IGNORE 로 중복 토글에도 안전."""
    with get_conn() as conn:
        if important:
            conn.execute(
                "INSERT OR IGNORE INTO report_user_important "
                "(user_id, session_id, created_at) VALUES (?, ?, ?)",
                (user_id, session_id, _now()),
            )
        else:
            conn.execute(
                "DELETE FROM report_user_important WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            )


# ── users (웹 로그인 계정) ────────────────────────────────────────────────────

def get_user(user_id):
    """로그인 계정 조회. 없으면 None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, password_hash, created_at FROM report_user WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return _row(row)


def create_user(user_id, password_hash):
    """계정 생성 (첫 로그인 시 초기 비밀번호로 자동 가입). 동시 생성 경합은 IGNORE 로 무해."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO report_user (user_id, password_hash, created_at) "
            "VALUES (?, ?, ?)",
            (user_id, password_hash, _now()),
        )


def update_user_password(user_id, password_hash):
    """비밀번호 변경. 계정이 없으면 no-op (rowcount 0)."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE report_user SET password_hash=? WHERE user_id=?",
            (password_hash, user_id),
        )
        return cur.rowcount


def get_session_by_dataset_id(dataset_id):
    """dataset_id 로 가장 최근 세션 1건과 총 CSV 크기를 함께 반환."""
    sql = """
        SELECT s.session_id, s.file_name, s.product_type, s.process, s.product,
               s.revision, s.edm_link, s.lot_id, s.created_at, s.status, s.dataset_id, s.analysis_key,
               COALESCE(SUM(c.file_size), 0) AS total_file_size
        FROM report_session s
        LEFT JOIN report_csv_files c ON c.analysis_key = s.analysis_key
        WHERE s.dataset_id = ?
        GROUP BY s.session_id
        ORDER BY s.created_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (dataset_id,)).fetchone()
    return _row(row)


def get_session_path_by_analysis_key(analysis_key):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT file_path FROM report_session "
            "WHERE analysis_key=? AND file_path IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 1",
            (analysis_key,),
        ).fetchone()
    return row["file_path"] if row else None


# ── summary ──────────────────────────────────────────────────────────────────

def get_summary_by_analysis_key(analysis_key):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT item_name, bin_number, yield_percent, fail_count, cpk_val, "
            "mean_val, stdev_val, lsl, usl, unit "
            "FROM report_analysis_summary WHERE analysis_key=? "
            "ORDER BY item_name, bin_number IS NULL DESC, bin_number",
            (analysis_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def has_summary(analysis_key):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM report_analysis_summary WHERE analysis_key=? LIMIT 1",
            (analysis_key,),
        ).fetchone()
    return row is not None


def save_summary_batch(analysis_key, session_id, rows):
    if not rows:
        return 0
    now = _now()
    payload = [
        (
            analysis_key, session_id,
            r["item_name"], r.get("bin_number"),
            r.get("yield_percent"), r.get("fail_count"), r.get("cpk_val"),
            r.get("mean_val"), r.get("stdev_val"), r.get("lsl"), r.get("usl"),
            r.get("unit"), now,
        )
        for r in rows
    ]
    placeholders = ",".join(["?"] * len(_SUMMARY_COLUMNS))
    cols = ",".join(_SUMMARY_COLUMNS)
    with get_conn() as conn:
        conn.executemany(
            f"INSERT OR IGNORE INTO report_analysis_summary ({cols}) VALUES ({placeholders})",
            payload,
        )
    return len(payload)


# ── object_info ──────────────────────────────────────────────────────────────

def get_object_info(analysis_key, object_type="plotly"):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM report_object_info WHERE analysis_key=? AND object_type=?",
            (analysis_key, object_type),
        ).fetchone()
    return _row(row)


def get_all_object_infos(analysis_key):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM report_object_info WHERE analysis_key=? ORDER BY object_type",
            (analysis_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_object_info(analysis_key, content_hash, options_json,
                       object_type, bucket, key, uri):
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_object_info "
            "(analysis_key, object_type, content_hash, options_json, "
            " s3_bucket, s3_key, s3_uri, created_at, last_accessed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(analysis_key, object_type) DO UPDATE SET "
            "  content_hash=excluded.content_hash, "
            "  options_json=excluded.options_json, "
            "  s3_bucket=excluded.s3_bucket, "
            "  s3_key=excluded.s3_key, "
            "  s3_uri=excluded.s3_uri, "
            "  last_accessed=excluded.last_accessed",
            (analysis_key, object_type, content_hash, options_json,
             bucket, key, uri, now, now),
        )


def touch_object_info(analysis_key, object_type="plotly"):
    with get_conn() as conn:
        conn.execute(
            "UPDATE report_object_info SET last_accessed=? "
            "WHERE analysis_key=? AND object_type=?",
            (_now(), analysis_key, object_type),
        )


# ── lock ─────────────────────────────────────────────────────────────────────

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


# ── csv files ─────────────────────────────────────────────────────────────────

def upsert_csv_file(analysis_key, filename, s3_key, s3_uri, file_size):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_csv_files "
            "(analysis_key, filename, s3_key, s3_uri, file_size, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(analysis_key, filename) DO UPDATE SET "
            "  s3_key=excluded.s3_key, s3_uri=excluded.s3_uri, "
            "  file_size=excluded.file_size, uploaded_at=excluded.uploaded_at",
            (analysis_key, filename, s3_key, s3_uri, file_size, _now()),
        )


def get_csv_files(analysis_key):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT filename, s3_key, s3_uri, file_size, uploaded_at "
            "FROM report_csv_files WHERE analysis_key=? ORDER BY filename",
            (analysis_key,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── annotations ──────────────────────────────────────────────────────────────

def create_annotation(session_id, analysis_key, target, content):
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO report_annotation "
            "(session_id, analysis_key, target, content, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, analysis_key, target, content, now, now),
        )
        return cur.lastrowid


def get_annotations(session_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, session_id, analysis_key, target, content, created_at, updated_at "
            "FROM report_annotation WHERE session_id=? ORDER BY created_at",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── sheet_data (순수 텍스트 데이터 캐시) ─────────────────────────────────────

def upsert_sheet_data(analysis_key: str, sheet_name: str, data) -> None:
    """data(dict|list) → JSON 직렬화해 upsert. 스타일 없는 셀 텍스트 데이터."""
    import json
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_sheet_data (analysis_key, sheet_name, data_json, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(analysis_key, sheet_name) DO UPDATE SET "
            "  data_json=excluded.data_json, updated_at=excluded.updated_at",
            (analysis_key, sheet_name, json.dumps(data, ensure_ascii=False), _now()),
        )


def get_sheet_data(analysis_key: str, sheet_name: str):
    """없으면 None. JSON 역직렬화해 반환."""
    import json
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data_json FROM report_sheet_data "
            "WHERE analysis_key=? AND sheet_name=?",
            (analysis_key, sheet_name),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


def get_all_sheet_data(analysis_key: str) -> dict:
    """{'summary':..., 'yield':..., 'issue_table':...} 존재하는 것만."""
    import json
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sheet_name, data_json FROM report_sheet_data WHERE analysis_key=?",
            (analysis_key,),
        ).fetchall()
    result = {}
    for row in rows:
        try:
            result[row[0]] = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def update_annotation(annotation_id, content):
    with get_conn() as conn:
        conn.execute(
            "UPDATE report_annotation SET content=?, updated_at=? WHERE id=?",
            (content, _now(), annotation_id),
        )


def delete_annotation(annotation_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM report_annotation WHERE id=?", (annotation_id,))


# ── dashboard comments (Dash UI 편집 셀 저장소) ────────────────────────────────

def get_dashboard_comments(dataset_id, kind):
    """`(dataset_id, kind)` 에 속한 모든 행을 `{item_key: value}` 로 반환.
    value 가 JSON 인 경우 호출 측에서 파싱."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT item_key, value FROM report_dashboard_comment "
            "WHERE dataset_id=? AND kind=?",
            (dataset_id, kind),
        ).fetchall()
    return {r["item_key"]: r["value"] for r in rows}


def replace_dashboard_comments(dataset_id, kind, items):
    """`(dataset_id, kind)` 의 모든 행을 `items` 로 치환 (DELETE + INSERT)."""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM report_dashboard_comment WHERE dataset_id=? AND kind=?",
            (dataset_id, kind),
        )
        if items:
            payload = [
                (dataset_id, kind, str(k), str(v), now)
                for k, v in items.items()
                if v not in (None, "")
            ]
            if payload:
                conn.executemany(
                    "INSERT INTO report_dashboard_comment "
                    "(dataset_id, kind, item_key, value, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    payload,
                )

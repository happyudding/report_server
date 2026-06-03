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
    dataset_id    TEXT,
    lot_id        TEXT,
    password      TEXT,
    is_debug      INTEGER DEFAULT 0,
    source        TEXT DEFAULT 'xlsx_upload'
);
CREATE INDEX IF NOT EXISTS idx_report_session_analysis_key
    ON report_session(analysis_key);

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
    result         TEXT DEFAULT 'ok',    -- 'ok' | 'fail'
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_audit_created_at
    ON report_audit_log(created_at);
"""

_SUMMARY_COLUMNS = (
    "analysis_key", "session_id", "item_name", "bin_number",
    "yield_percent", "fail_count", "cpk_val", "mean_val", "stdev_val",
    "lsl", "usl", "unit", "created_at",
)


def _now():
    return int(time.time())


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


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
            "product_type", "process", "product", "revision",
            "dataset_id", "lot_id", "password",
        ):
            if col not in sess_cols:
                conn.execute(f"ALTER TABLE report_session ADD COLUMN {col} TEXT")
        if "is_debug" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN is_debug INTEGER DEFAULT 0")
        if "source" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN source TEXT DEFAULT 'xlsx_upload'")

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
                   source='xlsx_upload'):
    now = _now()
    file_path_str = str(file_path) if file_path is not None else None
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_session "
            "(session_id, file_name, file_path, product_type, product, dataset_id, lot_id, "
            " password, is_debug, source, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (session_id, file_name, file_path_str, product_type, product, dataset_id, lot_id,
             password, is_debug, source, now, now),
        )


_SESSION_UPDATABLE = {"analysis_key", "content_hash", "status", "error_message", "file_path"}


def delete_session(session_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM report_annotation WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM report_session WHERE session_id=?", (session_id,))


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


def get_history(product_type=None, process=None, product=None, revision=None, lot_id=None,
                source=None, limit=500):
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
    where = " AND ".join(conditions)
    params.append(limit)
    sql = f"""
        SELECT s.session_id, s.file_name, s.product_type, s.process, s.product,
               s.revision, s.lot_id, s.created_at, s.status, s.dataset_id,
               s.is_debug, s.source,
               CASE WHEN s.password IS NOT NULL THEN 1 ELSE 0 END AS has_password,
               COALESCE(SUM(c.file_size), 0) AS total_file_size
        FROM report_session s
        LEFT JOIN report_csv_files c ON c.analysis_key = s.analysis_key
        WHERE {where}
        GROUP BY s.session_id
        ORDER BY s.created_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── audit log ─────────────────────────────────────────────────────────────────

_AUDIT_COLUMNS = (
    "action", "session_id", "analysis_key", "product_type", "product",
    "lot_id", "file_name", "changed_fields", "client_ip", "user_agent",
    "result", "created_at",
)


def log_audit(action, session_id=None, analysis_key=None, product_type=None,
              product=None, lot_id=None, file_name=None, changed_fields=None,
              client_ip=None, user_agent=None, result="ok"):
    """업로드/수정/삭제 감사 기록 1행 추가. user_agent 는 과도하게 길면 잘라 저장."""
    if user_agent and len(user_agent) > 500:
        user_agent = user_agent[:500]
    values = (
        action, session_id, analysis_key, product_type, product,
        lot_id, file_name, changed_fields, client_ip, user_agent,
        result, _now(),
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
               result, created_at
        FROM report_audit_log
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_session_by_dataset_id(dataset_id):
    """dataset_id 로 가장 최근 세션 1건과 총 CSV 크기를 함께 반환."""
    sql = """
        SELECT s.session_id, s.file_name, s.product_type, s.process, s.product,
               s.revision, s.lot_id, s.created_at, s.status, s.dataset_id, s.analysis_key,
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


def replace_summary_batch(analysis_key, session_id, rows):
    """analysis_key 의 summary 행 전체를 rows 로 치환 (DELETE + INSERT).
    수정(edit) 모드에서 yield 표를 통째로 다시 저장할 때 사용."""
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
        conn.execute(
            "DELETE FROM report_analysis_summary WHERE analysis_key=?", (analysis_key,)
        )
        if payload:
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

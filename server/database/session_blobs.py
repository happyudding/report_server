"""세션 단위 큰 본문의 **포인터** 테이블 (report_session_blob) + analysis 원본 상태
(report_analysis) + 마이그레이션 진행 기록 (report_schema_migration).
(report_db facade 구현 — 2026-08-14 세션 DB 개선)

본문 자체는 storage_gateway 의 session blob 백엔드(S3 또는 로컬 spool)에 있고, 여기에는
어디에 무엇이 있는지와 무결성 검증에 필요한 값만 둔다. 조회·조인 대상이 아니므로 본문을
DB 에서 빼도 잃는 것이 없다 — 반대로 두면 10MB TEXT 를 쓰는 동안 DB 전역 쓰기가 막힌다.
"""
from .core import _now, get_conn


# ── report_session_blob ──────────────────────────────────────────────────────

def get_session_blob(session_id, kind):
    """포인터 1건 (없으면 None). 본문은 읽지 않는다."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_id, kind, backend, object_key, content_hash, base_token, "
            "       size_bytes, content_encoding, format_version, updated_at, updated_by "
            "FROM report_session_blob WHERE session_id=? AND kind=?",
            (session_id, kind)).fetchone()
    return dict(row) if row else None


def list_session_blobs(session_id):
    """세션의 포인터 전부 (세션 삭제 훅이 (backend, object_key) 를 얻는 용도)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, kind, backend, object_key, content_hash, base_token, "
            "       size_bytes, content_encoding, format_version, updated_at, updated_by "
            "FROM report_session_blob WHERE session_id=?", (session_id,)).fetchall()
    return [dict(r) for r in rows]


def upsert_session_blob(session_id, kind, *, backend, object_key, content_hash,
                        base_token=None, size_bytes=0, content_encoding="gzip",
                        format_version=1, updated_by=None, conn=None):
    """포인터 upsert. conn 을 받으면 그 트랜잭션 안에서 실행한다(Note 저장이 본문·포인터·
    legacy 행·rev 를 한 트랜잭션으로 묶기 때문)."""
    args = (session_id, kind, backend, object_key, content_hash, base_token,
            int(size_bytes or 0), content_encoding, int(format_version or 1),
            _now(), updated_by)
    sql = (
        "INSERT INTO report_session_blob "
        "(session_id, kind, backend, object_key, content_hash, base_token, size_bytes, "
        " content_encoding, format_version, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id, kind) DO UPDATE SET "
        "  backend=excluded.backend, object_key=excluded.object_key, "
        "  content_hash=excluded.content_hash, base_token=excluded.base_token, "
        "  size_bytes=excluded.size_bytes, content_encoding=excluded.content_encoding, "
        "  format_version=excluded.format_version, updated_at=excluded.updated_at, "
        "  updated_by=excluded.updated_by")
    if conn is not None:
        conn.execute(sql, args)
        return
    with get_conn() as c:
        c.execute(sql, args)


def delete_session_blob_row(session_id, kind, conn=None):
    sql = "DELETE FROM report_session_blob WHERE session_id=? AND kind=?"
    if conn is not None:
        conn.execute(sql, (session_id, kind))
        return
    with get_conn() as c:
        c.execute(sql, (session_id, kind))


def list_pending_session_blobs(limit=200):
    """S3 이관 대기(local_pending) 목록 — cleanup 재시도 + 관리자 경고 배지."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, kind, object_key, size_bytes, updated_at "
            "FROM report_session_blob WHERE backend='local_pending' "
            "ORDER BY updated_at LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def count_pending_session_blobs():
    """(건수, 바이트 합) — 관리자 화면 경고."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS b "
            "FROM report_session_blob WHERE backend='local_pending'").fetchone()
    return (int(row["n"]), int(row["b"])) if row else (0, 0)


def mark_session_blob_backend(session_id, kind, backend):
    with get_conn() as conn:
        conn.execute(
            "UPDATE report_session_blob SET backend=? WHERE session_id=? AND kind=?",
            (backend, session_id, kind))


# ── report_analysis ──────────────────────────────────────────────────────────

def get_analysis(analysis_key):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM report_analysis WHERE analysis_key=?", (analysis_key,)
        ).fetchone()
    return dict(row) if row else None


def upsert_analysis(analysis_key, *, content_hash, source=None, source_count=0,
                    artifact_status="ok"):
    """analysis 원본 상태 upsert (authoritative content_hash 의 진실)."""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_analysis "
            "(analysis_key, content_hash, source, source_count, artifact_status, "
            " created_at, updated_at, last_access_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(analysis_key) DO UPDATE SET "
            "  content_hash=excluded.content_hash, "
            "  source=COALESCE(excluded.source, report_analysis.source), "
            "  source_count=MAX(excluded.source_count, report_analysis.source_count), "
            "  artifact_status=excluded.artifact_status, updated_at=excluded.updated_at",
            (analysis_key, content_hash, source, int(source_count or 0),
             artifact_status, now, now, now))


def touch_analysis(analysis_key):
    """last_access_at 갱신 (best-effort — 실패해도 조회를 막지 않는다)."""
    with get_conn() as conn:
        conn.execute("UPDATE report_analysis SET last_access_at=? WHERE analysis_key=?",
                     (_now(), analysis_key))


def delete_analysis(analysis_key):
    with get_conn() as conn:
        conn.execute("DELETE FROM report_analysis WHERE analysis_key=?", (analysis_key,))


# ── report_schema_migration ──────────────────────────────────────────────────

def get_migration_step(step):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT step, status, cursor, detail, updated_at "
            "FROM report_schema_migration WHERE step=?", (step,)).fetchone()
    return dict(row) if row else None


def set_migration_step(step, status, cursor=None, detail=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_schema_migration (step, status, cursor, detail, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(step) DO UPDATE SET status=excluded.status, "
            "  cursor=excluded.cursor, detail=excluded.detail, "
            "  updated_at=excluded.updated_at",
            (step, status, cursor, detail, _now()))


def list_migration_steps():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT step, status, cursor, detail, updated_at "
            "FROM report_schema_migration ORDER BY step").fetchall()
    return [dict(r) for r in rows]

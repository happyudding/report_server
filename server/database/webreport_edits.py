"""web_report 편집 상태 (세션 단위 — web_report/edits.py 가 소비)
(report_db facade 구현)."""
from .core import get_conn, _now


def get_webreport_edit_rev(session_id):
    """세션 편집 rev (없으면 0). 캐시 키의 무효화 토큰."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rev FROM report_webreport_edit_rev WHERE session_id=?",
            (session_id,)).fetchone()
    return int(row["rev"]) if row else 0


def get_webreport_edits(session_id):
    """세션의 편집행 전부 [(kind, item_key, value)] — rowid(삽입) 순서 보존."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT kind, item_key, value FROM report_webreport_edit "
            "WHERE session_id=? ORDER BY rowid",
            (session_id,)).fetchall()
    return [dict(r) for r in rows]


def apply_webreport_edits(session_id, changes, updated_by=None):
    """changes: [(kind, item_key, value|None)] — None 은 삭제. 단일 트랜잭션으로
    적용하고 rev 를 1 증가시킨다 (빈 changes 는 no-op, rev 유지). 새 rev 반환.

    upsert 는 UPDATE 경로에서 rowid 를 유지하므로 etc_item 표시 순서가 보존된다."""
    if not changes:
        return get_webreport_edit_rev(session_id)
    now = _now()
    with get_conn() as conn:
        for kind, item_key, value in changes:
            if value is None:
                conn.execute(
                    "DELETE FROM report_webreport_edit "
                    "WHERE session_id=? AND kind=? AND item_key=?",
                    (session_id, kind, item_key))
            else:
                conn.execute(
                    "INSERT INTO report_webreport_edit "
                    "(session_id, kind, item_key, value, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id, kind, item_key) DO UPDATE SET "
                    "  value=excluded.value, updated_at=excluded.updated_at, "
                    "  updated_by=excluded.updated_by",
                    (session_id, kind, item_key, str(value), now, updated_by))
        conn.execute(
            "INSERT INTO report_webreport_edit_rev (session_id, rev) VALUES (?, 1) "
            "ON CONFLICT(session_id) DO UPDATE SET rev=rev+1",
            (session_id,))
        row = conn.execute(
            "SELECT rev FROM report_webreport_edit_rev WHERE session_id=?",
            (session_id,)).fetchone()
        return int(row["rev"]) if row else 0

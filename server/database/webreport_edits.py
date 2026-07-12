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


def get_webreport_edits(session_id, kinds=None, exclude_kinds=None):
    """세션의 편집행 [(kind, item_key, value, updated_at, updated_by)] — rowid(삽입) 순서 보존.

    kinds: 지정 시 해당 kind 만 조회. exclude_kinds: 지정 시 해당 kind 제외 —
    대용량 값(note_sheet 시트 JSON 등)을 표 상태 조회가 매번 끌어오지 않게 한다
    (web_report/edits.py 가 소비). 기본(둘 다 None)은 종전과 동일하게 전부."""
    sql = ("SELECT kind, item_key, value, updated_at, updated_by "
           "FROM report_webreport_edit WHERE session_id=?")
    params = [session_id]
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        params.extend(kinds)
    if exclude_kinds:
        sql += " AND kind NOT IN (%s)" % ",".join("?" * len(exclude_kinds))
        params.extend(exclude_kinds)
    sql += " ORDER BY rowid"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_webreport_edit_meta(session_id, kind):
    """kind 의 편집행 메타만 [(item_key, updated_at, updated_by)] — value 를 읽지 않는다.

    note_sheet(최대 2MB) 존재 여부/최종 수정자를 /full extras 가 매 요청 조회하는 용도."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT item_key, updated_at, updated_by FROM report_webreport_edit "
            "WHERE session_id=? AND kind=? ORDER BY rowid",
            (session_id, kind)).fetchall()
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

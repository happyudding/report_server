"""주석(report_annotation) + Dash 대시보드 편집 셀(report_dashboard_comment)
(report_db facade 구현)."""
from .core import get_conn, _now


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

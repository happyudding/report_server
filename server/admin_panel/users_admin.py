"""관리자용 웹 로그인 계정(report_user) 관리 — 목록 · 비밀번호 초기화/설정 · 삭제.

report_db.py 는 수정하지 않는다 (sessions_admin 과 동일 방침). 목록·삭제는 get_conn()
자체 SQL, 비밀번호 변경은 report_db.update_user_password 를 재사용한다. 비밀번호 원문/해시는
절대 노출하지 않는다.
"""
from werkzeug.security import generate_password_hash

from database import report_db

_DEFAULT_PIN = "0000"


def list_users(q=None, limit=200, offset=0):
    """계정 목록. user_id · 생성일 · 즐겨찾기 수 · 업로드 세션 수(uploaded_by 꼬리 일치)."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    where, params = "1=1", []
    if q:
        where += " AND u.user_id LIKE ?"
        params.append(f"%{q}%")

    with report_db.get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM report_user u WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"""
            SELECT u.user_id, u.created_at,
                   (SELECT COUNT(*) FROM report_user_favorite f
                     WHERE f.user_id = u.user_id) AS fav_count,
                   (SELECT COUNT(*) FROM report_session s
                     WHERE lower(
                       CASE WHEN instr(s.uploaded_by, '\\') > 0
                            THEN substr(s.uploaded_by, instr(s.uploaded_by, '\\') + 1)
                            ELSE s.uploaded_by END) = u.user_id) AS upload_count
            FROM report_user u
            WHERE {where}
            ORDER BY u.user_id
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
    return {"total": int(total), "limit": limit, "offset": offset,
            "rows": [dict(r) for r in rows]}


def reset_password(user_id):
    """비밀번호를 초기값 0000 으로 되돌린다. 계정 없으면 rowcount 0."""
    return report_db.update_user_password(user_id, generate_password_hash(_DEFAULT_PIN)) > 0


def set_password(user_id, password):
    """지정한 4자리로 설정 (형식 검증은 라우트에서). 계정 없으면 rowcount 0."""
    return report_db.update_user_password(user_id, generate_password_hash(password)) > 0


def delete_user(user_id):
    """계정 삭제 + 해당 계정의 즐겨찾기도 정리. 삭제된 계정 수 반환."""
    with report_db.get_conn() as conn:
        conn.execute("DELETE FROM report_user_favorite WHERE user_id=?", (user_id,))
        cur = conn.execute("DELETE FROM report_user WHERE user_id=?", (user_id,))
        return cur.rowcount > 0

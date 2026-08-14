"""관리자용 웹 로그인 계정(report_user) 관리 — 목록 · 비밀번호 초기화/설정 · 삭제.

report_db.py 는 수정하지 않는다 (sessions_admin 과 동일 방침). 목록·삭제는 get_conn()
자체 SQL, 비밀번호 변경은 report_db.update_user_password 를 재사용한다. 비밀번호 원문/해시는
절대 노출하지 않는다.
"""
from werkzeug.security import generate_password_hash

from database import report_db
from identity_norm import normalize_uid

_DEFAULT_PIN = "0000"


def attach_names(rows, *keys):
    """행 목록에 실명 `name` 을 붙인다 (관리자 화면의 '이름(ID)' 표기용).

    관리자 표들은 신원을 저마다 다른 컬럼명으로 담고 있어(client_user·who·user·user_id)
    키를 가변인자로 받는다. 앞 키가 비면 다음 키로 폴백한다(감사로그의
    client_user → resolved_user). 'ip:1.2.3.4' 같은 무신원 키는 매칭이 없어 빈 이름이 된다.
    조회는 display_names 배치 한 번 — 행마다 조회하면 200행 표에서 N+1 이 된다."""
    if not rows:
        return rows

    def pick(r):
        for k in keys:
            v = normalize_uid(r.get(k))
            if v:
                return v
        return ""

    names = report_db.display_names([pick(r) for r in rows])
    for r in rows:
        uid = pick(r)
        r["name"] = names.get(uid, "")
        # 화면이 '이름(ID)' 로 그릴 때 쓰는 정규화된 ID. 감사로그처럼 원문 컬럼
        # (client_user='SECDS\\HGD123')을 그대로 보여주면 같은 사람이 표기별로 갈라져
        # 보이므로, 표시용 ID 를 따로 실어 보낸다 (원문 컬럼은 감사 근거라 남겨 둔다).
        r["uid"] = uid
    return rows


def list_users(q=None, limit=200, offset=0):
    """계정 목록. user_id · 실명 · 생성일 · 즐겨찾기 수 · 업로드 세션 수(uploaded_by 꼬리 일치).
    검색어 q 는 ID 와 실명 둘 다에 매칭한다 (화면 표기가 '이름(ID)' 이므로)."""
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
        where += " AND (u.user_id LIKE ? OR p.display_name LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]

    join = "LEFT JOIN report_user_profile p ON p.user_id = u.user_id"
    with report_db.get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM report_user u {join} WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"""
            SELECT u.user_id, u.created_at, p.display_name,
                   (SELECT COUNT(*) FROM report_user_favorite f
                     WHERE f.user_id = u.user_id) AS fav_count,
                   (SELECT COUNT(*) FROM report_session s
                     WHERE lower(
                       CASE WHEN instr(s.uploaded_by, '\\') > 0
                            THEN substr(s.uploaded_by, instr(s.uploaded_by, '\\') + 1)
                            ELSE s.uploaded_by END) = u.user_id) AS upload_count
            FROM report_user u {join}
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


def set_display_name(user_id, display_name, admin_user=""):
    """관리자가 사용자 실명을 지정 (오타·개명 대응). 로그인 계정이 없는 uid 여도 저장된다
    — 프로필은 report_user 와 별개 테이블이다. 형식 검증은 라우트 책임."""
    report_db.set_display_name(user_id, display_name, f"admin:{admin_user}" if admin_user else "admin")
    return True


def delete_user(user_id):
    """계정 삭제 + 해당 계정의 즐겨찾기도 정리. 삭제된 계정 수 반환.
    실명(report_user_profile)은 **지우지 않는다** — 로그인 계정이 없어져도 그 사람은 Honey
    사용자로 남고, 감사로그·권한 창의 과거 표기가 ID 로 되돌아가면 안 되기 때문."""
    with report_db.get_conn() as conn:
        conn.execute("DELETE FROM report_user_favorite WHERE user_id=?", (user_id,))
        cur = conn.execute("DELETE FROM report_user WHERE user_id=?", (user_id,))
        return cur.rowcount > 0

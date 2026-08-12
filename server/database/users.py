"""사용자 부속 상태: 즐겨찾기 / 편집 권한 위임 / 방문자 풀 / 개인 중요표시 /
(폐지된 ID·PW 로그인 계정 — 보존만) (report_db facade 구현)."""
from .core import get_conn, _now, _row


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
    """편집자 후보 검색. q 가 있으면 부분일치, 없으면 최근순 전체. user_id 목록 반환.

    후보 풀은 **web_report 방문자(report_web_visitor) ∪ 웹 로그인 계정(report_user)** 이다.
    방문자만 보면 갓 가입한 계정은 web_report 세션을 한 번 열기 전까지 후보에 뜨지 않아
    "가입했는데 권한을 못 준다"가 된다 — 가입(=report_user INSERT) 즉시 후보가 되도록
    두 테이블을 합친다. 정렬 기준 시각은 방문자=last_seen, 계정=created_at."""
    q = (q or "").strip().lower()
    sql = """
        SELECT user_id, MAX(ts) AS ts FROM (
            SELECT user_id, last_seen  AS ts FROM report_web_visitor
            UNION ALL
            SELECT user_id, created_at AS ts FROM report_user
        )
        {where}
        GROUP BY user_id ORDER BY ts DESC LIMIT ?
    """
    with get_conn() as conn:
        if q:
            rows = conn.execute(
                sql.format(where="WHERE user_id LIKE ?"), (f"%{q}%", int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(sql.format(where=""), (int(limit),)).fetchall()
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


# ── users (웹 로그인 계정 — ID/PW 로그인 폐지로 미사용, 보존만) ───────────────

def get_user(user_id):
    """로그인 계정 조회. 없으면 None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, password_hash, created_at FROM report_user WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return _row(row)


def create_user(user_id, password_hash):
    """계정 생성 (첫 로그인 시 초기 비밀번호로 자동 가입). 동시 생성 경합은 IGNORE 로 무해.
    실제로 삽입됐으면 1, 이미 있어서 무시됐으면 0 (웹 회원가입의 경합 판정용)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO report_user (user_id, password_hash, created_at) "
            "VALUES (?, ?, ?)",
            (user_id, password_hash, _now()),
        )
        return cur.rowcount


def has_honey_history(user_id):
    """이 계정으로 Honey 를 쓴 적이 있는지 — 업로드(uploaded_by 꼬리 일치) 또는
    web_report 방문(report_web_visitor). 웹 자유가입의 계정 선점 차단 판정용:
    이력이 있으면 본인은 Honey 에서 비밀번호를 설정하면 되므로 웹 가입을 막는다."""
    if not user_id:
        return False
    with get_conn() as conn:
        row = conn.execute("""
            SELECT 1 FROM report_session
             WHERE lower(
                   CASE WHEN instr(uploaded_by, '\\') > 0
                        THEN substr(uploaded_by, instr(uploaded_by, '\\') + 1)
                        ELSE uploaded_by END) = ?
             LIMIT 1
        """, (user_id,)).fetchone()
        if row:
            return True
        row = conn.execute(
            "SELECT 1 FROM report_web_visitor WHERE user_id=?", (user_id,)).fetchone()
    return bool(row)


def update_user_password(user_id, password_hash):
    """비밀번호 변경. 계정이 없으면 no-op (rowcount 0)."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE report_user SET password_hash=? WHERE user_id=?",
            (password_hash, user_id),
        )
        return cur.rowcount

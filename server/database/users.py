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
    """web_report 세션을 연 Honey 사용자 기록 (UPSERT). first_seen 유지, last_seen 갱신.

    busy_timeout 을 기본(5초)이 아니라 150ms 로 잡는다 — 이 쓰기는 세션을 열 때마다
    (/my_access) 일어나는 best-effort 기록이고 호출부가 예외를 삼킨다. 잠금이 붐빌 때
    5초를 기다리면 그만큼 세션 열기가 통째로 멈추는데, 정작 얻는 것은 편집자 후보
    목록의 last_seen 한 칸이다. 잠깐 못 쓰면 다음 조회에서 다시 기록된다."""
    if not user_id:
        return
    now = _now()
    with get_conn(busy_timeout_ms=150) as conn:
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
    두 테이블을 합친다. 정렬 기준 시각은 방문자=last_seen, 계정=created_at.

    q 는 ID 뿐 아니라 **실명(report_user_profile.display_name)에도 매칭**한다 — 권한 부여 창이
    '이름(ID)' 로 표시되므로 눈에 보이는 이름으로 검색이 안 되면 찾을 수가 없다.
    반환은 계속 user_id 문자열 목록이다(호출부 무변경 — 이름은 display_names 로 따로 붙인다)."""
    q = (q or "").strip().lower()
    sql = """
        SELECT v.user_id AS user_id, MAX(v.ts) AS ts FROM (
            SELECT user_id, last_seen  AS ts FROM report_web_visitor
            UNION ALL
            SELECT user_id, created_at AS ts FROM report_user
        ) v
        LEFT JOIN report_user_profile p ON p.user_id = v.user_id
        {where}
        GROUP BY v.user_id ORDER BY ts DESC LIMIT ?
    """
    with get_conn() as conn:
        if q:
            rows = conn.execute(
                sql.format(where="WHERE v.user_id LIKE ? OR lower(p.display_name) LIKE ?"),
                (f"%{q}%", f"%{q}%", int(limit)),
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


# ── 사용자 실명 (report_user_profile — 표시 전용) ─────────────────────────────
# 로그인 계정(report_user)과 분리한 이유는 core.py SCHEMA 주석 참조: Honey 전용 사용자도
# 이름을 가져야 한다. 관리자가 로그인 계정을 삭제해도(delete_user) 프로필은 남긴다 —
# 계정이 없어져도 그 사람은 Honey 사용자로 계속 남기 때문.

def get_display_name(user_id):
    """사용자 실명. 미등록이면 None."""
    if not user_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT display_name FROM report_user_profile WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return row["display_name"] if row else None


def set_display_name(user_id, display_name, updated_by="self"):
    """실명 등록/변경 (UPSERT). 형식 검증은 호출부(라우트) 책임."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_user_profile (user_id, display_name, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (user_id, display_name, _now(), updated_by),
        )


def display_names(user_ids):
    """{user_id: 실명} 배치 조회 — 목록 화면이 행마다 조회(N+1)하지 않도록 하는 유일한 경로.
    미등록 사용자는 키 자체가 없다(프런트가 이름 없으면 ID 만 표시). 소문자 정규화된 키로
    조회하며, SQLite 파라미터 상한(999)을 넘지 않게 나눠 던진다."""
    uids = sorted({(u or "").strip().lower() for u in (user_ids or []) if u})
    if not uids:
        return {}
    out = {}
    with get_conn() as conn:
        for i in range(0, len(uids), 900):
            chunk = uids[i:i + 900]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT user_id, display_name FROM report_user_profile "
                f"WHERE user_id IN ({marks})", chunk,
            ).fetchall()
            for r in rows:
                out[r["user_id"]] = r["display_name"]
    return out

"""VOC 게시판 DB — 세션 DB(report.db)와 분리된 별도 SQLite 파일.

config.REPORT_VOC_DB_PATH 파일에 자체 커넥션으로 연결한다 (core.get_conn 미사용 —
eval_export.open_conn 과 같은 별도 파일 패턴). 스키마는 커넥션 오픈 시점에
executescript 로 멱등 생성한다. 이미지 바이트는 여기 저장하지 않는다(메타만) —
실파일은 storage_gateway note_image 백엔드의 voc_<id> 네임스페이스.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_voc (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT    NOT NULL,
    category   TEXT    NOT NULL,
    title      TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'open',
    guest_token TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_voc_created
    ON report_voc(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS report_voc_image (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    voc_id       INTEGER NOT NULL REFERENCES report_voc(id) ON DELETE CASCADE,
    image_id     TEXT    NOT NULL,
    content_type TEXT    NOT NULL,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    UNIQUE(voc_id, image_id)
);
CREATE INDEX IF NOT EXISTS idx_report_voc_image_voc
    ON report_voc_image(voc_id, sort_order);

CREATE TABLE IF NOT EXISTS report_voc_comment (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    voc_id     INTEGER NOT NULL REFERENCES report_voc(id) ON DELETE CASCADE,
    user_id    TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    guest_token TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_voc_comment_voc
    ON report_voc_comment(voc_id, id);
"""

STATUSES = ("open", "close")   # 신규 등록은 항상 'open', 'close' 전환은 관리자만

# 스키마/마이그레이션을 끝낸 DB 경로 — 커넥션마다 재실행하지 않기 위한 캐시.
# bool 이 아니라 경로 집합인 이유: 테스트가 런타임에 REPORT_VOC_DB_PATH 를 바꿔 끼운다.
# 락은 두지 않는다 — 겹쳐 실행돼도 스키마가 멱등이고 _migrate 가 duplicate column 을
# 무시하므로 이중 초기화는 무해하다.
_initialized_paths = set()


def _now():
    return int(time.time())


def db_path() -> Path:
    import config  # server/ 가 sys.path 에 있음
    return Path(config.REPORT_VOC_DB_PATH)


# 구 voc.db 보정용 (테이블, 컬럼, ALTER 문). 신규 DB 는 위 SCHEMA 가 이미 만든다.
_ADDED_COLUMNS = (
    ("report_voc", "status",
     "ALTER TABLE report_voc ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"),
    ("report_voc", "guest_token",
     "ALTER TABLE report_voc ADD COLUMN guest_token TEXT"),
    ("report_voc_comment", "guest_token",
     "ALTER TABLE report_voc_comment ADD COLUMN guest_token TEXT"),
)


def _migrate(conn):
    """뒤늦게 추가된 컬럼이 없는 구 voc.db 를 멱등 보정한다.

    Flask 는 waitress 단일 프로세스라 경합은 스레드 수준뿐이고, 겹쳐 실행되면 뒤늦은
    쪽이 duplicate column 오류를 받으므로 그 경우만 무시한다."""
    for table, column, ddl in _ADDED_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols or column in cols:
            continue
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def _like_escape(text):
    """LIKE 패턴 특수문자 이스케이프 (질의에 ESCAPE '\\' 를 함께 붙인다)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_where(q):
    """검색어 → (WHERE 조각, 파라미터). 숫자면 번호(id) 일치도 함께 본다."""
    q = (q or "").strip()
    if not q:
        return "", []
    like = f"%{_like_escape(q)}%"
    if q.isdigit() and len(q) <= 18:          # 18자리 초과는 SQLite INTEGER 범위 밖
        return " WHERE (id = ? OR title LIKE ? ESCAPE '\\')", [int(q), like]
    return " WHERE title LIKE ? ESCAPE '\\'", [like]


@contextmanager
def open_conn():
    """voc.db 커넥션 (+스키마 멱등 생성). yield 후 commit, finally close."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")  # report_voc_image CASCADE 용
        key = str(path)
        if key not in _initialized_paths:
            conn.executescript(SCHEMA)
            _migrate(conn)
            _initialized_paths.add(key)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_voc(user_id, category, title, content, guest_token=None):
    """VOC 1건 등록 → 새 id 반환.

    guest_token: Honey 신원 없이 이름만 적고 쓴 글의 브라우저 소유 증표(없으면 None).
    user_id 는 Honey 계정이거나 게스트가 입력한 이름이다."""
    with open_conn() as conn:
        cur = conn.execute(
            "INSERT INTO report_voc"
            " (user_id, category, title, content, guest_token, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, category, title, content, guest_token, _now()),
        )
        return cur.lastrowid


def add_voc_images(voc_id, images):
    """이미지 메타 일괄 등록. images = [(image_id, content_type, sort_order), ...]"""
    now = _now()
    with open_conn() as conn:
        conn.executemany(
            "INSERT INTO report_voc_image"
            " (voc_id, image_id, content_type, sort_order, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(voc_id, iid, ctype, order, now) for iid, ctype, order in images],
        )


def list_voc(limit=20, offset=0, q=None):
    """최신순 VOC 목록 + 댓글 수. (items, total) 반환.

    목록은 본문·이미지를 싣지 않는다(상세에서만 조회). q 는 제목 부분일치이며
    숫자면 번호(id) 일치도 함께 본다."""
    where, params = _search_where(q)
    with open_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM report_voc{where}", params).fetchone()[0]
        rows = conn.execute(
            "SELECT id, user_id, category, title, status, created_at,"
            " guest_token IS NOT NULL AS is_guest,"   # 토큰 자체는 노출하지 않는다
            " (SELECT COUNT(*) FROM report_voc_comment c WHERE c.voc_id = report_voc.id)"
            " AS comment_count"
            f" FROM report_voc{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows], total


def get_voc(voc_id):
    """VOC 1건. guest_token 을 포함하므로 응답에 그대로 실지 말 것
    (routes_voc._public_voc 가 걷어내고 is_guest 로 바꾼다)."""
    with open_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, category, title, content, status, guest_token, created_at"
            " FROM report_voc WHERE id = ?", (voc_id,)).fetchone()
        return dict(row) if row else None


def list_voc_images(voc_id):
    """해당 VOC 의 이미지 메타 (sort_order 순) — 상세 조회용."""
    with open_conn() as conn:
        rows = conn.execute(
            "SELECT image_id, content_type, sort_order FROM report_voc_image"
            " WHERE voc_id = ? ORDER BY sort_order", (voc_id,)).fetchall()
        return [dict(r) for r in rows]


def update_voc(voc_id, category, title, content):
    """본문 수정 (작성자 전용 — 스크린샷은 대상 외). 수정됐으면 True."""
    with open_conn() as conn:
        cur = conn.execute(
            "UPDATE report_voc SET category = ?, title = ?, content = ? WHERE id = ?",
            (category, title, content, voc_id))
        return cur.rowcount > 0


def set_voc_status(voc_id, status):
    """처리 상태 변경 (관리자 전용). 변경됐으면 True."""
    with open_conn() as conn:
        cur = conn.execute("UPDATE report_voc SET status = ? WHERE id = ?",
                           (status, voc_id))
        return cur.rowcount > 0


def get_voc_image(voc_id, image_id):
    """해당 VOC 에 등록된 이미지 메타 (소속 확인 — 타 VOC 이미지는 None)."""
    with open_conn() as conn:
        row = conn.execute(
            "SELECT voc_id, image_id, content_type, sort_order"
            " FROM report_voc_image WHERE voc_id = ? AND image_id = ?",
            (voc_id, image_id)).fetchone()
        return dict(row) if row else None


def delete_voc(voc_id):
    """VOC 하드 삭제 (이미지·댓글 메타는 FK CASCADE). 삭제됐으면 True."""
    with open_conn() as conn:
        cur = conn.execute("DELETE FROM report_voc WHERE id = ?", (voc_id,))
        return cur.rowcount > 0


def add_comment(voc_id, user_id, content, guest_token=None):
    """댓글 1건 등록 → 새 id 반환. guest_token 은 create_voc 와 같은 의미."""
    with open_conn() as conn:
        cur = conn.execute(
            "INSERT INTO report_voc_comment"
            " (voc_id, user_id, content, guest_token, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (voc_id, user_id, content, guest_token, _now()),
        )
        return cur.lastrowid


def list_comments(voc_id):
    """해당 VOC 의 댓글 (작성순 = id 오름차순).

    guest_token 을 포함하므로 응답에 그대로 실지 말 것 (routes_voc._public_row)."""
    with open_conn() as conn:
        rows = conn.execute(
            "SELECT id, user_id, content, guest_token, created_at"
            " FROM report_voc_comment WHERE voc_id = ? ORDER BY id",
            (voc_id,)).fetchall()
        return [dict(r) for r in rows]


def get_comment(voc_id, comment_id):
    """해당 VOC 에 달린 댓글 (소속 확인 — 타 VOC 댓글은 None).

    guest_token 을 포함한다 — 권한 확인 전용이며 응답에 싣지 않는다."""
    with open_conn() as conn:
        row = conn.execute(
            "SELECT id, voc_id, user_id, content, guest_token, created_at"
            " FROM report_voc_comment WHERE voc_id = ? AND id = ?",
            (voc_id, comment_id)).fetchone()
        return dict(row) if row else None


def delete_comment(comment_id):
    """댓글 삭제. 삭제됐으면 True."""
    with open_conn() as conn:
        cur = conn.execute("DELETE FROM report_voc_comment WHERE id = ?", (comment_id,))
        return cur.rowcount > 0

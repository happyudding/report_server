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
"""


def _now():
    return int(time.time())


def db_path() -> Path:
    import config  # server/ 가 sys.path 에 있음
    return Path(config.REPORT_VOC_DB_PATH)


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
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_voc(user_id, category, title, content):
    """VOC 1건 등록 → 새 id 반환."""
    with open_conn() as conn:
        cur = conn.execute(
            "INSERT INTO report_voc (user_id, category, title, content, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, category, title, content, _now()),
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


def list_voc(limit=20, offset=0):
    """최신순 VOC 목록 + 각 건의 이미지 메타. (items, total) 반환."""
    with open_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM report_voc").fetchone()[0]
        rows = conn.execute(
            "SELECT id, user_id, category, title, content, created_at"
            " FROM report_voc ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        items = [dict(r) for r in rows]
        if items:
            ids = [it["id"] for it in items]
            placeholders = ",".join("?" for _ in ids)
            img_rows = conn.execute(
                f"SELECT voc_id, image_id, content_type, sort_order"
                f" FROM report_voc_image WHERE voc_id IN ({placeholders})"
                f" ORDER BY voc_id, sort_order",
                ids,
            ).fetchall()
            by_voc = {}
            for r in img_rows:
                by_voc.setdefault(r["voc_id"], []).append(
                    {"image_id": r["image_id"], "content_type": r["content_type"],
                     "sort_order": r["sort_order"]})
            for it in items:
                it["images"] = by_voc.get(it["id"], [])
        return items, total


def get_voc(voc_id):
    with open_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, category, title, content, created_at"
            " FROM report_voc WHERE id = ?", (voc_id,)).fetchone()
        return dict(row) if row else None


def get_voc_image(voc_id, image_id):
    """해당 VOC 에 등록된 이미지 메타 (소속 확인 — 타 VOC 이미지는 None)."""
    with open_conn() as conn:
        row = conn.execute(
            "SELECT voc_id, image_id, content_type, sort_order"
            " FROM report_voc_image WHERE voc_id = ? AND image_id = ?",
            (voc_id, image_id)).fetchone()
        return dict(row) if row else None


def delete_voc(voc_id):
    """VOC 하드 삭제 (이미지 메타는 FK CASCADE). 삭제됐으면 True."""
    with open_conn() as conn:
        cur = conn.execute("DELETE FROM report_voc WHERE id = ?", (voc_id,))
        return cur.rowcount > 0

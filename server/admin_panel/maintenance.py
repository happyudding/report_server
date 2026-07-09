"""DB 컨트롤 — 백업 즉시 실행/목록 · cleanup 수동 실행 · 진단 · 감사로그 CSV · 로그 tail.

백업/cleanup 은 기존 db_backup.run_backup / report_cleanup.run_cleanup 을 그대로 호출.
데몬 스케줄러와 겹치지 않게 non-blocking Lock 으로 감싸고 실행 중이면 busy 를 돌려준다.
"""
import csv
import io
import logging
import sqlite3
import threading
from pathlib import Path

import config
import db_backup
import report_cleanup
from database import report_db

_log = logging.getLogger(__name__)

_backup_lock = threading.Lock()
_cleanup_lock = threading.Lock()


class Busy(RuntimeError):
    """같은 작업이 이미 실행 중 (HTTP 409 로 매핑)."""


def backup_now():
    if not _backup_lock.acquire(blocking=False):
        raise Busy("백업이 이미 실행 중입니다.")
    try:
        dest = db_backup.run_backup()
        return {"ok": True, "file": dest.name, "bytes": dest.stat().st_size}
    finally:
        _backup_lock.release()


def list_backups():
    backup_dir = Path(config.REPORT_DB_BACKUP_DIR)
    if not backup_dir.exists():
        return {"dir": str(backup_dir), "rows": []}
    rows = []
    for p in sorted(backup_dir.glob("report_*.db"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        rows.append({"name": p.name, "bytes": st.st_size, "mtime": int(st.st_mtime)})
    return {"dir": str(backup_dir), "keep": config.REPORT_DB_BACKUP_KEEP, "rows": rows}


def cleanup_now(dry_run):
    if not _cleanup_lock.acquire(blocking=False):
        raise Busy("cleanup 이 이미 실행 중입니다.")
    try:
        summary = report_cleanup.run_cleanup(dry_run=bool(dry_run))
        summary["retention_days"] = config.REPORT_RETENTION_DAYS
        return summary
    finally:
        _cleanup_lock.release()


def diagnostics(full=False):
    """DB 파일 크기 + integrity(quick_check 기본, full=1 이면 integrity_check)
    + 테이블별 행 수. 둘 다 읽기 전용이라 WAL 에서 안전."""
    db = Path(config.REPORT_DB_PATH)
    out = {
        "db_path": str(db),
        "db_file": db.stat().st_size if db.exists() else 0,
        "db_wal": (db.with_name(db.name + "-wal").stat().st_size
                   if db.with_name(db.name + "-wal").exists() else 0),
        "check_kind": "integrity_check" if full else "quick_check",
    }
    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        pragma = "integrity_check" if full else "quick_check"
        rows = conn.execute(f"PRAGMA {pragma}").fetchall()
        out["check_result"] = [r[0] for r in rows][:20]
        out["check_ok"] = bool(rows) and rows[0][0] == "ok"
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out["tables"] = [
            {"name": t, "rows": conn.execute(
                f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]}
            for t in tables
        ]
    finally:
        conn.close()
    return out


# ── 감사로그 CSV ─────────────────────────────────────────────────────────────

_CSV_COLUMNS = ("created_at", "action", "result", "product_type", "product", "lot_id",
                "file_name", "changed_fields", "client_user", "client_host",
                "client_ip", "user_agent", "session_id", "analysis_key")
_CSV_CHUNK = 1000       # get_audit_logs limit 상한이 1000
_CSV_MAX_ROWS = 100000  # 폭주 방지 상한


def audit_csv_iter(action=None, q=None):
    """CSV 행 generator. 첫 청크에 UTF-8 BOM(Excel 한글 대응) + 헤더."""
    def _line(values):
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerow(values)
        return buf.getvalue()

    yield "\ufeff" + _line(_CSV_COLUMNS)
    offset = 0
    while offset < _CSV_MAX_ROWS:
        rows = report_db.get_audit_logs(action=action, q=q,
                                        limit=_CSV_CHUNK, offset=offset)
        if not rows:
            break
        for r in rows:
            yield _line([r.get(c) if r.get(c) is not None else "" for c in _CSV_COLUMNS])
        if len(rows) < _CSV_CHUNK:
            break
        offset += _CSV_CHUNK


# ── 서버 로그 tail ───────────────────────────────────────────────────────────

def log_tail(nbytes=65536):
    """server/log/server_*.txt 중 최신 파일 꼬리를 텍스트로 반환."""
    log_dir = config.ROOT_DIR / "server" / "log"
    try:
        nbytes = max(1024, min(int(nbytes), 1024 * 1024))
    except (TypeError, ValueError):
        nbytes = 65536
    files = sorted(log_dir.glob("server_*.txt"),
                   key=lambda p: p.stat().st_mtime, reverse=True) if log_dir.exists() else []
    if not files:
        return {"file": None, "text": "(로그 파일 없음: %s)" % log_dir}
    target = files[0]
    size = target.stat().st_size
    with open(target, "rb") as f:
        if size > nbytes:
            f.seek(-nbytes, 2)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    if size > nbytes:  # 잘린 첫 줄 제거
        text = text.split("\n", 1)[-1]
    return {"file": target.name, "size": size, "text": text}

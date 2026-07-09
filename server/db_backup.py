"""report.db 주기 백업 스케줄러.

WAL 모드에서는 파일 단순 복사(shutil.copy)가 -wal 미반영/파손 위험이 있으므로
반드시 sqlite3 backup API(src.backup(dst))로 온라인 백업한다 (백업 중 쓰기는
API 가 페이지 단위로 재시도 처리). 백업본에 integrity_check 를 돌려 로그로 남기고,
REPORT_DB_BACKUP_KEEP 초과분은 report_*.db 글롭만 오래된 순으로 삭제한다.

스케줄러 패턴은 report_cleanup.py 와 동일 (daemon 스레드, _started 중복 방지).
"""
import logging
import sqlite3
import threading
import time

import config

_log = logging.getLogger(__name__)
_started = False


def _maintain_wal(conn):
    """백업 사이클에 얹는 원본 DB 유지보수 (best-effort).

    - wal_checkpoint(TRUNCATE): WAL 페이지를 본 파일로 반영하고 -wal 파일을 0 으로
      잘라 -wal 무한 증가를 막는다 (기본 auto-checkpoint 는 truncate 하지 않음).
    - PRAGMA optimize: 쿼리 패턴 기반 통계 갱신 (sqlite 권장 주기 실행).
    실패해도 백업 자체를 막지 않는다. VACUUM 은 장시간 잠금이라 자동 실행하지 않는다
    (필요 시 수동 — README 참조).
    """
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA optimize")
        _log.info("[db-backup] wal_checkpoint(TRUNCATE) + optimize done")
    except Exception:
        _log.exception("[db-backup] wal maintenance failed")


def run_backup():
    """백업 1회 수행 (+ 원본 WAL checkpoint/optimize). 백업 파일 경로 반환 (실패 시 예외)."""
    backup_dir = config.REPORT_DB_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"report_{time.strftime('%Y%m%d_%H%M%S')}.db"

    src = sqlite3.connect(config.REPORT_DB_PATH)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
            row = dst.execute("PRAGMA integrity_check").fetchone()
            ok = bool(row) and row[0] == "ok"
        finally:
            dst.close()
        _maintain_wal(src)
    finally:
        src.close()

    if ok:
        _log.info("[db-backup] ok: %s", dest)
    else:
        _log.error("[db-backup] integrity_check failed: %s", dest)

    # 보존 개수 초과분 정리 — 다른 파일 오삭 방지를 위해 report_*.db 만 대상
    keep = max(1, int(config.REPORT_DB_BACKUP_KEEP))
    backups = sorted(backup_dir.glob("report_*.db"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-keep]:
        try:
            old.unlink()
            _log.info("[db-backup] pruned: %s", old)
        except Exception:
            _log.exception("[db-backup] prune failed: %s", old)
    return dest


def start_backup_scheduler():
    """daemon 스레드로 주기 백업 시작. 중복 기동은 _started 플래그로 방지."""
    global _started
    if _started:
        return
    if not config.REPORT_DB_BACKUP_ENABLED:
        _log.info("[db-backup] disabled (REPORT_DB_BACKUP_ENABLED=0)")
        return
    _started = True
    interval = max(0.1, float(config.REPORT_DB_BACKUP_INTERVAL_HOURS)) * 3600

    def _loop():
        time.sleep(60)  # 서버 초기화와 겹치지 않게 첫 실행만 잠깐 지연
        while True:
            try:
                run_backup()
            except Exception:
                _log.exception("[db-backup] run_backup crashed")
            time.sleep(interval)

    threading.Thread(target=_loop, name="report-db-backup", daemon=True).start()
    _log.info("[db-backup] scheduler started: interval=%.1fh keep=%d dir=%s",
              config.REPORT_DB_BACKUP_INTERVAL_HOURS, config.REPORT_DB_BACKUP_KEEP,
              config.REPORT_DB_BACKUP_DIR)

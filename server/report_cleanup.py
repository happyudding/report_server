"""오래된 세션 자동정리 스케줄러.

created_at 이 REPORT_RETENTION_DAYS(기본 180일) 이전이고 중요표시(is_important)가
아닌 세션을 주기적으로 삭제한다. 세션 삭제 라우트(report_routes.delete_session_route)와
동일한 산출물 정리 경로(storage_gateway.delete_report_artifacts +
report_db.delete_analysis_rows)를 재사용하며, 같은 analysis_key 를 참조하는 다른 세션이
남아있으면(특히 중요표시 세션) 산출물을 보존한다.

REPORT_CLEANUP_DRYRUN=True(기본) 이면 실제 삭제 없이 대상만 로그/감사로그에 남긴다.
실삭제하려면 REPORT_CLEANUP_DRYRUN=0 으로 명시해야 한다.
"""
import logging
import re
import threading
import time
from pathlib import Path

import config
from database import report_db

_log = logging.getLogger(__name__)
_started = False

_AKEY_RE = re.compile(r"^[0-9a-f]{64}$")


def _log_audit(session, result):
    """정리 이력을 report_audit_log 에 기록 (best-effort). 백그라운드 스레드라
    Flask request 컨텍스트가 없으므로 client_ip/user_agent 는 고정값을 넣는다."""
    try:
        report_db.log_audit(
            "delete",
            session_id=session.get("session_id"),
            analysis_key=session.get("analysis_key"),
            product_type=session.get("product_type"),
            product=session.get("product"),
            lot_id=session.get("lot_id"),
            file_name=session.get("file_name"),
            changed_fields=None,
            client_ip="system",
            user_agent="cleanup-scheduler",
            result=result,
        )
    except Exception:
        pass


def _remove_rawedit_backups(akey):
    """Raw Data 편집 백업(webreport_backup/<akey>/) 정리 — 마지막 참조가 사라질 때.

    storage_gateway.delete_report_artifacts 는 이 디렉토리를 모른다(백업은 web_report
    rawedit 소유). best-effort."""
    try:
        from web_report import rawedit
        if rawedit.remove_backups(akey, Path(config.REPORT_UPLOAD_DIR)):
            _log.info("[cleanup] rawedit backup removed: %s", akey)
    except Exception:
        _log.exception("[cleanup] rawedit backup cleanup failed for %s", akey)


def _cleanup_one(session, dry_run):
    """만료 세션 1건 처리. 실제 삭제했으면 True 반환."""
    sid = session["session_id"]
    akey = session.get("analysis_key")
    # 이 세션을 제외하고 같은 analysis_key 를 쓰는 세션이 없으면(=마지막 참조) 산출물 정리.
    last_ref = bool(akey) and report_db.count_sessions_for_analysis_key(
        akey, exclude_session_id=sid) == 0

    if dry_run:
        _log.info("[cleanup:dry-run] would delete session=%s akey=%s artifacts=%s",
                  sid, akey, last_ref)
        _log_audit(session, "dryrun")
        return False

    if last_ref:
        try:
            import storage_gateway
            result = storage_gateway.delete_report_artifacts(
                akey, upload_root=Path(config.REPORT_UPLOAD_DIR))
            for warning in result.get("warnings", []):
                _log.warning("cleanup artifact (%s): %s", akey, warning)
            report_db.delete_analysis_rows(akey)
        except Exception:
            _log.exception("cleanup artifact failed for analysis_key %s", akey)
        # 캐시 무효화는 부가 작업 — 실패해도 위 산출물/DB 정리를 되돌리지 않는다.
        try:
            from web_report import service as web_report_service
            web_report_service.invalidate_caches(akey)
        except Exception:
            _log.exception("cleanup cache invalidate failed for %s", akey)
        _remove_rawedit_backups(akey)
    report_db.delete_session(sid)
    _log_audit(session, "ok")
    _log.info("[cleanup] deleted session=%s akey=%s last_ref=%s", sid, akey, last_ref)
    return True


def _purge_audit_logs():
    """감사 로그 롤오프 (REPORT_AUDIT_RETENTION_DAYS 이전 행 삭제). 삭제 행 수 반환.

    세션 cleanup 의 dry-run 과 무관하게 동작한다 — 감사 행 삭제는 산출물 파괴가 아니고,
    비활성(0 이하)으로 두면 무한 증가하기 때문. 실패해도 세션 정리를 막지 않는다.
    """
    days = int(getattr(config, "REPORT_AUDIT_RETENTION_DAYS", 0) or 0)
    if days <= 0:
        return 0
    cutoff = int(time.time()) - days * 86400
    try:
        purged = report_db.purge_audit_logs(cutoff)
        if purged:
            _log.info("[cleanup] purged %d audit rows older than %d days", purged, days)
        return purged
    except Exception:
        _log.exception("[cleanup] audit purge failed")
        return 0


def _purge_fs_orphans(dry_run):
    """DB 참조 없는 uploads/web_report/<akey>/ 고아 산출물 회수. 대상 건수 반환.

    ingest 는 저장(파일/S3) 후 세션행을 만들므로(web_report/ingest.py) 그 사이 실패하면
    산출물이 세션행 없이 남고, 기존 orphan pending 회수(DB 행 기준)로는 발견되지 않는다.
    세션이 하나도 참조하지 않는 akey 디렉터리를 48h 유예(진행 중 ingest 보호) 후
    세션 삭제와 동일 경로(delete_report_artifacts + delete_analysis_rows)로 정리한다.
    로컬 디렉터리 스캔 기준이라 S3 단독 고아는 대상 밖(관리자 스토리지 탭과 동일 정책)."""
    root = Path(config.REPORT_UPLOAD_DIR) / "web_report"
    if not root.is_dir():
        return 0
    cutoff = time.time() - 48 * 3600
    found = 0
    for entry in root.iterdir():
        try:
            akey = entry.name
            if not entry.is_dir() or not _AKEY_RE.match(akey):
                continue
            if entry.stat().st_mtime > cutoff:
                continue
            if report_db.count_sessions_for_analysis_key(akey) > 0:
                continue
            found += 1
            if dry_run:
                _log.info("[cleanup:dry-run] would purge orphan artifacts akey=%s", akey)
                continue
            import storage_gateway
            result = storage_gateway.delete_report_artifacts(
                akey, upload_root=Path(config.REPORT_UPLOAD_DIR))
            for warning in result.get("warnings", []):
                _log.warning("orphan purge (%s): %s", akey, warning)
            report_db.delete_analysis_rows(akey)
            try:
                from web_report import service as web_report_service
                web_report_service.invalidate_caches(akey)
            except Exception:
                _log.exception("orphan purge cache invalidate failed for %s", akey)
            _remove_rawedit_backups(akey)
            _log.info("[cleanup] purged orphan artifacts akey=%s", akey)
        except Exception:
            _log.exception("[cleanup] orphan artifact purge failed for %s", entry)
    return found


def run_cleanup(dry_run=None):
    """ingest 크래시 잔존물 회수 + 휴지통 경과분 purge + 감사 로그 롤오프.
    dry_run 미지정 시 config 기본값 사용.
    {'scanned','deleted','dry_run','audit_purged','fs_orphans','trash_scanned','trash_purged'}
    요약 반환.

    6개월 만료 세션은 더 이상 삭제하지 않는다 — 데이터 유실 방지를 위해 report_tiering 이
    산출물만 S3 로 아카이브하고(세션/DB 는 유지) 종전 retention 삭제를 대체한다.
    여기서는 산출물 참조가 없는 orphan pending(48h) 세션 행과, 사용자가 삭제해 휴지통에
    들어간 뒤 REPORT_TRASH_RETENTION_DAYS(기본 30일)가 지난 세션을 회수한다."""
    if dry_run is None:
        dry_run = config.REPORT_CLEANUP_DRYRUN
    audit_purged = _purge_audit_logs()

    # ingest 크래시 잔존물(status='pending'·analysis_key 없음) — 48h 지나면 회수.
    # analysis_key 가 없어 산출물 참조도 없으므로 세션 행만 지워진다.
    try:
        orphans = report_db.get_orphan_pending_sessions(int(time.time()) - 48 * 3600)
    except Exception:
        orphans = []
        _log.exception("[cleanup] get_orphan_pending_sessions failed")

    deleted = 0
    for session in orphans:
        try:
            if _cleanup_one(session, dry_run):
                deleted += 1
        except Exception:
            _log.exception("[cleanup] session %s failed", session.get("session_id"))

    # 휴지통(soft delete) 경과분 영구 정리 — 관리자 수동 purge 와 같은 경로를 재사용한다.
    # 사용자 삭제가 soft delete 로 바뀐 뒤 휴지통 세션은 만료 조회에서 제외되므로, 여기서
    # 걷어가지 않으면 삭제할수록 산출물이 영구 잔존한다.
    def _trash_audit(session, result):
        _log_audit(session, result)
        if result == "dryrun":
            _log.info("[cleanup:dry-run] would purge trashed session=%s akey=%s deleted_at=%s",
                      session.get("session_id"), session.get("analysis_key"),
                      session.get("deleted_at"))

    try:
        from admin_panel import sessions_admin
        trash = sessions_admin.purge_trashed(all_expired=True, dry_run=dry_run,
                                             audit=_trash_audit)
        if trash["purged"]:
            _log.info("[cleanup] purged trashed sessions: %s", trash["purged"])
    except Exception:
        trash = {"scanned": 0, "purged": []}
        _log.exception("[cleanup] trash purge failed")

    # 세션행 생성 전 실패한 ingest 의 FS 고아 산출물(세션 참조 없는 akey 디렉터리) 회수.
    try:
        fs_orphans = _purge_fs_orphans(dry_run)
    except Exception:
        fs_orphans = 0
        _log.exception("[cleanup] fs orphan purge failed")

    _log.info("[cleanup] done: orphan_pending=%d deleted=%d trash_scanned=%d trash_purged=%d "
              "fs_orphans=%d dry_run=%s audit_purged=%d",
              len(orphans), deleted, trash["scanned"], len(trash["purged"]), fs_orphans,
              dry_run, audit_purged)
    return {"scanned": len(orphans), "deleted": deleted, "dry_run": dry_run,
            "audit_purged": audit_purged, "fs_orphans": fs_orphans,
            "trash_scanned": trash["scanned"], "trash_purged": len(trash["purged"])}


def start_cleanup_scheduler():
    """daemon 스레드로 주기 정리 시작. 중복 기동은 _started 플래그로 방지."""
    global _started
    if _started:
        return
    if not config.REPORT_CLEANUP_ENABLED:
        _log.info("[cleanup] disabled (REPORT_CLEANUP_ENABLED=0)")
        return
    _started = True
    interval = max(0.1, float(config.REPORT_CLEANUP_INTERVAL_HOURS)) * 3600

    def _loop():
        time.sleep(30)  # 서버 초기화와 겹치지 않게 첫 실행만 잠깐 지연
        while True:
            try:
                run_cleanup()
            except Exception:
                _log.exception("[cleanup] run_cleanup crashed")
            # 로컬 hot 캐시 → S3 티어링 (같은 주기에 얹어 실행 — S3 미설정이면 no-op).
            try:
                import report_tiering
                report_tiering.run_tiering()
            except Exception:
                _log.exception("[tier] run_tiering crashed")
            time.sleep(interval)

    threading.Thread(target=_loop, name="report-cleanup", daemon=True).start()
    _log.info("[cleanup] scheduler started: interval=%.1fh dryrun=%s | tier: enabled=%s "
              "dryrun=%s age_days=%d local_max_gb=%.0f",
              config.REPORT_CLEANUP_INTERVAL_HOURS, config.REPORT_CLEANUP_DRYRUN,
              config.REPORT_TIER_ENABLED, config.REPORT_TIER_DRYRUN,
              config.REPORT_TIER_AGE_DAYS, config.REPORT_TIER_LOCAL_MAX_GB)

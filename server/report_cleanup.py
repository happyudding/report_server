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
import threading
import time
from pathlib import Path

import config
from database import report_db

_log = logging.getLogger(__name__)
_started = False


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


def run_cleanup(dry_run=None):
    """만료 세션을 조회해 정리 + 감사 로그 롤오프. dry_run 미지정 시 config 기본값 사용.
    {'scanned','deleted','dry_run','audit_purged'} 요약 반환."""
    if dry_run is None:
        dry_run = config.REPORT_CLEANUP_DRYRUN
    audit_purged = _purge_audit_logs()
    cutoff = int(time.time()) - int(config.REPORT_RETENTION_DAYS) * 86400
    try:
        expired = report_db.get_expired_sessions(cutoff)
    except Exception:
        _log.exception("[cleanup] get_expired_sessions failed")
        return {"scanned": 0, "deleted": 0, "dry_run": dry_run, "audit_purged": audit_purged}

    deleted = 0
    for session in expired:
        try:
            if _cleanup_one(session, dry_run):
                deleted += 1
        except Exception:
            _log.exception("[cleanup] session %s failed", session.get("session_id"))
    _log.info("[cleanup] done: scanned=%d deleted=%d dry_run=%s retention_days=%d audit_purged=%d",
              len(expired), deleted, dry_run, config.REPORT_RETENTION_DAYS, audit_purged)
    return {"scanned": len(expired), "deleted": deleted, "dry_run": dry_run,
            "audit_purged": audit_purged}


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
            time.sleep(interval)

    threading.Thread(target=_loop, name="report-cleanup", daemon=True).start()
    _log.info("[cleanup] scheduler started: interval=%.1fh dryrun=%s retention_days=%d",
              config.REPORT_CLEANUP_INTERVAL_HOURS, config.REPORT_CLEANUP_DRYRUN,
              config.REPORT_RETENTION_DAYS)

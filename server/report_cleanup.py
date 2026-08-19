"""잔존물 자동정리 스케줄러.

**보존기간(retention) 만료 세션 삭제는 폐지됐다** — 데이터 유실 방지를 위해
report_tiering 이 산출물만 S3 로 아카이브하고 세션/DB 는 유지한다. 이 모듈이 실제로
하는 일은 7가지다:
  1. 감사 로그 롤오프 (REPORT_AUDIT_RETENTION_DAYS, dry-run 무관 항상 실행)
  2. 운영 로그 롤오프 — 챗봇 원문(집계로 접은 뒤 삭제)·사용량 (dry-run 무관, 1과 동일 논리)
  3. S3 이관 대기(local_pending) 본문 재시도
  4. ingest 크래시 잔존 세션행(orphan pending, 48h) 회수
  5. 휴지통(soft delete) 경과분(REPORT_TRASH_RETENTION_DAYS) 영구 purge
  6. 세션 참조 없는 FS 고아 산출물(48h) 회수 — akey 축(web_report/issue_img/
     dist_combined/webreport_backup) + 세션 축(note_img/session_blob)
  7. eval 룰 지표 일별 집계 (report_eval_daily) — 원본을 지우지 않는 **비파괴 집계**라
     dry-run 무관. 안 돌면 정확도·커버리지 추이가 영영 남지 않는다

세션 삭제 라우트(report_routes.delete_session_route)와 동일한 산출물 정리 경로
(storage_gateway.delete_report_artifacts + report_db.delete_analysis_rows)를 재사용하며,
같은 analysis_key 를 참조하는 다른 세션이 남아있으면 산출물을 보존한다.

REPORT_CLEANUP_DRYRUN=True(기본) 이면 2·3·4 는 실제 삭제 없이 대상만 로그/감사로그에
남긴다(1 감사 롤오프는 제외 — _purge_audit_logs 주석 참조).
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

# 스케줄러 상태 (관리자 패널 /api/schedulers 노출용). 시간은 epoch 초.
STATE = {"last_run": None, "last_ok": None, "last_result": None, "next_run": None}

_AKEY_RE = re.compile(r"^[0-9a-f]{64}$")


def _log_audit(session, result):
    """정리 이력을 report_audit_log 에 기록 (best-effort). 백그라운드 스레드라
    Flask request 컨텍스트가 없으므로 client_ip/user_agent 는 고정값을 넣는다.

    busy_timeout 은 log_audit 기본값(짧음 — 요청 지연 방지)이 아니라 5초를 명시한다:
    여기는 사용자를 기다리게 하지 않는 스케줄러이고, 산출물을 실제로 지운 기록이라
    유실되면 "왜 사라졌나"를 추적할 방법이 없다."""
    try:
        report_db.log_audit(
            "delete",
            busy_timeout_ms=5000,
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
    # Note 이미지·큰 본문 blob 은 세션 단위라 akey 공유 여부와 무관하게 정리한다
    # (sessions_admin._delete_one/_purge_one 과 대칭 — 여기만 빠져 있었다).
    try:
        import storage_gateway
        for warning in storage_gateway.delete_note_images(sid):
            _log.warning("cleanup note image (%s): %s", sid, warning)
        keys = [(b["backend"], b["object_key"]) for b in report_db.list_session_blobs(sid)]
        for warning in storage_gateway.delete_session_blobs(sid, keys):
            _log.warning("cleanup session blob (%s): %s", sid, warning)
    except Exception:
        _log.exception("cleanup session-scoped artifact cleanup failed for %s", sid)
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


def _purge_operational_logs():
    """운영 로그 롤오프 — 챗봇 원문(집계로 접은 뒤 삭제) + 사용량(시간별/일별·Peak).

    감사 롤오프와 같은 이유로 dry-run 과 무관하게 동작한다: 로그 행 삭제는 산출물 파괴가
    아니고, 끄면 무한 증가한다. **세션 원본·사용자 편집은 여기 대상이 아니다.**
    {"chat","usage_hourly","usage_daily"} 반환."""
    out = {"chat": 0, "usage_hourly": 0, "usage_daily": 0}
    now = int(time.time())

    chat_days = int(getattr(config, "REPORT_CHATBOT_RETENTION_DAYS", 0) or 0)
    if chat_days > 0:
        try:
            out["chat"] = report_db.rollup_chat_daily(now - chat_days * 86400)
            if out["chat"]:
                _log.info("[cleanup] chatbot 원문 %d행을 일별 집계로 접고 삭제 (%d일 경과)",
                          out["chat"], chat_days)
        except Exception:
            _log.exception("[cleanup] chatbot purge failed")

    hourly_days = int(getattr(config, "REPORT_USAGE_HOURLY_RETENTION_DAYS", 0) or 0)
    daily_days = int(getattr(config, "REPORT_USAGE_DAILY_RETENTION_DAYS", 0) or 0)
    if hourly_days > 0 or daily_days > 0:
        def _day(days):
            if days <= 0:
                return None
            return time.strftime("%Y-%m-%d", time.localtime(now - days * 86400))
        try:
            usage = report_db.purge_usage(hourly_cutoff_day=_day(hourly_days),
                                          daily_cutoff_day=_day(daily_days))
            out["usage_hourly"] = usage["hourly"]
            out["usage_daily"] = usage["daily"] + usage["peak"]
            if out["usage_hourly"] or out["usage_daily"]:
                _log.info("[cleanup] usage 롤오프: hourly=%d daily+peak=%d",
                          out["usage_hourly"], out["usage_daily"])
        except Exception:
            _log.exception("[cleanup] usage purge failed")
    return out


def _rollup_eval_stats():
    """eval 룰 지표 일별 집계 + 보존기간 경과분 삭제. 갱신한 (day, engine_version) 수 반환.

    **집계는 dry-run 과 무관하게 돈다** — 원본(eval.db)을 지우지 않는 비파괴 작업이고,
    안 돌면 정확도·커버리지 추이가 영영 남지 않는다(감사 롤오프와 같은 논리).
    보존기간은 사용량 일별과 같은 값을 쓴다(둘 다 장기 추이 그래프 소스).
    실패해도 예외를 밖으로 내지 않는다 — 집계가 세션 정리를 막으면 안 된다.
    """
    days = int(getattr(config, "REPORT_EVAL_ROLLUP_DAYS", 0) or 0)
    if days <= 0:
        return 0
    try:
        n = report_db.rollup_eval_daily(days=days)
    except Exception:
        _log.exception("[cleanup] eval 지표 집계 실패")
        return 0
    keep = int(getattr(config, "REPORT_USAGE_DAILY_RETENTION_DAYS", 0) or 0)
    if keep > 0:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - keep * 86400))
        try:
            report_db.purge_eval_daily(cutoff)
        except Exception:
            _log.exception("[cleanup] eval 지표 롤오프 실패")
    if n:
        _log.info("[cleanup] eval 지표 %d일치 집계 갱신 (최근 %d일 재계산)", n, days)
    return n


def _purge_stale_eval_runs(dry_run):
    """옛 eval 스냅샷 run 정리 — 같은 (세션, 소스)의 최신이 아니고 라벨이 안 붙은 것만.

    **dry-run 을 존중한다** — 판정 근거를 실제로 지우는 파괴적 작업이라 집계·로그
    롤오프와 성격이 다르다. 대상 선정 규약은 `eval_admin.purge_stale_snapshots` 참조.
    실패해도 예외를 밖으로 내지 않는다.
    """
    if not int(getattr(config, "REPORT_EVAL_PURGE_STALE_RUNS", 0) or 0):
        return 0
    try:
        from admin_panel import eval_admin
        res = eval_admin.purge_stale_snapshots(dry_run=dry_run)
    except Exception:
        _log.exception("[cleanup] eval 스냅샷 run 정리 실패")
        return 0
    if res["runs"]:
        _log.info("[cleanup%s] eval 옛 스냅샷 run %d개%s",
                  ":dry-run" if dry_run else "", res["runs"],
                  "" if dry_run else f" 정리 (판정 {res['deleted']}행)")
    return res["runs"]


def _retry_pending_blobs():
    """S3 이관 대기(local_pending) 본문 재시도. 이관 성공 건수 반환.

    S3 업로드가 실패한 순간에도 사용자 입력은 로컬에 원자적으로 저장돼 있다. 여기서 다시
    올려 backend 를 s3 로 승격한다 — 실패하면 다음 주기에 또 시도한다(데이터는 그대로)."""
    promoted = 0
    try:
        pending = report_db.list_pending_session_blobs()
    except Exception:
        _log.exception("[cleanup] pending blob 조회 실패")
        return 0
    if not pending:
        return 0
    import storage_gateway
    for row in pending:
        try:
            if storage_gateway.promote_session_blob(row["object_key"]):
                report_db.mark_session_blob_backend(row["session_id"], row["kind"], "s3")
                promoted += 1
        except Exception:
            _log.warning("[cleanup] session blob 재이관 실패 (%s)", row["object_key"],
                         exc_info=True)
    if promoted:
        _log.info("[cleanup] session blob %d건 S3 재이관 완료", promoted)
    return promoted


def _purge_session_orphans(dry_run):
    """세션 참조 없는 **세션 단위** 잔존물 회수 — note_img/<sid>/ · session_blob/<sid>/.

    akey 단위 고아(_purge_fs_orphans)와 달리 이쪽은 세션이 지워졌는데 파일만 남은 경우다.
    관리자 삭제 경로에는 정리 훅이 있지만, 그 훅이 없던 시절에 지워진 세션과 훅이 실패한
    건은 여기서만 회수된다. 48h 유예는 진행 중인 업로드/편집 보호."""
    cutoff = time.time() - 48 * 3600
    found = 0
    roots = [Path(config.REPORT_UPLOAD_DIR) / "note_img",
             Path(config.REPORT_UPLOAD_DIR) / "session_blob"]
    for root in roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            try:
                if not entry.is_dir() or entry.stat().st_mtime > cutoff:
                    continue
                sid = entry.name
                if report_db.get_session(sid):
                    continue
                found += 1
                if dry_run:
                    _log.info("[cleanup:dry-run] would purge orphan %s/%s", root.name, sid)
                    continue
                import shutil
                shutil.rmtree(entry)
                _log.info("[cleanup] purged orphan %s/%s", root.name, sid)
            except Exception:
                _log.exception("[cleanup] session orphan purge failed for %s", entry)
    return found


def _purge_fs_orphans(dry_run):
    """DB 참조 없는 uploads/web_report/<akey>/ 고아 산출물 회수. 대상 건수 반환.

    ingest 는 저장(파일/S3) 후 세션행을 만들므로(web_report/ingest.py) 그 사이 실패하면
    산출물이 세션행 없이 남고, 기존 orphan pending 회수(DB 행 기준)로는 발견되지 않는다.
    세션이 하나도 참조하지 않는 akey 디렉터리를 48h 유예(진행 중 ingest 보호) 후
    세션 삭제와 동일 경로(delete_report_artifacts + delete_analysis_rows)로 정리한다.
    로컬 디렉터리 스캔 기준이라 S3 단독 고아는 대상 밖(관리자 스토리지 탭과 동일 정책).

    스캔 루트는 web_report/ 하나가 아니다 — issue_img/·dist_combined/·webreport_backup/
    도 akey 네임스페이스라 같은 이유로 고아가 될 수 있는데 종전에는 어떤 자동 경로로도
    회수되지 않았다. akey 집합을 네 곳에서 모아 한 번에 판정한다."""
    upload_root = Path(config.REPORT_UPLOAD_DIR)
    cutoff = time.time() - 48 * 3600
    candidates = {}          # akey -> 가장 최근 mtime (한 곳이라도 최근이면 유예)
    for name in ("web_report", "issue_img", "webreport_backup"):
        root = upload_root / name
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.is_dir() and _AKEY_RE.match(entry.name):
                try:
                    candidates[entry.name] = max(candidates.get(entry.name, 0),
                                                 entry.stat().st_mtime)
                except OSError:
                    continue
    dist_root = upload_root / "dist_combined"
    if dist_root.is_dir():
        for entry in dist_root.iterdir():
            if entry.is_file() and _AKEY_RE.match(entry.stem):
                try:
                    candidates[entry.stem] = max(candidates.get(entry.stem, 0),
                                                 entry.stat().st_mtime)
                except OSError:
                    continue

    found = 0
    for akey, mtime in candidates.items():
        try:
            if mtime > cutoff:
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
            _log.exception("[cleanup] orphan artifact purge failed for %s", akey)
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
    logs = _purge_operational_logs()
    blobs_promoted = _retry_pending_blobs()

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

    # 세션 단위 잔존물(note_img/·session_blob/) — akey 스캔이 못 보는 축.
    try:
        session_orphans = _purge_session_orphans(dry_run)
    except Exception:
        session_orphans = 0
        _log.exception("[cleanup] session orphan purge failed")

    # eval 지표 일별 집계 — 원본을 지우지 않는 **비파괴 집계**라 dry-run 과 무관하다
    # (감사·로그 롤오프와 같은 논리). 정확도·커버리지 추이를 남기는 유일한 경로다.
    # ⚠ 아래 스냅샷 정리보다 **먼저** 돌아야 한다 — 순서가 뒤집히면 집계 원재료를
    #    먼저 지워 그날 지표가 비게 된다.
    eval_days = _rollup_eval_stats()
    eval_runs = _purge_stale_eval_runs(dry_run)

    _log.info("[cleanup] done: orphan_pending=%d deleted=%d trash_scanned=%d trash_purged=%d "
              "fs_orphans=%d session_orphans=%d dry_run=%s audit_purged=%d chat_purged=%d "
              "usage_purged=%d blobs_promoted=%d eval_days=%d eval_stale_runs=%d",
              len(orphans), deleted, trash["scanned"], len(trash["purged"]), fs_orphans,
              session_orphans, dry_run, audit_purged, logs["chat"],
              logs["usage_hourly"] + logs["usage_daily"], blobs_promoted, eval_days,
              eval_runs)
    return {"scanned": len(orphans), "deleted": deleted, "dry_run": dry_run,
            "audit_purged": audit_purged, "fs_orphans": fs_orphans,
            "session_orphans": session_orphans,
            "chat_purged": logs["chat"],
            "usage_purged": logs["usage_hourly"] + logs["usage_daily"],
            "blobs_promoted": blobs_promoted, "eval_days": eval_days,
            "eval_stale_runs": eval_runs,
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
                summary = run_cleanup()
                STATE["last_ok"] = True
                STATE["last_result"] = (
                    f"orphan {summary['deleted']}/{summary['scanned']} · "
                    f"trash {summary['trash_purged']} · fs {summary['fs_orphans']} · "
                    f"sess {summary['session_orphans']} · audit {summary['audit_purged']} · "
                    f"chat {summary['chat_purged']} · usage {summary['usage_purged']} · "
                    f"blob↑{summary['blobs_promoted']}"
                    + (" (dry-run)" if summary["dry_run"] else ""))
            except Exception:
                STATE["last_ok"] = False
                STATE["last_result"] = "run_cleanup crashed"
                _log.exception("[cleanup] run_cleanup crashed")
            # 로컬 hot 캐시 → S3 티어링 (같은 주기에 얹어 실행 — S3 미설정이면 no-op).
            try:
                import report_tiering
                report_tiering.run_tiering()
            except Exception:
                _log.exception("[tier] run_tiering crashed")
            STATE["last_run"] = int(time.time())
            STATE["next_run"] = int(time.time() + interval)
            time.sleep(interval)

    threading.Thread(target=_loop, name="report-cleanup", daemon=True).start()
    _log.info("[cleanup] scheduler started: interval=%.1fh dryrun=%s | tier: enabled=%s "
              "dryrun=%s age_days=%d local_max_gb=%.0f",
              config.REPORT_CLEANUP_INTERVAL_HOURS, config.REPORT_CLEANUP_DRYRUN,
              config.REPORT_TIER_ENABLED, config.REPORT_TIER_DRYRUN,
              config.REPORT_TIER_AGE_DAYS, config.REPORT_TIER_LOCAL_MAX_GB)

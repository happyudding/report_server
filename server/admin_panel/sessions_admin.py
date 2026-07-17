"""관리자용 세션 컨트롤 — 전체 status 목록 · PIN 없는 삭제 · 중요 토글 · PIN 재설정.

report_db.py 는 수정하지 않는다. 목록/UPDATE 는 get_conn() 자체 SQL,
삭제는 report_routes.delete_session_route(563-590) 와 동일한 산출물 정리 경로
(count_sessions_for_analysis_key 공유 가드 → storage_gateway.delete_report_artifacts
→ delete_analysis_rows → invalidate_caches → delete_session)를 PIN 검사만 빼고 재사용한다.
"""
import logging
from pathlib import Path

import config
import storage_gateway
from database import report_db

_log = logging.getLogger(__name__)


def list_sessions(q=None, status=None, limit=100, offset=0, trashed=None):
    """전체 status 세션 목록 (기존 get_history 는 done/reused 만 반환해 사용 불가).
    password 원문은 절대 노출하지 않고 has_password 만 내려준다.

    trashed: None=전체(활성+휴지통) / "1"=휴지통만(deleted_at NOT NULL) / "0"=활성만.
    내부 관리용 조회라 휴지통 세션도 포함해 보여준다(일반 목록은 get_history 가 제외)."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    conditions = ["1=1"]
    params = []
    if status:
        conditions.append("s.status = ?")
        params.append(status)
    if trashed == "1":
        conditions.append("s.deleted_at IS NOT NULL")
    elif trashed == "0":
        conditions.append("s.deleted_at IS NULL")
    if q:
        conditions.append(
            "(s.file_name LIKE ? OR s.product LIKE ? OR s.lot_id LIKE ? "
            " OR s.session_id LIKE ? OR s.product_type LIKE ?)")
        like = f"%{q}%"
        params.extend([like] * 5)
    where = " AND ".join(conditions)

    with report_db.get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM report_session s WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"""
            SELECT s.session_id, s.analysis_key, s.file_name, s.product_type, s.process,
                   s.product, s.revision, s.lot_id, s.created_at, s.status, s.source,
                   s.uploaded_by, s.client_host, s.error_message,
                   s.deleted_at, s.deleted_by,
                   COALESCE(s.mode, 'Normal') AS mode,
                   COALESCE(s.is_important, 0) AS is_important,
                   CASE WHEN s.password IS NOT NULL AND s.password <> '' THEN 1 ELSE 0 END
                       AS has_password
            FROM report_session s
            WHERE {where}
            ORDER BY s.created_at DESC, s.session_id
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
    return {"total": int(total), "limit": limit, "offset": offset,
            "rows": [dict(r) for r in rows]}


def status_summary():
    """status 별 세션 수 (세션 탭 헤더용)."""
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM report_session GROUP BY status").fetchall()
    return {r["status"] or "(없음)": r["cnt"] for r in rows}


def _delete_one(session):
    """세션 1건 삭제 (PIN 검사 없음). report_routes 삭제 플로우와 동일한 정리 경로."""
    sid = session["session_id"]
    akey = session.get("analysis_key")
    if akey and report_db.count_sessions_for_analysis_key(
            akey, exclude_session_id=sid) == 0:
        try:
            result = storage_gateway.delete_report_artifacts(
                akey, upload_root=Path(config.REPORT_UPLOAD_DIR))
            for warning in result.get("warnings", []):
                _log.warning("[admin-panel] artifact cleanup (%s): %s", akey, warning)
            report_db.delete_analysis_rows(akey)
        except Exception:
            _log.exception("[admin-panel] artifact cleanup failed for %s", akey)
        try:
            from web_report import service as web_report_service
            web_report_service.invalidate_caches(akey)
        except Exception:
            _log.exception("[admin-panel] cache invalidate failed for %s", akey)
    report_db.delete_session(sid)


def bulk_delete(session_ids, audit):
    """세션 여러 건 삭제. 건별 try/except 로 계속 진행하고 건별 감사 기록.
    audit(session, result) 는 라우트가 넘겨주는 기록 콜백."""
    deleted, failed = [], []
    for sid in session_ids:
        session = report_db.get_session(sid)
        if not session:
            failed.append({"session_id": sid, "error": "not found"})
            continue
        try:
            _delete_one(session)
            deleted.append(sid)
            audit(session, "ok")
        except Exception as exc:
            _log.exception("[admin-panel] delete failed: %s", sid)
            failed.append({"session_id": sid, "error": str(exc)[:200]})
            audit(session, "fail")
    return {"deleted": deleted, "failed": failed}


def restore_sessions(session_ids, audit):
    """휴지통 세션 복원 (deleted_at/deleted_by clear). 건별 audit 콜백.
    실제 휴지통 상태인 것만 복원하고, 아니면 skipped 로 분류한다."""
    restored, skipped = [], []
    for sid in session_ids:
        session = report_db.get_session(sid)
        if not session:
            skipped.append({"session_id": sid, "reason": "not found"})
            continue
        if not session.get("deleted_at"):
            skipped.append({"session_id": sid, "reason": "not trashed"})
            continue
        report_db.restore_session(sid)
        restored.append(sid)
        audit(session, "ok")
    return {"restored": restored, "skipped": skipped}


def _purge_one(session):
    """휴지통 세션 1건 영구 정리 — 사용자 삭제 라우트와 동일한 완전 정리 경로.

    마지막 참조 세션이면 산출물(S3/로컬)·analysis 메타 행·캐시를 정리하고, Note 이미지는
    세션 단위라 공유 여부와 무관하게 정리한다. best-effort — 정리 실패가 세션 행 삭제를
    막지 않는다."""
    sid = session["session_id"]
    akey = session.get("analysis_key")
    if akey and report_db.count_sessions_for_analysis_key(
            akey, exclude_session_id=sid) == 0:
        try:
            result = storage_gateway.delete_report_artifacts(
                akey, upload_root=Path(config.REPORT_UPLOAD_DIR))
            for warning in result.get("warnings", []):
                _log.warning("[admin-panel] purge artifact (%s): %s", akey, warning)
            report_db.delete_analysis_rows(akey)
        except Exception:
            _log.exception("[admin-panel] purge artifact failed for %s", akey)
        try:
            from web_report import service as web_report_service
            web_report_service.invalidate_caches(akey)
        except Exception:
            _log.exception("[admin-panel] purge cache invalidate failed for %s", akey)
    try:
        for warning in storage_gateway.delete_note_images(sid):
            _log.warning("[admin-panel] purge note image (%s): %s", sid, warning)
    except Exception:
        _log.exception("[admin-panel] purge note image cleanup failed for %s", sid)
    report_db.delete_session(sid)


def purge_trashed(session_ids=None, all_expired=False, dry_run=True, cutoff_days=None,
                  audit=None):
    """휴지통 세션 영구 삭제(purge) — deleted_at 이 cutoff_days(기본 REPORT_TRASH_RETENTION_DAYS)
    이전인 경과분만 대상. 관리자 수동 실행 전용(스케줄러 자동 purge 없음).

    all_expired=True 면 경과분 전체, 아니면 session_ids 중 경과분만(미경과분은 skipped).
    dry_run=True 면 실제 정리 없이 대상만 집계/감사(result='dryrun'). audit(session, result)
    는 라우트가 넘기는 감사 콜백. 공유 analysis_key 는 count_sessions_for_analysis_key
    가드로 마지막 참조일 때만 산출물을 회수한다."""
    import time
    days = int(cutoff_days if cutoff_days is not None else config.REPORT_TRASH_RETENTION_DAYS)
    cutoff = int(time.time()) - days * 86400
    audit = audit or (lambda s, r: None)

    if all_expired:
        targets = report_db.get_trashed_sessions(before_epoch=cutoff)
    else:
        targets = []
        skipped_early = []
        for sid in (session_ids or []):
            session = report_db.get_session(sid)
            if not session or not session.get("deleted_at"):
                skipped_early.append({"session_id": sid, "reason": "not trashed"})
                continue
            if int(session.get("deleted_at")) > cutoff:
                skipped_early.append({"session_id": sid, "reason": "not expired"})
                continue
            targets.append(dict(session))

    purged, failed = [], []
    for session in targets:
        sid = session["session_id"]
        if dry_run:
            audit(session, "dryrun")
            continue
        try:
            _purge_one(session)
            purged.append(sid)
            audit(session, "ok")
        except Exception as exc:
            _log.exception("[admin-panel] purge failed: %s", sid)
            failed.append({"session_id": sid, "error": str(exc)[:200]})
            audit(session, "fail")

    out = {"scanned": len(targets), "purged": purged, "failed": failed,
           "dry_run": bool(dry_run), "cutoff_days": days}
    if not all_expired:
        out["skipped"] = skipped_early
    return out


def set_important(session_id, important):
    with report_db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE report_session SET is_important=? WHERE session_id=?",
            (1 if important else 0, session_id))
        return cur.rowcount > 0


def set_password(session_id, password):
    """PIN 재설정. 빈 문자열이면 해제(NULL). 4자리 숫자만 허용 (업로드 규칙과 동일)."""
    value = (password or "").strip() or None
    with report_db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE report_session SET password=? WHERE session_id=?",
            (value, session_id))
        return cur.rowcount > 0

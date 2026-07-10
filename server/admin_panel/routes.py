"""admin_panel blueprint — 얇은 HTTP 핸들러만. 구현은 sysinfo/stats/sessions_admin/maintenance.

접근 게이트는 비밀 URL prefix (__init__.register_admin_panel 이 부여).
변경요청(비-GET)은 X-Admin-Request: 1 커스텀 헤더를 요구한다 — 교차출처 폼은
커스텀 헤더를 붙일 수 없어 CSRF 가 차단된다 (report_routes 의 쿠키 페어 방식은
여기선 불필요).
"""
import logging
import re
from pathlib import Path

from flask import Blueprint, Response, abort, jsonify, request

from admin_panel import maintenance, sessions_admin, stats, sysinfo, users_admin
from database import report_db
from report.static_pages import send_html_gzip

_log = logging.getLogger(__name__)

admin_panel_bp = Blueprint("admin_panel", __name__)

_ADMIN_HTML = Path(__file__).resolve().parent / "admin_panel.html"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_PIN_RE = re.compile(r"^\d{4}$")
_USER_ID_RE = re.compile(r"^[^\s\\/]{1,64}$")


@admin_panel_bp.before_request
def _guard_mutations():
    if request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.headers.get("X-Admin-Request") != "1":
        abort(403, "X-Admin-Request header required")


def _client_meta():
    """감사 로그용 (client_ip, user_agent). report_routes._client_meta 와 동일 규칙."""
    fwd = request.headers.get("X-Forwarded-For")
    ip = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
    return ip, str(request.user_agent)


def _audit(action, session=None, session_id=None, changed_fields=None, result="ok"):
    """admin 작업 감사 기록 (best-effort). client_user='admin-panel' 로 구분."""
    try:
        ip, ua = _client_meta()
        meta = session or {}
        report_db.log_audit(
            action,
            session_id=session_id or meta.get("session_id"),
            analysis_key=meta.get("analysis_key"),
            product_type=meta.get("product_type"),
            product=meta.get("product"),
            lot_id=meta.get("lot_id"),
            file_name=meta.get("file_name"),
            changed_fields=changed_fields,
            client_ip=ip,
            user_agent=ua,
            client_user="admin-panel",
            result=result,
        )
    except Exception:
        pass


# ── 페이지 ───────────────────────────────────────────────────────────────────

@admin_panel_bp.get("/")
def dashboard_page():
    return send_html_gzip(_ADMIN_HTML)


# ── 현황 ─────────────────────────────────────────────────────────────────────

@admin_panel_bp.get("/api/health")
def api_health():
    return jsonify(sysinfo.health())


@admin_panel_bp.get("/api/storage")
def api_storage():
    refresh = request.args.get("refresh") == "1"
    return jsonify(sysinfo.storage(refresh=refresh))


@admin_panel_bp.get("/api/s3-status")
def api_s3_status():
    return jsonify(sysinfo.s3_status())


# ── 통계 ─────────────────────────────────────────────────────────────────────

@admin_panel_bp.get("/api/stats/daily")
def api_stats_daily():
    return jsonify(stats.daily_counts(request.args.get("days", 30)))


@admin_panel_bp.get("/api/stats/users")
def api_stats_users():
    return jsonify(stats.user_ranking(request.args.get("days", 30)))


# ── 세션 컨트롤 ──────────────────────────────────────────────────────────────

@admin_panel_bp.get("/api/sessions")
def api_sessions():
    return jsonify(sessions_admin.list_sessions(
        q=(request.args.get("q") or "").strip() or None,
        status=(request.args.get("status") or "").strip() or None,
        limit=request.args.get("limit", 100),
        offset=request.args.get("offset", 0),
    ))


@admin_panel_bp.get("/api/sessions/status_summary")
def api_sessions_status_summary():
    return jsonify(sessions_admin.status_summary())


@admin_panel_bp.post("/api/sessions/delete")
def api_sessions_delete():
    body = request.get_json(force=True, silent=True) or {}
    sids = body.get("session_ids")
    if not isinstance(sids, list) or not sids or len(sids) > 200:
        abort(400, "session_ids: 1~200개 리스트 필요")
    for sid in sids:
        if not isinstance(sid, str) or not _SESSION_ID_RE.match(sid):
            abort(400, f"invalid session_id: {sid!r}")
    result = sessions_admin.bulk_delete(
        sids, audit=lambda session, res: _audit("delete", session=session, result=res))
    return jsonify(result)


@admin_panel_bp.post("/api/session/<session_id>/important")
def api_session_important(session_id):
    if not _SESSION_ID_RE.match(session_id):
        abort(400, "invalid session_id")
    body = request.get_json(force=True, silent=True) or {}
    important = bool(body.get("important"))
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    sessions_admin.set_important(session_id, important)
    _audit("edit", session=session, changed_fields="is_important(admin)")
    return jsonify({"ok": True, "session_id": session_id, "is_important": int(important)})


@admin_panel_bp.post("/api/session/<session_id>/password")
def api_session_password(session_id):
    if not _SESSION_ID_RE.match(session_id):
        abort(400, "invalid session_id")
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    if password and not _PIN_RE.match(password):
        abort(400, "password must be 4 digits or empty")
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    sessions_admin.set_password(session_id, password)
    _audit("edit", session=session,
           changed_fields="password(admin:%s)" % ("set" if password else "clear"))
    return jsonify({"ok": True, "session_id": session_id, "has_password": bool(password)})


# ── 사용자(웹 로그인 계정) 컨트롤 ───────────────────────────────────────────

def _norm_user_id(user_id):
    uid = (user_id or "").strip().lower()
    if not _USER_ID_RE.match(uid):
        abort(400, "invalid user_id")
    return uid


@admin_panel_bp.get("/api/users")
def api_users():
    return jsonify(users_admin.list_users(
        q=(request.args.get("q") or "").strip() or None,
        limit=request.args.get("limit", 200),
        offset=request.args.get("offset", 0),
    ))


@admin_panel_bp.post("/api/user/<user_id>/reset_password")
def api_user_reset_password(user_id):
    uid = _norm_user_id(user_id)
    if not users_admin.reset_password(uid):
        abort(404, "user not found")
    _audit("edit", changed_fields="user_password_reset(%s)" % uid)
    return jsonify({"ok": True, "user_id": uid})


@admin_panel_bp.post("/api/user/<user_id>/password")
def api_user_set_password(user_id):
    uid = _norm_user_id(user_id)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    if not _PIN_RE.match(password):
        abort(400, "password must be 4 digits")
    if not users_admin.set_password(uid, password):
        abort(404, "user not found")
    _audit("edit", changed_fields="user_password_set(%s)" % uid)
    return jsonify({"ok": True, "user_id": uid})


@admin_panel_bp.post("/api/user/<user_id>/delete")
def api_user_delete(user_id):
    uid = _norm_user_id(user_id)
    if not users_admin.delete_user(uid):
        abort(404, "user not found")
    _audit("delete", changed_fields="user_delete(%s)" % uid)
    return jsonify({"ok": True, "user_id": uid})


# ── DB 컨트롤 ────────────────────────────────────────────────────────────────

@admin_panel_bp.post("/api/db/backup")
def api_db_backup():
    try:
        return jsonify(maintenance.backup_now())
    except maintenance.Busy as exc:
        return jsonify({"error": str(exc)}), 409


@admin_panel_bp.get("/api/db/backups")
def api_db_backups():
    return jsonify(maintenance.list_backups())


@admin_panel_bp.post("/api/db/cleanup")
def api_db_cleanup():
    body = request.get_json(force=True, silent=True) or {}
    dry_run = body.get("dry_run", True)
    try:
        return jsonify(maintenance.cleanup_now(dry_run=bool(dry_run)))
    except maintenance.Busy as exc:
        return jsonify({"error": str(exc)}), 409


@admin_panel_bp.get("/api/db/diagnostics")
def api_db_diagnostics():
    return jsonify(maintenance.diagnostics(full=request.args.get("full") == "1"))


# ── 감사 로그 (구 /pe/admin 탭 흡수) ─────────────────────────────────────────

@admin_panel_bp.get("/api/audit")
def api_audit():
    return jsonify(report_db.get_audit_logs(
        action=(request.args.get("action") or "").strip() or None,
        session_id=(request.args.get("session_id") or "").strip() or None,
        q=(request.args.get("q") or "").strip() or None,
        limit=request.args.get("limit", 200),
        offset=request.args.get("offset", 0),
    ))


@admin_panel_bp.get("/api/audit.csv")
def api_audit_csv():
    it = maintenance.audit_csv_iter(
        action=(request.args.get("action") or "").strip() or None,
        q=(request.args.get("q") or "").strip() or None)
    return Response(it, mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=report_audit_log.csv",
        "Cache-Control": "no-store",
    })


# ── 서버 로그 ────────────────────────────────────────────────────────────────

@admin_panel_bp.get("/api/logs/tail")
def api_logs_tail():
    return jsonify(maintenance.log_tail(request.args.get("bytes", 65536)))

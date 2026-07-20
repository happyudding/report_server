"""admin_panel blueprint — 얇은 HTTP 핸들러만. 구현은 sysinfo/stats/sessions_admin/maintenance.

접근 게이트는 비밀 URL prefix (__init__.register_admin_panel 이 부여).
변경요청(비-GET)은 X-Admin-Request: 1 커스텀 헤더를 요구한다 — 교차출처 폼은
커스텀 헤더를 붙일 수 없어 CSRF 가 차단된다 (report_routes 의 쿠키 페어 방식은
여기선 불필요).
"""
import hmac
import logging
import re
from pathlib import Path

from flask import Blueprint, Response, abort, jsonify, request

import config
from admin_panel import (GATE_COOKIE_VOC, GATE_COOKIE_VOC_PATH, eval_admin,
                         gate_token, maintenance, metrics, sessions_admin, stats,
                         storage_admin, sysinfo, users_admin)
from database import report_db
from report.static_pages import send_html_gzip

_log = logging.getLogger(__name__)

admin_panel_bp = Blueprint("admin_panel", __name__)

_ADMIN_HTML = Path(__file__).resolve().parent / "admin_panel.html"
_LOGIN_HTML = Path(__file__).resolve().parent / "admin_login.html"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_PIN_RE = re.compile(r"^\d{4}$")
_USER_ID_RE = re.compile(r"^[^\s\\/]{1,64}$")

# ── 접속 비밀번호 게이트 (아무나 못 들어오게 하는 간단한 쿠키 게이트) ─────────
# 비밀번호가 맞으면 쿠키를 발급하고, before_request 가 매 요청 쿠키를 확인한다.
# 쿠키 값은 비밀번호 원문이 아니라 sha256 토큰(원문 노출 방지).
_AUTH_COOKIE = "pe_admin_gate"
_COOKIE_PATH = f"/pe/admin-{config.REPORT_ADMIN_SECRET}"


def _expected_token():
    return gate_token()   # 공식 정본은 admin_panel/__init__.py (VOC 쪽과 공유)


_LOGIN_PAGE_CACHE = None


def _login_page():
    global _LOGIN_PAGE_CACHE
    if _LOGIN_PAGE_CACHE is None:
        _LOGIN_PAGE_CACHE = _LOGIN_HTML.read_text(encoding="utf-8")
    return Response(_LOGIN_PAGE_CACHE, status=401, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


@admin_panel_bp.before_request
def _auth_gate():
    if request.endpoint == "admin_panel.login":
        return None  # 로그인 처리 엔드포인트는 통과
    if hmac.compare_digest(request.cookies.get(_AUTH_COOKIE, ""), _expected_token()):
        return None  # 인증됨
    if request.endpoint == "admin_panel.dashboard_page":
        return _login_page()  # 대시보드 진입 → 로그인 화면
    abort(401, "admin login required")  # API 등 그 외 → 401


@admin_panel_bp.post("/login")
def login():
    body = request.get_json(force=True, silent=True) or {}
    if (body.get("password") or "").strip() != config.REPORT_ADMIN_PASSWORD:
        return jsonify({"ok": False}), 401
    resp = jsonify({"ok": True})
    resp.set_cookie(_AUTH_COOKIE, _expected_token(), max_age=12 * 3600,
                    httponly=True, samesite="Lax", secure=request.is_secure,
                    path=_COOKIE_PATH)
    # VOC 게시판 관리자 권한(상태 Open/Close 전환)용 사본 — 위 쿠키는 admin 경로
    # 전용이라 /pe/report/* 요청에 실려오지 않는다. report/routes_voc._is_admin 이 검증.
    resp.set_cookie(GATE_COOKIE_VOC, _expected_token(), max_age=12 * 3600,
                    httponly=True, samesite="Lax", secure=request.is_secure,
                    path=GATE_COOKIE_VOC_PATH)
    return resp


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


@admin_panel_bp.get("/api/watchdog")
def api_watchdog():
    return jsonify(sysinfo.watchdog_status())


# ── 스토리지 관리 ────────────────────────────────────────────────────────────
# 세션 삭제는 기존 /api/sessions/delete (artifact-aware) 를 그대로 재사용한다.

@admin_panel_bp.get("/api/storage/overview")
def api_storage_overview():
    refresh = request.args.get("refresh") == "1"
    return jsonify(storage_admin.overview(refresh=refresh))


@admin_panel_bp.get("/api/storage/sessions")
def api_storage_sessions():
    return jsonify(storage_admin.list_sessions_by_storage(
        sort=(request.args.get("sort") or "size").strip(),
        order=(request.args.get("order") or "desc").strip(),
        q=(request.args.get("q") or "").strip() or None,
        limit=request.args.get("limit", 100),
        offset=request.args.get("offset", 0),
        refresh=request.args.get("refresh") == "1",
    ))


# ── Eval DB (Issue Table 코멘트 export — web_report/eval_export.py) ──────────

_CASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")


@admin_panel_bp.get("/api/eval/overview")
def api_eval_overview():
    return jsonify(eval_admin.overview())


@admin_panel_bp.get("/api/eval/labels")
def api_eval_labels():
    return jsonify(eval_admin.list_labels(
        q=(request.args.get("q") or "").strip() or None,
        limit=request.args.get("limit", 100),
        offset=request.args.get("offset", 0),
    ))


@admin_panel_bp.post("/api/eval/cases/delete")
def api_eval_cases_delete():
    body = request.get_json(force=True, silent=True) or {}
    cids = body.get("case_ids")
    if not isinstance(cids, list) or not cids or len(cids) > 200:
        abort(400, "case_ids: 1~200개 리스트 필요")
    for cid in cids:
        if not isinstance(cid, str) or not _CASE_ID_RE.match(cid):
            abort(400, f"invalid case_id: {cid!r}")
    result = eval_admin.delete_cases(cids)
    _audit("delete", changed_fields=f"eval_cases({result.get('deleted', 0)})")
    return jsonify(result)


@admin_panel_bp.post("/api/eval/session/<session_id>/reexport")
def api_eval_reexport(session_id):
    if not _SESSION_ID_RE.match(session_id):
        abort(400, "invalid session_id")
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    result = eval_admin.reexport(session_id)
    _audit("edit", session=session, changed_fields=f"eval_reexport({result})")
    return jsonify(result)


@admin_panel_bp.get("/api/metrics/history")
def api_metrics_history():
    window = min(max(int(request.args.get("window", 3600)), 60), 86400)
    return jsonify(metrics.snapshot_history(window))


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
        trashed=(request.args.get("trashed") or "").strip() or None,
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


@admin_panel_bp.post("/api/sessions/restore")
def api_sessions_restore():
    body = request.get_json(force=True, silent=True) or {}
    sids = body.get("session_ids")
    if not isinstance(sids, list) or not sids or len(sids) > 200:
        abort(400, "session_ids: 1~200개 리스트 필요")
    for sid in sids:
        if not isinstance(sid, str) or not _SESSION_ID_RE.match(sid):
            abort(400, f"invalid session_id: {sid!r}")
    result = sessions_admin.restore_sessions(
        sids, audit=lambda session, res: _audit(
            "edit", session=session, changed_fields="restore", result=res))
    return jsonify(result)


@admin_panel_bp.post("/api/sessions/purge")
def api_sessions_purge():
    """휴지통 세션 영구 삭제 — 30일(REPORT_TRASH_RETENTION_DAYS) 경과분만.
    body: {session_ids:[...]} 또는 {all_expired:true}, dry_run(기본 true)."""
    body = request.get_json(force=True, silent=True) or {}
    all_expired = bool(body.get("all_expired"))
    dry_run = body.get("dry_run", True)
    sids = body.get("session_ids")
    if not all_expired:
        if not isinstance(sids, list) or not sids or len(sids) > 200:
            abort(400, "session_ids: 1~200개 리스트 필요 (또는 all_expired:true)")
        for sid in sids:
            if not isinstance(sid, str) or not _SESSION_ID_RE.match(sid):
                abort(400, f"invalid session_id: {sid!r}")
    result = sessions_admin.purge_trashed(
        session_ids=sids, all_expired=all_expired, dry_run=bool(dry_run),
        audit=lambda session, res: _audit(
            "delete", session=session, changed_fields="purge", result=res))
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

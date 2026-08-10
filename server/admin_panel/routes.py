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
from admin_panel import (GATE_COOKIE_EVAL, GATE_COOKIE_EVAL_PATH, GATE_COOKIE_VOC,
                         GATE_COOKIE_VOC_PATH, MASTER_COOKIE,
                         MASTER_COOKIE_PATH, MASTER_TTL_SECONDS,
                         chatbot_admin, eval_gate_token, gate_token, identity_merge,
                         issue_master_value, maintenance,
                         metrics, sessions_admin, stats, storage_admin, sysinfo,
                         users_admin, voc_admin, voc_gate_token)
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
    # VOC 게시판 관리자 권한(상태 Open/Close 전환)용 별도 토큰 — 위 쿠키는 admin 경로
    # 전용이라 /pe/report/* 요청에 실려오지 않는다. report/routes_voc._is_admin 이 검증.
    # 값이 admin 토큰과 다른 이유는 voc_gate_token() docstring 참조.
    resp.set_cookie(GATE_COOKIE_VOC, voc_gate_token(), max_age=12 * 3600,
                    httponly=True, samesite="Lax", secure=request.is_secure,
                    path=GATE_COOKIE_VOC_PATH)
    # master 권한 — 로그인한 PC 에 4h 동안 '전 세션 편집 + 비공개 조회/목록표시'.
    # 값에 만료시각이 서명돼 있어 서버가 4h 를 직접 강제한다(report/security._is_master).
    resp.set_cookie(MASTER_COOKIE, issue_master_value(), max_age=MASTER_TTL_SECONDS,
                    httponly=True, samesite="Lax", secure=request.is_secure,
                    path=MASTER_COOKIE_PATH)
    # eval 룰 패널(/pe/eval) — 여기서 함께 발급해 admin 로그인 후 바로 진입할 수 있게 한다.
    resp.set_cookie(GATE_COOKIE_EVAL, eval_gate_token(), max_age=12 * 3600,
                    httponly=True, samesite="Lax", secure=request.is_secure,
                    path=GATE_COOKIE_EVAL_PATH)
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


@admin_panel_bp.get("/api/watchdog/checks")
def api_watchdog_checks():
    hours = min(max(int(request.args.get("hours", 24)), 1), 168)
    return jsonify(sysinfo.watchdog_checks(hours=hours))


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


# Eval DB 라우트(/api/eval/*)는 2026-08-03 eval_panel(/pe/eval)로 이관했다.
# 구현 모듈 admin_panel/eval_admin.py 는 그대로 남아 eval_panel 이 import 한다.


@admin_panel_bp.get("/api/metrics/history")
def api_metrics_history():
    window = min(max(int(request.args.get("window", 3600)), 60), 86400)
    return jsonify(metrics.snapshot_history(window))


@admin_panel_bp.get("/api/metrics/file_history")
def api_metrics_file_history():
    """파일 기반 이력 — 서버 재시작으로 초기화되지 않는다 (현황 탭 실시간 차트와 병행)."""
    hours = min(max(int(request.args.get("hours", 24)), 1), metrics.FILE_HISTORY_MAX_HOURS)
    return jsonify(metrics.file_history(hours))


@admin_panel_bp.get("/api/public_api")
def api_public_api():
    """공개 API(/pe/api/v1) 호출 계측 — 'public API' 탭.

    기능이 폴더 단위로 계속 늘어나므로(외부 담당자 추가분 포함) endpoint 를 열거하지 않고
    Blueprint 이름 접두(public_api_)로 자동 수집한 것을 그대로 돌려준다.
    hours 를 주면 재시작과 무관한 파일 이력(publicapi_*.log)도 함께 싣는다.
    api_runtime 과 같은 이유로 구성요소별 try — 하나가 실패해도 나머지는 살린다."""
    window = min(max(int(request.args.get("window", 3600)), 60), 86400)
    out = {"snapshot": None, "history": None}
    try:
        from public_api import metrics as pa_metrics
        out["snapshot"] = pa_metrics.snapshot(window)
    except Exception:
        pass
    hours = request.args.get("hours")
    if hours:
        try:
            from public_api import metrics as pa_metrics
            out["history"] = pa_metrics.file_history(
                min(max(int(hours), 1), 24 * 14))
        except Exception:
            pass
    return jsonify(out)


@admin_panel_bp.get("/api/webreport/builds")
def api_webreport_builds():
    """web_report 콜드 빌드 이력 — 단계별 소요 + 대기 시간 + 실패(타임아웃·워커 붕괴).

    "콜드 빌드가 300초 걸렸다" 가 계산이 느려서인지 앞 작업 대기에 밀려서인지는
    queue_wait/pool_wait 를 봐야 구분된다 (web_report/build_log.py 참조)."""
    hours = min(max(int(request.args.get("hours", 24)), 1), 24 * 14)
    limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    out = {"builds": [], "compute": None}
    try:
        from web_report import build_log
        out["builds"] = build_log.history(hours, limit)
    except Exception:
        pass
    try:
        from web_report import compute
        out["compute"] = compute.status()
    except Exception:
        pass
    return jsonify(out)


@admin_panel_bp.get("/api/active_users")
def api_active_users():
    """실시간 접속 사용자 — 사용자 탭 전용(10초 폴링).

    api/runtime 에도 같은 값이 실려 있지만, 사용자 탭은 응답시간·캐시·스케줄러가 필요 없어
    이 가벼운 엔드포인트를 따로 쓴다."""
    return jsonify(metrics.active_users(
        request.args.get("window", metrics.ACTIVE_USER_WINDOW_SEC)))


@admin_panel_bp.get("/api/runtime")
def api_runtime():
    """응답시간 백분위 · 컴퓨트 워커 · 캐시 히트율 · DB 잠금 · 스케줄러 ·
    동시 열람 세션 · 실시간 접속 사용자 · 진행 중 콜드 빌드.

    개별 API 로 쪼개면 화면이 여러 번 왕복해야 해서 한 번에 묶어 돌려준다. 각 구성요소는
    실패해도 나머지를 막지 않는다 (모듈 미기동/kill-switch 대비)."""
    out = {}
    try:
        out["latency"] = metrics.latency_snapshot()
    except Exception:
        out["latency"] = None
    try:
        out["viewers"] = metrics.viewers()
    except Exception:
        out["viewers"] = None
    try:
        out["active_users"] = metrics.active_users(
            request.args.get("user_window", metrics.ACTIVE_USER_WINDOW_SEC))
    except Exception:
        out["active_users"] = None
    try:
        from web_report import build_status
        out["builds"] = build_status.snapshot_all()
    except Exception:
        out["builds"] = None
    try:
        from web_report import compute
        out["compute"] = compute.status()
    except Exception:
        out["compute"] = None
    try:
        from web_report import cache as wr_cache
        out["cache"] = wr_cache.cache_stats()
    except Exception:
        out["cache"] = None
    try:
        import ops
        out["db_lock"] = dict(ops.DB_LOCK_ERRORS)
    except Exception:
        out["db_lock"] = None
    try:
        import db_backup
        import report_cleanup
        out["schedulers"] = {"backup": dict(db_backup.STATE),
                             "cleanup": dict(report_cleanup.STATE)}
    except Exception:
        out["schedulers"] = None
    return jsonify(out)


# ── 통계 ─────────────────────────────────────────────────────────────────────

@admin_panel_bp.get("/api/stats/daily")
def api_stats_daily():
    return jsonify(stats.daily_counts(request.args.get("days", 30)))


@admin_panel_bp.get("/api/stats/users")
def api_stats_users():
    return jsonify(stats.user_ranking(request.args.get("days", 30)))


@admin_panel_bp.get("/api/stats/client_errors")
def api_stats_client_errors():
    return jsonify(stats.client_error_count(request.args.get("hours", 24)))


@admin_panel_bp.get("/api/stats/usage")
def api_stats_usage():
    """접속 사용량 순위 — Honey 실행 · 웹페이지 방문 (report_usage_daily 집계)."""
    return jsonify(stats.usage_ranking(request.args.get("days", 30)))


# ── VOC 게시판 (읽기 전용 — 등록/수정/상태전환은 /pe/report/voc) ────────────────

@admin_panel_bp.get("/api/voc/overview")
def api_voc_overview():
    return jsonify(voc_admin.overview())


@admin_panel_bp.get("/api/voc")
def api_voc_list():
    return jsonify(voc_admin.list_voc(
        q=(request.args.get("q") or "").strip() or None,
        limit=request.args.get("limit", 50),
        offset=request.args.get("offset", 0),
    ))


# ── 웹 챗봇 (관리자 전용 기능이라 사용 현황·부하도 여기서만 본다) ─────────────

@admin_panel_bp.get("/api/chatbot")
def api_chatbot_overview():
    return jsonify(chatbot_admin.overview(request.args.get("hours", 24)))


@admin_panel_bp.get("/api/chatbot/log")
def api_chatbot_log():
    return jsonify(chatbot_admin.list_logs(
        q=(request.args.get("q") or "").strip() or None,
        limit=request.args.get("limit", 50),
        offset=request.args.get("offset", 0),
        errors_only=request.args.get("errors") == "1",
    ))


# ── 세션 컨트롤 ──────────────────────────────────────────────────────────────

@admin_panel_bp.get("/api/sessions")
def api_sessions():
    return jsonify(sessions_admin.list_sessions(
        q=(request.args.get("q") or "").strip() or None,
        status=(request.args.get("status") or "").strip() or None,
        limit=request.args.get("limit", 100),
        offset=request.args.get("offset", 0),
        trashed=(request.args.get("trashed") or "").strip() or None,
        date_from=(request.args.get("date_from") or "").strip() or None,
        date_to=(request.args.get("date_to") or "").strip() or None,
        uploader=(request.args.get("uploader") or "").strip() or None,
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
    """휴지통 세션 영구 삭제 — 기본은 30일(REPORT_TRASH_RETENTION_DAYS) 경과분만.
    body: {session_ids:[...]} 또는 {all_expired:true} 또는 {all_trashed:true},
    dry_run(기본 true), force(기본 false — true 면 경과일 무시, 명시 session_ids 에만 허용).

    all_trashed 는 휴지통 **전체**(미경과분 포함)를 비우는 관리자 수동 경로다. 자동 정리
    (report_cleanup)는 지금도 all_expired 만 쓴다 — 자동 경로가 미경과분을 지우면 사용자의
    복구 창이 통째로 사라지기 때문."""
    body = request.get_json(force=True, silent=True) or {}
    all_expired = bool(body.get("all_expired"))
    all_trashed = bool(body.get("all_trashed"))
    dry_run = body.get("dry_run", True)
    force = bool(body.get("force"))
    sids = body.get("session_ids")
    if all_expired and all_trashed:
        abort(400, "all_expired 와 all_trashed 는 함께 쓸 수 없습니다")
    if not all_expired and not all_trashed:
        if not isinstance(sids, list) or not sids or len(sids) > 200:
            abort(400, "session_ids: 1~200개 리스트 필요 (또는 all_expired/all_trashed:true)")
        for sid in sids:
            if not isinstance(sid, str) or not _SESSION_ID_RE.match(sid):
                abort(400, f"invalid session_id: {sid!r}")
    elif force and all_expired:
        # 경과분 일괄 경로의 force 는 의미가 모호하다 — 전체를 지우려면 all_trashed 를 쓴다.
        abort(400, "force 는 session_ids 지정 시에만 사용할 수 있습니다 "
                   "(휴지통 전체 비우기는 all_trashed)")
    changed = "purge_all" if all_trashed else ("purge_force" if force else "purge")
    result = sessions_admin.purge_trashed(
        session_ids=sids, all_expired=all_expired, all_trashed=all_trashed,
        dry_run=bool(dry_run), force=force,
        audit=lambda session, res: _audit(
            "delete", session=session, changed_fields=changed, result=res))
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
    """감사 기록 조회. "IP 가 같으면 같은 사용자" 규칙을 두 군데에 적용한다:
    (1) 계정명으로 검색하면 그 계정의 IP 에서 남은 무신원 기록도 함께 걸리고,
    (2) 각 행에 resolved_user(신원이 빈 행의 추정 계정)를 실어 화면이 표시할 수 있게 한다."""
    q = (request.args.get("q") or "").strip() or None
    mapping = identity_merge.ip_to_user()
    extra_ips = [ip for ip, uid in mapping.items() if q and uid == q.strip().lower()]
    rows = report_db.get_audit_logs(
        action=(request.args.get("action") or "").strip() or None,
        session_id=(request.args.get("session_id") or "").strip() or None,
        q=q,
        limit=request.args.get("limit", 200),
        offset=request.args.get("offset", 0),
        extra_ips=extra_ips,
    )
    for r in rows:
        if not (r.get("client_user") or "").strip():
            uid = mapping.get(r.get("client_ip"))
            if uid:
                r["resolved_user"] = uid
    return jsonify(rows)


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

@admin_panel_bp.get("/api/logs/list")
def api_logs_list():
    return jsonify({"files": maintenance.log_list()})


@admin_panel_bp.get("/api/logs/tail")
def api_logs_tail():
    name = (request.args.get("file") or "").strip() or None
    try:
        return jsonify(maintenance.log_tail(request.args.get("bytes", 65536), name=name))
    except ValueError as e:
        abort(400, str(e))

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
                         issue_master_value, maintenance, messages,
                         metrics, sessions_admin, stats, storage_admin, sysinfo,
                         users_admin, voc_admin, voc_gate_token)
from database import report_db
from identity_norm import normalize_uid
from report.static_pages import send_html_gzip

_log = logging.getLogger(__name__)

admin_panel_bp = Blueprint("admin_panel", __name__)

_ADMIN_HTML = Path(__file__).resolve().parent / "admin_panel.html"
_LOGIN_HTML = Path(__file__).resolve().parent / "admin_login.html"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_PIN_RE = re.compile(r"^\d{4}$")
_USER_ID_RE = re.compile(r"^[^\s\\/]{1,64}$")
# 사용자 실명 = 완성형 한글 2~10자. 정본은 report/security.py `_DISPLAY_NAME_RE` 이며
# 여기 사본은 import 순환(report.security → admin_panel)을 피하려고 값만 맞춰 둔 것이다
# — 한쪽만 고치면 관리자가 지정한 이름과 본인이 넣은 이름의 규칙이 갈라진다.
_DISPLAY_NAME_RE = re.compile(r"^[가-힣]{2,10}$")

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


@admin_panel_bp.post("/api/webreport/build_action")
def api_webreport_build_action():
    """진행 중/막힌 콜드 빌드에 대한 관리자 개입 (clear_failure / clear_stuck / rebuild).

    개별 빌드만 취소하는 수단은 없다 — ProcessPoolExecutor 는 실행 중 잡을 cancel 할 수
    없기 때문이다(web_report/compute.py run()). 여기 액션은 전부 '막힌 것을 푸는' 쪽이며
    캐시·편집·산출물은 건드리지 않는다."""
    from admin_panel import builds_admin
    body = request.get_json(force=True, silent=True) or {}
    action = str(body.get("action") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    kind = str(body.get("kind") or "report").strip()
    if not _SESSION_ID_RE.match(session_id):
        abort(400, "invalid session_id")
    out = builds_admin.act(action, session_id, kind)
    if not out.get("ok"):
        out["error"] = out.get("message", "")   # 프런트 postJSON 이 error 키를 띄운다
    _audit("build_action", session_id=session_id,
           changed_fields=f"action={action} kind={kind} :: {out.get('message', '')}"[:1500],
           result="ok" if out.get("ok") else "fail")
    return jsonify(out), (200 if out.get("ok") else 400)


@admin_panel_bp.post("/api/users/action")
def api_users_action():
    """'접속중' 탭에서 특정 사용자의 대기를 강제로 끊는다 (⛔ 중단).

    쓰는 상황: 콜드 빌드가 오래 걸리는 세션을 열어둔 채 자리를 뜬 사용자. 그 탭은 최대
    15분간 폴링하며 재빌드를 계속 유발한다. 여기서 두 가지를 한다 —
      ① 그 세션의 빌드 대기 차단(builds_admin kill_wait) → /full 이 즉시 503,
      ② 그 사용자 브라우저에 중단 신호(messages.request_stop) → 다음 폴링에 폴링 정지.
    **진행 중인 워커 계산 자체는 끊지 못한다**(build_action docstring 참조).
    """
    from admin_panel import builds_admin, messages
    body = request.get_json(force=True, silent=True) or {}
    user_key = str(body.get("user_key") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    if not user_key:
        return jsonify({"ok": False, "error": "user_key 가 필요합니다"}), 400
    if session_id and not _SESSION_ID_RE.match(session_id):
        abort(400, "invalid session_id")
    parts = []
    if session_id:
        out = builds_admin.act("kill_wait", session_id, "report")
        parts.append(out.get("message", ""))
    if messages.request_stop(user_key, "관리자 중단"):
        parts.append("사용자 화면에 중단 신호 예약(다음 폴링 최대 30초)")
    msg = " / ".join(p for p in parts if p) or "중단할 대상이 없습니다"
    _audit("user_action", session_id=session_id or None,
           changed_fields=f"action=kill_wait user={user_key} :: {msg}"[:1500], result="ok")
    return jsonify({"ok": True, "message": msg})


@admin_panel_bp.get("/api/diagnostics/events")
def api_diagnostics_events():
    """진단 사건 목록 — 서버 500/503·느린 요청·콜드 빌드 실패·브라우저/Honey 오류.

    "에러가 났는데 어디를 봐야 하나"의 단일 진입점이다 (server/diagnostics.py 참조)."""
    from admin_panel import diagnostics_admin
    return jsonify(diagnostics_admin.events(request.args))


@admin_panel_bp.get("/api/diagnostics/events/<event_id>")
def api_diagnostics_event(event_id):
    """사건 1건 + 상관 ID 로 이어진 타임라인 + 빌드 기록 + watchdog + 원인 안내."""
    from admin_panel import diagnostics_admin
    hours = min(max(int(request.args.get("hours", 24 * 7)), 1), 24 * 14)
    return jsonify(diagnostics_admin.event_detail(event_id, hours))


@admin_panel_bp.post("/api/diagnostics/events/<event_id>/ack")
def api_diagnostics_ack(event_id):
    """사건 확인 처리 (미확인 경고 칩에서 제외된다)."""
    import diagnostics
    ok = diagnostics.ack(event_id, by="admin-panel")
    _audit("diag_ack", session_id=None, changed_fields=f"event_id={event_id}",
           result="ok" if ok else "fail")
    return jsonify({"ok": ok})


@admin_panel_bp.get("/api/active_users")
def api_active_users():
    """실시간 접속 사용자 — 사용자 탭 전용(10초 폴링).

    api/runtime 에도 같은 값이 실려 있지만, 사용자 탭은 응답시간·캐시·스케줄러가 필요 없어
    이 가벼운 엔드포인트를 따로 쓴다.

    여기서 두 가지를 덧붙인다(둘 다 계측 자체는 건드리지 않는다):
      - 클라 버전: UA 토큰이 없는 행은 버전 대장(DB)의 마지막 실행 버전으로 폴백
      - 대기 상태: 그 사람이 보는 세션이 지금 콜드 빌드 중인지 (build_status 순간값)
    """
    out = metrics.active_users(request.args.get("window", metrics.ACTIVE_USER_WINDOW_SEC))
    rows = out.get("users") or []
    users_admin.attach_names(rows, "user")
    _attach_client_version(rows)
    _attach_waiting(rows)
    return jsonify(out)


def _attach_client_version(rows):
    """UA 에 버전 토큰이 없는 행을 버전 대장으로 메운다 (`ver_src`: "ua" | "db").

    구버전 클라는 어느 쪽에도 값이 없어 빈 문자열로 남는다 — 그 자체가 '업데이트 안 함'
    신호다. DB 조회는 배치 1회(행마다 조회하면 N+1)."""
    need = [r.get("user") for r in rows if r.get("user") and not r.get("ver")]
    versions = {}
    if need:
        try:
            versions = report_db.get_client_versions(need)
        except Exception:
            versions = {}
    for r in rows:
        if r.get("ver"):
            r["ver_src"] = "ua"
            continue
        v = versions.get(str(r.get("user") or "").lower(), "")
        r["ver"] = v
        r["ver_src"] = "db" if v else ""


def _attach_waiting(rows):
    """보고 있는 세션이 콜드 빌드 중이면 `waiting`={stage, elapsed} 를 붙인다.

    소스는 build_status 의 메모리 스냅샷 하나라 DB·파일 접근이 없다 (진행 중 빌드가
    없으면 비용 0). builds_admin.active_builds() 는 세션 메타까지 붙이는 무거운 쪽이라
    10초 폴링에는 쓰지 않는다."""
    sids = {r.get("session_id") for r in rows if r.get("session_id")}
    if not sids:
        return
    try:
        from web_report import build_status
        builds = build_status.snapshot_all()
    except Exception:
        return
    by_sid = {}
    for b in builds:
        sid = b.get("session_id")
        if sid not in sids:
            continue
        cur = by_sid.get(sid)
        # 한 세션에 여러 stage 가 동시에 돌면 가장 오래된 것을 대표로 — 사용자가 실제로
        # 기다린 시간이 그것이다.
        if cur is None or float(b.get("elapsed") or 0) > float(cur.get("elapsed") or 0):
            by_sid[sid] = b
    for r in rows:
        b = by_sid.get(r.get("session_id"))
        if b:
            r["waiting"] = {"stage": b.get("stage") or "report",
                            "elapsed": round(float(b.get("elapsed") or 0))}


@admin_panel_bp.get("/api/user_timeline")
def api_user_timeline():
    """접속자 1명의 최근 요청 목록 — 사용자 탭에서 행을 펼칠 때만 호출된다.

    '지금 하는 일'은 마지막 요청 1건이라 무엇을 하다 막혔는지 흐름이 안 보인다.
    소스는 메모리 링버퍼(사람당 최근 20건)라 서버 재시작 시 비워진다."""
    out = metrics.user_timeline(
        request.args.get("key", ""),
        request.args.get("window", metrics.ACTIVE_USER_WINDOW_SEC))
    return jsonify(out)


@admin_panel_bp.get("/api/client_versions")
def api_client_versions():
    """Honey 클라 버전 현황 — 버전별 인원 + 사용자별 마지막 실행 버전.

    '지금 접속 중' 표가 순간을 본다면 이쪽은 **최근 N일 안에 Honey 를 실행한 전원**을
    본다(지금 안 켠 사람 포함). 최신 버전은 릴리스 manifest 에서 읽어 함께 준다 —
    화면이 '구버전 몇 명'을 판정하는 기준이다."""
    out = report_db.version_report(request.args.get("days", 30))
    out["latest"] = _latest_release_version()
    users_admin.attach_names(out.get("rows"), "user_id")
    return jsonify(out)


def _latest_release_version():
    """releases/version.json 의 최신 버전 문자열. 없으면 "" (화면은 비교를 생략한다)."""
    try:
        import json
        path = config.HONEY_VERSION_JSON
        if not path.exists():
            return ""
        return str(json.loads(path.read_text(encoding="utf-8")).get("version") or "")
    except Exception:
        return ""


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
        # 진행 중 콜드 빌드는 (세션, stage, 경과) 3칸만으로는 손을 쓸 수 없어, 세션 메타·
        # 대기자·워커 현재 단계·예상 대비 초과를 붙여서 준다 (admin_panel/builds_admin.py).
        # 빌드 0건이면 DB·파일 접근이 전혀 없다.
        from admin_panel import builds_admin
        out["builds"] = builds_admin.active_builds()
        out["build_queues"] = builds_admin.queues()
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
    out = stats.user_ranking(request.args.get("days", 30))
    users_admin.attach_names(out.get("rows"), "who")
    return jsonify(out)


@admin_panel_bp.get("/api/stats/client_errors")
def api_stats_client_errors():
    return jsonify(stats.client_error_count(request.args.get("hours", 24)))


@admin_panel_bp.get("/api/stats/usage")
def api_stats_usage():
    """접속 사용량 순위 — Honey 실행 · 웹페이지 방문 (report_usage_daily 집계)."""
    out = stats.usage_ranking(request.args.get("days", 30))
    users_admin.attach_names(out.get("rows"), "user_id")
    return jsonify(out)


@admin_panel_bp.get("/api/stats/usage_trend")
def api_stats_usage_trend():
    """일별 접속 추이 — 고유 사용자·접속 횟수·주간(WAU)·누적·일별 Peak 동시 접속자."""
    return jsonify(stats.usage_trend(request.args.get("days", 30)))


@admin_panel_bp.get("/api/stats/usage_hourly")
def api_stats_usage_hourly():
    """요일×시간 접속 히트맵 (report_usage_hourly 집계)."""
    return jsonify(stats.usage_hourly_heatmap(request.args.get("days", 30)))


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
    out = chatbot_admin.list_logs(
        q=(request.args.get("q") or "").strip() or None,
        limit=request.args.get("limit", 50),
        offset=request.args.get("offset", 0),
        errors_only=request.args.get("errors") == "1",
    )
    users_admin.attach_names(out.get("rows"), "user")
    return jsonify(out)


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
    """[폐지 2026-08-14] 세션 PIN 설정.

    세션 PIN 은 접근제어에 쓰이지 않은 지 오래인데 평문으로 DB 에 남아 있었다. 신규 저장은
    중단하고 기존 값은 마이그레이션에서 비운다. 라우트는 구 화면이 호출해도 500 이 되지
    않도록 남겨두되, 저장은 하지 않고 410 으로 폐지를 알린다."""
    if not _SESSION_ID_RE.match(session_id):
        abort(400, "invalid session_id")
    return jsonify({"ok": False, "session_id": session_id, "has_password": False,
                    "error": "세션 PIN 은 폐지되었습니다 (접근제어는 신원 기반)."}), 410


# ── 사용자(웹 로그인 계정) 컨트롤 ───────────────────────────────────────────

def _norm_user_id(user_id):
    """URL 로 들어온 계정 → 신원 키. 규칙은 사용자 라우트와 같다 (identity_norm)."""
    uid = normalize_uid(user_id)
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


@admin_panel_bp.post("/api/user/<user_id>/name")
def api_user_set_name(user_id):
    """사용자 실명 지정/변경 (오타·개명 대응). 사용자 본인 경로는
    report/routes_misc.py 의 POST /api/auth/display_name."""
    uid = _norm_user_id(user_id)
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not _DISPLAY_NAME_RE.match(name):
        abort(400, "이름은 한글 2~10자로 입력해주세요. (예: 홍길동)")
    users_admin.set_display_name(uid, name, admin_user="admin-panel")
    _audit("edit", changed_fields="user_name_set(%s)" % uid)
    return jsonify({"ok": True, "user_id": uid, "display_name": name})


@admin_panel_bp.post("/api/user/<user_id>/delete")
def api_user_delete(user_id):
    uid = _norm_user_id(user_id)
    if not users_admin.delete_user(uid):
        abort(404, "user not found")
    _audit("delete", changed_fields="user_delete(%s)" % uid)
    return jsonify({"ok": True, "user_id": uid})


# ── 사용자 팝업 메시지 (프로세스 메모리 — admin_panel/messages.py) ────────────
# 수신·읽음 처리는 사용자 쪽 라우트(report/routes_misc.py)가 담당한다.

@admin_panel_bp.get("/api/messages")
def api_messages():
    return jsonify({"rows": messages.list_all()})


@admin_panel_bp.post("/api/messages")
def api_message_create():
    body = request.get_json(force=True, silent=True) or {}
    targets = messages.normalize_targets(body.get("targets"))
    for uid in targets:
        if not _USER_ID_RE.match(uid):
            abort(400, "invalid target user: %s" % uid)
    try:
        msg = messages.create(
            body.get("body"), title=body.get("title"), targets=targets,
            level=(body.get("level") or "info"), created_by="admin-panel")
    except ValueError:
        abort(400, "body required")
    _audit("edit", changed_fields="admin_message_send(id=%d, targets=%s)"
           % (msg["id"], ",".join(targets) or "all"))
    return jsonify(msg)


@admin_panel_bp.post("/api/messages/<int:message_id>/revoke")
def api_message_revoke(message_id):
    if not messages.revoke(message_id):
        abort(404, "message not found")
    _audit("edit", changed_fields="admin_message_revoke(id=%d)" % message_id)
    return jsonify({"ok": True, "id": message_id})


@admin_panel_bp.post("/api/messages/<int:message_id>/delete")
def api_message_delete(message_id):
    if not messages.delete(message_id):
        abort(404, "message not found")
    _audit("delete", changed_fields="admin_message_delete(id=%d)" % message_id)
    return jsonify({"ok": True, "id": message_id})


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
    # 실명은 각 행에 붙인다 — 응답이 배열이라 별도 names 맵을 실을 자리가 없다(구조 유지).
    users_admin.attach_names(rows, "client_user", "resolved_user")
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

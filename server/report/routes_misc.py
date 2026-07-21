"""주석·즐겨찾기·페이지·vendor·정적모듈·히스토리·(폐지)인증 스텁·디버그 라우트
(Phase 4 분리 — 구 report_routes.py)."""
import logging
import re
import time
from pathlib import Path

from flask import abort, jsonify, make_response, request, send_file
from flask import session as flask_session
from werkzeug.security import check_password_hash, generate_password_hash

from auth_identity import (
    _from_login_session,
    current_user as _current_user,
    identity_source as _identity_source,
)
from config import (
    REPORT_ANALYSIS_INDEX_HTML,
    REPORT_VIEW_HTML,
)
from database import report_db
from product_info import list_search_candidates
from report.report_extension import report_bp
from report.security import (
    _PIN_RE,
    _USER_ID_RE,
    _active_or_404,
    _client_meta,
    _issue_csrf_cookie,
    _normalize_user_id,
    _private_guard,
    _require_csrf,
    _validate_session_id,
)
from report.static_pages import send_html_gzip

_log = logging.getLogger(__name__)

# CSRF 토큰 쿠키 sliding refresh — 모든 /pe/report/* 응답에서 만료(24h)를 연장한다.
# 페이지를 하루 이상 열어두면 저장이 전부 403 되던 문제의 재발급 경로(403 응답도 재발급).
report_bp.after_request(_issue_csrf_cookie)


# ── annotations ───────────────────────────────────────────────────────────────

@report_bp.post("/annotation")
def create_annotation():
    _require_csrf()
    body = request.get_json(force=True, silent=True) or {}
    session_id = body.get("session_id", "")
    _validate_session_id(session_id)
    analysis_key = body.get("analysis_key")
    target = (body.get("target") or "").strip()
    content = (body.get("content") or "").strip()
    if not target or not content:
        abort(400, "target and content are required")
    ann_id = report_db.create_annotation(session_id, analysis_key, target, content)
    return jsonify({"id": ann_id, "session_id": session_id, "target": target}), 201


@report_bp.get("/annotation/<session_id>")
def list_annotations(session_id):
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if session:
        _private_guard(session)
        _active_or_404(session)
    return jsonify(report_db.get_annotations(session_id))


@report_bp.patch("/annotation/<int:aid>")
def update_annotation(aid):
    _require_csrf()
    body = request.get_json(force=True, silent=True) or {}
    content = (body.get("content") or "").strip()
    if not content:
        abort(400, "content is required")
    report_db.update_annotation(aid, content)
    return jsonify({"id": aid, "updated": True})


@report_bp.delete("/annotation/<int:aid>")
def delete_annotation(aid):
    _require_csrf()
    report_db.delete_annotation(aid)
    return jsonify({"id": aid, "deleted": True})


# ── Report Analysis index / view pages ───────────────────────────────────────
# gzip+ETag 캐시 서빙 (report_view.html 202KB 비압축 전송 제거) — static_pages 참조.

@report_bp.get("/")
def index_page():
    return send_html_gzip(REPORT_ANALYSIS_INDEX_HTML)   # CSRF 쿠키는 after_request 가 발급


@report_bp.get("/view/<session_id>")
def view_page(session_id):
    _validate_session_id(session_id)
    # 비공개·휴지통 세션은 상세 HTML 자체를 숨긴다. 세션이 없으면 기존대로 HTML 서빙(JS 가 에러 표시).
    session = report_db.get_session(session_id)
    if session:
        _private_guard(session)
        _active_or_404(session)
    return send_html_gzip(REPORT_VIEW_HTML)   # CSRF 쿠키는 after_request 가 발급


@report_bp.get("/help")
def help_page():
    return send_html_gzip(REPORT_VIEW_HTML.parent / "help.html")


# help.html 안내 스크린샷. 정적 폴더 라우트가 없어 vendor/webreport 와 같은 화이트리스트
# 방식으로 서빙한다(경로 traversal 차단). help.html 은 /pe/report/static/help_assets/<name>
# 루트상대 경로로 참조 — 상대경로면 페이지 URL 깊이가 달라질 때 다시 404 가 된다.
_HELP_ASSETS_DIR = REPORT_VIEW_HTML.parent / "static" / "help_assets"
_HELP_ASSET_RE = re.compile(r"^[A-Za-z0-9_]+\.png$")


@report_bp.get("/static/help_assets/<filename>")
def help_asset(filename):
    if not _HELP_ASSET_RE.match(filename):
        abort(404)
    path = _HELP_ASSETS_DIR / filename
    if not path.is_file():
        abort(404)
    resp = make_response(send_file(path, mimetype="image/png", conditional=True))
    # 배포와 함께만 바뀌는 안내 이미지 — vendor 와 동일하게 브라우저 캐시 허용
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ── Vendored 정적 자산 (Tabulator 등) ─────────────────────────────────────────
# report_view.html 이 send_file 로 통째 전송되고 정적 폴더 라우트가 없으므로, vendoring 한
# JS/CSS 를 화이트리스트로만 서빙(경로 traversal 차단). CDN/인터넷 불필요(폐쇄망 대응).
_VENDOR_DIR = REPORT_VIEW_HTML.parent / "vendor"
_VENDOR_MIME = {
    "tabulator.min.js": "application/javascript",
    "tabulator.min.css": "text/css",
    "plotly.min.js": "application/javascript",
    "exceljs.min.js": "application/javascript",
    "pretendard/PretendardVariable.woff2": "font/woff2",
}
# Luckysheet(Note 탭)는 자산 40여 개(css 가 폰트·스프라이트를 상대경로로 참조)라
# 파일별 나열 대신 luckysheet/ 하위 트리를 경로 정규식 + 확장자 mime 으로 서빙한다.
_LUCKYSHEET_PATH_RE = re.compile(r"^luckysheet/[A-Za-z0-9_./-]+$")
_VENDOR_EXT_MIME = {
    ".js": "application/javascript", ".css": "text/css",
    ".png": "image/png", ".gif": "image/gif", ".svg": "image/svg+xml",
    ".ico": "image/x-icon", ".ttf": "font/ttf", ".woff": "font/woff",
    ".woff2": "font/woff2", ".eot": "application/vnd.ms-fontobject",
}


@report_bp.get("/vendor/<path:filename>")
def vendor_asset(filename):
    mime = _VENDOR_MIME.get(filename)
    if not mime and _LUCKYSHEET_PATH_RE.match(filename) and ".." not in filename:
        mime = _VENDOR_EXT_MIME.get(Path(filename).suffix.lower())
    if not mime:
        abort(404)
    # 사전압축 .gz 가 있으면 그대로 서빙 (plotly.min.js 4.8MB→1.4MB). 요청마다 압축하지
    # 않도록 파일은 배포 시 미리 만들어 둔다 (vendor 파일 교체 시 .gz 도 함께 재생성할 것).
    path = _VENDOR_DIR / filename
    if not path.is_file():
        abort(404)   # luckysheet/ 트리는 정규식 통과라 실재 여부를 여기서 확정
    gz_path = _VENDOR_DIR / (filename + ".gz")
    if gz_path.exists() and "gzip" in (request.headers.get("Accept-Encoding") or ""):
        resp = make_response(send_file(gz_path, mimetype=mime))
        resp.headers["Content-Encoding"] = "gzip"
    else:
        resp = make_response(send_file(path, mimetype=mime))
    resp.headers["Vary"] = "Accept-Encoding"
    # vendor 는 수동 교체 전까지 불변 — 브라우저 캐시로 재방문 시 재다운로드/재검증 제거
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ── web_report 프런트 모듈 (Phase 7b — report_view.html 에서 분할된 classic scripts) ──
# 파일들은 순서대로 로드되면 분할 전 단일 <script> 와 이어붙인 내용이 동일하다
# (전역 스코프 공유). 배포마다 바뀔 수 있어 no-cache + 조건부(ETag/mtime) 304 로 서빙.
_WEBREPORT_STATIC_DIR = REPORT_VIEW_HTML.parent / "static" / "webreport"
_WEBREPORT_JS_RE = re.compile(r"^[a-z0-9_]+\.js$")


@report_bp.get("/static/webreport/<filename>")
def webreport_static(filename):
    if not _WEBREPORT_JS_RE.match(filename):
        abort(404)
    path = _WEBREPORT_STATIC_DIR / filename
    if not path.is_file():
        abort(404)
    resp = make_response(send_file(path, mimetype="application/javascript",
                                   conditional=True))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# Note 탭 Luckysheet 격리 iframe 문서 (text/html). Luckysheet 를 전역 CSS·스크롤·배율 오염이
# 없는 깨끗한 풀페이지로 실행해 셀 밀림·격자 떨림을 근본 차단한다(→ note.js·note_frame.html).
# 정적 JS 라우트(webreport_static)는 mimetype 을 application/javascript 로 강제하므로 별도.
@report_bp.get("/note_frame")
def note_frame_page():
    resp = make_response(send_file(REPORT_VIEW_HTML.parent / "note_frame.html",
                                   mimetype="text/html", conditional=True))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@report_bp.get("/api/history")
def history():
    filters = {
        "product_type": request.args.get("product_type") or None,
        "process": request.args.get("process") or None,
        "product": request.args.get("product") or None,
        "revision": request.args.get("revision") or None,
        "lot_id": request.args.get("lot_id") or None,
        "source": request.args.get("source") or None,
    }
    # 비공개 세션 필터: 업로더/위임 편집자 외에는 목록에서 숨긴다 (신원 없음 ""=전부 숨김).
    viewer = _current_user()
    limit_raw = request.args.get("limit")
    offset_raw = request.args.get("offset")
    if limit_raw is None and offset_raw is None:
        # 하위호환: 페이지네이션 파라미터가 없으면 기존 리스트 응답 (limit=500 고정)
        return jsonify(report_db.get_history(**filters, viewer=viewer))
    try:
        limit = max(1, min(int(limit_raw or 500), 1000))
    except (TypeError, ValueError):
        limit = 500
    try:
        offset = max(0, int(offset_raw or 0))
    except (TypeError, ValueError):
        offset = 0
    rows = report_db.get_history(**filters, limit=limit, offset=offset, viewer=viewer)
    total = report_db.count_history(**filters, viewer=viewer)
    return jsonify({"rows": rows, "total": total, "limit": limit, "offset": offset})


# ── 사용자 인증 (웹 로그인) ───────────────────────────────────────────────────
# 신원은 auth_identity provider 체인으로 매 요청 자동 식별한다:
#   SSO 헤더 → Honey UA → 웹 로그인 세션.
# Honey 는 UA 토큰으로 자동 식별되어 로그인이 불필요하고, 일반 브라우저는
# singleID + 비밀번호(4자리)로 로그인해 Honey 와 동등한 권한을 얻는다.
# 비밀번호 설정은 Honey 접속(identity_source()=="honey")에서만 가능하다 — 서버는 SECDS
# 계정의 실재 여부를 확인할 수단이 없고, Honey 실행 자체가 본인확인 역할을 한다.

_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_SEC = 300
# uid -> [실패횟수, 최초실패 monotonic]. 프로세스 메모리라 재기동 시 초기화되고
# 멀티프로세스 전환 시 무효 — 실질 탐지는 login_fail 감사로그가 담당한다.
_login_fails = {}


def _normalize_login_id(value):
    """로그인 폼의 singleID → current_user() 포맷(소문자, 도메인 없음).
    폼은 SECDS\\ 를 입력받지 않지만 붙여넣기 대비로 도메인 접두를 떼어낸다.
    (_normalize_user_id 는 백슬래시를 거부하므로 재사용할 수 없다.)"""
    uid = (value or "").split("\\")[-1].strip().lower()
    if not _USER_ID_RE.match(uid):
        abort(400, "invalid user_id")
    return uid


def _auth_audit(action, uid, result="ok"):
    """인증 이벤트 감사 기록 — best-effort (실패해도 요청을 깨뜨리지 않는다)."""
    try:
        ip, ua = _client_meta()
        report_db.log_audit(action, client_ip=ip, user_agent=ua,
                            client_user=uid, result=result)
    except Exception:
        pass


def _login_locked(uid):
    """5분 창 안에서 _LOGIN_MAX_FAILS 회 실패하면 잠금. 창이 지나면 카운터 초기화."""
    rec = _login_fails.get(uid)
    if not rec:
        return False
    if time.monotonic() - rec[1] > _LOGIN_LOCK_SEC:
        _login_fails.pop(uid, None)
        return False
    return rec[0] >= _LOGIN_MAX_FAILS


def _record_login_fail(uid):
    """실패 카운터 증가. 존재하지 않는 singleID 로 무작위 시도하면 항목이 다시 조회되지
    않아 영영 남으므로, 커지면 만료분을 일괄 정리해 무한 증식을 막는다."""
    now = time.monotonic()
    if len(_login_fails) > 1000:
        for k in [k for k, v in _login_fails.items() if now - v[1] > _LOGIN_LOCK_SEC]:
            _login_fails.pop(k, None)
    rec = _login_fails.setdefault(uid, [0, now])
    rec[0] += 1


@report_bp.post("/api/auth/login")
def auth_login():
    """singleID + 비밀번호(4자리) 웹 로그인."""
    _require_csrf()
    body = request.get_json(force=True, silent=True) or {}
    uid = _normalize_login_id(body.get("user_id"))
    pin = (body.get("password") or "").strip()

    if _login_locked(uid):
        _auth_audit("login_fail", uid, result="locked")
        return jsonify({"error": "로그인 시도가 너무 많습니다 — 5분 후 다시 시도해주세요."}), 429

    row = report_db.get_user(uid)
    if not row or not _PIN_RE.match(pin) or not check_password_hash(row["password_hash"], pin):
        _record_login_fail(uid)
        _auth_audit("login_fail", uid, result="fail")
        # singleID 존재 여부를 구분하지 않는 단일 메시지 (계정 열거 방지)
        return jsonify({"error": "singleID 또는 비밀번호가 올바르지 않습니다."}), 401

    _login_fails.pop(uid, None)
    flask_session.clear()               # session fixation 방지
    flask_session["uid"] = uid
    flask_session.permanent = True
    _auth_audit("login", uid)
    return jsonify({"ok": True, "user_id": uid, "source": "login"})


@report_bp.post("/api/auth/set_password")
def auth_set_password():
    """웹 로그인 비밀번호(4자리) 설정/변경 — Honey 로 접속한 본인 계정만.
    비밀번호를 잊으면 Honey 를 열어 다시 설정한다 (구 비밀번호 확인 없음)."""
    _require_csrf()
    if _identity_source() != "honey":
        return jsonify({
            "error": "비밀번호 설정은 Honey 앱에서만 가능합니다 (본인 확인 목적)."
        }), 403

    uid = _current_user()
    pin = ((request.get_json(force=True, silent=True) or {}).get("password") or "").strip()
    if not _PIN_RE.match(pin):
        return jsonify({"error": "비밀번호는 숫자 4자리여야 합니다."}), 400

    pw_hash = generate_password_hash(pin)
    if not report_db.update_user_password(uid, pw_hash):
        report_db.create_user(uid, pw_hash)
    _login_fails.pop(uid, None)
    _auth_audit("password_set", uid)
    return jsonify({"ok": True, "user_id": uid})


@report_bp.post("/api/auth/change_password")
def auth_change_password():
    """[폐지] /api/auth/set_password 로 대체 (Honey 에서 설정)."""
    _require_csrf()
    return jsonify({"error": "비밀번호는 Honey 앱에서 설정해주세요."}), 410


@report_bp.post("/api/auth/logout")
def auth_logout():
    _require_csrf()
    uid = _from_login_session()
    flask_session.clear()
    if uid:
        _auth_audit("logout", uid)
    return jsonify({"ok": True})


@report_bp.get("/api/auth/me")
def auth_me():
    """현재 신원 + 출처. has_pin 은 Honey 접속 시 '비밀번호 설정' UI 노출 판단용."""
    uid = _current_user()
    src = _identity_source()
    resp = {"user_id": uid, "source": src}
    if src == "honey" and uid:
        resp["has_pin"] = bool(report_db.get_user(uid))
    return jsonify(resp)


# ── user favorites (검색결과 즐겨찾기, 로그인 계정 별) ────────────────────────

@report_bp.get("/api/favorites")
def get_favorites():
    uid = _current_user()
    if not uid:
        return jsonify({"user_id": None, "favorites": []})
    return jsonify({"user_id": uid, "favorites": report_db.get_user_favorites(uid)})


@report_bp.post("/api/favorites")
def set_favorite():
    _require_csrf()
    uid = _current_user()
    if not uid:
        return jsonify({"error": "로그인해야 즐겨찾기를 사용할 수 있습니다."}), 401
    body = request.get_json(force=True, silent=True) or {}
    session_id = body.get("session_id", "")
    _validate_session_id(session_id)
    if not report_db.get_session(session_id):
        abort(404, "session not found")
    favorite = bool(body.get("favorite"))
    report_db.set_user_favorite(uid, session_id, favorite)
    return jsonify({"ok": True, "user_id": uid, "session_id": session_id,
                    "favorite": favorite})


@report_bp.get("/api/part_ids")
def part_ids():
    """product_info.db 의 part_id + sub_part_id(중괄호) flatten 검색 후보. 업로드 Product 검색용.

    파일 없음/읽기 실패는 best-effort 로 빈 리스트 반환(500 안 냄) — product_info 로더가 내부 처리.
    """
    return jsonify({"part_ids": list_search_candidates()})


# ── 클라이언트 JS 에러 beacon (error_beacon.js) ───────────────────────────────

_CLIENT_ERR_MAX_BODY = 8 * 1024
_CLIENT_ERR_WINDOW = 60.0            # per-IP 스로틀 창(초)
_CLIENT_ERR_MAX_PER_WINDOW = 10
_client_err_hits = {}                # {ip: [epoch, ...]} — best-effort in-memory


def _client_err_throttled(ip):
    import time
    now = time.time()
    hits = [t for t in _client_err_hits.get(ip, ()) if now - t < _CLIENT_ERR_WINDOW]
    if len(hits) >= _CLIENT_ERR_MAX_PER_WINDOW:
        _client_err_hits[ip] = hits
        return True
    hits.append(now)
    if len(_client_err_hits) > 1000:  # dict 비대 방지 (스로틀 리셋 감수)
        _client_err_hits.clear()
    _client_err_hits[ip] = hits
    return False


@report_bp.post("/api/client_error")
def client_error():
    """브라우저 JS 에러 beacon 수신. CSRF 미적용 — sendBeacon 은 커스텀 헤더를 못
    붙이고 이 라우트는 로그 기록만 한다. 어떤 입력에도 항상 204 (beacon 은 실패 불가)."""
    ip = request.remote_addr or "?"
    if (request.content_length or 0) > _CLIENT_ERR_MAX_BODY or _client_err_throttled(ip):
        return "", 204
    body = request.get_json(force=True, silent=True) or {}
    msg = str(body.get("message") or "")[:500]
    if not msg:
        return "", 204
    session_id = re.sub(r"[^0-9a-zA-Z_\-]", "", str(body.get("session_id") or ""))[:64]
    detail = " | ".join(p for p in (
        str(body.get("kind") or "error"),
        msg,
        f"{str(body.get('source'))[:300]}:{body.get('line') or ''}" if body.get("source") else "",
        f"tab={body.get('tab')}" if body.get("tab") else "",
        str(body.get("url") or "")[:300],
        str(body.get("stack") or "")[:1000],
    ) if p)
    _log.warning("client_error [%s] %s", ip, detail)
    try:  # 감사 기록은 best-effort — 실패해도 beacon 응답은 정상
        report_db.log_audit(
            action="client_error", session_id=session_id or None,
            changed_fields=detail[:1500], client_ip=ip,
            user_agent=request.headers.get("User-Agent"),
            client_user=_current_user(), result="error")
    except Exception:
        _log.warning("client_error 감사 기록 실패", exc_info=True)
    return "", 204


# ── debug helpers ─────────────────────────────────────────────────────────────

@report_bp.get("/_threads")
def debug_threads():
    """모든 스레드의 stack trace 덤프. hang 진단용."""
    import sys, threading, traceback
    out = []
    tid_to_name = {t.ident: t.name for t in threading.enumerate()}
    for tid, frame in sys._current_frames().items():
        name = tid_to_name.get(tid, "?")
        out.append(f"=== Thread {tid} ({name}) ===")
        out.append("".join(traceback.format_stack(frame)))
    from flask import Response
    return Response("\n".join(out), mimetype="text/plain; charset=utf-8")

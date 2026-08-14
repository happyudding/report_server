"""주석·즐겨찾기·페이지·vendor·정적모듈·히스토리·(폐지)인증 스텁·디버그 라우트
(Phase 4 분리 — 구 report_routes.py)."""
import logging
import re
import threading
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
    _DISPLAY_NAME_RE,
    _PIN_RE,
    _USER_ID_RE,
    _active_or_404,
    _client_meta,
    _editor_guard,
    _is_master,
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
    # 소속 세션의 편집 권한을 확인한다 — CSRF 만으로는 인가가 아니라, 무권한자가
    # 임의(비공개 포함) 세션에 주석을 다는 IDOR 이 열린다. 조회 라우트와 동일한 가드.
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    _private_guard(session)
    _active_or_404(session)
    guard = _editor_guard(session)
    if guard:
        return guard
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


def _annotation_session_or_404(aid):
    """주석 id → 소속 세션 조회. 무권한자가 정수 id 를 열거해 남의 주석을 수정/삭제하는
    IDOR 을 막기 위해, 편집/삭제 전 소속 세션의 편집 권한을 확인한다."""
    ann = report_db.get_annotation(aid)
    if not ann:
        abort(404, "annotation not found")
    session = report_db.get_session(ann["session_id"])
    if not session:
        abort(404, "session not found")
    _private_guard(session)
    return session


@report_bp.patch("/annotation/<int:aid>")
def update_annotation(aid):
    _require_csrf()
    session = _annotation_session_or_404(aid)
    guard = _editor_guard(session)
    if guard:
        return guard
    body = request.get_json(force=True, silent=True) or {}
    content = (body.get("content") or "").strip()
    if not content:
        abort(400, "content is required")
    report_db.update_annotation(aid, content)
    return jsonify({"id": aid, "updated": True})


@report_bp.delete("/annotation/<int:aid>")
def delete_annotation(aid):
    _require_csrf()
    session = _annotation_session_or_404(aid)
    guard = _editor_guard(session)
    if guard:
        return guard
    report_db.delete_annotation(aid)
    return jsonify({"id": aid, "deleted": True})


# ── Report Analysis index / view pages ───────────────────────────────────────
# gzip+ETag 캐시 서빙 (report_view.html 202KB 비압축 전송 제거) — static_pages 참조.

def _record_page_visit(kind):
    """접속 사용량 집계 (best-effort) — 관리자 통계 탭 '접속 사용량' 카드용.

    신원(HoneyUser UA / 웹 로그인)이 없으면 IP 로 집계. 실패해도 페이지 서빙을
    막지 않는다."""
    try:
        uid = _current_user()
        if not uid:
            ip, _ = _client_meta()
            uid = f"ip:{ip}" if ip else ""
        report_db.record_usage(kind, uid)
    except Exception:
        pass


@report_bp.get("/")
def index_page():
    _record_page_visit("web_index")
    return send_html_gzip(REPORT_ANALYSIS_INDEX_HTML)   # CSRF 쿠키는 after_request 가 발급


@report_bp.get("/view/<session_id>")
def view_page(session_id):
    _validate_session_id(session_id)
    # 비공개·휴지통 세션은 상세 HTML 자체를 숨긴다. 세션이 없으면 기존대로 HTML 서빙(JS 가 에러 표시).
    session = report_db.get_session(session_id)
    if session:
        _private_guard(session)
        _active_or_404(session)
    _record_page_visit("web_view")
    return send_html_gzip(REPORT_VIEW_HTML)   # CSRF 쿠키는 after_request 가 발급


# Honey 액션 브리지의 폴백 페이지. 세션 상세의 ✏️ 버튼은 이 URL 로 이동을 시도하고,
# Honey 내장 브라우저는 그 네비게이션을 가로채 취소한 뒤(honey_main._browser_leave_guard)
# 편집 다이얼로그를 띄우므로 실제 요청은 오지 않는다. 가드가 없는 환경(일반 브라우저,
# 구버전 Honey)에서 눌렸을 때 막다른 404 대신 이유를 알려주기 위한 라우트다.
@report_bp.get("/honey/session_meta/<session_id>")
def honey_session_meta_fallback(session_id):
    _validate_session_id(session_id)
    return make_response((
        '<meta charset="utf-8"><body style="font:16px/1.6 sans-serif;padding:40px">'
        '<h3>세션 정보 수정은 Honey 앱에서만 가능합니다.</h3>'
        '<p>Honey 앱으로 이 세션을 연 뒤 우상단 ✏️ 버튼을 눌러 주세요.</p>'
        f'<p><a href="/pe/report/view/{session_id}">← 세션으로 돌아가기</a></p>'
        '</body>'), 200)


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
    # error_beacon.js 만 콘텐츠 버전 URL(?v= — static_pages 가 HTML 에 주입)로 오면
    # immutable 장기 캐시. 나머지 모듈 17종은 순서·내용 결합이 있어 no-cache 유지.
    if filename == "error_beacon.js" and request.args.get("v"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
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


def _epoch_arg(name, end_of_day=False):
    """'YYYY-MM-DD' 쿼리 파라미터 → epoch 초(로컬). 없거나 형식 오류면 None."""
    value = (request.args.get(name) or "").strip()
    if not value:
        return None
    try:
        ts = int(time.mktime(time.strptime(value, "%Y-%m-%d")))
    except (TypeError, ValueError):
        return None
    return ts + 86399 if end_of_day else ts


def _uid_tail(value):
    """'SECDS\\hgd123' → 'hgd123' (소문자). 세션의 uploaded_by 처럼 도메인이 섞여 들어오는
    값을 report_user_profile 의 키(소문자 singleID)로 맞춘다."""
    return (value or "").split("\\")[-1].strip().lower()


@report_bp.get("/api/history")
def history():
    filters = {
        "product_type": request.args.get("product_type") or None,
        "process": request.args.get("process") or None,
        "product": request.args.get("product") or None,
        "revision": request.args.get("revision") or None,
        "lot_id": request.args.get("lot_id") or None,
        "source": request.args.get("source") or None,
        # 검색결과 페이지의 필터가 전부 서버로 넘어온다 — 예전엔 전량을 내려받아
        # 클라이언트에서 걸렀다(세션이 늘수록 첫 화면이 느려지는 구조).
        "q": (request.args.get("q") or "").strip() or None,
        "mode": (request.args.get("mode") or "").strip() or None,
        "date_from": _epoch_arg("date_from"),
        "date_to": _epoch_arg("date_to", end_of_day=True),
        "mine": request.args.get("mine") == "1",
        "visibility": (request.args.get("visibility") or "").strip() or None,
    }
    # 비공개 세션 필터: 업로더/위임 편집자 외에는 목록에서 숨긴다 (신원 없음 ""=전부 숨김).
    # master PC(admin 로그인 4h)는 비공개도 목록에 노출한다.
    viewer = _current_user()
    master = _is_master()
    limit_raw = request.args.get("limit")
    offset_raw = request.args.get("offset")
    if limit_raw is None and offset_raw is None:
        # 하위호환: 페이지네이션 파라미터가 없으면 기존 리스트 응답 (limit=500 고정)
        return jsonify(report_db.get_history(**filters, viewer=viewer, see_all_private=master))
    try:
        limit = max(1, min(int(limit_raw or 500), 1000))
    except (TypeError, ValueError):
        limit = 500
    try:
        offset = max(0, int(offset_raw or 0))
    except (TypeError, ValueError):
        offset = 0
    sort = (request.args.get("sort") or "new").strip()
    rows, total = report_db.get_history_page(**filters, limit=limit, offset=offset,
                                             viewer=viewer, sort=sort,
                                             see_all_private=master)
    # 신원을 함께 실어 첫 화면의 /api/auth/me 왕복을 없앤다 (auth_me 와 동일 규칙).
    viewer_info = {"user_id": viewer, "source": _identity_source(), "is_master": master,
                   "display_name": report_db.get_display_name(viewer) or ""}
    if viewer_info["source"] == "honey" and viewer:
        viewer_info["has_pin"] = bool(report_db.get_user(viewer))
    # 업로더 표기를 '이름(ID)' 로 그리기 위한 uid→이름 맵. 행마다 이름을 인라인하지 않고
    # 맵으로 한 번만 실어 보낸다(같은 업로더가 여러 행에 반복되므로).
    names = report_db.display_names([_uid_tail(r.get("uploaded_by")) for r in rows])
    return jsonify({"rows": rows, "total": total, "limit": limit, "offset": offset,
                    "viewer": viewer_info, "names": names})


# ── 사용자 인증 (웹 로그인) ───────────────────────────────────────────────────
# 신원은 auth_identity provider 체인으로 매 요청 자동 식별한다:
#   SSO 헤더 → Honey UA → 웹 로그인 세션.
# Honey 는 UA 토큰으로 자동 식별되어 로그인이 불필요하고, 일반 브라우저는
# singleID + 비밀번호(4자리)로 로그인해 Honey 와 동등한 권한을 얻는다.
# 계정 생성 경로는 2개다:
#   (1) Honey 접속에서 /api/auth/set_password — 실행 자체가 본인확인 역할.
#   (2) 웹 /api/auth/signup — 서버는 SECDS 계정 실재 여부를 확인할 수단이 없으므로
#       'Honey 사용 이력이 없는 미사용 singleID' 만 자유 가입시킨다(선점 차단).
# 이미 계정이 있는 사람의 비밀번호 재설정은 여전히 Honey(또는 관리자 초기화)뿐이다.

_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_SEC = 300
# uid -> [실패횟수, 최초실패 monotonic]. 프로세스 메모리라 재기동 시 초기화되고
# 멀티프로세스 전환 시 무효 — 실질 탐지는 login_fail 감사로그가 담당한다.
_login_fails = {}

# 웹 회원가입(계정 생성) — IP 당 시간창 제한 + 자동완성 힌트 조회 범위.
_SIGNUP_MAX_PER_IP = 5
_SIGNUP_WINDOW_SEC = 3600
_SIGNUP_HINT_WINDOW_SEC = 180 * 86400
# ip -> [가입횟수, 창 시작 monotonic] (_login_fails 와 동일한 프로세스 메모리 한계)
_signup_ips = {}


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


def _signup_flooded(ip):
    """같은 IP 의 가입이 1시간 안에 _SIGNUP_MAX_PER_IP 회를 넘었는지. _login_fails 와
    같은 프로세스 메모리라 재기동 시 초기화된다 — 실질 추적은 감사로그 signup 행."""
    if not ip:
        return False
    rec = _signup_ips.get(ip)
    if not rec:
        return False
    if time.monotonic() - rec[1] > _SIGNUP_WINDOW_SEC:
        _signup_ips.pop(ip, None)
        return False
    return rec[0] >= _SIGNUP_MAX_PER_IP


def _record_signup(ip):
    if not ip:
        return
    now = time.monotonic()
    if len(_signup_ips) > 1000:
        for k in [k for k, v in _signup_ips.items() if now - v[1] > _SIGNUP_WINDOW_SEC]:
            _signup_ips.pop(k, None)
    rec = _signup_ips.setdefault(ip, [0, now])
    rec[0] += 1


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
    return jsonify({"ok": True, "user_id": uid, "source": "login",
                    "display_name": report_db.get_display_name(uid) or ""})


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


@report_bp.post("/api/auth/signup")
def auth_signup():
    """웹 회원가입 — Honey 없이 일반 브라우저에서 계정을 만든다.

    본인 확인 수단이 없으므로 '아직 쓰인 적 없는 singleID' 만 자유 가입시킨다:
    이미 Honey 로 업로드/방문한 계정은 본인이 Honey 에서 설정하면 되고, 그 계정을
    타인이 선점하는 것을 막는다. (잘못 선점된 계정 회수는 관리자 패널 계정 탭 삭제.)"""
    _require_csrf()
    body = request.get_json(force=True, silent=True) or {}
    uid = _normalize_login_id(body.get("user_id"))
    pin = (body.get("password") or "").strip()
    if not _PIN_RE.match(pin):
        return jsonify({"error": "비밀번호는 숫자 4자리여야 합니다."}), 400
    # 이름은 폼에서 필수지만 **서버는 강제하지 않는다** — 브라우저에 캐시된 옛 JS 가 name
    # 없이 보내도 가입 자체는 되어야 한다. 비면 첫 화면에서 이름 입력창이 뜨므로 결과는 같다.
    name = (body.get("name") or "").strip()
    if name and not _DISPLAY_NAME_RE.match(name):
        return jsonify({"error": "이름은 1~30자여야 합니다."}), 400

    ip, _ua = _client_meta()
    if _signup_flooded(ip):
        _auth_audit("signup", uid, result="ratelimit")
        return jsonify({"error": "가입 시도가 너무 많습니다 — 잠시 후 다시 시도해주세요."}), 429

    if report_db.get_user(uid):
        return jsonify({
            "error": "이미 가입된 계정입니다. 비밀번호를 잊었다면 Honey 앱에서 재설정하세요."
        }), 409
    if report_db.has_honey_history(uid):
        return jsonify({
            "error": "이 계정은 Honey 사용 이력이 있습니다 — 비밀번호는 Honey 앱에서 설정해주세요."
        }), 403

    if not report_db.create_user(uid, generate_password_hash(pin)):
        # 동시 가입 경합 — 위 존재 확인과 INSERT 사이에 다른 요청이 만든 경우
        return jsonify({"error": "이미 가입된 계정입니다."}), 409
    if name:
        report_db.set_display_name(uid, name, "self")

    _record_signup(ip)
    _login_fails.pop(uid, None)
    flask_session.clear()               # session fixation 방지 (로그인과 동일)
    flask_session["uid"] = uid
    flask_session.permanent = True
    _auth_audit("signup", uid)
    return jsonify({"ok": True, "user_id": uid, "source": "login",
                    "display_name": name})


@report_bp.get("/api/auth/signup_hint")
def auth_signup_hint():
    """회원가입 창의 singleID 자동완성 힌트 — **요청자 자신의 IP** 로만 조회한다
    (IP 를 파라미터로 받지 않아 타인 IP 열거 불가). 신원 판단에는 쓰지 않는다.

    힌트의 출처가 Honey 업로드 기록이므로 잡히는 계정은 대개 가입 차단 대상이다
    (honey_seen) — 프런트는 그 경우 'Honey 앱에서 설정' 안내를 함께 띄운다."""
    ip, _ua = _client_meta()
    try:
        uid = report_db.recent_upload_user_by_ip(ip, int(time.time()) - _SIGNUP_HINT_WINDOW_SEC)
    except Exception:
        uid = None
    if not uid:
        return jsonify({})
    uid = uid.split("\\")[-1].strip().lower()
    return jsonify({"user_id": uid, "honey_seen": True})


@report_bp.get("/api/auth/me")
def auth_me():
    """현재 신원 + 출처. has_pin 은 Honey 접속 시 '비밀번호 설정' UI 노출 판단용.
    display_name 이 빈 문자열이면 프런트가 이름 입력창을 띄운다."""
    uid = _current_user()
    src = _identity_source()
    resp = {"user_id": uid, "source": src,
            "display_name": report_db.get_display_name(uid) or ""}
    if src == "honey" and uid:
        resp["has_pin"] = bool(report_db.get_user(uid))
    return jsonify(resp)


@report_bp.post("/api/auth/display_name")
def auth_display_name():
    """사용자 실명 등록/변경 — 신원이 확인된 본인만(Honey UA · 웹 로그인 · SSO 모두 허용).

    로그인 계정(report_user)이 없는 Honey 전용 사용자도 저장된다 — 저장소가 별도
    테이블(report_user_profile)이라 계정 유무와 무관하다. 이름은 표시 전용이며 접근제어에
    쓰지 않으므로, 사칭 위험은 화면이 항상 '이름(ID)' 로 ID 를 함께 보여주는 것으로 감당한다."""
    _require_csrf()
    uid = _current_user()
    if not uid:
        return jsonify({"error": "이름을 저장하려면 Honey 앱이나 웹 로그인이 필요합니다."}), 403
    name = ((request.get_json(force=True, silent=True) or {}).get("name") or "").strip()
    if not _DISPLAY_NAME_RE.match(name):
        return jsonify({"error": "이름은 1~30자여야 합니다."}), 400
    report_db.set_display_name(uid, name, "self")
    _auth_audit("display_name", uid)
    return jsonify({"ok": True, "user_id": uid, "display_name": name})


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


# ── 관리자 팝업 메시지 수신 (저장소는 admin_panel/messages.py — 프로세스 메모리) ──

def _message_user_key():
    """메시지 수신자 키 — 사용량 집계(_record_page_visit)와 같은 규칙.

    신원이 있으면 소문자 계정, 없으면 'ip:<addr>'. 둘 다 없으면 빈 문자열(수신 없음).
    """
    uid = _current_user()
    if uid:
        return uid.lower()
    ip, _ = _client_meta()
    return f"ip:{ip}" if ip else ""


@report_bp.get("/api/my_messages")
def my_messages():
    """아직 확인하지 않은 관리자 메시지. 화면이 30초마다 폴링하는 경로라 가볍게 유지한다.

    admin_panel 미등록(REPORT_ADMIN_SECRET 없음) 환경에서도 import 자체는 안전하지만,
    폴링이 페이지 동작을 막지 않도록 실패는 빈 목록으로 삼킨다."""
    stop = None
    try:
        from admin_panel import messages as admin_messages
        key = _message_user_key()
        rows = admin_messages.pending_for(key)
        # 관리자가 '접속중' 탭에서 이 사용자의 대기를 끊었으면 그 신호를 함께 싣는다 —
        # 방치된 탭의 콜드 빌드 폴링을 멈추게 하는 통로다(별도 폴링을 만들지 않는다).
        stop = admin_messages.take_stop(key)
    except Exception:
        rows = []
    out = {"messages": rows}
    if stop:
        out["stop"] = stop
    return jsonify(out)


@report_bp.post("/api/my_messages/<int:message_id>/ack")
def ack_my_message(message_id):
    """확인 버튼 — 그 사람에게 다시 뜨지 않게 읽음 기록."""
    _require_csrf()
    from admin_panel import messages as admin_messages
    admin_messages.mark_read(message_id, _message_user_key())
    return jsonify({"ok": True, "id": message_id})


@report_bp.get("/api/part_ids")
def part_ids():
    """product_info.db 의 part_id + sub_part_id(중괄호) flatten 검색 후보. 업로드 Product 검색용.

    파일 없음/읽기 실패는 best-effort 로 빈 리스트 반환(500 안 냄) — product_info 로더가 내부 처리.
    """
    return jsonify({"part_ids": list_search_candidates()})


# ── /pe 랜딩 현황 수치 ────────────────────────────────────────────────────────
# 무인증 공개 페이지에 나가는 값이라 **집계 숫자만** 싣는다 — 계정ID·IP·보고 있는
# session_id 는 어떤 경로로도 포함하지 않는다(active_users() 는 count 만 꺼내 쓴다).
# 랜딩 페이지(/pe)가 아니라 report_bp 에 두는 이유 2가지:
#   1) after_request 가 report_csrf 쿠키를 발급한다 — 랜딩이 첫 방문이어도 이 응답
#      하나로 쿠키가 심어져 로그아웃 POST 가 동작한다(GET /pe 응답엔 안 붙는다).
#   2) /pe/api/v1(public_api) 에 두면 metrics._skip_user_track 이 호출자를 활성 사용자
#      집계에서 빼버려, 랜딩을 보는 사람이 ONLINE 수에 안 잡히는 자기모순이 생긴다.

_LANDING_TTL_SEC = 30.0
_landing_cache = None                    # (ts, payload) — 신원 무관 전역 1슬롯
_landing_cache_lock = threading.Lock()


def _landing_stats():
    """세션/사용량/활성 접속자 집계 (TTL 캐시). -> (payload, cache_age_s)

    캐시가 필요한 이유: active_users() 는 조회 시점에 O(n) prune + identity_merge
    DB 조회를 한다. 캐시가 전역 1개로 충분한 이유: 세션 수를 비공개 포함 전체로
    세기로 해서 값이 신원별로 갈리지 않는다.
    """
    global _landing_cache
    now = time.time()
    cached = _landing_cache
    if cached and now - cached[0] < _LANDING_TTL_SEC:
        return cached[1], int(now - cached[0])

    # 부분 실패는 나머지 값을 살린다 — 랜딩이 500 이면 첫 화면이 통째로 죽는다.
    try:
        sessions = report_db.count_by_product_type()
    except Exception:
        _log.warning("landing: 세션 카운트 실패", exc_info=True)
        sessions = {}
    sessions["total"] = sum(sessions.values())
    try:
        recent = report_db.count_recent_activity(7)
    except Exception:
        _log.warning("landing: 최근 활동 집계 실패", exc_info=True)
        recent = {}
    try:
        usage = report_db.usage_totals()
    except Exception:
        _log.warning("landing: 사용량 집계 실패", exc_info=True)
        usage = {}
    try:
        from admin_panel import metrics
        au = metrics.active_users()
        active = {"count": int(au.get("count") or 0),
                  "window_sec": int(au.get("window_sec") or 0)}
    except Exception:
        _log.warning("landing: 활성 접속자 조회 실패", exc_info=True)
        active = {"count": 0, "window_sec": 0}

    payload = {"sessions": sessions, "recent": recent, "usage": usage, "active": active}
    with _landing_cache_lock:
        _landing_cache = (now, payload)
    return payload, 0


@report_bp.get("/api/landing")
def landing_info():
    """/pe 랜딩의 유일한 조회 — 신원(요청마다) + 현황 수치(30초 캐시)를 한 응답에."""
    stats, age = _landing_stats()
    return jsonify({
        "viewer": {"user_id": _current_user(), "source": _identity_source(),
                   "display_name": report_db.get_display_name(_current_user()) or ""},
        "cache_age_s": age,
        **stats,
    })


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
    _emit_client_event("browser", body, msg, session_id, ip)
    return "", 204


def _emit_client_event(component, body, msg, session_id, ip):
    """클라이언트 오류를 진단 사건으로도 남긴다 — 감사 로그와 역할이 다르다.

    감사 로그는 "누가 무엇을 했나"의 시계열이라 오류가 섞이면 이력이 밀려나고,
    상관 ID(요청·빌드)로 이어붙일 수도 없다. 사건 저장소가 그 짝이다."""
    try:
        import diagnostics
        kind = str(body.get("kind") or "error")[:40]
        sev = "info" if kind in ("poll_timeout",) else "warning"
        diagnostics.emit(sev, component, kind,
                         event_id=body.get("event_id"),
                         message=diagnostics.scrub_paths(msg),
                         session_id=session_id or None,
                         operation_id=str(body.get("operation_id") or "")[:32] or None,
                         request_id=str(body.get("error_id") or "")[:32] or None,
                         http_status=body.get("status") or None,
                         endpoint=str(body.get("url") or "")[:300] or None,
                         error_type=str(body.get("error_type") or "")[:80] or None,
                         source=str(body.get("source") or "")[:200] or None,
                         honey_version=str(body.get("version") or "")[:40] or None,
                         user=_current_user() or None, client_ip=ip,
                         stack=diagnostics.scrub_paths(body.get("stack") or "") or None,
                         detail=body.get("detail") or None)
    except Exception:
        _log.warning("진단 사건 기록 실패", exc_info=True)


# ── Honey 클라이언트 진단 수집 (client/transport/error_report.py) ─────────────

_CLIENT_DIAG_MAX_BODY = 640 * 1024   # detail 모드(정제 traceback + 실행 로그 꼬리)


@report_bp.post("/api/client_diagnostic")
def client_diagnostic():
    """Honey 데스크톱 앱의 오류 보고 수신. client_error 와 같은 규약(항상 204, CSRF
    미적용, IP 스로틀)이되 상세 본문 상한만 크다.

    event_id 는 **클라가 만든 값을 그대로 쓴다** — 서버가 잠깐 죽어 있던 동안 로컬
    큐에 쌓였다가 재전송되므로, 서버가 새 ID 를 발급하면 같은 사고가 여러 건으로
    보인다."""
    ip = request.remote_addr or "?"
    if (request.content_length or 0) > _CLIENT_DIAG_MAX_BODY or _client_err_throttled(ip):
        return "", 204
    body = request.get_json(force=True, silent=True) or {}
    msg = str(body.get("message") or "")[:500]
    if not msg:
        return "", 204
    session_id = re.sub(r"[^0-9a-zA-Z_\-]", "", str(body.get("session_id") or ""))[:64]
    if str(body.get("mode") or "minimal") != "detail":
        body.pop("detail", None)
    detail = " | ".join(p for p in (
        f"honey:{body.get('kind') or 'error'}",
        msg,
        f"op={body.get('operation_id')}" if body.get("operation_id") else "",
        f"ver={body.get('version')}" if body.get("version") else "",
    ) if p)
    _log.warning("honey_diagnostic [%s] %s", ip, detail)
    try:
        report_db.log_audit(
            action="client_error", session_id=session_id or None,
            changed_fields=detail[:1500], client_ip=ip,
            user_agent=request.headers.get("User-Agent"),
            client_user=_current_user(), result="error")
    except Exception:
        _log.warning("honey_diagnostic 감사 기록 실패", exc_info=True)
    _emit_client_event("honey", body, msg, session_id, ip)
    return "", 204


# ── debug helpers ─────────────────────────────────────────────────────────────

@report_bp.get("/_threads")
def debug_threads():
    """모든 스레드의 stack trace 덤프. hang 진단용. 스택·파일 경로가 노출되므로
    admin 로그인(master 게이트) PC 에서만 — 그 외엔 존재 자체를 숨긴다(404)."""
    if not _is_master():
        abort(404)
    from diag_listener import dump_threads_text
    from flask import Response
    return Response(dump_threads_text(), mimetype="text/plain; charset=utf-8")

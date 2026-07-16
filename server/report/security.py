"""라우트 공통 보안·검증 헬퍼 (Phase 4 분리 — 구 report_routes.py 상단부).

CSRF(double-submit cookie), 신원 가드(_uploader_guard/_editor_guard), 입력 검증,
감사 로그 기록(_audit) 등 routes_* 모듈들이 공유하는 요청-보안 계층.
신원 자체는 auth_identity provider 체인(SSO-ready)에서 온다.
"""
import logging
import re
import secrets

from flask import abort, jsonify, make_response, request

from auth_identity import current_user as _current_user, is_uploader as _is_uploader
from database import report_db

_log = logging.getLogger(__name__)

_ANALYSIS_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _validate_analysis_key(value):
    if not value or not _ANALYSIS_KEY_RE.match(value):
        abort(400, "invalid analysis_key")


def _validate_session_id(value):
    if not value or not _SESSION_ID_RE.match(value):
        abort(400, "invalid session_id")


# ── CSRF (double-submit cookie) ───────────────────────────────────────────────
# 쿠키 기반 세션 인증이 없고 PIN 을 본문으로 보내는 구조라, 표준 stateless 방어인
# double-submit 쿠키 패턴을 쓴다: GET(/, /view)에서 JS 가 읽을 수 있는 토큰 쿠키를
# 발급하고, 변경요청(PATCH/DELETE/POST)은 같은 토큰을 X-CSRF-Token 헤더로 되돌려
# 보낸다. 교차출처 공격자는 동일출처 정책 때문에 쿠키를 읽거나 커스텀 헤더를 위조할
# 수 없다. 단, Honey 클라이언트가 호출하는 /upload_xlsx 는 브라우저가 아니므로 제외.
_CSRF_COOKIE = "report_csrf"
_CSRF_HEADER = "X-CSRF-Token"


def _issue_csrf_cookie(resp):
    """토큰 쿠키 발급/만료 연장(sliding). 기존 토큰은 유지하고 max_age 만 갱신 —
    세션 상세를 하루 이상 열어둔 페이지의 24h 만료 → 저장 전부 403 을 막는다.
    report_bp.after_request 로 등록되어 모든 응답(403 포함)이 재발급 경로가 되고,
    클라 csrfToken() 은 쿠키를 라이브로 읽으므로 만료 후 재시도가 자동 복구된다.
    JS 가 읽어야 하므로 httponly=False."""
    token = request.cookies.get(_CSRF_COOKIE) or secrets.token_urlsafe(32)
    resp.set_cookie(
        _CSRF_COOKIE, token,
        max_age=86400, samesite="Strict",
        secure=request.is_secure, httponly=False, path="/",
    )
    return resp


def _require_csrf():
    """변경요청에서 헤더 토큰이 쿠키와 일치하는지 검증. 불일치 시 403(JSON) —
    abort(403, msg) 는 HTML 오류 페이지라 클라 toast 가 원인을 표시할 수 없다."""
    cookie = request.cookies.get(_CSRF_COOKIE) or ""
    header = request.headers.get(_CSRF_HEADER) or ""
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        _log.warning("CSRF reject: %s %s (cookie=%s, header=%s)",
                     request.method, request.path, bool(cookie), bool(header))
        abort(make_response(jsonify(
            {"error": "보안 토큰이 만료되었거나 없습니다 — 페이지를 새로고침 해주세요."}), 403))


def _public_session(session):
    """password 같은 민감 컬럼을 제거하고 has_password 플래그만 노출."""
    if not session:
        return session
    pub = dict(session)
    pub["has_password"] = bool(pub.get("password"))
    pub.pop("password", None)
    return pub


def _password_ok(session, password):
    """세션에 비밀번호가 설정돼 있으면 일치해야 True. 없으면 항상 True (legacy)."""
    stored = (session or {}).get("password")
    if not stored:
        return True
    return (password or "").strip() == stored


def _uploader_guard(session):
    """세션 삭제·비공개·권한부여 가드 — PC 사용자(HoneyUser) == 업로더.

    Honey 밖(신원 없음)은 읽기전용(401). 통과하면 None, 거부면 (json, status)."""
    uid = _current_user()
    if not uid:
        return jsonify({"error": "Honey 를 통해 접속한 사용자만 수정/삭제할 수 있습니다 (읽기 전용)."}), 401
    if not _is_uploader(session, uid):
        return jsonify({"error": "업로더만 수정/삭제할 수 있습니다."}), 403
    return None


def _editor_guard(session):
    """콘텐츠 편집·개인 중요표시 가드 — 업로더 본인 또는 위임받은 편집자면 통과.
    (삭제·비공개·권한부여는 _uploader_guard 로 업로더 전용 유지.)
    거부는 warning 로그를 남긴다 — "저장이 안 됐다" 신고 시 서버 측 추적 근거."""
    uid = _current_user()
    if not uid:
        _log.warning("editor guard reject (no identity): %s %s", request.method, request.path)
        return jsonify({"error": "Honey 를 통해 접속한 사용자만 편집할 수 있습니다 (읽기 전용)."}), 401
    sid = (session or {}).get("session_id")
    if _is_uploader(session, uid) or report_db.is_session_editor(sid, uid):
        return None
    _log.warning("editor guard reject: %s %s user=%r session=%s",
                 request.method, request.path, uid, sid)
    return jsonify({"error": "편집 권한이 없습니다."}), 403


def _can_view(session, uid=None):
    """비공개 세션 조회 가능 여부 — 공개면 항상 True. 비공개면 업로더 본인
    (legacy: uploaded_by 빈 세션은 Honey 신원 전원 — is_uploader 규칙) 또는
    위임 편집자만. _editor_guard 와 동일한 판별 프리미티브를 재사용한다."""
    if not int((session or {}).get("is_private") or 0):
        return True
    if uid is None:
        uid = _current_user()
    if not uid:
        return False
    return _is_uploader(session, uid) or report_db.is_session_editor(
        (session or {}).get("session_id"), uid)


def _private_guard(session):
    """비공개 세션 조회 가드 — 목록 숨김 정책과 일치하게 존재 자체를 숨긴다(404).
    (편집 가드의 401/403 JSON 은 세션 존재를 노출 — 조회는 404 스타일.)"""
    if not _can_view(session):
        abort(404, "session not found")


def _client_meta():
    """감사 로그용 (client_ip, user_agent). 역프록시 뒤면 X-Forwarded-For 첫 IP 사용."""
    fwd = request.headers.get("X-Forwarded-For")
    ip = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
    return ip, str(request.user_agent)


def _audit(action, session=None, session_id=None, changed_fields=None, result="ok"):
    """감사 로그 best-effort 기록 — 실패해도 본 요청 처리를 깨뜨리지 않는다."""
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
            result=result,
        )
    except Exception:
        pass


def _record_web_visit(session):
    """web_report 세션 조회 시 현재 Honey 사용자를 방문자 풀에 기록(best-effort).
    편집 권한 위임 시 후보 목록(report_web_visitor)에 쓰인다."""
    try:
        if (session or {}).get("source") == "web_report":
            uid = _current_user()
            if uid:
                report_db.record_web_visitor(uid)
    except Exception:
        pass


def _require_web_report_session(session_id):
    """session 조회 + web_report 세션인지 확인. 아니면 404. 비공개 세션은
    업로더/위임 편집자 외 404 (routes_webreport 전 라우트 공통 진입점)."""
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    if session.get("source") != "web_report":
        abort(404, "not a web_report session")
    _private_guard(session)
    return session


# ── 사용자 ID 검증 [ID/PW 로그인 폐지 후에도 편집자 위임 입력 검증에 사용] ──────

_USER_ID_RE = re.compile(r"^[^\s\\/]{1,64}$")
_PIN_RE = re.compile(r"^\d{4}$")   # 비밀번호는 숫자 4자리 (폐지 — 보존만)
_DEFAULT_PIN = "0000"              # 모든 신규 계정의 초기 비밀번호 (폐지 — 보존만)


def _normalize_user_id(value):
    """Windows ID 는 대소문자 무구분 — 소문자 정규화. 형식 불량이면 400."""
    uid = (value or "").strip().lower()
    if not _USER_ID_RE.match(uid):
        abort(400, "invalid user_id")
    return uid

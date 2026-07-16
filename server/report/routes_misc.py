"""주석·즐겨찾기·페이지·vendor·정적모듈·히스토리·(폐지)인증 스텁·디버그 라우트
(Phase 4 분리 — 구 report_routes.py)."""
import logging
import re
from pathlib import Path

from flask import abort, jsonify, make_response, request, send_file

from auth_identity import current_user as _current_user
from config import (
    REPORT_ANALYSIS_INDEX_HTML,
    REPORT_VIEW_HTML,
)
from database import report_db
from product_info import list_search_candidates
from report.report_extension import report_bp
from report.security import (
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
    # 비공개 세션은 상세 HTML 자체를 숨긴다. 세션이 없으면 기존대로 HTML 서빙(JS 가 에러 표시).
    session = report_db.get_session(session_id)
    if session:
        _private_guard(session)
    return send_html_gzip(REPORT_VIEW_HTML)   # CSRF 쿠키는 after_request 가 발급


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


# ── 사용자 인증 [폐지됨] ──────────────────────────────────────────────────────
# ID/PW 로그인은 폐지되고 신원은 auth_identity provider 체인(기본 HoneyUser UA)으로
# 매 요청 자동 식별한다. 아래 라우트는 구 프런트 호환용으로 남겨두되 비밀번호를
# 확인하지 않는다. report_user 테이블은 보존(미사용).

@report_bp.post("/api/auth/login")
def auth_login():
    """[폐지] 비밀번호 확인 없이 현재 UA 사용자만 돌려준다(호환용)."""
    _require_csrf()
    return jsonify({"ok": True, "user_id": _current_user(), "is_default_password": False})


@report_bp.post("/api/auth/change_password")
def auth_change_password():
    """[폐지] 비밀번호 로그인 폐지."""
    _require_csrf()
    return jsonify({"error": "비밀번호 로그인은 폐지되었습니다 (PC 계정으로 자동 식별)."}), 410


@report_bp.post("/api/auth/logout")
def auth_logout():
    _require_csrf()
    return jsonify({"ok": True})


@report_bp.get("/api/auth/me")
def auth_me():
    return jsonify({"user_id": _current_user()})


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
        return jsonify({"error": "Honey 를 통해 접속해야 즐겨찾기를 사용할 수 있습니다."}), 401
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
    """product_info.csv 의 part_id + sub_part_id(중괄호) flatten 검색 후보. 업로드 Product 검색용.

    파일 없음/파싱 실패는 best-effort 로 빈 리스트 반환(500 안 냄) — product_info 로더가 내부 처리.
    """
    return jsonify({"part_ids": list_search_candidates()})


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

"""VOC 게시판 라우트 — 페이지 + 목록/등록/상세/수정/상태/댓글/이미지/삭제 API.

데이터는 별도 SQLite(database/voc_db.py, REPORT_VOC_DB_PATH)에 저장하고, 스크린샷
파일은 동결된 storage_gateway 의 note_image 공개 API 를 voc_<id> 네임스페이스로
재사용한다(S3/로컬 폴백 그대로). 조회는 공개, 등록·수정·본인 글 삭제·댓글은 Honey
UA/SSO 신원 또는 **게스트 이름**(일반 브라우저) + CSRF. 처리 상태(Open/Close) 전환만
관리자 전용이며, 관리자 판별은 admin 대시보드 게이트 쿠키의 /pe/report 경로
사본(_is_admin)으로 한다. 감사는 voc_* 액션으로 메인 report.db 에 기록한다.

**게스트 신원**: Honey UA 가 없는 브라우저는 이름을 직접 적어 등록·댓글을 쓸 수 있다.
이름은 표시용일 뿐이라 그것만으로는 남의 글을 수정·삭제할 수 있으므로, 첫 게스트
쓰기에서 무작위 토큰을 발급해 httponly 쿠키(_GUEST_COOKIE)에 심고 글/댓글 행에 저장한다.
이후 수정·삭제 권한은 **그 토큰을 가진 브라우저**에게만 준다(이름 사칭 무력화).
게스트 글은 is_guest 로 표시해 Honey 계정 글과 화면에서 구분한다.
"""
import logging
import hmac
import re
import secrets
import uuid

from flask import Response, abort, jsonify, request

import storage_gateway
from admin_panel import GATE_COOKIE_VOC, voc_gate_token
from auth_identity import current_user as _current_user
from config import REPORT_VIEW_HTML
from database import report_db, voc_db
from report.report_extension import report_bp
from report.security import _client_meta, _require_csrf
from report.static_pages import send_html_gzip
from storage_gateway import S3NotConfigured

_log = logging.getLogger(__name__)

_CATEGORIES = ("버그", "개선 제안", "문의", "기타")
_TITLE_MAX = 120
_CONTENT_MAX = 4000
_COMMENT_MAX = 1000
_IMAGE_MAX_BYTES = 2 * 1024 * 1024
_IMAGE_MAX_COUNT = 3
# Flask 전역 MAX_CONTENT_LENGTH 는 2048MB(wsgi.py) — VOC 는 자체 상한으로 선차단.
_REQUEST_MAX_BYTES = _IMAGE_MAX_COUNT * _IMAGE_MAX_BYTES + 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_IMAGE_ID_RE = re.compile(r"^[a-f0-9]{32}\.(png|jpg)$")

# 게스트(일반 브라우저) 신원 — 이름은 표시용, 소유권은 이 쿠키의 토큰이 증명한다.
_GUEST_COOKIE = "report_voc_guest"
_GUEST_COOKIE_PATH = "/pe/report"
_GUEST_COOKIE_MAX_AGE = 180 * 24 * 3600     # 반년 — 자기 글을 나중에 고칠 수 있게
_GUEST_NAME_MAX = 20
_GUEST_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")


def _ns(voc_id):
    """storage_gateway note_image 네임스페이스 — 세션 id 형식(<epoch>_<hex>)과 충돌 불가."""
    return f"voc_{voc_id}"


def _is_admin():
    """관리자 여부 — admin 대시보드 로그인이 발급한 VOC 전용 게이트 쿠키(/pe/report 경로)."""
    return hmac.compare_digest(request.cookies.get(GATE_COOKIE_VOC, ""), voc_gate_token())


def _screenshot_urls(voc_id, images):
    """이미지 메타 → 프론트가 그대로 쓰는 {image_id, url} 목록."""
    return [{"image_id": img["image_id"],
             "url": f"/pe/report/api/voc/{voc_id}/screenshots/{img['image_id']}"}
            for img in images]


def _guest_token():
    """이 브라우저의 게스트 토큰 (형식 위반·없음이면 "")."""
    token = request.cookies.get(_GUEST_COOKIE, "")
    return token if _GUEST_TOKEN_RE.match(token) else ""


def _identity():
    """(uid, guest_token) — Honey 신원이 있으면 게스트 토큰은 쓰지 않는다."""
    uid = _current_user()
    return (uid, "") if uid else ("", _guest_token())


def _owns(row, uid, gtoken):
    """이 글/댓글의 작성자인가. Honey 계정 글과 게스트 글은 서로 넘볼 수 없다."""
    if row["guest_token"]:
        return bool(gtoken) and hmac.compare_digest(row["guest_token"], gtoken)
    return bool(uid) and row["user_id"] == uid


def _guest_name(body):
    """게스트 이름 검증 — (이름, 에러응답) 중 하나만 채워 반환."""
    name = (body.get("guest_name") or "").strip()
    if not 1 <= len(name) <= _GUEST_NAME_MAX:
        return None, (jsonify(
            {"error": f"이름을 입력해주세요 (1~{_GUEST_NAME_MAX}자)."}), 400)
    return name, None


def _issue_guest_cookie(resp, token):
    """게스트 토큰을 httponly 쿠키로 심는다 (이미 있으면 만료만 갱신)."""
    resp.set_cookie(_GUEST_COOKIE, token, max_age=_GUEST_COOKIE_MAX_AGE,
                    httponly=True, samesite="Lax", secure=request.is_secure,
                    path=_GUEST_COOKIE_PATH)
    return resp


def _public_row(row, **extra):
    """응답용 글/댓글 — guest_token 을 걷어내고 is_guest 불린만 남긴다."""
    pub = dict(row)
    pub["is_guest"] = bool(pub.pop("guest_token", None))
    pub.update(extra)
    return pub


def _text_field(body, name, maxlen):
    """제목/내용 공통 검증 — (값, 에러응답) 중 하나만 채워 반환."""
    value = (body.get(name) or "").strip()
    if not 1 <= len(value) <= maxlen:
        label = {"title": "제목", "content": "내용"}.get(name, name)
        return None, (jsonify({"error": f"{label}은 1~{maxlen}자입니다."}), 400)
    return value, None


def _audit_voc(action, voc_id, detail, uid, result="ok"):
    """voc_create/voc_delete 감사 — 메인 DB report_audit_log (best-effort).

    VOC 본문은 이미 별도 voc.db 에 확정된 뒤다. 메인 report.db 가 업로드/편집으로 바쁠 때
    감사 1행 때문에 사용자 응답을 최대 5초 붙잡지 않도록 100ms 안에 기록하거나 포기한다.
    """
    try:
        ip, ua = _client_meta()
        report_db.log_audit(
            action,
            changed_fields=f"voc_id={voc_id} {detail}"[:1500],
            client_ip=ip, user_agent=ua, client_user=uid, result=result,
            busy_timeout_ms=100,
        )
    except Exception:
        _log.warning("VOC 감사 기록 실패 (voc_id=%s)", voc_id, exc_info=True)


@report_bp.get("/voc")
def voc_page():
    return send_html_gzip(REPORT_VIEW_HTML.parent / "voc.html")   # CSRF 쿠키는 after_request 가 발급


@report_bp.get("/api/voc")
def voc_list():
    """VOC 목록 (익명 허용) — 최신순, limit/offset 페이지네이션, q 로 제목·번호 검색.

    본문·스크린샷은 싣지 않는다 (상세 API 전용)."""
    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 100))
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    q = (request.args.get("q") or "").strip()[:_TITLE_MAX] or None
    items, total = voc_db.list_voc(limit=limit, offset=offset, q=q)
    return jsonify({"items": items, "total": total, "limit": limit,
                    "offset": offset, "q": q or "",
                    "user": _current_user(), "is_admin": _is_admin()})


@report_bp.get("/api/voc/<int:voc_id>")
def voc_detail(voc_id):
    """VOC 상세 (익명 허용) — 본문 + 스크린샷 + 댓글 + 요청자 권한 플래그."""
    voc = voc_db.get_voc(voc_id)
    if not voc:
        abort(404, "voc not found")
    uid, gtoken = _identity()
    is_admin = _is_admin()
    mine = _owns(voc, uid, gtoken)
    comments = [_public_row(c, can_delete=is_admin or _owns(c, uid, gtoken))
                for c in voc_db.list_comments(voc_id)]
    return jsonify({
        "voc": _public_row(voc),
        "screenshots": _screenshot_urls(voc_id, voc_db.list_voc_images(voc_id)),
        "comments": comments,
        "user": uid, "is_admin": is_admin,
        "can_edit": mine, "can_delete": mine,
    })


@report_bp.post("/api/voc")
def voc_create():
    """VOC 등록 (multipart: category/title/content/guest_name/screenshots ≤3장).

    Honey 신원이 없으면 guest_name 이 필요하고, 그 브라우저에 게스트 토큰을 발급한다.
    전 검증 통과 후에만 쓰기 시작하고, 이미지 저장 실패 시 생성분(VOC 행 + 저장된
    이미지)을 정리해 불완전한 글이 남지 않게 한다."""
    _require_csrf()
    uid, gtoken = _identity()
    author = uid
    if not uid:
        author, err = _guest_name(request.form)
        if err:
            return err
        gtoken = gtoken or secrets.token_hex(16)   # 첫 게스트 쓰기면 새로 발급
    if (request.content_length or 0) > _REQUEST_MAX_BYTES:
        return jsonify({"error": f"요청이 너무 큽니다 (스크린샷 최대 {_IMAGE_MAX_COUNT}장, 장당 2MB)."}), 413
    category = (request.form.get("category") or "").strip()
    if category not in _CATEGORIES:
        return jsonify({"error": "분류가 올바르지 않습니다."}), 400
    title, err = _text_field(request.form, "title", _TITLE_MAX)
    if err:
        return err
    content, err = _text_field(request.form, "content", _CONTENT_MAX)
    if err:
        return err
    files = [f for f in request.files.getlist("screenshots")
             if f and (f.filename or "").strip()]
    if len(files) > _IMAGE_MAX_COUNT:
        return jsonify({"error": f"스크린샷은 최대 {_IMAGE_MAX_COUNT}장입니다."}), 400
    blobs = []
    for f in files:
        data = f.read()
        if not data:
            return jsonify({"error": "빈 이미지 파일이 있습니다."}), 400
        if len(data) > _IMAGE_MAX_BYTES:
            return jsonify({"error": "이미지가 너무 큽니다 (장당 최대 2MB)."}), 413
        if data[:8] == _PNG_MAGIC:
            ext = "png"
        elif data[:3] == _JPEG_MAGIC:
            ext = "jpg"
        else:
            return jsonify({"error": "PNG/JPEG 이미지만 업로드할 수 있습니다."}), 400
        blobs.append((data, ext))
    voc_id = voc_db.create_voc(author, category, title, content,
                               guest_token=gtoken or None)
    try:
        metas = []
        for i, (data, ext) in enumerate(blobs):
            image_id = f"{uuid.uuid4().hex}.{ext}"   # 원본 파일명은 경로에 미사용
            storage_gateway.save_note_image(_ns(voc_id), image_id, data)
            metas.append((image_id, "image/png" if ext == "png" else "image/jpeg", i))
        if metas:
            voc_db.add_voc_images(voc_id, metas)
    except Exception:
        _log.exception("VOC 이미지 저장 실패 — 롤백 (voc_id=%s)", voc_id)
        try:
            storage_gateway.delete_note_images(_ns(voc_id))
        except Exception:
            _log.warning("VOC 롤백 이미지 정리 실패 (voc_id=%s)", voc_id, exc_info=True)
        voc_db.delete_voc(voc_id)
        return jsonify({"error": "스크린샷 저장에 실패했습니다 — 다시 시도해주세요."}), 500
    _audit_voc("voc_create", voc_id,
               f"category={category} images={len(blobs)} title={title[:80]}",
               uid or f"guest:{author}")
    resp = jsonify({"ok": True, "id": voc_id})
    resp.status_code = 201
    return _issue_guest_cookie(resp, gtoken) if not uid else resp


@report_bp.patch("/api/voc/<int:voc_id>")
def voc_update(voc_id):
    """VOC 본문 수정 — 작성자 본인만(게스트는 등록한 브라우저에서만).

    스크린샷은 수정 대상이 아니다."""
    _require_csrf()
    uid, gtoken = _identity()
    voc = voc_db.get_voc(voc_id)
    if not voc:
        abort(404, "voc not found")
    if not _owns(voc, uid, gtoken):
        return jsonify({"error": "본인이 등록한 VOC 만 수정할 수 있습니다."}), 403
    body = request.get_json(silent=True) or {}
    category = (body.get("category") or "").strip()
    if category not in _CATEGORIES:
        return jsonify({"error": "분류가 올바르지 않습니다."}), 400
    title, err = _text_field(body, "title", _TITLE_MAX)
    if err:
        return err
    content, err = _text_field(body, "content", _CONTENT_MAX)
    if err:
        return err
    voc_db.update_voc(voc_id, category, title, content)
    _audit_voc("voc_edit", voc_id, f"category={category} title={title[:80]}",
               uid or f"guest:{voc['user_id']}")
    return jsonify({"ok": True, "id": voc_id})


@report_bp.post("/api/voc/<int:voc_id>/status")
def voc_set_status(voc_id):
    """처리 상태 전환 (Open ↔ Close) — 관리자 전용."""
    _require_csrf()
    if not _is_admin():
        return jsonify({"error": "관리자만 처리 상태를 변경할 수 있습니다."}), 403
    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    if status not in voc_db.STATUSES:
        return jsonify({"error": "상태 값이 올바르지 않습니다."}), 400
    if not voc_db.get_voc(voc_id):
        abort(404, "voc not found")
    voc_db.set_voc_status(voc_id, status)
    _audit_voc("voc_status", voc_id, f"status={status}", _current_user() or "admin-panel")
    return jsonify({"ok": True, "id": voc_id, "status": status})


@report_bp.post("/api/voc/<int:voc_id>/comments")
def voc_comment_create(voc_id):
    """댓글 등록 — Honey 신원 또는 guest_name.

    Close 된 글에도 남길 수 있다(상태는 잠금이 아니다)."""
    _require_csrf()
    uid, gtoken = _identity()
    if not voc_db.get_voc(voc_id):
        abort(404, "voc not found")
    body = request.get_json(silent=True) or {}
    author = uid
    if not uid:
        author, err = _guest_name(body)
        if err:
            return err
        gtoken = gtoken or secrets.token_hex(16)
    content = (body.get("content") or "").strip()
    if not 1 <= len(content) <= _COMMENT_MAX:
        return jsonify({"error": f"댓글은 1~{_COMMENT_MAX}자입니다."}), 400
    comment_id = voc_db.add_comment(voc_id, author, content,
                                    guest_token=gtoken or None)
    _audit_voc("voc_comment_create", voc_id, f"comment_id={comment_id}",
               uid or f"guest:{author}")
    resp = jsonify({"ok": True, "id": comment_id})
    resp.status_code = 201
    return _issue_guest_cookie(resp, gtoken) if not uid else resp


@report_bp.delete("/api/voc/<int:voc_id>/comments/<int:comment_id>")
def voc_comment_delete(voc_id, comment_id):
    """댓글 삭제 — 작성자 본인(게스트는 쓴 브라우저에서) 또는 관리자."""
    _require_csrf()
    uid, gtoken = _identity()
    is_admin = _is_admin()
    comment = voc_db.get_comment(voc_id, comment_id)
    if not comment:
        abort(404, "comment not found")
    if not is_admin and not _owns(comment, uid, gtoken):
        return jsonify({"error": "본인이 쓴 댓글만 삭제할 수 있습니다."}), 403
    voc_db.delete_comment(comment_id)
    _audit_voc("voc_comment_delete", voc_id, f"comment_id={comment_id}",
               uid or ("admin-panel" if is_admin else f"guest:{comment['user_id']}"))
    return jsonify({"ok": True, "id": comment_id})


@report_bp.get("/api/voc/<int:voc_id>/screenshots/<image_id>")
def voc_screenshot(voc_id, image_id):
    """VOC 스크린샷 서빙 — 소속 확인 후 반환 (타 VOC 이미지·임의 경로 404)."""
    if not _IMAGE_ID_RE.match(image_id):
        abort(404, "invalid image id")
    meta = voc_db.get_voc_image(voc_id, image_id)
    if not meta:
        abort(404, "image not found")
    try:
        data, mime = storage_gateway.load_note_image(_ns(voc_id), image_id)
    except S3NotConfigured:
        abort(503, "storage not available")
    except Exception:
        abort(404, "image not found")
    return Response(data, mimetype=mime, headers={
        "Cache-Control": "private, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    })


@report_bp.delete("/api/voc/<int:voc_id>")
def voc_delete(voc_id):
    """VOC 하드 삭제 — 작성자 본인만(게스트는 등록한 브라우저에서만).

    이미지 메타(CASCADE)+실파일 함께 정리."""
    _require_csrf()
    uid, gtoken = _identity()
    voc = voc_db.get_voc(voc_id)
    if not voc:
        abort(404, "voc not found")
    if not _owns(voc, uid, gtoken):
        return jsonify({"error": "본인이 등록한 VOC 만 삭제할 수 있습니다."}), 403
    voc_db.delete_voc(voc_id)   # DB 먼저(진실 원장) — 파일 정리 실패는 고아 파일 + 로그
    try:
        for w in storage_gateway.delete_note_images(_ns(voc_id)):
            _log.warning("VOC 이미지 정리 경고 (voc_id=%s): %s", voc_id, w)
    except Exception:
        _log.warning("VOC 이미지 정리 실패 (voc_id=%s)", voc_id, exc_info=True)
    _audit_voc("voc_delete", voc_id, f"title={voc['title'][:80]}",
               uid or f"guest:{voc['user_id']}")
    return jsonify({"ok": True, "id": voc_id})

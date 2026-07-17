"""VOC 게시판 라우트 — 페이지 + 목록/등록/이미지/삭제 API.

데이터는 별도 SQLite(database/voc_db.py, REPORT_VOC_DB_PATH)에 저장하고, 스크린샷
파일은 동결된 storage_gateway 의 note_image 공개 API 를 voc_<id> 네임스페이스로
재사용한다(S3/로컬 폴백 그대로). 조회는 공개, 등록·본인 글 삭제는 Honey UA/SSO
신원 + CSRF. 감사는 voc_create/voc_delete 만 메인 report.db 에 기록한다.
"""
import logging
import re
import uuid

from flask import Response, abort, jsonify, request

import storage_gateway
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
_IMAGE_MAX_BYTES = 2 * 1024 * 1024
_IMAGE_MAX_COUNT = 3
# Flask 전역 MAX_CONTENT_LENGTH 는 2048MB(wsgi.py) — VOC 는 자체 상한으로 선차단.
_REQUEST_MAX_BYTES = _IMAGE_MAX_COUNT * _IMAGE_MAX_BYTES + 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_IMAGE_ID_RE = re.compile(r"^[a-f0-9]{32}\.(png|jpg)$")


def _ns(voc_id):
    """storage_gateway note_image 네임스페이스 — 세션 id 형식(<epoch>_<hex>)과 충돌 불가."""
    return f"voc_{voc_id}"


def _audit_voc(action, voc_id, detail, uid, result="ok"):
    """voc_create/voc_delete 감사 — 메인 DB report_audit_log (best-effort)."""
    try:
        ip, ua = _client_meta()
        report_db.log_audit(
            action,
            changed_fields=f"voc_id={voc_id} {detail}"[:1500],
            client_ip=ip, user_agent=ua, client_user=uid, result=result,
        )
    except Exception:
        _log.warning("VOC 감사 기록 실패 (voc_id=%s)", voc_id, exc_info=True)


@report_bp.get("/voc")
def voc_page():
    return send_html_gzip(REPORT_VIEW_HTML.parent / "voc.html")   # CSRF 쿠키는 after_request 가 발급


@report_bp.get("/api/voc")
def voc_list():
    """VOC 목록 (익명 허용) — 최신순, limit/offset 페이지네이션."""
    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 100))
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    uid = _current_user()
    items, total = voc_db.list_voc(limit=limit, offset=offset)
    for it in items:
        it["can_delete"] = bool(uid) and it["user_id"] == uid
        it["screenshots"] = [
            {"image_id": img["image_id"],
             "url": f"/pe/report/api/voc/{it['id']}/screenshots/{img['image_id']}"}
            for img in it.pop("images")]
    return jsonify({"items": items, "total": total, "limit": limit,
                    "offset": offset, "user": uid})


@report_bp.post("/api/voc")
def voc_create():
    """VOC 등록 (multipart: category/title/content/screenshots ≤3장).

    전 검증 통과 후에만 쓰기 시작하고, 이미지 저장 실패 시 생성분(VOC 행 + 저장된
    이미지)을 정리해 불완전한 글이 남지 않게 한다."""
    _require_csrf()
    uid = _current_user()
    if not uid:
        return jsonify({"error": "Honey 를 통해 접속한 사용자만 등록할 수 있습니다."}), 401
    if (request.content_length or 0) > _REQUEST_MAX_BYTES:
        return jsonify({"error": f"요청이 너무 큽니다 (스크린샷 최대 {_IMAGE_MAX_COUNT}장, 장당 2MB)."}), 413
    category = (request.form.get("category") or "").strip()
    if category not in _CATEGORIES:
        return jsonify({"error": "분류가 올바르지 않습니다."}), 400
    title = (request.form.get("title") or "").strip()
    if not 1 <= len(title) <= _TITLE_MAX:
        return jsonify({"error": f"제목은 1~{_TITLE_MAX}자입니다."}), 400
    content = (request.form.get("content") or "").strip()
    if not 1 <= len(content) <= _CONTENT_MAX:
        return jsonify({"error": f"내용은 1~{_CONTENT_MAX}자입니다."}), 400
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
    voc_id = voc_db.create_voc(uid, category, title, content)
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
               f"category={category} images={len(blobs)} title={title[:80]}", uid)
    return jsonify({"ok": True, "id": voc_id}), 201


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
    """VOC 하드 삭제 — 작성자 본인만. 이미지 메타(CASCADE)+실파일 함께 정리."""
    _require_csrf()
    uid = _current_user()
    if not uid:
        return jsonify({"error": "Honey 를 통해 접속한 사용자만 삭제할 수 있습니다."}), 401
    voc = voc_db.get_voc(voc_id)
    if not voc:
        abort(404, "voc not found")
    if voc["user_id"] != uid:
        return jsonify({"error": "본인이 등록한 VOC 만 삭제할 수 있습니다."}), 403
    voc_db.delete_voc(voc_id)   # DB 먼저(진실 원장) — 파일 정리 실패는 고아 파일 + 로그
    try:
        for w in storage_gateway.delete_note_images(_ns(voc_id)):
            _log.warning("VOC 이미지 정리 경고 (voc_id=%s): %s", voc_id, w)
    except Exception:
        _log.warning("VOC 이미지 정리 실패 (voc_id=%s)", voc_id, exc_info=True)
    _audit_voc("voc_delete", voc_id, f"title={voc['title'][:80]}", uid)
    return jsonify({"ok": True, "id": voc_id})

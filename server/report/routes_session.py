"""세션 조회/삭제/권한 라우트 (Phase 4 분리 — 구 report_routes.py).

/result /session/<sid> /session/<sid>/full — 조회,
DELETE /session/<sid>, important/private/verify_password/content — 변경,
my_access/editors — 권한·위임. URL·응답 형태는 분리 전과 동일하다.
"""
import gzip
import json
import logging
from pathlib import Path

from flask import Response, abort, jsonify, request

from auth_identity import current_user as _current_user, is_uploader as _is_uploader
from config import REPORT_UPLOAD_DIR
from database import report_db
import storage_gateway
from report.report_extension import report_bp
from report.security import (
    _audit,
    _editor_guard,
    _normalize_user_id,
    _public_session,
    _record_web_visit,
    _require_csrf,
    _uploader_guard,
    _validate_session_id,
)
from web_report import service as web_report_service
from web_report import response_cache as web_report_response_cache

_log = logging.getLogger(__name__)


def _load_json_object(objects, object_type):
    """objects 인덱스에 object_type 이 있으면 S3 JSON 다운로드, 실패 시 None."""
    return storage_gateway.load_json_object(objects, object_type)


@report_bp.get("/result/<session_id>")
def result(session_id):
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    analysis_key = session.get("analysis_key")
    summary = report_db.get_summary_by_analysis_key(analysis_key) if analysis_key else []
    return jsonify({
        "session_id": session_id,
        "analysis_key": analysis_key,
        "status": session.get("status"),
        "file_name": session.get("file_name"),
        "error_message": session.get("error_message"),
        "summary": summary,
    })


@report_bp.get("/session/<session_id>")
def session_info(session_id):
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    return jsonify(_public_session(session))


@report_bp.get("/session/<session_id>/full")
def session_full(session_id):
    """세션 완전 복원에 필요한 모든 참조 반환."""
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    akey = session.get("analysis_key")
    objects = {}
    if akey:
        for obj in report_db.get_all_object_infos(akey):
            objects[obj["object_type"]] = {
                "s3_uri": obj["s3_uri"],
                "s3_key": obj["s3_key"],
            }

    charts = []
    if "chart_index" in objects:
        manifest = _load_json_object(objects, "chart_index")
        count = int((manifest or {}).get("count", 0))
        charts = [{"index": i, "url": f"/pe/report/chart/{session_id}/{i}"}
                  for i in range(count)]
    # Issue_table 행별 분포 이미지. 저장소(S3 또는 로컬 폴백)에서 행 인덱스를 조회.
    # web_report 세션은 issue 이미지 업로드 흐름이 없으므로 S3 왕복 자체를 생략.
    issue_images = []
    if akey and session.get("source") != "web_report":
        try:
            for row in storage_gateway.list_issue_image_rows(akey):
                issue_images.append({"row": int(row),
                                     "url": f"/pe/report/issue_image/{session_id}/{int(row)}"})
        except Exception:
            issue_images = []
    # Distribution 합성 PNG: S3 오브젝트 또는 로컬 폴백 파일이 있으면 프록시 URL 반환.
    distribution_url = None
    if "distribution_combined" in objects:
        distribution_url = f"/pe/report/distribution_combined/{session_id}"
    elif akey:
        if (Path(REPORT_UPLOAD_DIR) / "dist_combined" / f"{akey}.png").exists():
            distribution_url = f"/pe/report/distribution_combined/{session_id}"
    # 값싼(DB·경로 조회) 부분 — web_report 분기의 응답 캐시 키(extras digest)에도 쓰인다.
    extras = {
        "session": _public_session(session),
        "summary": report_db.get_summary_by_analysis_key(akey) if akey else [],
        "charts": charts,
        "issue_images": issue_images,
        "distribution_url": distribution_url,
        "csv_files": report_db.get_csv_files(akey) if akey else [],
        "objects": objects,
        "annotations": report_db.get_annotations(session_id),
    }
    if session.get("source") == "web_report":
        # 차트 주석(도형/코멘트) + Note 탭 존재 메타 — 세션 편집 DB 의 값싼 조회.
        # 편집 저장은 edits_rev 증가 + extras digest 변경으로 응답 캐시가 무효화된다.
        # Note 시트 본문(최대 2MB)은 싣지 않고 GET .../web_report/note 로 지연 로드.
        extras["chart_notes"] = web_report_service.get_chart_notes(
            session_id, report_db=report_db)
        extras["note_info"] = web_report_service.get_note_meta(
            session_id, report_db=report_db)

    if session.get("source") == "web_report":
        # web_report 세션: parquet 원본에서 재계산 (decoded tables 는 service 의 LRU 캐시 활용).
        # 대용량 Distribution ECDF 는 제외하고 distribution_deferred=True 로 내려보낸다 —
        # 프런트가 GET .../web_report/distribution 으로 백그라운드 지연 로드.
        # 최종 payload 의 JSON 직렬화+gzip bytes 는 response_cache 가 캐시 — warm 요청은
        # bytes 반환뿐이다. annotations/is_important 등 변경은 extras digest 로 자연 무효화.
        try:
            etag, body = web_report_response_cache.get_full_gzip(
                session_id, session=session, extras=extras,
                report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
        except FileNotFoundError:
            abort(404, "web_report session data not found")
        except KeyError:
            abort(404, "session not found")
        except Exception:
            _log.exception("web_report recompute failed for session %s", session_id)
            abort(500, "web_report recompute failed")
        headers = {"Vary": "Accept-Encoding", "ETag": etag}
        if request.headers.get("If-None-Match") == etag:
            return Response(status=304, headers=headers)
        if "gzip" in (request.headers.get("Accept-Encoding") or ""):
            headers["Content-Encoding"] = "gzip"
        else:
            body = gzip.decompress(body)   # 실사용 브라우저는 전부 gzip — 폴백 경로
        return Response(body, mimetype="application/json", headers=headers)

    # legacy(xlsx) 세션 — sheet_data: DB 우선. DB 에 없으면 S3 폴백(구형 세션 하위호환).
    sheet_data = report_db.get_all_sheet_data(akey) if akey else {}
    payload = dict(extras)
    payload["summary_text"] = sheet_data.get("summary") or _load_json_object(objects, "summary_text")
    payload["yield_text"] = sheet_data.get("yield") or _load_json_object(objects, "yield_text")
    payload["issue_table_text"] = sheet_data.get("issue_table") or _load_json_object(objects, "issue_table_text")
    payload["web_report"] = sheet_data.get("web_report")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Vary": "Accept-Encoding"}
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        body = gzip.compress(body, compresslevel=1)
        headers["Content-Encoding"] = "gzip"
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.delete("/session/<session_id>")
def delete_session_route(session_id):
    _require_csrf()
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    denied = _uploader_guard(session)
    if denied:
        return denied
    # 마지막 참조 세션이면 산출물(S3 오브젝트·로컬 폴백 파일)과 관련 DB 행까지 정리.
    # best-effort — 정리 실패가 세션 삭제 자체를 막지 않는다 (audit 패턴과 동일).
    akey = session.get("analysis_key")
    if akey and report_db.count_sessions_for_analysis_key(
            akey, exclude_session_id=session_id) == 0:
        try:
            result = storage_gateway.delete_report_artifacts(
                akey, upload_root=Path(REPORT_UPLOAD_DIR))
            for warning in result.get("warnings", []):
                _log.warning("artifact cleanup (%s): %s", akey, warning)
            report_db.delete_analysis_rows(akey)
            web_report_service.invalidate_caches(akey)
        except Exception:
            _log.exception("artifact cleanup failed for analysis_key %s", akey)
    # Note 탭 이미지는 세션 단위 저장 — akey 공유 여부와 무관하게 항상 정리 (best-effort).
    try:
        for warning in storage_gateway.delete_note_images(session_id):
            _log.warning("note image cleanup (%s): %s", session_id, warning)
    except Exception:
        _log.exception("note image cleanup failed for session %s", session_id)
    report_db.delete_session(session_id)
    _audit("delete", session=session)
    return jsonify({"deleted": True, "session_id": session_id})


@report_bp.post("/session/<session_id>/important")
def set_session_important(session_id):
    """사용자별 개인 '중요' 표시 토글 — 누른 사용자 화면에만 적용(전역 is_important 와 별개).
    개인 중요표시가 하나라도 있는 세션은 자동정리에서 제외된다.
    업로더 또는 위임받은 편집자면 가능(각자 자기 표시만 바꾼다)."""
    _require_csrf()
    _validate_session_id(session_id)
    body = request.get_json(force=True, silent=True) or {}
    important = bool(body.get("important"))
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    denied = _editor_guard(session)
    if denied:
        return denied
    uid = _current_user()
    report_db.set_user_important(uid, session_id, important)
    _audit("edit", session=session, changed_fields="my_important")
    return jsonify({"ok": True, "session_id": session_id, "important": important})


@report_bp.post("/session/<session_id>/private")
def set_session_private(session_id):
    """세션 '비공개' 표시 토글. 업로더 로그인 필요. 목록에서 숨기지는 않고 자물쇠 아이콘 마커용."""
    _require_csrf()
    _validate_session_id(session_id)
    body = request.get_json(force=True, silent=True) or {}
    private = 1 if body.get("private") else 0
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    denied = _uploader_guard(session)
    if denied:
        return denied
    report_db.update_session(session_id, is_private=private)
    _audit("edit", session=session, changed_fields="is_private")
    return jsonify({"ok": True, "session_id": session_id, "is_private": private})


@report_bp.post("/session/<session_id>/verify_password")
def verify_session_password(session_id):
    """수정/삭제 진입 전 권한 확인 — PC 사용자(HoneyUser)==업로더면 ok.
    (구 PIN 방식 폐지: 비밀번호는 더 이상 확인하지 않는다. 응답 형태는 하위호환 유지.)"""
    _require_csrf()
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    denied = _uploader_guard(session)
    if denied is not None:
        resp, status = denied
        payload = resp.get_json() or {}
        return jsonify({"ok": False, "error": payload.get("error", "수정 권한이 없습니다.")}), status
    return jsonify({"ok": True, "has_password": False})


@report_bp.patch("/session/<session_id>/content")
def update_session_content(session_id):
    """수정 모드 저장 라우트 — 기능 비활성화(2026-07-08 사용자 요청, 항상 405).

    report_view.html 이 아직 이 경로를 호출하므로 라우트 자체는 유지한다.
    구 구현(summary/yield/issue 텍스트 치환 + S3 재업로드 체인)은 2026-07-09
    리팩토링에서 제거 — 재활성화 시 git 히스토리 참조."""
    return jsonify({"error": "세션 수정 기능이 비활성화되었습니다."}), 405


# ── 편집 권한 위임 / 개인 접근 상태 ───────────────────────────────────────────

@report_bp.get("/session/<session_id>/my_access")
def session_my_access(session_id):
    """현재 요청자 기준 이 세션에 대한 권한/개인상태 — 사용자별 값이라 session_full
    (세션 단위 gzip 응답 캐시)과 분리한 경량 엔드포인트. 프런트가 병렬 호출한다."""
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    _record_web_visit(session)
    uid = _current_user()
    is_uploader = _is_uploader(session, uid) if uid else False
    can_edit = is_uploader or (bool(uid) and report_db.is_session_editor(session_id, uid))
    return jsonify({
        "user_id": uid,
        "is_uploader": is_uploader,
        "can_edit": can_edit,
        "my_important": report_db.is_user_important(uid, session_id) if uid else False,
    })


@report_bp.get("/session/<session_id>/editors")
def list_editors(session_id):
    """세션 편집 권한 위임 목록 (업로더 전용)."""
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    denied = _uploader_guard(session)
    if denied:
        return denied
    return jsonify({"session_id": session_id,
                    "editors": report_db.list_session_editors(session_id)})


@report_bp.post("/session/<session_id>/editors")
def add_editor(session_id):
    """편집 권한 부여 (업로더 전용). body: {user}. 위임받은 사용자는 내용 편집·저장 및
    개인 중요표시만 가능하며 삭제·비공개·권한부여는 못 한다."""
    _require_csrf()
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    denied = _uploader_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    editor = _normalize_user_id(body.get("user"))
    uploader = _current_user()
    if editor == uploader:
        return jsonify({"error": "본인에게는 권한을 부여할 수 없습니다."}), 400
    report_db.add_session_editor(session_id, editor, uploader)
    _audit("edit", session=session, changed_fields=f"grant_editor:{editor}")
    return jsonify({"ok": True, "session_id": session_id, "editor": editor,
                    "editors": report_db.list_session_editors(session_id)})


@report_bp.delete("/session/<session_id>/editors/<editor_user>")
def remove_editor(session_id, editor_user):
    """편집 권한 회수 (업로더 전용)."""
    _require_csrf()
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    denied = _uploader_guard(session)
    if denied:
        return denied
    editor = _normalize_user_id(editor_user)
    report_db.remove_session_editor(session_id, editor)
    _audit("edit", session=session, changed_fields=f"revoke_editor:{editor}")
    return jsonify({"ok": True, "session_id": session_id, "editor": editor,
                    "editors": report_db.list_session_editors(session_id)})


@report_bp.get("/session/<session_id>/editors/candidates")
def editor_candidates(session_id):
    """편집 권한 부여 후보 — web_report 방문자 검색 (업로더 전용). 업로더 자신은 제외,
    이미 편집자인 사용자는 already=True 로 표시."""
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    denied = _uploader_guard(session)
    if denied:
        return denied
    q = request.args.get("q") or ""
    uploader = _current_user()
    current = {e["editor_user"] for e in report_db.list_session_editors(session_id)}
    out = [{"user": uid, "already": uid in current}
           for uid in report_db.search_web_visitors(q, limit=50) if uid != uploader]
    return jsonify({"candidates": out})

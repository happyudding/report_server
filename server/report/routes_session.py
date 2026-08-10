"""세션 조회/삭제/권한 라우트 (Phase 4 분리 — 구 report_routes.py).

/result /session/<sid> /session/<sid>/full — 조회,
DELETE /session/<sid>, important/private/verify_password/content — 변경,
my_access/editors — 권한·위임. URL·응답 형태는 분리 전과 동일하다.
"""
import gzip
import json
import logging
import re
from pathlib import Path

from flask import Response, abort, jsonify, request

from auth_identity import (
    current_user as _current_user,
    identity_source as _identity_source,
    is_uploader as _is_uploader,
)
from config import REPORT_TRASH_RETENTION_DAYS, REPORT_UPLOAD_DIR
from database import report_db
import product_info
import storage_gateway
from report.report_extension import report_bp
from report.security import (
    _active_or_404,
    _audit,
    _editor_guard,
    _is_master,
    _normalize_user_id,
    _private_guard,
    _public_session,
    _record_web_visit,
    _require_csrf,
    _uploader_guard,
    _validate_session_id,
)
from web_report import service as web_report_service
from web_report.validation import validate_meta as _validate_upload_meta
from web_report import response_cache as web_report_response_cache
from web_report import build_status as web_report_build_status
from web_report import compute as web_report_compute
from web_report import eta as web_report_eta

_log = logging.getLogger(__name__)


def _load_json_object(objects, object_type):
    """objects 인덱스에 object_type 이 있으면 S3 JSON 다운로드, 실패 시 None."""
    return storage_gateway.load_json_object(objects, object_type)


def _building_response(session_id, kind="report", session=None):
    """콜드 빌드 요청 + 202(building) 응답. 연속 실패로 차단된 세션은 503.

    503 이 없으면 프런트는 실패한 빌드를 최대 15분간 폴링만 하다 타임아웃한다 —
    사용자에게는 "영원히 로딩 중"으로 보인다. 사실대로 알려 즉시 끝낸다.

    session 을 주면 입력 규모 기반 예상초(eta)를 함께 실어 로드 오버레이가 "예상 약 N초"
    를 안내한다 (모르면 키 자체를 넣지 않는다 — 프런트는 없으면 종전 문구).
    """
    blocked = web_report_build_status.failure_blocked(session_id, kind)
    if blocked:
        return jsonify({
            "build_failed": True,
            "fail_count": blocked["count"],
            "error": "리포트 계산이 반복 실패했습니다. 잠시 후 다시 시도하거나 "
                     "관리자에게 문의해 주세요.",
        }), 503
    web_report_compute.request_build(session_id, str(REPORT_UPLOAD_DIR), kind)
    status = web_report_build_status.snapshot(session_id)
    body = {"building": True, "stage": status.get("stage", kind),
            "elapsed": status.get("elapsed", 0)}
    if session is not None:
        eta_sec = web_report_eta.session_eta(session, Path(REPORT_UPLOAD_DIR))
        if eta_sec is not None:
            body["eta"] = eta_sec
    return jsonify(body), 202


@report_bp.get("/result/<session_id>")
def result(session_id):
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    _private_guard(session)
    _active_or_404(session)
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
    _private_guard(session)
    _active_or_404(session)
    return jsonify(_public_session(session))


@report_bp.get("/session/<session_id>/full")
def session_full(session_id):
    """세션 완전 복원에 필요한 모든 참조 반환."""
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    _private_guard(session)
    _active_or_404(session)
    if session.get("source") == "web_report":
        # 콜드면 extras(DB 왕복 7~10건 + 다운로드 + digest)를 조립하기 **전에** 202 를
        # 낸다 — 그 값들은 200 payload 에만 쓰이는데, 콜드 세션은 프런트가 최대 15분간
        # 1~5초 간격으로 폴링하므로 조립 비용이 수백 번 반복됐다. 판정 자체는
        # SELECT 1회 + stat 1회. (판정 후 축출되는 레이스는 아래 ColdBuildRequired 폴백)
        try:
            cold = web_report_service.report_is_cold(
                session_id, report_db=report_db,
                upload_root=Path(REPORT_UPLOAD_DIR), session=session)
        except Exception:
            _log.exception("cold probe failed for session %s", session_id)
            cold = False        # 판정 실패는 기존 경로(느리지만 정확)로 흘려보낸다
        if cold:
            return _building_response(session_id, "report", session=session)
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
            _log.exception("issue image 목록 조회 실패 (session=%s)", session_id)
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
        # Note 시트 본문(최대 10MB)은 싣지 않고 GET .../web_report/note 로 지연 로드.
        extras["chart_notes"] = web_report_service.get_chart_notes(
            session_id, report_db=report_db)
        extras["note_info"] = web_report_service.get_note_meta(
            session_id, report_db=report_db)
        # 앵커 태그(태그명→Note 셀 위치) — comment 의 #[태그명] 점프 대상.
        extras["note_tags"] = web_report_service.get_note_tags(
            session_id, report_db=report_db)

    if session.get("source") == "web_report":
        # web_report 세션: parquet 원본에서 재계산 (decoded tables 는 service 의 LRU 캐시 활용).
        # 대용량 Distribution ECDF 는 제외하고 distribution_deferred=True 로 내려보낸다 —
        # 프런트가 GET .../web_report/distribution 으로 백그라운드 지연 로드.
        # 최종 payload 의 JSON 직렬화+gzip bytes 는 response_cache 가 캐시 — warm 요청은
        # bytes 반환뿐이다. annotations/is_important 등 변경은 extras digest 로 자연 무효화.
        # 콜드 빌드(수 초~수십 초)를 요청 스레드에서 기다리지 않는다 — waitress 스레드는
        # 8개뿐이라 여러 명이 서로 다른 신규 세션을 동시에 열면 값싼 요청까지 밀린다.
        # 콜드면 백그라운드 빌드를 걸고 202 를 즉시 반환하고, 프런트(boot.js)가
        # build_status 를 폴링한 뒤 다시 요청한다. warm/디스크 히트는 종전대로 200.
        try:
            etag, body = web_report_response_cache.get_full_gzip(
                session_id, session=session, extras=extras,
                report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
                build_if_cold=False)
        except web_report_service.ColdBuildRequired:
            # 위 조기 판정을 통과했는데 여기 온 경우 = 판정 후 축출된 레이스.
            return _building_response(session_id, "report", session=session)
        except FileNotFoundError:
            abort(404, "web_report session data not found")
        except KeyError:
            abort(404, "session not found")
        except Exception:
            _log.exception("web_report recompute failed for session %s", session_id)
            abort(500, "web_report recompute failed")
        # 시딩(service.seed_map) 도입 전 세션은 map 캐시가 없다 — 여기서 백그라운드
        # 빌드만 예약하고 기다리지 않는다. 사용자가 몇 초 뒤 Map/Issue Table 탭을 열 때
        # 콜드 202 로 30초+ 기다리지 않게 하는 백필 (CLAUDE.md §5-11).
        web_report_service.schedule_map_backfill(
            session_id, session, report_db=report_db,
            upload_root=Path(REPORT_UPLOAD_DIR))
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
    """세션을 휴지통으로 이동(soft delete). 업로더만 가능.

    산출물·DB 행은 그대로 두고 deleted_at/deleted_by 만 찍는다 — 실제 정리는 30일 경과 후
    관리자 purge 에서만 이뤄진다(데이터 유실 방지). 응답은 하위호환 deleted=true 를 유지하고
    trashed·purge_at 을 추가한다."""
    _require_csrf()
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    denied = _uploader_guard(session)
    if denied:
        return denied
    deleted_at = report_db.trash_session(session_id, deleted_by=_current_user())
    purge_at = deleted_at + REPORT_TRASH_RETENTION_DAYS * 86400
    _audit("delete", session=session, changed_fields="trash")
    return jsonify({"deleted": True, "trashed": True, "purge_at": purge_at,
                    "session_id": session_id})


@report_bp.post("/session/<session_id>/restore")
def restore_session_route(session_id):
    """휴지통 세션 복원. 원래 업로더(is_uploader) 또는 삭제한 사용자(deleted_by)만 가능.
    (운영 복원 UI 는 관리자 패널이지만, 요구사항상 업로더/삭제자도 복원할 수 있어야 한다.)"""
    _require_csrf()
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    if not session.get("deleted_at"):
        return jsonify({"error": "휴지통에 있는 세션이 아닙니다."}), 400
    uid = _current_user()
    if not uid:
        return jsonify({"error": "로그인한 사용자만 복원할 수 있습니다 (현재 읽기 전용)."}), 401
    deleted_by = str(session.get("deleted_by") or "").strip().lower()
    if not (_is_uploader(session, uid) or uid == deleted_by):
        return jsonify({"error": "복원 권한이 없습니다 (업로더 또는 삭제한 사용자만)."}), 403
    report_db.restore_session(session_id)
    _audit("edit", session=session, changed_fields="restore")
    return jsonify({"ok": True, "restored": True, "session_id": session_id})


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
    """세션 '비공개' 토글. 업로더만 변경 가능. 비공개 세션은 업로더+위임 편집자만
    조회 가능 — 목록(history)에서도 숨겨지고 상세/데이터/이미지 조회는 404."""
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


# ── 세션 메타(이름/Family/Product/LOT/Process) 수정 ──────────────────────────

# 표시명(file_name)에서 걸러낼 문자 — 경로 구분자/Windows 금지문자/제어문자.
# secure_filename 을 쓰지 않는 이유: 한글 이름이 통째로 사라진다(표시용 값이라 불필요).
_NAME_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _clean_session_name(value):
    """세션 이름 정규화. 빈 값이면 None (호출부가 400)."""
    name = _NAME_BAD_CHARS.sub("", str(value or "")).strip()
    return name[:120] or None


@report_bp.patch("/session/<session_id>/meta")
def update_session_meta_route(session_id):
    """세션 메타 수정 — Honey 편집창 전용 (업로드 다이얼로그 재사용).

    Honey 클라(브라우저 아님)가 호출하므로 CSRF 대신 커스텀 헤더 X-Honey-Agent 를 요구한다
    (rawdata_replace 선례 — 커스텀 헤더는 브라우저 폼으로 위조 불가). 이 헤더 요구가
    "수정은 Honey 에서만" 을 서버가 강제하는 지점이다.

    product 가 바뀌면 product_info.db 를 다시 lookup 해 세션 기준정보 14컬럼을 갱신한다
    (미등록 part_id 면 비운다 — 옛 제품 값이 남으면 상단바가 틀린 정보를 보여준다).

    analysis_key 는 재산출하지 않는다 — 산출물(parquet/manifest/summary)이 전부 그 키로
    저장돼 있어 키를 바꾸면 세션이 자기 데이터를 잃는다. 불변 규칙 #3 의 산출식은 '업로드
    시점' 규약이며, 수정 후에는 dedup(같은 데이터 재업로드) 매칭만 어긋난다.
    """
    if request.headers.get("X-Honey-Agent") != "1":
        abort(403, "X-Honey-Agent header required")
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    denied = _editor_guard(session)
    if denied:
        return denied

    body = request.get_json(force=True, silent=True) or {}
    # family/product/lot/process 정규화는 업로드 ingest 와 같은 규칙(strip + 길이 제한)을 쓴다.
    norm = _validate_upload_meta(body)
    name = _clean_session_name(body.get("file_name"))
    if not name:
        return jsonify({"error": "세션 이름을 입력하세요."}), 400
    if not norm["product"] or not norm["lot_id"]:
        return jsonify({"error": "Product 와 LOT ID 를 모두 입력하세요."}), 400

    meta = {"file_name": name, "family_product": norm["family_product"],
            "product": norm["product"], "lot_id": norm["lot_id"],
            "process": norm["process"]}
    changed = [k for k, v in meta.items() if (session.get(k) or "") != v]
    report_db.update_session_meta(session_id, meta,
                                  product_info=product_info.lookup(norm["product"]))
    _audit("edit", session=session,
           changed_fields="meta:" + (",".join(changed) if changed else "none"))
    return jsonify({"ok": True, "session_id": session_id, "changed": changed,
                    **meta})


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
    is_master = _is_master()
    is_uploader = _is_uploader(session, uid) if uid else False
    # master PC 는 업로더와 동일 권한 — 편집뿐 아니라 삭제·비공개토글·권한부여도 통과한다
    # (security._uploader_guard). 프런트도 is_master 를 IS_UPLOADER 에 합류시킨다(core.js).
    can_edit = is_master or is_uploader or (bool(uid) and report_db.is_session_editor(session_id, uid))
    return jsonify({
        "user_id": uid,
        "source": _identity_source(),   # 프런트의 'Honey 전용 기능' 안내 판단용
        "is_uploader": is_uploader,
        "is_master": is_master,
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

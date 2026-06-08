import re
import secrets

from flask import Response, abort, jsonify, make_response, request, send_file

from database import report_db
from report_utils import to_float as _to_float, to_int as _to_int
import storage_gateway
from config import (
    REPORT_ANALYSIS_INDEX_HTML,
    REPORT_VIEW_HTML,
)
from report.report_extension import report_bp

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
    """토큰 쿠키가 없으면 새로 발급. JS 가 읽어야 하므로 httponly=False."""
    if not request.cookies.get(_CSRF_COOKIE):
        resp.set_cookie(
            _CSRF_COOKIE, secrets.token_urlsafe(32),
            max_age=86400, samesite="Strict",
            secure=request.is_secure, httponly=False, path="/",
        )
    return resp


def _require_csrf():
    """변경요청에서 헤더 토큰이 쿠키와 일치하는지 검증. 불일치 시 403."""
    cookie = request.cookies.get(_CSRF_COOKIE) or ""
    header = request.headers.get(_CSRF_HEADER) or ""
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        abort(403, "CSRF token missing or invalid")


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


def _coerce_yield_row(row):
    """수정 모드에서 넘어온 yield 행 dict 를 summary 컬럼 타입으로 정리."""
    name = str(row.get("item_name") or "").strip()
    unit = row.get("unit")
    return {
        "item_name": name,
        "bin_number": _to_int(row.get("bin_number")),
        "yield_percent": _to_float(row.get("yield_percent")),
        "fail_count": _to_int(row.get("fail_count")),
        "cpk_val": _to_float(row.get("cpk_val")),
        "mean_val": _to_float(row.get("mean_val")),
        "stdev_val": _to_float(row.get("stdev_val")),
        "lsl": _to_float(row.get("lsl")),
        "usl": _to_float(row.get("usl")),
        "unit": (str(unit).strip() if unit not in (None, "") else None),
    }


# ── session ─────────────────────────────────────────────────────────────────

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

    # sheet_data: DB 우선. DB 에 없으면 S3 폴백(구형 세션 하위호환).
    sheet_data = report_db.get_all_sheet_data(akey) if akey else {}
    summary_text = sheet_data.get("summary") or _load_json_object(objects, "summary_text")
    yield_text = sheet_data.get("yield") or _load_json_object(objects, "yield_text")
    issue_table_text = sheet_data.get("issue_table") or _load_json_object(objects, "issue_table_text")
    charts = []
    if "chart_index" in objects:
        manifest = _load_json_object(objects, "chart_index")
        count = int((manifest or {}).get("count", 0))
        charts = [{"index": i, "url": f"/pe/report/chart/{session_id}/{i}"}
                  for i in range(count)]
    # Issue_table 행별 분포 이미지. 저장소(S3 또는 로컬 폴백)에서 행 인덱스를 조회.
    issue_images = []
    if akey:
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
        from pathlib import Path
        from config import REPORT_UPLOAD_DIR
        if (Path(REPORT_UPLOAD_DIR) / "dist_combined" / f"{akey}.png").exists():
            distribution_url = f"/pe/report/distribution_combined/{session_id}"
    return jsonify({
        "session": _public_session(session),
        "summary": report_db.get_summary_by_analysis_key(akey) if akey else [],
        "summary_text": summary_text,
        "yield_text": yield_text,
        "issue_table_text": issue_table_text,
        "charts": charts,
        "issue_images": issue_images,
        "distribution_url": distribution_url,
        "csv_files": report_db.get_csv_files(akey) if akey else [],
        "objects": objects,
        "annotations": report_db.get_annotations(session_id),
    })


def _load_json_object(objects, object_type):
    """objects 인덱스에 object_type 이 있으면 S3 JSON 다운로드, 실패 시 None."""
    return storage_gateway.load_json_object(objects, object_type)


@report_bp.delete("/session/<session_id>")
def delete_session_route(session_id):
    _require_csrf()
    _validate_session_id(session_id)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    if not _password_ok(session, password):
        return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 403
    report_db.delete_session(session_id)
    _audit("delete", session=session)
    return jsonify({"deleted": True, "session_id": session_id})


@report_bp.post("/session/<session_id>/verify_password")
def verify_session_password(session_id):
    """수정/삭제 진입 전 PIN 확인. 비밀번호 미설정 세션은 ok=True."""
    _require_csrf()
    _validate_session_id(session_id)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    if not _password_ok(session, password):
        return jsonify({"ok": False, "error": "비밀번호가 일치하지 않습니다."}), 403
    return jsonify({"ok": True, "has_password": bool(session.get("password"))})


@report_bp.patch("/session/<session_id>/content")
def update_session_content(session_id):
    """수정 모드 저장: 텍스트 콘텐츠(summary_text / issue_rows / yield_rows) 치환.

    summary_text, issue_rows 는 S3 JSON 으로 다시 업로드하고, yield_rows 는
    report_analysis_summary 를 통째로 치환한다. analysis_key 는 재계산하지 않는다
    (원본 업로드 식별자로 유지)."""
    _require_csrf()
    _validate_session_id(session_id)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    if not _password_ok(session, password):
        return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 403

    akey = session.get("analysis_key")
    if not akey:
        return jsonify({"error": "이 세션에는 analysis_key 가 없어 수정할 수 없습니다."}), 400

    updated = {}
    errors = {}

    # yield_rows → DB (S3 미설정과 무관하게 저장 가능)
    if body.get("yield_rows") is not None:
        try:
            rows = [_coerce_yield_row(r) for r in (body.get("yield_rows") or [])]
            rows = [r for r in rows if r["item_name"]]
            report_db.replace_summary_batch(akey, session_id, rows)
            updated["yield_rows"] = len(rows)
        except Exception as exc:
            errors["yield_rows"] = str(exc)

    # summary_text → DB 갱신 (S3 는 있으면 추가 저장)
    if body.get("summary_text") is not None:
        try:
            report_db.upsert_sheet_data(akey, "summary", body["summary_text"])
            updated["summary_text"] = True
        except Exception as exc:
            errors["summary_text"] = str(exc)
        else:
            try:
                _write_text_object(akey, session, "summary_text", body["summary_text"])
            except Exception:
                pass

    # yield_text → DB 갱신
    if body.get("yield_text") is not None:
        try:
            report_db.upsert_sheet_data(akey, "yield", body["yield_text"])
            updated["yield_text"] = True
        except Exception as exc:
            errors["yield_text"] = str(exc)
        else:
            try:
                _write_text_object(akey, session, "yield_text", body["yield_text"])
            except Exception:
                pass

    # issue_table_text → DB 갱신
    issue_payload = body.get("issue_table_text")
    if issue_payload is None:
        issue_payload = body.get("issue_rows")
    if issue_payload is not None:
        try:
            report_db.upsert_sheet_data(akey, "issue_table", issue_payload)
            updated["issue_table_text"] = True
        except Exception as exc:
            errors["issue_table_text"] = str(exc)
        else:
            try:
                _write_text_object(akey, session, "issue_table_text", issue_payload)
            except Exception:
                pass

    status = 200 if not errors else (207 if updated else 500)
    if updated:
        _audit("edit", session=session,
               changed_fields=",".join(sorted(updated.keys())),
               result="ok" if not errors else "fail")
    return jsonify({"ok": not errors, "updated": updated, "errors": errors}), status


def _write_text_object(analysis_key, session, object_type, data):
    """텍스트 콘텐츠 JSON 을 S3 에 다시 올리고 report_object_info 를 갱신.
    content_hash / options_json 은 기존 행 값을 유지(없으면 세션 값/빈 객체로 폴백)."""
    storage_gateway.save_text_object(analysis_key, session, object_type, data)


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

@report_bp.get("/")
def index_page():
    return _issue_csrf_cookie(make_response(send_file(REPORT_ANALYSIS_INDEX_HTML)))


@report_bp.get("/view/<session_id>")
def view_page(session_id):
    _validate_session_id(session_id)
    return _issue_csrf_cookie(make_response(send_file(REPORT_VIEW_HTML)))


@report_bp.get("/api/history")
def history():
    product_type = request.args.get("product_type") or None
    process = request.args.get("process") or None
    product = request.args.get("product") or None
    revision = request.args.get("revision") or None
    lot_id = request.args.get("lot_id") or None
    source = request.args.get("source") or None
    rows = report_db.get_history(
        product_type=product_type,
        process=process,
        product=product,
        revision=revision,
        lot_id=lot_id,
        source=source,
    )
    return jsonify(rows)


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

import re

from flask import Response, abort, jsonify, make_response, request, send_file

from database import report_db
from s3_storage import report_s3
from config import (
    REPORT_ANALYSIS_INDEX_HTML,
    REPORT_VIEW_HTML,
)
from report.report_extension import report_bp
from s3_storage.report_s3 import S3NotConfigured, S3ObjectCorrupted

_ANALYSIS_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _validate_analysis_key(value):
    if not value or not _ANALYSIS_KEY_RE.match(value):
        abort(400, "invalid analysis_key")


def _validate_session_id(value):
    if not value or not _SESSION_ID_RE.match(value):
        abort(400, "invalid session_id")


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


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


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
        try:
            manifest = report_s3.download_json_from_s3(objects["chart_index"]["s3_key"])
            count = int((manifest or {}).get("count", 0))
            charts = [{"index": i, "url": f"/pe/report/chart/{session_id}/{i}"}
                      for i in range(count)]
        except (S3NotConfigured, S3ObjectCorrupted, Exception):
            charts = []
    # Issue_table 행별 분포 이미지. 저장소(S3 또는 로컬 폴백)에서 행 인덱스를 조회.
    issue_images = []
    if akey:
        try:
            from issue_image_store import list_rows
            for row in list_rows(akey):
                issue_images.append({"row": int(row),
                                     "url": f"/pe/report/issue_image/{session_id}/{int(row)}"})
        except Exception:
            issue_images = []
    # Distribution 합성 PNG: 있으면 프록시 URL 반환.
    distribution_url = None
    if "distribution_combined" in objects:
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
    if object_type not in objects:
        return None
    try:
        return report_s3.download_json_from_s3(objects[object_type]["s3_key"])
    except (S3NotConfigured, S3ObjectCorrupted, Exception):
        return None


@report_bp.get("/chart/<session_id>/<int:idx>")
def chart_image(session_id, idx):
    """클라이언트가 렌더해 올린 차트 PNG 를 S3 에서 스트리밍.
    공개 버킷/presign 없이 서버 경유로 서빙 (기존 패턴 일관)."""
    _validate_session_id(session_id)
    if idx < 0 or idx > 1000:
        abort(404, "invalid chart index")
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    akey = session.get("analysis_key")
    if not akey:
        abort(404, "no analysis_key for session")
    try:
        key = report_s3.make_chart_png_s3_key(akey, idx)
        data = report_s3.download_bytes_from_s3(key)
    except S3NotConfigured:
        abort(503, "S3 not configured")
    except Exception:
        abort(404, "chart not found")
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@report_bp.get("/issue_image/<session_id>/<int:row>")
def issue_image(session_id, row):
    """Issue_table 행별 분포 PNG 를 S3 에서 스트리밍 (골격).
    chart_image 와 동일 패턴 — 서버 경유 서빙. 이미지 미존재 시 404."""
    _validate_session_id(session_id)
    if row < 0 or row > 10000:
        abort(404, "invalid image row")
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    akey = session.get("analysis_key")
    if not akey:
        abort(404, "no analysis_key for session")
    try:
        from issue_image_store import load_image
        data = load_image(akey, row)
    except S3NotConfigured:
        abort(503, "S3 not configured")
    except Exception:
        abort(404, "image not found")
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@report_bp.get("/distribution_combined/<session_id>")
def distribution_combined_png(session_id):
    """클라이언트 차트 PNG 그리드 합성 이미지를 S3 에서 스트리밍."""
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    akey = session.get("analysis_key")
    if not akey:
        abort(404, "no analysis_key for session")
    objs = {o["object_type"]: o for o in report_db.get_all_object_infos(akey)}
    if "distribution_combined" not in objs:
        abort(404, "distribution combined PNG 없음")
    try:
        data = report_s3.download_bytes_from_s3(objs["distribution_combined"]["s3_key"])
    except S3NotConfigured:
        abort(503, "S3 not configured")
    except Exception:
        abort(404, "distribution combined not found")
    resp = make_response(data)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@report_bp.delete("/session/<session_id>")
def delete_session_route(session_id):
    _validate_session_id(session_id)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    if not _password_ok(session, password):
        return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 403
    report_db.delete_session(session_id)
    return jsonify({"deleted": True, "session_id": session_id})


@report_bp.post("/session/<session_id>/verify_password")
def verify_session_password(session_id):
    """수정/삭제 진입 전 PIN 확인. 비밀번호 미설정 세션은 ok=True."""
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
                _write_text_object(akey, session, "summary_text",
                                   report_s3.make_summary_text_s3_key, body["summary_text"])
            except (S3NotConfigured, Exception):
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
                _write_text_object(akey, session, "yield_text",
                                   report_s3.make_yield_text_s3_key, body["yield_text"])
            except (S3NotConfigured, Exception):
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
                _write_text_object(akey, session, "issue_table_text",
                                   report_s3.make_issue_text_s3_key, issue_payload)
            except (S3NotConfigured, Exception):
                pass

    status = 200 if not errors else (207 if updated else 500)
    return jsonify({"ok": not errors, "updated": updated, "errors": errors}), status


def _write_text_object(analysis_key, session, object_type, key_builder, data):
    """텍스트 콘텐츠 JSON 을 S3 에 다시 올리고 report_object_info 를 갱신.
    content_hash / options_json 은 기존 행 값을 유지(없으면 세션 값/빈 객체로 폴백)."""
    key = key_builder(analysis_key)
    uri = report_s3.upload_json_to_s3(key, data)
    existing = report_db.get_object_info(analysis_key, object_type) or {}
    content_hash = existing.get("content_hash") or session.get("content_hash") or ""
    options_json = existing.get("options_json") or "{}"
    report_db.upsert_object_info(
        analysis_key, content_hash, options_json, object_type,
        report_s3.bucket_name(), key, uri,
    )


# ── annotations ───────────────────────────────────────────────────────────────

@report_bp.post("/annotation")
def create_annotation():
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
    body = request.get_json(force=True, silent=True) or {}
    content = (body.get("content") or "").strip()
    if not content:
        abort(400, "content is required")
    report_db.update_annotation(aid, content)
    return jsonify({"id": aid, "updated": True})


@report_bp.delete("/annotation/<int:aid>")
def delete_annotation(aid):
    report_db.delete_annotation(aid)
    return jsonify({"id": aid, "deleted": True})


# ── Report Analysis index / view pages ───────────────────────────────────────

@report_bp.get("/")
def index_page():
    return send_file(REPORT_ANALYSIS_INDEX_HTML)


@report_bp.get("/view/<session_id>")
def view_page(session_id):
    _validate_session_id(session_id)
    return send_file(REPORT_VIEW_HTML)


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

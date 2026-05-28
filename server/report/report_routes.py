import re

from flask import Response, abort, jsonify, request, send_file

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
    return jsonify(session)


@report_bp.get("/session/<session_id>/full")
def session_full(session_id):
    """세션 완전 복원에 필요한 모든 참조 반환."""
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    akey = session.get("analysis_key")
    objects = {}
    summary_text = None
    issue_table_text = None
    if akey:
        for obj in report_db.get_all_object_infos(akey):
            objects[obj["object_type"]] = {
                "s3_uri": obj["s3_uri"],
                "s3_key": obj["s3_key"],
            }
        if "summary_text" in objects:
            try:
                summary_text = report_s3.download_json_from_s3(objects["summary_text"]["s3_key"])
            except (S3NotConfigured, S3ObjectCorrupted, Exception):
                summary_text = None
        if "issue_table_text" in objects:
            try:
                issue_table_text = report_s3.download_json_from_s3(objects["issue_table_text"]["s3_key"])
            except (S3NotConfigured, S3ObjectCorrupted, Exception):
                issue_table_text = None
    charts = []
    if "chart_index" in objects:
        try:
            manifest = report_s3.download_json_from_s3(objects["chart_index"]["s3_key"])
            count = int((manifest or {}).get("count", 0))
            charts = [{"index": i, "url": f"/pe/report/chart/{session_id}/{i}"}
                      for i in range(count)]
        except (S3NotConfigured, S3ObjectCorrupted, Exception):
            charts = []
    return jsonify({
        "session": session,
        "summary": report_db.get_summary_by_analysis_key(akey) if akey else [],
        "summary_text": summary_text,
        "issue_table_text": issue_table_text,
        "charts": charts,
        "csv_files": report_db.get_csv_files(akey) if akey else [],
        "objects": objects,
        "annotations": report_db.get_annotations(session_id),
    })


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


@report_bp.delete("/session/<session_id>")
def delete_session_route(session_id):
    _validate_session_id(session_id)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    stored_password = session.get("password")
    if stored_password:
        if password != stored_password:
            return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 403
    report_db.delete_session(session_id)
    return jsonify({"deleted": True, "session_id": session_id})


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

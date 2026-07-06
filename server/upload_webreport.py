"""/pe/report/upload_webreport route."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from flask import Response, abort, jsonify, request

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import REPORT_UPLOAD_DIR
from database import report_db
from report.report_extension import report_bp
from web_report import service as web_report_service

_MAX_WEBREPORT_BYTES = 512 * 1024 * 1024


def _client_meta():
    fwd = request.headers.get("X-Forwarded-For")
    ip = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
    return ip, str(request.user_agent)


def _read_manifest():
    raw = request.form.get("manifest")
    if not raw:
        abort(400, "missing manifest")
    try:
        manifest = json.loads(raw)
    except Exception:
        abort(400, "manifest must be valid JSON")
    if not isinstance(manifest, dict):
        abort(400, "manifest must be a JSON object")
    return manifest


def _read_files():
    items = []
    idx = 0
    while True:
        f = request.files.get(f"webreport_{idx}")
        if f is None:
            break
        data = f.read()
        if not data:
            abort(400, f"webreport_{idx} is empty")
        if len(data) > _MAX_WEBREPORT_BYTES:
            abort(413, f"webreport_{idx} payload is too large")
        items.append({"name": f"webreport_{idx}", "filename": f.filename, "data": data})
        idx += 1
    if not items:
        abort(400, "missing webreport parquet files")
    return items


@report_bp.post("/upload_webreport")
def upload_webreport():
    try:
        manifest = _read_manifest()
        files = _read_files()
        ip, ua = _client_meta()
        result = web_report_service.ingest_webreport(
            manifest, files, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            client_ip=ip, user_agent=ua)
    except RuntimeError as exc:
        return jsonify({"status": "failed", "error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"status": "failed", "error": str(exc)}), 400

    return jsonify(result)


@report_bp.get("/web_report/<session_id>")
def web_report_page(session_id):
    try:
        html = web_report_service.render_session(session_id, report_db=report_db)
    except KeyError:
        abort(404, "session not found")
    except FileNotFoundError:
        abort(404, "web report not found")
    return Response(html, mimetype="text/html; charset=utf-8")

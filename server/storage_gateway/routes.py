"""Storage-backed image routes mounted on the report blueprint.

The public URL contract remains unchanged; only the storage implementation is
behind the gateway boundary now.
"""
import re

from flask import Response, abort, make_response

from database import report_db
from report.report_extension import report_bp
from s3_storage.report_s3 import S3NotConfigured
from storage_gateway import load_chart_png, load_distribution_png, load_issue_image

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _validate_session_id(value):
    if not value or not _SESSION_ID_RE.match(value):
        abort(400, "invalid session_id")


def _session_analysis_key(session_id):
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    akey = session.get("analysis_key")
    if not akey:
        abort(404, "no analysis_key for session")
    return akey


@report_bp.get("/chart/<session_id>/<int:idx>")
def chart_image(session_id, idx):
    """Stream a chart PNG through the server."""
    if idx < 0 or idx > 1000:
        abort(404, "invalid chart index")
    akey = _session_analysis_key(session_id)
    try:
        data = load_chart_png(akey, idx)
    except S3NotConfigured:
        abort(503, "S3 not configured")
    except Exception:
        abort(404, "chart not found")
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@report_bp.get("/issue_image/<session_id>/<int:row>")
def issue_image(session_id, row):
    """Stream an Issue_table row distribution PNG."""
    if row < 0 or row > 10000:
        abort(404, "invalid image row")
    akey = _session_analysis_key(session_id)
    try:
        data = load_issue_image(akey, row)
    except S3NotConfigured:
        abort(503, "S3 not configured")
    except Exception:
        abort(404, "image not found")
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@report_bp.get("/distribution_combined/<session_id>")
def distribution_combined_png(session_id):
    """Stream the combined Distribution PNG."""
    akey = _session_analysis_key(session_id)
    try:
        data = load_distribution_png(akey)
    except Exception:
        abort(404, "distribution combined PNG 없음")

    resp = make_response(data)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

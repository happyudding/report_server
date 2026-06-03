"""서버 유지관리용 감사 로그 대시보드 (/pe/admin).

업로드/수정/삭제 이벤트가 report_audit_log 에 쌓이고, 여기서 조회한다.
인증은 두지 않는다 — 내부망 전용 가정. 조회 전용(GET)이며 CSRF 도 불필요.

- GET /pe/admin/          : 대시보드 HTML
- GET /pe/admin/api/audit : 감사 로그 JSON (action/q/limit/offset 쿼리파라미터)
"""
from flask import Blueprint, jsonify, request, send_file

from config import ADMIN_DASHBOARD_HTML
from database import report_db

admin_bp = Blueprint("admin", __name__, url_prefix="/pe/admin")


@admin_bp.get("/")
def dashboard_page():
    return send_file(ADMIN_DASHBOARD_HTML)


@admin_bp.get("/api/audit")
def audit_logs():
    action = request.args.get("action") or None
    session_id = request.args.get("session_id") or None
    q = request.args.get("q") or None
    limit = request.args.get("limit", 200)
    offset = request.args.get("offset", 0)
    rows = report_db.get_audit_logs(
        action=action, session_id=session_id, q=q, limit=limit, offset=offset,
    )
    return jsonify(rows)

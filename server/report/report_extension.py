from flask import Blueprint

report_bp = Blueprint("report", __name__, url_prefix="/pe/report")

# 라우트 등록 트리거 (report_bp 데코레이터가 이 시점에 모두 평가됨)
from report import report_routes  # noqa: E402,F401
import upload_xlsx  # noqa: E402,F401


def init_app(app):  # noqa: ARG001
    """Blueprint 등록 후 호출. DB 스키마 초기화 (이미 있으면 no-op)."""
    from database import report_db
    report_db.init_report_db()

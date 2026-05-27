from flask import Blueprint

from database import report_db

report_bp = Blueprint("report", __name__, url_prefix="/pe/report")

# DB 초기화 (스키마 생성 - 이미 있으면 no-op)
report_db.init_report_db()

# 라우트 등록 트리거 (report_bp 데코레이터가 이 시점에 모두 평가됨)
from report import report_routes  # noqa: E402,F401
import upload_xlsx  # noqa: E402,F401

from flask import Blueprint

report_bp = Blueprint("report", __name__, url_prefix="/pe/report")

# 라우트 등록 트리거 (report_bp 데코레이터가 이 시점에 모두 평가됨)
from report import report_routes  # noqa: E402,F401
import upload_xlsx  # noqa: E402,F401
import upload_webreport  # noqa: E402,F401
from storage_gateway import routes as storage_routes  # noqa: E402,F401


def init_app(app):  # noqa: ARG001
    """Blueprint 등록 후 호출. DB 스키마 초기화 (이미 있으면 no-op) + 정리 스케줄러 기동.

    web_report 저장소 포트 주입의 컴포지션 루트 — web_report 는 storage_gateway 를
    직접 import 하지 않는다 (web_report/ports.py 참조)."""
    from database import report_db
    report_db.init_report_db()
    import storage_gateway
    from web_report import runtime as web_report_runtime
    web_report_runtime.configure(storage_gateway)
    try:
        import report_cleanup
        report_cleanup.start_cleanup_scheduler()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("cleanup scheduler start failed")

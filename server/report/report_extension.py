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
    # 재기동 직전에 돌던 콜드 빌드의 잔해(sidecar)를 걷어 사건으로 남긴다 — watchdog
    # 재기동이 무엇을 끊었는지는 이 흔적으로만 알 수 있다.
    try:
        from web_report import compute as web_report_compute
        web_report_compute.sweep_interrupted_builds()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("interrupted build sweep failed")
    # 기동 직후 최근 세션의 콜드 report 를 유휴 워커로 미리 데운다 (env 로 끌 수 있음).
    try:
        import config
        from web_report import compute as web_report_compute
        web_report_compute.start_rewarm_sweep(str(config.REPORT_UPLOAD_DIR))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("rewarm sweep start failed")

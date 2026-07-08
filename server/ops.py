"""운영 보조 진입점 — plugin.register_report_server 가 init_ops(app) 로 호출한다.

- GET /healthz : 경량 health check (DB SELECT 1). 모니터링/기동 확인용 —
  기존 start.bat 이 쓰던 GET /pe/report/ 는 전체 페이지 렌더라 헬스체크로 무겁다.
- 전역 에러 핸들러 : 처리되지 않은 예외의 상세(경로·스택 힌트)를 사용자 응답에서
  숨기고 서버 로그에만 남긴다. HTTPException(abort 류·404 등)은 그대로 통과 —
  honey_routes 등의 의도된 정적 메시지를 보존한다.
- DB 백업 스케줄러 기동 (db_backup 참조).
"""
import logging
import time

from flask import jsonify
from werkzeug.exceptions import HTTPException

_log = logging.getLogger(__name__)
_STARTED_AT = time.time()


def init_ops(app):
    from database import report_db

    @app.get("/healthz")
    def healthz():
        try:
            with report_db.get_conn() as conn:
                conn.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:
            _log.exception("healthz db check failed")
            db_ok = False
        body = {"ok": db_ok, "db": "ok" if db_ok else "fail",
                "uptime_s": int(time.time() - _STARTED_AT)}
        return jsonify(body), (200 if db_ok else 503)

    @app.errorhandler(Exception)
    def _unhandled_exception(e):
        if isinstance(e, HTTPException):
            return e   # abort(...)·404 등 의도된 응답은 그대로
        _log.exception("unhandled exception")
        return jsonify({"error": "internal server error"}), 500

    try:
        import db_backup
        db_backup.start_backup_scheduler()
    except Exception:
        _log.exception("db backup scheduler start failed")

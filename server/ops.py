"""운영 보조 진입점 — plugin.register_report_server 가 init_ops(app) 로 호출한다.

- GET /healthz : 경량 health check (DB SELECT 1). 모니터링/기동 확인용 —
  기존 start.bat 이 쓰던 GET /pe/report/ 는 전체 페이지 렌더라 헬스체크로 무겁다.
- 전역 에러 핸들러 : 처리되지 않은 예외의 상세(경로·스택 힌트)를 사용자 응답에서
  숨기고 서버 로그에만 남긴다. HTTPException(abort 류·404 등)은 그대로 통과 —
  honey_routes 등의 의도된 정적 메시지를 보존한다.
  응답에는 error_id(=request_id)를 실어 준다 — 사용자가 화면에서 읽어 신고하면
  그 한 값으로 콘솔 로그·진단 사건을 모두 찾을 수 있다 (diagnostics 참조).
- 요청 상관 ID 훅 등록 (diagnostics.init_app).
- DB 백업 스케줄러 기동 (db_backup 참조).
"""
import logging
import re
import sqlite3
import time
import traceback
from concurrent.futures.process import BrokenProcessPool

from flask import g, jsonify, make_response, request
from werkzeug.exceptions import HTTPException

import diagnostics

_log = logging.getLogger(__name__)
_STARTED_AT = time.time()

# abort(code, "...") 의 영문 개발자 문구를 대신할 상태코드별 한국어 안내.
# 4xx/5xx 공통 폴백은 code//100*100 (400/500) 키로 찾는다.
_HANGUL_RE = re.compile(r"[가-힣]")
_KO_BY_STATUS = {
    400: "요청이 올바르지 않습니다.",
    401: "권한이 없습니다 — Honey 접속(또는 로그인)이 필요합니다.",
    403: "권한이 없습니다.",
    404: "요청한 데이터를 찾을 수 없습니다 (삭제되었거나 비공개일 수 있습니다).",
    405: "허용되지 않는 요청입니다.",
    409: "다른 변경과 충돌했습니다 — 새로고침 후 다시 시도해 주세요.",
    413: "업로드 용량이 상한을 넘었습니다.",
    429: "요청이 너무 잦습니다 — 잠시 후 다시 시도해 주세요.",
    500: "서버 처리 중 오류가 발생했습니다 — 잠시 후 다시 시도해 주세요. "
         "계속되면 관리자에게 오류 번호를 알려주세요.",
    503: "서버가 붐비거나 일시적으로 응답할 수 없습니다 — 잠시 후 다시 시도해 주세요.",
}


def _error_html(code, msg, rid):
    """브라우저 내비게이션용 최소 한국어 오류 페이지 (템플릿 무의존, 인라인 단색)."""
    rid_line = (f'<p style="color:#888;font-size:12px">오류 번호: {rid}</p>' if rid else "")
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        f'<title>{code} 오류</title></head>'
        '<body style="font-family:sans-serif;max-width:560px;margin:80px auto;'
        'padding:0 20px;color:#333">'
        f'<h1 style="font-size:48px;margin:0 0 8px">{code}</h1>'
        f'<p style="font-size:15px;line-height:1.6">{msg}</p>'
        f'{rid_line}'
        '<p><a href="/pe/report/" style="color:#2563eb">&larr; 검색결과로 돌아가기</a></p>'
        '</body></html>'
    )

# SQLite 잠금 경합 누적 카운터 (관리자 패널 노출용). busy_timeout(5s)을 넘겨 실패한
# 횟수 — 0 이 아니면 동시 쓰기가 실제로 대기 한계를 넘고 있다는 신호다.
DB_LOCK_ERRORS = {"count": 0, "last": None}


def _count_db_lock(exc):
    if isinstance(exc, sqlite3.OperationalError) and \
            any(w in str(exc).lower() for w in ("locked", "busy")):
        DB_LOCK_ERRORS["count"] += 1
        DB_LOCK_ERRORS["last"] = int(time.time())


def _inflight_excluding_self():
    """진행 중인 요청 수(이 healthz 요청 자신은 제외). 카운터가 없으면 None."""
    try:
        from admin_panel import metrics
        n = metrics.current_inflight()
    except Exception:
        return None
    if n is None:
        return None
    return max(0, n - 1)


def _request_context():
    """사건에 실을 요청 맥락 — 요청 컨텍스트가 깨져 있어도 진단이 죽으면 안 된다.

    비밀번호·쿠키·인증 헤더·본문은 담지 않는다 (경로·상태·신원까지만)."""
    ctx = {}
    try:
        ctx["endpoint"] = request.path[:300]
        ctx["method"] = request.method
        ctx["client_ip"] = request.remote_addr or ""
        ctx["session_id"] = (request.view_args or {}).get("session_id") or ""
    except Exception:
        pass
    try:
        from auth_identity import current_user
        ctx["user"] = current_user() or ""
    except Exception:
        pass
    try:
        ctx["request_id"] = getattr(g, "request_id", "") or ""
        ctx["operation_id"] = getattr(g, "operation_id", "") or ""
    except Exception:
        pass
    return {k: v for k, v in ctx.items() if v}


def init_ops(app):
    from database import report_db

    diagnostics.init_app(app)

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
        # 종료 drain 판정용 — terminate.bat 이 이 값이 0 이 되는 순간을 노려 서버를
        # 내린다. metrics 가 꺼져 있으면 키 자체를 넣지 않는다("모름"과 "0건"의 구분).
        inflight = _inflight_excluding_self()
        if inflight is not None:
            body["inflight"] = inflight
        return jsonify(body), (200 if db_ok else 503)

    @app.errorhandler(Exception)
    def _unhandled_exception(e):
        if isinstance(e, HTTPException):
            return e   # abort(...)·404 등 의도된 응답은 그대로
        _count_db_lock(e)
        ctx = _request_context()
        rid = ctx.get("request_id") or diagnostics.new_id()
        # 컴퓨트 워커 붕괴/타임아웃은 서버 코드 버그가 아니라 일시적 용량 문제다.
        # 500 으로 뭉뚱그리면 클라·사용자가 "다시 시도"를 못 고른다.
        if isinstance(e, (BrokenProcessPool, TimeoutError)):
            _log.error("compute unavailable [rid=%s] %s %s: %s", rid,
                       ctx.get("method", "?"), ctx.get("endpoint", "?"), type(e).__name__)
            # event_id 를 request_id 와 같게 둔다 — 사용자가 화면에서 읽어주는 번호가
            # 곧 관리자가 상세를 여는 키여야 한다(번호 두 개면 아무도 못 쓴다).
            diagnostics.emit("warning", "server", "compute_unavailable", event_id=rid,
                             http_status=503, error_type=type(e).__name__,
                             message=str(e)[:500], **ctx)
            return jsonify({"error": "리포트 생성이 지연되고 있습니다. 잠시 후 다시 시도해 주세요.",
                            "error_id": rid}), 503
        _log.exception("unhandled exception [rid=%s] %s %s", rid,
                       ctx.get("method", "?"), ctx.get("endpoint", "?"))
        diagnostics.emit("critical", "server", "unhandled_exception", event_id=rid,
                         http_status=500, error_type=type(e).__name__,
                         message=str(e)[:500], stack=traceback.format_exc(), **ctx)
        return jsonify({"error": "internal server error", "error_id": rid}), 500

    try:
        import db_backup
        db_backup.start_backup_scheduler()
    except Exception:
        _log.exception("db backup scheduler start failed")

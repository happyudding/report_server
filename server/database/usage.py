"""접속 사용량 일별 집계 (report_usage_daily) — Honey 실행·웹페이지 방문 카운터.

기록은 best-effort 다: 페이지 서빙/버전체크 응답을 막으면 안 되므로 짧은
busy_timeout 으로 시도하고 실패는 조용히 버린다 (호출측도 try/except).
집계 조회는 admin_panel/stats.py 가 get_conn() 자체 SELECT 로 수행한다.
"""
import time

from .core import get_conn, _now

# kind 값 (호출측과 admin 집계가 공유하는 규약)
KIND_HONEY_RUN = "honey_run"   # Honey 시작 시 /honey/version 체크 1회
KIND_WEB_INDEX = "web_index"   # 검색결과 페이지 (GET /pe/report/)
KIND_WEB_VIEW = "web_view"     # 세션 상세 페이지 (GET /pe/report/view/<sid>)


def record_usage(kind, user_id):
    """(오늘, kind, user_id) 카운터 +1. user_id 가 비어 있으면 no-op.

    쓰기 경합 시 100ms 만 기다리고 포기한다 — 사용량 집계 1건 유실은 무해하고
    본 요청 지연이 더 해롭다 (VOC 감사와 같은 원칙).
    """
    if not user_id:
        return
    now = _now()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    with get_conn(busy_timeout_ms=100) as conn:
        conn.execute(
            "INSERT INTO report_usage_daily (day, kind, user_id, count, last_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(day, kind, user_id) "
            "DO UPDATE SET count = count + 1, last_at = excluded.last_at",
            (day, kind, user_id, now),
        )

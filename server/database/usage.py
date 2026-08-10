"""접속 사용량 일별 집계 (report_usage_daily) — Honey 실행·웹페이지 방문 카운터.

기록은 best-effort 다: 페이지 서빙/버전체크 응답을 막으면 안 되므로 짧은
busy_timeout 으로 시도하고 실패는 조용히 버린다 (호출측도 try/except).
사용자별 순위 집계는 admin_panel/stats.py 가 get_conn() 자체 SELECT 로 수행하고,
여기 usage_totals 는 사용자 축을 지운 하루 합계만 돌려준다 (/pe 랜딩 현황 수치).
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


def usage_totals(day=None):
    """하루치 접속 사용량 합계 -> {"day","total","honey_run","web_index","web_view"}.

    무인증 랜딩에 나가는 값이라 **사용자 축을 지운 합계만** 돌려준다 — user_id
    (무신원은 'ip:<addr>')는 어떤 형태로도 포함하지 않는다.
    day 기본값은 record_usage 와 같은 localtime 기준이어야 자정 경계가 어긋나지 않는다.
    """
    day = day or time.strftime("%Y-%m-%d", time.localtime(_now()))
    out = {"day": day, "total": 0,
           KIND_HONEY_RUN: 0, KIND_WEB_INDEX: 0, KIND_WEB_VIEW: 0}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT kind, SUM(count) AS n FROM report_usage_daily "
            "WHERE day = ? GROUP BY kind", (day,)).fetchall()
    for r in rows:
        n = int(r["n"] or 0)
        out["total"] += n
        if r["kind"] in out:
            out[r["kind"]] = n
    return out

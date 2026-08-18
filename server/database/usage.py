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

    같은 이벤트를 일별(report_usage_daily)과 시간별(report_usage_hourly) 두 테이블에
    함께 올린다 — 일별은 날짜 문자열이라 시간대 분포를 복원할 수 없기 때문이다.
    한 트랜잭션이라 둘은 항상 같이 성공하거나 같이 실패한다.

    쓰기 경합 시 100ms 만 기다리고 포기한다 — 사용량 집계 1건 유실은 무해하고
    본 요청 지연이 더 해롭다 (VOC 감사와 같은 원칙).
    """
    if not user_id:
        return
    now = _now()
    # localtime 을 한 번만 구해 재사용한다 — 두 번 부르면 자정 경계에서 day 와 hour 가
    # 서로 다른 날을 가리킬 수 있다.
    lt = time.localtime(now)
    day = time.strftime("%Y-%m-%d", lt)
    with get_conn(busy_timeout_ms=100) as conn:
        conn.execute(
            "INSERT INTO report_usage_daily (day, kind, user_id, count, last_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(day, kind, user_id) "
            "DO UPDATE SET count = count + 1, last_at = excluded.last_at",
            (day, kind, user_id, now),
        )
        conn.execute(
            "INSERT INTO report_usage_hourly (day, hour, kind, user_id, count, last_at) "
            "VALUES (?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(day, hour, kind, user_id) "
            "DO UPDATE SET count = count + 1, last_at = excluded.last_at",
            (day, lt.tm_hour, kind, user_id, now),
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


def record_active_peak(count, window_sec, now=None):
    """오늘의 동시 접속자(사람) 최대값 갱신 — admin_panel/metrics 샘플러가 호출한다.

    값은 **낮아지지 않는다**: 서버가 재시작하면 샘플러의 메모리 최대치가 0 부터 다시
    올라가므로, 그대로 덮어쓰면 그날 이미 기록한 피크가 지워진다. MAX 로 막고,
    peak_at 은 실제로 최대치가 갱신될 때만 바꾼다.
    """
    if count is None or count <= 0:
        return
    now = _now() if now is None else int(now)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    with get_conn(busy_timeout_ms=100) as conn:
        conn.execute(
            "INSERT INTO report_usage_peak_daily "
            "       (day, peak_users, peak_at, window_sec, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET "
            "  peak_at    = CASE WHEN excluded.peak_users > peak_users "
            "                    THEN excluded.peak_at ELSE peak_at END, "
            "  window_sec = CASE WHEN excluded.peak_users > peak_users "
            "                    THEN excluded.window_sec ELSE window_sec END, "
            "  peak_users = MAX(peak_users, excluded.peak_users), "
            "  updated_at = excluded.updated_at",
            (day, int(count), now, int(window_sec), now),
        )


def peak_series(cutoff_day):
    """cutoff_day 이후의 일별 피크 -> {day: {"peak_users","peak_at","window_sec"}}.

    수집 시작 이전 날짜는 행 자체가 없다 — 호출측이 '0명' 과 '기록 없음' 을 구분해야
    한다(0 으로 채우면 그날 아무도 안 쓴 것처럼 보인다).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT day, peak_users, peak_at, window_sec FROM report_usage_peak_daily "
            "WHERE day >= ?", (cutoff_day,)).fetchall()
    return {r["day"]: {"peak_users": int(r["peak_users"] or 0),
                       "peak_at": int(r["peak_at"] or 0),
                       "window_sec": int(r["window_sec"] or 0)} for r in rows}


def peak_first_day():
    """피크 수집이 시작된 첫 날('YYYY-MM-DD') 또는 None — 그래프의 '기록 없음' 경계."""
    with get_conn() as conn:
        row = conn.execute("SELECT MIN(day) AS d FROM report_usage_peak_daily").fetchone()
    return row["d"] if row and row["d"] else None


def purge_usage(hourly_cutoff_day=None, daily_cutoff_day=None):
    """사용량 롤오프 — cutoff **이전** 날짜 행 삭제. {"hourly","daily","peak"} 반환.

    시간별은 요일×시간 히트맵용이라 최근 구간만 있으면 되고(카디널리티가 24배), 일별·Peak
    은 장기 추이라 훨씬 길게 둔다. cutoff 는 'YYYY-MM-DD' 문자열 — day 컬럼이 문자열이라
    사전순 비교가 곧 날짜 비교다."""
    out = {"hourly": 0, "daily": 0, "peak": 0}
    with get_conn() as conn:
        if hourly_cutoff_day:
            out["hourly"] = conn.execute(
                "DELETE FROM report_usage_hourly WHERE day < ?",
                (hourly_cutoff_day,)).rowcount
        if daily_cutoff_day:
            out["daily"] = conn.execute(
                "DELETE FROM report_usage_daily WHERE day < ?",
                (daily_cutoff_day,)).rowcount
            out["peak"] = conn.execute(
                "DELETE FROM report_usage_peak_daily WHERE day < ?",
                (daily_cutoff_day,)).rowcount
    return out

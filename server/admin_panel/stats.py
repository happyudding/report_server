"""사용량 통계 — report_audit_log 집계 (일별 추이 · 사용자별 순위).

report_db.py 는 수정하지 않고 get_conn() 으로 자체 SELECT 만 수행한다.
created_at 은 epoch 초 → SQLite strftime(..., 'unixepoch', 'localtime') 로 일 단위 그룹.
"""
import time
from datetime import date, timedelta

from admin_panel import identity_merge
from database import report_db

_ACTIONS = ("upload", "edit", "delete")


def _clamp_days(days, default=30, lo=1, hi=365):
    try:
        return max(lo, min(int(days), hi))
    except (TypeError, ValueError):
        return default


def daily_counts(days=30):
    """최근 N일 일별 upload/edit/delete 건수. 빈 날짜는 0 으로 채워 반환."""
    days = _clamp_days(days)
    cutoff = int(time.time()) - days * 86400
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') AS day, "
            "       action, COUNT(*) AS cnt "
            "FROM report_audit_log WHERE created_at >= ? "
            "GROUP BY day, action", (cutoff,)).fetchall()
    by_day = {}
    for r in rows:
        d = by_day.setdefault(r["day"], {a: 0 for a in _ACTIONS})
        if r["action"] in d:
            d[r["action"]] += r["cnt"]
    out = []
    today = date.today()
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        counts = by_day.get(day, {a: 0 for a in _ACTIONS})
        out.append({"date": day, **counts})
    return {"days": days, "rows": out}


def client_error_count(hours=24):
    """최근 N시간 client_error(브라우저 beacon) 감사 행 수 — 현황 탭 경고 타일용.

    get_audit_logs 로 받아 세는 방식은 limit 상한(≤1000)에 걸려 과소집계되므로
    COUNT 로 직접 센다."""
    hours = _clamp_days(hours, default=24, lo=1, hi=720)
    cutoff = int(time.time()) - hours * 3600
    with report_db.get_conn() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM report_audit_log "
            "WHERE action = 'client_error' AND created_at >= ?", (cutoff,)).fetchone()[0]
    return {"hours": hours, "count": int(cnt)}


def usage_ranking(days=30, limit=50):
    """접속 사용량 순위 — report_usage_daily 집계 (database/usage.py 가 기록).

    honey_run = Honey 실행(시작 시 버전체크), web_index = 검색결과 페이지,
    web_view = 세션 상세 페이지. 신원 없는 접속은 'ip:<addr>' 행으로 집계되는데,
    그 IP 가 계정 하나로 확정되면 같은 사람으로 합친다(identity_merge).

    LIMIT 은 병합 **후** 적용한다 — 갈라져 있던 두 행이 합쳐지면 순위가 바뀌므로
    DB 단계에서 자르면 잘린 조각이 사라진다."""
    days = _clamp_days(days)
    cutoff_day = (date.today() - timedelta(days=days - 1)).isoformat()
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, "
            "       SUM(CASE WHEN kind='honey_run' THEN count ELSE 0 END) AS honey_run, "
            "       SUM(CASE WHEN kind='web_index' THEN count ELSE 0 END) AS web_index, "
            "       SUM(CASE WHEN kind='web_view'  THEN count ELSE 0 END) AS web_view, "
            "       SUM(count) AS total, MAX(last_at) AS last_at "
            "FROM report_usage_daily WHERE day >= ? "
            "GROUP BY user_id ORDER BY total DESC",
            (cutoff_day,)).fetchall()

    mapping = identity_merge.ip_to_user()
    merged = {}
    for r in rows:
        name, was_merged = identity_merge.resolve(r["user_id"], mapping=mapping)
        cur = merged.get(name)
        if cur is None:
            cur = merged[name] = {"user_id": name, "honey_run": 0, "web_index": 0,
                                  "web_view": 0, "total": 0, "last_at": 0,
                                  "merged_from": []}
        for col in ("honey_run", "web_index", "web_view", "total"):
            cur[col] += r[col] or 0
        cur["last_at"] = max(cur["last_at"] or 0, r["last_at"] or 0)
        if was_merged:
            cur["merged_from"].append(r["user_id"])
    out = sorted(merged.values(), key=lambda d: d["total"], reverse=True)[:int(limit)]
    return {"days": days, "rows": out}


def _merged_first_days(mapping):
    """{계정: 최초 접속일} — report_usage_daily 전 기간. IP 병합 후 다시 min 을 잡는다.

    신규/재방문 판정과 누적 고유 사용자의 근거다. 행 수가 사용자 수뿐이라 전 기간을 봐도
    저렴하고, **기간을 자르면 안 된다** — 30일 창만 보면 예전 사용자가 전부 신규가 된다.
    """
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, MIN(day) AS first_day FROM report_usage_daily "
            "GROUP BY user_id").fetchall()
    out = {}
    for r in rows:
        name, _ = identity_merge.resolve(r["user_id"], mapping=mapping)
        cur = out.get(name)
        if cur is None or r["first_day"] < cur:
            out[name] = r["first_day"]
    return out


def usage_trend(days=30):
    """일별 접속 추이 — 관리자 사용자 탭 그래프. report_usage_daily + 일별 피크.

    count 는 페이지 진입 '횟수'(새로고침 포함)라 사람 수가 아니다. 그래서 고유 사용자 수
    (users)와 횟수(visits)를 함께 돌려준다. 사용자 축은 usage_ranking 과 **같은 기준**으로
    IP 병합한다 — 한쪽만 병합하면 같은 사람이 두 명으로 세어져 표와 그래프가 어긋난다.

    wau(주간 접속자)는 그날 포함 최근 7일 롤링 고유 사용자다. 요청 기간의 첫날들도
    정확하려면 앞선 6일이 필요하므로 SQL cutoff 만 days+6 으로 넓히고 rows 는 days 만 준다.
    peak_users 는 수집 시작(peak_since) 이전 날짜에서 None 이다 — 0 으로 채우면 '아무도
    안 썼다' 로 잘못 읽힌다.
    """
    days = _clamp_days(days)
    today = date.today()
    span_from = today - timedelta(days=days - 1 + 6)   # WAU 워밍업 6일 포함
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT day, kind, user_id, SUM(count) AS n FROM report_usage_daily "
            "WHERE day >= ? GROUP BY day, kind, user_id",
            (span_from.isoformat(),)).fetchall()

    mapping = identity_merge.ip_to_user()
    by_day = {}          # day -> {"users": set, "visits": int, kind: int}
    for r in rows:
        name, _ = identity_merge.resolve(r["user_id"], mapping=mapping)
        d = by_day.get(r["day"])
        if d is None:
            d = by_day[r["day"]] = {"users": set(), "visits": 0,
                                    "honey_run": 0, "web_index": 0, "web_view": 0}
        n = int(r["n"] or 0)
        d["users"].add(name)
        d["visits"] += n
        if r["kind"] in d:
            d[r["kind"]] += n

    first_days = _merged_first_days(mapping)
    peaks = report_db.peak_series((today - timedelta(days=days - 1)).isoformat())
    peak_since = report_db.peak_first_day()

    # 누적 고유 사용자 — 최초 접속일별 인원수를 날짜순으로 더해 간다.
    new_by_day = {}
    for first in first_days.values():
        new_by_day[first] = new_by_day.get(first, 0) + 1
    cum = sum(n for d, n in new_by_day.items() if d < span_from.isoformat())

    window = []   # 최근 7일 사용자 집합 (롤링)
    out = []
    for i in range((today - span_from).days + 1):
        day = (span_from + timedelta(days=i)).isoformat()
        cur = by_day.get(day)
        users = cur["users"] if cur else set()
        window.append(users)
        if len(window) > 7:
            window.pop(0)
        cum += new_by_day.get(day, 0)
        if i < 6:
            continue   # 워밍업 구간 — 반환하지 않는다
        new_users = sum(1 for u in users if first_days.get(u) == day)
        pk = peaks.get(day)
        out.append({
            "date": day,
            "users": len(users),
            "new_users": new_users,
            "returning": len(users) - new_users,
            "visits": cur["visits"] if cur else 0,
            "honey_run": cur["honey_run"] if cur else 0,
            "web_index": cur["web_index"] if cur else 0,
            "web_view": cur["web_view"] if cur else 0,
            "wau": len(set().union(*window)) if window else 0,
            "cum_users": cum,
            "peak_users": pk["peak_users"] if pk else None,
        })
    # '동시'의 판정 창은 env 로 바뀔 수 있으므로 가장 최근 기록의 값을 함께 준다
    # (화면 안내 문구가 실제 기준과 어긋나지 않게).
    last_day = max(peaks, default=None)
    last_win = peaks[last_day]["window_sec"] if last_day else 0
    return {"days": days, "peak_since": peak_since,
            "peak_window": last_win or None, "rows": out}


def usage_hourly_heatmap(days=30):
    """요일×시간 접속 히트맵 — report_usage_hourly 집계.

    matrix[요일][시각] = 접속 횟수, users[요일][시각] = 고유 사용자 수 (요일 0=월).
    이 테이블은 2026-08-13 부터 쌓이므로 그 이전 기간은 비어 있는 것이 정상이다.
    """
    days = _clamp_days(days)
    cutoff_day = (date.today() - timedelta(days=days - 1)).isoformat()
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT day, hour, user_id, SUM(count) AS n FROM report_usage_hourly "
            "WHERE day >= ? GROUP BY day, hour, user_id",
            (cutoff_day,)).fetchall()

    mapping = identity_merge.ip_to_user()
    matrix = [[0] * 24 for _ in range(7)]
    users = [[set() for _ in range(24)] for _ in range(7)]
    total = 0
    for r in rows:
        try:
            wd = date.fromisoformat(r["day"]).weekday()
            hh = int(r["hour"])
        except (TypeError, ValueError):
            continue
        if not (0 <= hh <= 23):
            continue
        name, _ = identity_merge.resolve(r["user_id"], mapping=mapping)
        n = int(r["n"] or 0)
        matrix[wd][hh] += n
        users[wd][hh].add(name)
        total += n
    return {"days": days, "total": total,
            "max": max((v for row in matrix for v in row), default=0),
            "matrix": matrix,
            "users": [[len(s) for s in row] for row in users]}


def user_ranking(days=30, limit=50):
    """사용자별 사용량 순위. 신원은 client_user → client_host → client_ip 순 폴백
    (전부 클라이언트 신고값 + IP 라 참고용). 'system' 은 cleanup 스케줄러.

    이름이 IP 로 떨어진 행(= 신원 토큰 없이 남은 기록)은 그 IP 가 계정 하나로 확정되면
    같은 사람으로 합친다(identity_merge). usage_ranking 과 같은 이유로 LIMIT 은 병합 후."""
    days = _clamp_days(days)
    cutoff = int(time.time()) - days * 86400
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(client_user,''), NULLIF(client_host,''), "
            "                NULLIF(client_ip,''), 'unknown') AS who, "
            "       MAX(NULLIF(client_host,'')) AS host, "
            "       MAX(NULLIF(client_ip,'')) AS ip, "
            "       SUM(CASE WHEN action='upload' THEN 1 ELSE 0 END) AS upload, "
            "       SUM(CASE WHEN action='edit'   THEN 1 ELSE 0 END) AS edit, "
            "       SUM(CASE WHEN action='delete' THEN 1 ELSE 0 END) AS `delete`, "
            "       COUNT(*) AS total, MAX(created_at) AS last_at "
            "FROM report_audit_log WHERE created_at >= ? "
            "GROUP BY who ORDER BY total DESC", (cutoff,)).fetchall()

    mapping = identity_merge.ip_to_user()
    merged = {}
    for r in rows:
        name, was_merged = identity_merge.resolve(r["who"], r["ip"], mapping=mapping)
        cur = merged.get(name)
        if cur is None:
            cur = merged[name] = {"who": name, "host": r["host"], "ip": r["ip"],
                                  "upload": 0, "edit": 0, "delete": 0, "total": 0,
                                  "last_at": 0, "merged_from": []}
        for col in ("upload", "edit", "delete", "total"):
            cur[col] += r[col] or 0
        cur["host"] = cur["host"] or r["host"]
        cur["ip"] = cur["ip"] or r["ip"]
        cur["last_at"] = max(cur["last_at"] or 0, r["last_at"] or 0)
        if was_merged:
            cur["merged_from"].append(r["who"])
    out = sorted(merged.values(), key=lambda d: d["total"], reverse=True)[:int(limit)]
    return {"days": days, "rows": out}

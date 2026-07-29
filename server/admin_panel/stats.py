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

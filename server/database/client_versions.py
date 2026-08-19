"""Honey 클라이언트 버전 대장 (report_client_version) — "누가 어떤 버전을 쓰고 있나".

지금까지 서버는 접속자의 클라 버전을 알 수 없었다. 신원은 User-Agent 의 `HoneyUser/<계정>`
토큰으로 오지만 버전은 어디에도 실려 오지 않았고, 유일한 예외가 오류 보고 payload 라
**에러가 난 사람만** 버전을 알 수 있었다. 클라가 UA 에 `HoneyVer/<버전>` 을 함께 실어
보내면서(2026-08-18) 그 값을 사람 단위로 여기 남긴다.

기록 지점은 **앱 시작 시 1회** 오는 `GET /honey/version` 하나뿐이다(honey_routes._record_run).
요청마다 쓰면 DB 경합이 되고, 버전은 프로세스가 사는 동안 바뀌지 않으므로 그럴 이유도 없다.
기록은 사용량 집계와 같은 best-effort — 짧은 busy_timeout 으로 시도하고 실패는 버린다.

**행이 없는 = 버전 토큰을 안 보내는 구버전 클라**다. 이 '미상'이 곧 업데이트 안 한 사람이라
조회(`version_report`)는 대장에 없는 사람도 사용량 기록에서 끌어와 함께 돌려준다 — 없는
사람이 빠져 버리면 정작 찾으려던 대상이 목록에서 사라진다.
"""
import time

from .core import _now, get_conn


def record_client_version(user_id, version):
    """(사람, 버전) 갱신. 둘 중 하나라도 비면 no-op.

    버전이 바뀌면 prev_version 에 옛 값을 남기고 first_at·runs 를 새 버전 기준으로
    리셋한다 — "이 버전으로 언제부터 몇 번 실행했나" 가 업데이트 반영 확인의 근거다.
    """
    if not user_id or not version:
        return
    now = _now()
    with get_conn(busy_timeout_ms=100) as conn:
        conn.execute(
            "INSERT INTO report_client_version "
            "       (user_id, version, prev_version, first_at, last_at, runs, updated_at) "
            "VALUES (?, ?, NULL, ?, ?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  prev_version = CASE WHEN version = excluded.version "
            "                      THEN prev_version ELSE version END, "
            "  first_at     = CASE WHEN version = excluded.version "
            "                      THEN first_at ELSE excluded.first_at END, "
            "  runs         = CASE WHEN version = excluded.version "
            "                      THEN runs + 1 ELSE 1 END, "
            "  version      = excluded.version, "
            "  last_at      = excluded.last_at, "
            "  updated_at   = excluded.updated_at",
            (user_id, version, now, now, now),
        )


def get_client_versions(user_ids):
    """{user_id: version} 배치 조회 — 관리자 표가 행마다 조회하면 N+1 이 된다."""
    keys = [str(u).strip().lower() for u in (user_ids or []) if u]
    if not keys:
        return {}
    out = {}
    with get_conn() as conn:
        # SQLite 변수 상한(999)을 넘지 않게 나눠 조회한다 (접속자 300명 상한이라 보통 1회).
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                    f"SELECT user_id, version FROM report_client_version "
                    f"WHERE user_id IN ({marks})", chunk):
                out[r["user_id"]] = r["version"]
    return out


def version_report(days=30):
    """최근 days 안에 Honey 를 실행한 사람들의 버전 현황.

    -> {"days", "total", "known", "unknown", "versions":[{version,users,last_at}],
        "rows":[{user_id, version, prev_version, last_at, runs, version_at}]}

    모집단은 **대장이 아니라 사용량 기록**(report_usage_daily, kind=honey_run)이다 —
    대장에만 있는 사람을 세면 버전 토큰을 보내는 신버전 사용자만 남아 "전원이 최신"으로
    보인다. version 이 None 인 행이 곧 구버전(토큰 미전송) 사용자다.
    """
    try:
        days = max(1, min(int(days), 730))
    except (TypeError, ValueError):
        days = 30
    cutoff_day = time.strftime("%Y-%m-%d", time.localtime(_now() - days * 86400))
    with get_conn() as conn:
        rows = conn.execute("""
            WITH runs AS (
                SELECT user_id, MAX(last_at) AS last_at, SUM(count) AS runs
                  FROM report_usage_daily
                 WHERE kind = 'honey_run' AND day >= ? AND user_id NOT LIKE 'ip:%'
                 GROUP BY user_id
            )
            SELECT r.user_id, r.last_at, r.runs,
                   v.version, v.prev_version, v.last_at AS version_at
              FROM runs r
              LEFT JOIN report_client_version v ON v.user_id = r.user_id
             ORDER BY r.last_at DESC
        """, (cutoff_day,)).fetchall()

    out = [dict(r) for r in rows]
    versions = {}
    for r in out:
        v = r.get("version") or ""
        cur = versions.setdefault(v, {"version": v, "users": 0, "last_at": 0})
        cur["users"] += 1
        cur["last_at"] = max(cur["last_at"], int(r.get("last_at") or 0))
    # 버전 문자열 내림차순 — 숫자 조각으로 비교해야 '3.10.0' 이 '3.9.0' 뒤에 오지 않는다.
    # 미상("")은 항상 마지막.
    def _key(v):
        s = v["version"]
        if not s:
            return (0, ())
        parts = tuple(int(p) if p.isdigit() else 0 for p in s.split("."))
        return (1, parts)

    return {"days": days, "total": len(out),
            "known": sum(1 for r in out if r.get("version")),
            "unknown": sum(1 for r in out if not r.get("version")),
            "versions": sorted(versions.values(), key=_key, reverse=True),
            "rows": out}

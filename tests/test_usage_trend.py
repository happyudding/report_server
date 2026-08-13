"""접속 추이 집계 검증 — 관리자 사용자 탭 '📈 접속 추이' 그래프.

시나리오:
  (a) record_usage — 일별(report_usage_daily)과 시간별(report_usage_hourly)에 함께 +1
  (b) record_active_peak — 최대값만 올라가고 낮은 값으로 덮어써지지 않는다(서버 재시작 대비)
  (c) stats.usage_trend — 고유 사용자/신규·재방문/접속 횟수/WAU/누적/일별 Peak
  (d) stats.usage_hourly_heatmap — 요일×시간 매트릭스
  (e) 정합성 — usage_trend 의 visits 합계 == usage_ranking 의 total 합계 (같은 소스·같은 병합)

실행:
    python tests/test_usage_trend.py

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="usage_trend_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

from database import report_db  # noqa: E402
from database.core import get_conn  # noqa: E402
from admin_panel import stats  # noqa: E402

report_db.init_report_db()

_failures = []


def check(ok, label):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        _failures.append(label)


def clear():
    with get_conn() as conn:
        conn.execute("DELETE FROM report_usage_daily")
        conn.execute("DELETE FROM report_usage_hourly")
        conn.execute("DELETE FROM report_usage_peak_daily")


def day_of(offset):
    return (date.today() - timedelta(days=offset)).isoformat()


# ── (a) record_usage 가 두 테이블에 함께 기록 ────────────────────────────────

report_db.record_usage("web_view", "alice")
report_db.record_usage("web_view", "alice")
report_db.record_usage("web_view", "")          # no-op
with get_conn() as conn:
    d = [dict(r) for r in conn.execute("SELECT * FROM report_usage_daily")]
    h = [dict(r) for r in conn.execute("SELECT * FROM report_usage_hourly")]
check(len(d) == 1 and d[0]["count"] == 2, "(a) 일별 count=2 (기존 동작 그대로)")
check(len(h) == 1 and h[0]["count"] == 2, "(a) 시간별에도 같은 이벤트 count=2")
check(h[0]["day"] == d[0]["day"] and h[0]["hour"] == time.localtime().tm_hour,
      "(a) 시간별 day/hour = 서버 localtime 현재")
clear()

# ── (b) record_active_peak — 최대값만 갱신 ──────────────────────────────────

now = int(time.time())
report_db.record_active_peak(3, 300, now=now)
report_db.record_active_peak(7, 300, now=now + 10)
report_db.record_active_peak(2, 300, now=now + 20)   # 재시작 후 낮은 값 — 무시돼야 한다
report_db.record_active_peak(0, 300, now=now + 30)   # 0 은 no-op
pk = report_db.peak_series(day_of(1))[day_of(0)]
check(pk["peak_users"] == 7, "(b) 최대값 7 유지 (낮은 값으로 덮어써지지 않음)")
check(pk["peak_at"] == now + 10, "(b) peak_at 은 최대값을 찍은 시각")
check(report_db.peak_first_day() == day_of(0), "(b) peak_first_day = 수집 시작일")
clear()

# ── (c) usage_trend ─────────────────────────────────────────────────────────

with get_conn() as conn:
    conn.executemany(
        "INSERT INTO report_usage_daily (day, kind, user_id, count, last_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (day_of(8), "web_index", "alice", 5, now),   # alice 최초 접속 = 8일 전
            (day_of(1), "web_index", "alice", 2, now),
            (day_of(0), "web_index", "alice", 1, now),
            (day_of(0), "honey_run", "bob", 4, now),     # bob 최초 접속 = 오늘 → 신규
        ])
report_db.record_active_peak(6, 300, now=now)           # 오늘만 peak 기록

t = stats.usage_trend(days=7)
check(len(t["rows"]) == 7, "(c) rows 길이 = 요청 기간 (빈 날짜도 채움)")
check([r["date"] for r in t["rows"]] == [day_of(i) for i in range(6, -1, -1)],
      "(c) 날짜 오름차순 · 오늘로 끝남")

today_row = t["rows"][-1]
check(today_row["users"] == 2, "(c) 오늘 고유 사용자 2명 (alice, bob)")
check((today_row["new_users"], today_row["returning"]) == (1, 1),
      "(c) 신규 1(bob) / 재방문 1(alice)")
check(today_row["visits"] == 5 and today_row["web_index"] == 1 and today_row["honey_run"] == 4,
      "(c) 접속 횟수 5 = web_index 1 + honey_run 4")
check(today_row["wau"] == 2, "(c) WAU = 최근 7일 고유 사용자 2명")
check(today_row["cum_users"] == 2, "(c) 누적 고유 사용자 2명")
check(today_row["peak_users"] == 6, "(c) 오늘 Peak 동시 접속자 6명")

yday = t["rows"][-2]
check((yday["users"], yday["visits"]) == (1, 2), "(c) 어제 = alice 1명 / 2회")
check(yday["new_users"] == 0, "(c) 어제는 신규 0 (alice 최초 접속은 8일 전)")
check(yday["peak_users"] is None, "(c) 수집 시작 전 날짜의 peak 은 None (0 이 아님)")

first = t["rows"][0]   # 6일 전 — 접속 기록 없음
check((first["users"], first["visits"], first["wau"]) == (0, 0, 1),
      "(c) 기록 없는 날도 0 으로 채우되 WAU 는 8일 전 alice 를 포함하지 않는다")
check(first["cum_users"] == 1, "(c) 누적은 기간 이전(alice)까지 반영")
check(t["peak_since"] == day_of(0) and t["peak_window"] == 300,
      "(c) peak_since / peak_window 노출")

# ── (e) 정합성 — visits 합계 == usage_ranking total 합계 ─────────────────────

trend_sum = sum(r["visits"] for r in stats.usage_trend(days=30)["rows"])
rank_sum = sum(r["total"] for r in stats.usage_ranking(days=30)["rows"])
check(trend_sum == rank_sum, f"(e) visits 합계 == usage_ranking total 합계 ({trend_sum})")
clear()

# ── (d) usage_hourly_heatmap ────────────────────────────────────────────────

d0 = day_of(0)
wd0 = date.today().weekday()
with get_conn() as conn:
    conn.executemany(
        "INSERT INTO report_usage_hourly (day, hour, kind, user_id, count, last_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (d0, 14, "web_index", "alice", 3, now),
            (d0, 14, "web_view", "bob", 2, now),
            (d0, 9, "honey_run", "alice", 1, now),
        ])
hm = stats.usage_hourly_heatmap(days=7)
check(hm["matrix"][wd0][14] == 5, "(d) (오늘 요일, 14시) 접속 5회")
check(hm["users"][wd0][14] == 2, "(d) (오늘 요일, 14시) 고유 사용자 2명")
check(hm["matrix"][wd0][9] == 1 and hm["max"] == 5 and hm["total"] == 6,
      "(d) 9시 1회 · max=5 · total=6")
check(len(hm["matrix"]) == 7 and all(len(r) == 24 for r in hm["matrix"]),
      "(d) 7 × 24 매트릭스")
clear()

check(stats.usage_hourly_heatmap(days=7)["max"] == 0, "(d) 기록 없으면 max=0 (빈 히트맵)")
check(stats.usage_trend(days=7)["peak_since"] is None, "(c) 기록 없으면 peak_since=None")

print()
if _failures:
    print(f"FAILED: {len(_failures)}건")
    for f in _failures:
        print("  - " + f)
    sys.exit(1)
print("ALL OK")

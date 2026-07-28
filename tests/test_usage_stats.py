"""접속 사용량 집계 (report_usage_daily) 검증 — 관리자 통계 탭 '접속 사용량' 카드.

시나리오:
  (a) record_usage — UPSERT 증분(같은 날/kind/user 는 count+1), 빈 user_id 는 no-op
  (b) GET /honey/version — HoneyUser UA 면 계정으로, 없으면 ip:<addr> 로 honey_run 집계
      (version.json 이 없어 404 여도 실행 집계는 남는다)
  (c) GET /pe/report/ · /pe/report/view/<sid> — web_index / web_view 집계
  (d) stats.usage_ranking — kind 피벗 · total 내림차순 · days 컷오프

실행:
    python tests/test_usage_stats.py

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="usage_stats_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

from flask import Flask  # noqa: E402

from report.report_extension import report_bp  # noqa: E402  (전체 라우트 등록 트리거)
from honey_routes import honey_bp  # noqa: E402
from database import report_db  # noqa: E402
from database.core import get_conn  # noqa: E402
from admin_panel import stats  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
app.register_blueprint(honey_bp)
report_db.init_report_db()
client = app.test_client()

_failures = []


def check(ok, label):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        _failures.append(label)


def rows(where="1=1", args=()):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM report_usage_daily WHERE " + where +
            " ORDER BY kind, user_id", args).fetchall()]


def clear():
    with get_conn() as conn:
        conn.execute("DELETE FROM report_usage_daily")


# ── (a) record_usage UPSERT ──────────────────────────────────────────────────

report_db.record_usage("honey_run", "alice")
report_db.record_usage("honey_run", "alice")
report_db.record_usage("web_view", "alice")
report_db.record_usage("honey_run", "")          # no-op
r = rows()
check(len(r) == 2, "(a) 행 = (kind,user) 조합 2개 (빈 user_id 는 미기록)")
hr = [x for x in r if x["kind"] == "honey_run"][0]
check(hr["count"] == 2 and hr["user_id"] == "alice", "(a) 같은 날/kind/user 재기록 → count=2")
check(hr["day"] == time.strftime("%Y-%m-%d"), "(a) day = 오늘 (localtime)")
clear()

# ── (b) /honey/version → honey_run 집계 ─────────────────────────────────────

resp = client.get("/honey/version",
                  headers={"User-Agent": "python-requests HoneyUser/Tester%40PC"})
# version.json 은 저장소 상태에 따라 있을 수도(200) 없을 수도(404) 있다 — 어느 쪽이든 집계는 남는다.
check(resp.status_code in (200, 404), "(b) /honey/version 응답 200 또는 404 (기존 동작 유지)")
r = rows("kind='honey_run'")
check(len(r) == 1 and r[0]["user_id"] == "tester@pc" and r[0]["count"] == 1,
      "(b) HoneyUser UA → 소문자 계정으로 honey_run 1건 (manifest 유무 무관)")

resp = client.get("/honey/version")   # UA 에 신원 없음 → IP 폴백
r = rows("kind='honey_run' AND user_id LIKE 'ip:%'")
check(len(r) == 1 and r[0]["count"] == 1, "(b) 신원 없음 → ip:<addr> 로 집계")
clear()

# ── (c) 페이지 방문 → web_index / web_view 집계 ──────────────────────────────

ua = {"User-Agent": "Mozilla/5.0 HoneyUser/bob"}
check(client.get("/pe/report/", headers=ua).status_code == 200, "(c) 검색결과 페이지 200")
check(client.get("/pe/report/", headers=ua).status_code == 200, "(c) 검색결과 페이지 재방문 200")
check(client.get("/pe/report/view/12345_abc", headers=ua).status_code == 200,
      "(c) 세션 상세 200 (없는 세션도 HTML 서빙 — 기존 동작)")
r = rows("user_id='bob'")
check([(x["kind"], x["count"]) for x in r] == [("web_index", 2), ("web_view", 1)],
      "(c) bob: web_index=2, web_view=1")

check(client.get("/pe/report/").status_code == 200, "(c) 무신원 브라우저 200")
r = rows("kind='web_index' AND user_id LIKE 'ip:%'")
check(len(r) == 1 and r[0]["count"] == 1, "(c) 무신원 방문 → ip:<addr> 로 집계")
clear()

# ── (d) usage_ranking 피벗·정렬·컷오프 ───────────────────────────────────────

now = int(time.time())
today = time.strftime("%Y-%m-%d", time.localtime(now))
old_day = time.strftime("%Y-%m-%d", time.localtime(now - 40 * 86400))
with get_conn() as conn:
    conn.executemany(
        "INSERT INTO report_usage_daily (day, kind, user_id, count, last_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (today, "honey_run", "alice", 5, now),
            (today, "web_index", "alice", 3, now),
            (today, "web_view", "alice", 2, now),
            (today, "web_view", "bob", 20, now - 100),
            (old_day, "honey_run", "carol", 99, now - 40 * 86400),  # 30일 밖
        ])
out = stats.usage_ranking(days=30)
who = [r["user_id"] for r in out["rows"]]
check(who == ["bob", "alice"], "(d) total 내림차순 + 30일 컷오프 (carol 제외)")
alice = out["rows"][1]
check((alice["honey_run"], alice["web_index"], alice["web_view"], alice["total"])
      == (5, 3, 2, 10), "(d) kind 피벗 합계 (alice 5/3/2, total 10)")
out365 = stats.usage_ranking(days=365)
check("carol" in [r["user_id"] for r in out365["rows"]], "(d) days=365 면 carol 포함")

print()
if _failures:
    print(f"FAILED: {len(_failures)}건")
    for f in _failures:
        print("  - " + f)
    sys.exit(1)
print("ALL OK")

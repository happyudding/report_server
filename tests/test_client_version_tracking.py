"""Honey 클라 버전 추적 + 활동 상세 검증 (2026-08-18).

배경: 관리자 화면은 접속자가 **어떤 Honey 버전**을 쓰는지 알 수 없었고("지금 접속 중"에
계정·IP 만 있었다), '지금 하는 일'도 마지막 요청 1건뿐이라 무엇을 하다 막혔는지 흐름을
볼 수 없었다. 클라가 UA 에 `HoneyVer/<버전>` 을 함께 실어 보내면서 아래가 가능해졌다.

  (a) UA 파싱 — HoneyVer 토큰 → 버전, python-requests 여부 → 접속 경로(app/browser).
      기존 HoneyUser 신원 파싱이 **깨지지 않는 것**이 가장 중요하다(토큰을 뒤에 붙였다)
  (b) 버전 대장 upsert — 같은 버전 재실행은 runs 증가, 버전이 바뀌면 prev_version 보존
  (c) version_report — 모집단은 사용량 기록이라 **버전을 안 보내는 구버전 사용자도** 행으로
      남는다(version=None = '미상'). 이게 빠지면 정작 찾으려던 대상이 목록에서 사라진다
  (d) 실시간 계측 — active_users 행에 ver/agents 가 실리고, 버전 토큰이 없는 요청이 뒤에
      와도 이미 알아낸 버전을 **빈 값으로 덮지 않는다**
  (e) 활동 타임라인 — 사람별 최근 요청 목록(경로·세션·소요·상태코드)
  (f) 라우트 — api/client_versions, api/user_timeline, api/active_users(ver_src 폴백)

실행:
    python tests/test_client_version_tracking.py

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

_TMP = Path(tempfile.mkdtemp(prefix="client_ver_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

from flask import Flask  # noqa: E402

import admin_panel  # noqa: E402
import auth_identity  # noqa: E402
from admin_panel import metrics  # noqa: E402
from admin_panel.routes import admin_panel_bp  # noqa: E402
from database import report_db  # noqa: E402

report_db.init_report_db()

_failures = []

# 실제 클라가 만드는 UA 2종 (client/transport/uploader.py · embedded_browser.py)
UA_APP = "python-requests HoneyUser/HONG.GILDONG HoneyVer/3.2.0"
UA_BROWSER = "Mozilla/5.0 (Windows NT 10.0) Chrome/120 HoneyUser/hong.gildong HoneyVer/3.2.0"
UA_OLD = "python-requests HoneyUser/kim"          # 버전 토큰 없는 구버전 클라
UA_WEB = "Mozilla/5.0 (Windows NT 10.0) Chrome/120"   # 일반 브라우저


def check(ok, label):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        _failures.append(label)


def _client():
    app = Flask(__name__)
    app.register_blueprint(admin_panel_bp, url_prefix="/pe/admin-pte")
    c = app.test_client()
    c.set_cookie("pe_admin_gate", admin_panel.gate_token())
    return c


# ── (a) UA 파싱 ──────────────────────────────────────────────────────────────

def test_ua_parsing():
    check(auth_identity.client_version(UA_APP) == "3.2.0",
          f"(a) 앱 UA 에서 버전 추출 ({auth_identity.client_version(UA_APP)})")
    check(auth_identity.client_version(UA_BROWSER) == "3.2.0",
          "(a) 내장 브라우저 UA 에서 버전 추출")
    check(auth_identity.client_version(UA_OLD) == "", "(a) 구버전 클라는 버전 미상")
    check(auth_identity.client_version(UA_WEB) == "", "(a) 일반 브라우저는 버전 미상")

    check(auth_identity.client_agent(UA_APP) == "app", "(a) python-requests → Honey 앱")
    check(auth_identity.client_agent(UA_BROWSER) == "browser", "(a) 크로미움 UA → 내장 브라우저")
    check(auth_identity.client_agent(UA_OLD) == "app", "(a) 구버전 클라도 경로는 판별")
    check(auth_identity.client_agent(UA_WEB) == "", "(a) 일반 브라우저는 Honey 아님")

    # 가장 중요한 회귀 방지 — 버전 토큰을 뒤에 붙여도 신원 파싱이 그대로여야 한다.
    app = Flask(__name__)
    with app.test_request_context("/", headers={"User-Agent": UA_APP}):
        check(auth_identity.current_user() == "hong.gildong",
              f"(a) HoneyVer 를 붙여도 신원 파싱 불변 ({auth_identity.current_user()})")


# ── (b)(c) 버전 대장 ─────────────────────────────────────────────────────────

def test_version_ledger():
    report_db.record_client_version("hong.gildong", "3.1.0")
    report_db.record_client_version("hong.gildong", "3.1.0")
    row = report_db.get_client_versions(["hong.gildong"])
    check(row.get("hong.gildong") == "3.1.0", f"(b) 버전 기록 ({row})")

    with report_db.get_conn() as conn:
        r = dict(conn.execute("SELECT * FROM report_client_version WHERE user_id='hong.gildong'")
                 .fetchone())
    check(r["runs"] == 2 and r["prev_version"] is None,
          f"(b) 같은 버전 재실행은 runs 만 증가 ({r['runs']}, prev={r['prev_version']})")

    report_db.record_client_version("hong.gildong", "3.2.0")
    with report_db.get_conn() as conn:
        r = dict(conn.execute("SELECT * FROM report_client_version WHERE user_id='hong.gildong'")
                 .fetchone())
    check(r["version"] == "3.2.0" and r["prev_version"] == "3.1.0" and r["runs"] == 1,
          f"(b) 버전 변경 시 prev 보존 + runs 리셋 ({r})")

    report_db.record_client_version("", "3.2.0")
    report_db.record_client_version("nobody", "")
    with report_db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM report_client_version").fetchone()[0]
    check(n == 1, f"(b) 빈 사용자/빈 버전은 기록하지 않음 ({n}행)")

    # 모집단은 사용량 기록 — 버전을 안 보내는 사람(kim)도 '미상'으로 남아야 한다.
    report_db.record_usage("honey_run", "hong.gildong")
    report_db.record_usage("honey_run", "kim")
    report_db.record_usage("honey_run", "ip:10.1.1.1")
    rep = report_db.version_report(days=30)
    users = {r["user_id"]: r for r in rep["rows"]}
    check(rep["total"] == 2 and "ip:10.1.1.1" not in users,
          f"(c) 모집단은 신원 있는 Honey 실행자 ({rep['total']}, {list(users)})")
    check(users.get("kim", {}).get("version") is None,
          f"(c) 버전 미보고 사용자도 행으로 남는다 ({users.get('kim')})")
    check(rep["known"] == 1 and rep["unknown"] == 1,
          f"(c) known/unknown 분리 ({rep['known']}/{rep['unknown']})")
    vers = [v["version"] for v in rep["versions"]]
    check(vers[-1] == "", f"(c) '미상'은 버전 목록 맨 뒤 ({vers})")


# ── (d)(e) 실시간 계측 ───────────────────────────────────────────────────────

def _metrics_app():
    app = Flask(__name__)

    @app.get("/pe/report/session/<session_id>/full", endpoint="report.session_full")
    def full(session_id):
        return "ok"

    @app.get("/pe/report/", endpoint="report.index")
    def index():
        return "ok"

    metrics.init_app(app)
    return app


def test_live_version_and_timeline():
    metrics._active_users.clear()
    cl = _metrics_app().test_client()

    cl.get("/pe/report/session/S1/full", headers={"User-Agent": UA_APP})
    cl.get("/pe/report/", headers={"User-Agent": UA_BROWSER})
    au = metrics.active_users()
    row = [u for u in au["users"] if u["key"] == "hong.gildong"][0]
    check(row["ver"] == "3.2.0", f"(d) 접속자 행에 클라 버전 ({row['ver']})")
    check(sorted(row["agents"]) == ["app", "browser"],
          f"(d) 앱·내장 브라우저 경로를 함께 표시 ({row['agents']})")

    # 버전 토큰이 없는 요청이 뒤에 와도 이미 알아낸 버전을 지우면 안 된다(화면 깜빡임).
    cl.get("/pe/report/", headers={"User-Agent": "Mozilla/5.0 HoneyUser/hong.gildong"})
    row = [u for u in metrics.active_users()["users"] if u["key"] == "hong.gildong"][0]
    check(row["ver"] == "3.2.0", f"(d) 빈 버전으로 덮어쓰지 않음 ({row['ver']})")

    tl = metrics.user_timeline("hong.gildong")
    check(tl["count"] == 3, f"(e) 최근 요청 3건 기록 ({tl['count']})")
    check(tl["items"][0]["route"] == "report.index"
          and tl["items"][-1]["route"] == "report.session_full",
          f"(e) 최신 순 정렬 ({[i['route'] for i in tl['items']]})")
    check(tl["items"][-1]["session_id"] == "S1",
          f"(e) 세션 요청은 세션 ID 를 남긴다 ({tl['items'][-1]})")
    check(all(i["status"] == 200 for i in tl["items"]),
          f"(e) 응답 상태코드 기록 ({[i['status'] for i in tl['items']]})")
    check(metrics.user_timeline("없는사람")["count"] == 0, "(e) 미접속 키는 빈 목록")
    metrics._active_users.clear()


# ── (f) 라우트 계약 ──────────────────────────────────────────────────────────

def test_routes():
    c = _client()

    res = c.get("/pe/admin-pte/api/client_versions?days=30")
    data = res.get_json() or {}
    check(res.status_code == 200 and data.get("total") == 2,
          f"(f) api/client_versions ({res.status_code} total={data.get('total')})")
    check("latest" in data, "(f) 최신 배포 버전 필드 동봉 (구버전 판정 기준)")

    # UA 에 버전이 없는 접속자는 대장(DB) 값으로 폴백하고 출처를 표시한다.
    metrics._active_users.clear()
    metrics._active_users["hong.gildong"] = {
        "uid": "hong.gildong", "ip": "10.0.0.9", "honey": True, "agent": "browser",
        "ver": "", "first": time.time(), "last": time.time(), "count": 1,
        "route": "report.session_full", "session_id": "S1", "recent": []}
    try:
        res = c.get("/pe/admin-pte/api/active_users?window=60")
        row = ((res.get_json() or {}).get("users") or [{}])[0]
        check(row.get("ver") == "3.2.0" and row.get("ver_src") == "db",
              f"(f) UA 에 버전이 없으면 대장으로 폴백 ({row.get('ver')}/{row.get('ver_src')})")

        res = c.get("/pe/admin-pte/api/user_timeline?key=hong.gildong")
        check(res.status_code == 200 and (res.get_json() or {}).get("key") == "hong.gildong",
              f"(f) api/user_timeline ({res.status_code} {res.get_json()})")
    finally:
        metrics._active_users.clear()


if __name__ == "__main__":
    test_ua_parsing()
    test_version_ledger()
    test_live_version_and_timeline()
    test_routes()
    print()
    print("ALL OK" if not _failures else f"FAILED {len(_failures)}: {_failures}")
    sys.exit(1 if _failures else 0)

"""관리자 패널 — 세션 영구 삭제(purge) + 실시간 접속 사용자 현황 검증 (2026-07-29).

배경: 휴지통 세션 purge 가 "30일 경과분만" 이라 방금 지운 세션은 영구 삭제가 안 됐고,
관리자 화면에는 "지금 누가 접속해 있나"를 볼 수단이 없었다.

  (a) purge 기본 동작 — 미경과 세션은 skipped(not expired), 경과분은 정리
  (b) purge force — 경과일과 무관하게 즉시 영구 삭제 (관리자 수동 전용)
  (c) 라우트 계약 — force 는 session_ids 지정 시에만 허용(all_expired+force = 400)
  (d) 관리자 삭제(api/sessions/delete)는 휴지통을 거치지 않는 즉시 영구 삭제다
  (e) 접속 사용자 계측 — HoneyUser UA 는 계정으로, 신원 없으면 ip:<addr> 로 묶임.
      admin/healthz/static 요청은 집계 제외, 윈도우 밖은 조회 시 prune
  (f) GET api/runtime 이 active_users 를 실제로 직렬화해 돌려준다

실행:
    python tests/test_admin_purge_and_users.py

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

_TMP = Path(tempfile.mkdtemp(prefix="admin_purge_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

from flask import Flask  # noqa: E402

import admin_panel  # noqa: E402
from admin_panel import metrics, sessions_admin  # noqa: E402
from admin_panel.routes import admin_panel_bp  # noqa: E402
from database import report_db  # noqa: E402

report_db.init_report_db()

_failures = []


def check(ok, label):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        _failures.append(label)


def _make_session(sid, akey=None):
    report_db.create_session(sid, f"{sid}.parquet", None, product_type="MDDI",
                             source="web_report", uploaded_by="tester")
    if akey:
        report_db.update_session(sid, analysis_key=akey)
    return report_db.get_session(sid)


def _exists(sid):
    return report_db.get_session(sid) is not None


# ── (a)(b) purge 경과일 규칙 / force ─────────────────────────────────────────

def test_purge_force():
    _make_session("S_fresh")
    report_db.trash_session("S_fresh", deleted_by="tester")

    r = sessions_admin.purge_trashed(session_ids=["S_fresh"], dry_run=False)
    check(r["purged"] == [] and r["skipped"][0]["reason"] == "not expired",
          f"(a) 미경과 세션은 기본 purge 대상 아님 ({r})")
    check(_exists("S_fresh"), "(a) 스킵된 세션은 DB 에 그대로 남는다")

    r = sessions_admin.purge_trashed(session_ids=["S_fresh"], dry_run=True, force=True)
    check(r["scanned"] == 1 and r["purged"] == [] and r["force"] is True,
          f"(b) force dry_run 은 대상만 집계 ({r})")
    check(_exists("S_fresh"), "(b) dry_run 은 실제로 지우지 않는다")

    r = sessions_admin.purge_trashed(session_ids=["S_fresh"], dry_run=False, force=True)
    check(r["purged"] == ["S_fresh"], f"(b) force 는 경과일 무관 즉시 purge ({r})")
    check(not _exists("S_fresh"), "(b) purge 후 세션 행이 사라진다")

    # 휴지통이 아닌(활성) 세션은 force 여도 purge 대상이 아니다 — 삭제 경로가 따로 있다
    _make_session("S_live")
    r = sessions_admin.purge_trashed(session_ids=["S_live"], dry_run=False, force=True)
    check(r["purged"] == [] and r["skipped"][0]["reason"] == "not trashed",
          f"(b) 활성 세션은 force 여도 purge 안 됨 ({r})")
    check(_exists("S_live"), "(b) 활성 세션 보존")

    # 경과분 일괄(all_expired)은 force 없이도 30일 경과분을 잡는다
    _make_session("S_old")
    report_db.trash_session("S_old", deleted_by="tester")
    with report_db.get_conn() as conn:
        conn.execute("UPDATE report_session SET deleted_at=? WHERE session_id=?",
                     (int(time.time()) - 40 * 86400, "S_old"))
    r = sessions_admin.purge_trashed(all_expired=True, dry_run=False)
    check(r["purged"] == ["S_old"], f"(a) 30일 경과분은 기존대로 purge ({r})")


# ── (c)(d) 라우트 계약 ───────────────────────────────────────────────────────

def _client():
    app = Flask(__name__)
    app.register_blueprint(admin_panel_bp, url_prefix="/pe/admin-pte")
    c = app.test_client()
    c.set_cookie("pe_admin_gate", admin_panel.gate_token())
    return c


_HDR = {"X-Admin-Request": "1"}   # 관리자 패널 변경 요청 가드 (_guard_mutations)


def test_routes():
    c = _client()

    _make_session("S_api")
    report_db.trash_session("S_api", deleted_by="tester")
    res = c.post("/pe/admin-pte/api/sessions/purge", headers=_HDR,
                 json={"session_ids": ["S_api"], "dry_run": False, "force": True})
    check(res.status_code == 200 and (res.get_json() or {}).get("purged") == ["S_api"],
          f"(c) 라우트 force purge ({res.status_code} {res.get_json()})")
    check(not _exists("S_api"), "(c) 라우트 purge 후 세션 제거")

    res = c.post("/pe/admin-pte/api/sessions/purge", headers=_HDR,
                 json={"all_expired": True, "dry_run": False, "force": True})
    check(res.status_code == 400, f"(c) all_expired + force 는 거부 ({res.status_code})")

    _make_session("S_del")
    res = c.post("/pe/admin-pte/api/sessions/delete", headers=_HDR,
                 json={"session_ids": ["S_del"]})
    check(res.status_code == 200 and (res.get_json() or {}).get("deleted") == ["S_del"],
          f"(d) 관리자 삭제 200 ({res.status_code} {res.get_json()})")
    check(not _exists("S_del"), "(d) 관리자 삭제는 휴지통이 아니라 즉시 영구 삭제")


# ── (e) 실시간 접속 사용자 계측 ──────────────────────────────────────────────

def test_active_users():
    metrics._active_users.clear()
    app = Flask(__name__)

    @app.get("/pe/report/session/<session_id>/full", endpoint="report.session_full")
    def full(session_id):
        return "ok"

    @app.get("/pe/report/", endpoint="report.index")
    def index():
        return "ok"

    @app.get("/healthz", endpoint="healthz")
    def healthz():
        return "ok"

    @app.get("/pe/admin-pte/api/runtime", endpoint="admin_panel.api_runtime")
    def admin_runtime():
        return "ok"

    metrics.init_app(app)
    cl = app.test_client()
    honey = {"User-Agent": "Mozilla/5.0 HoneyUser/HONG.GILDONG"}
    cl.get("/pe/report/session/SID1/full", headers=honey)
    cl.get("/pe/report/", headers=honey)
    cl.get("/pe/report/")                     # 신원 없는 일반 브라우저
    cl.get("/healthz")                        # watchdog 폴링 — 집계 제외
    cl.get("/pe/admin-pte/api/runtime")       # 관리자 자신 — 집계 제외

    au = metrics.active_users()
    keys = {u["key"] for u in au["users"]}
    check(au["count"] == 2, f"(e) 접속 사용자 2명 ({au})")
    check("hong.gildong" in keys, f"(e) HoneyUser UA → 계정 키 ({keys})")
    check(any(k.startswith("ip:") for k in keys), f"(e) 무신원은 ip:<addr> 로 묶임 ({keys})")
    check(au["named"] == 1 and au["honey"] == 1, f"(e) named/honey 카운트 ({au})")

    me = [u for u in au["users"] if u["key"] == "hong.gildong"][0]
    check(me["requests"] == 2, f"(e) 요청 수 누적 ({me})")
    check(me["session_id"] == "SID1", f"(e) 마지막으로 본 세션 기록 ({me})")

    metrics._active_users["stale"] = {"uid": "x", "ip": "1.1.1.1", "honey": False,
                                      "first": time.time() - 999, "last": time.time() - 999,
                                      "count": 1, "route": "r", "session_id": None}
    check(metrics.active_users(window_sec=60)["count"] == 2, "(e) 윈도우 밖 사용자 제외")
    check("stale" not in metrics._active_users, "(e) 조회 시 오래된 항목 prune")
    metrics._active_users.clear()


# ── (f) api/runtime 계약 ─────────────────────────────────────────────────────

def test_api_runtime_users():
    metrics._active_users.clear()
    metrics._active_users["kim"] = {"uid": "kim", "ip": "10.0.0.9", "honey": True,
                                    "first": time.time(), "last": time.time(),
                                    "count": 3, "route": "report.session_full",
                                    "session_id": "S1"}
    try:
        res = _client().get("/pe/admin-pte/api/runtime")
        data = res.get_json() or {}
        au = data.get("active_users") or {}
        check(res.status_code == 200 and au.get("count") == 1,
              f"(f) api/runtime active_users 반환 ({res.status_code} {au})")
        check((au.get("users") or [{}])[0].get("user") == "kim",
              f"(f) 사용자 행 직렬화 ({au.get('users')})")
    finally:
        metrics._active_users.clear()


if __name__ == "__main__":
    test_purge_force()
    test_routes()
    test_active_users()
    test_api_runtime_users()
    print()
    if _failures:
        print(f"FAILED {len(_failures)}건: " + " / ".join(_failures))
        sys.exit(1)
    print("ALL OK")

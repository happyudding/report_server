"""관리자 패널 — 세션 영구 삭제(purge) + 실시간 접속 사용자 현황 검증 (2026-07-29).

배경: 휴지통 세션 purge 가 "30일 경과분만" 이라 방금 지운 세션은 영구 삭제가 안 됐고,
관리자 화면에는 "지금 누가 접속해 있나"를 볼 수단이 없었다.

  (a) purge 기본 동작 — 미경과 세션은 skipped(not expired), 경과분은 정리
  (b) purge force — 경과일과 무관하게 즉시 영구 삭제 (관리자 수동 전용)
  (c) 라우트 계약 — force 는 session_ids 지정 시에만 허용(all_expired+force = 400)
  (d) 관리자 삭제(api/sessions/delete)는 휴지통을 거치지 않는 즉시 영구 삭제다
  (g) 휴지통 비우기(all_trashed) — 경과분 + 아직 복구 가능한 것까지 한꺼번에
  (e) 접속 사용자 계측 — HoneyUser UA 는 계정으로, 신원 없으면 ip:<addr> 로 묶임.
      admin/healthz/static 요청은 집계 제외, 윈도우 밖은 조회 시 prune
  (f) GET api/runtime 이 active_users 를 실제로 직렬화해 돌려준다
  (h) 하트비트 — 브라우저가 자동으로 보내는 폴링은 '접속 유지'로만 세고 마지막 행동·
      타임라인을 덮지 않는다. 화면 종류·마지막 입력 시각은 힌트로 받되 이상하면 버린다

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

    res = c.post("/pe/admin-pte/api/sessions/purge", headers=_HDR,
                 json={"all_expired": True, "all_trashed": True, "dry_run": True})
    check(res.status_code == 400, f"(c) all_expired + all_trashed 는 거부 ({res.status_code})")

    _make_session("S_del")
    res = c.post("/pe/admin-pte/api/sessions/delete", headers=_HDR,
                 json={"session_ids": ["S_del"]})
    check(res.status_code == 200 and (res.get_json() or {}).get("deleted") == ["S_del"],
          f"(d) 관리자 삭제 200 ({res.status_code} {res.get_json()})")
    check(not _exists("S_del"), "(d) 관리자 삭제는 휴지통이 아니라 즉시 영구 삭제")


# ── (g) 휴지통 비우기 (all_trashed) ──────────────────────────────────────────

def test_purge_all_trashed():
    """세션 탭 '🗑 휴지통 비우기' — 경과분 + 아직 복구 가능한 것까지 한꺼번에."""
    c = _client()
    _make_session("S_t_old")
    _make_session("S_t_new")
    _make_session("S_t_live")            # 휴지통 아님 — 대상에서 빠져야 한다
    report_db.trash_session("S_t_old", deleted_by="tester")
    report_db.trash_session("S_t_new", deleted_by="tester")
    with report_db.get_conn() as conn:
        conn.execute("UPDATE report_session SET deleted_at=? WHERE session_id=?",
                     (int(time.time()) - 40 * 86400, "S_t_old"))

    res = c.post("/pe/admin-pte/api/sessions/purge", headers=_HDR,
                 json={"all_trashed": True, "dry_run": True})
    dry = res.get_json() or {}
    check(dry.get("scanned") == 2 and dry.get("scanned_expired") == 1
          and dry.get("scanned_recent") == 1,
          f"(g) dry-run 이 경과/미경과를 쪼개서 보고 ({dry})")
    check(_exists("S_t_new"), "(g) dry-run 은 지우지 않는다")

    res = c.post("/pe/admin-pte/api/sessions/purge", headers=_HDR,
                 json={"all_trashed": True, "dry_run": False})
    r = res.get_json() or {}
    check(sorted(r.get("purged") or []) == ["S_t_new", "S_t_old"],
          f"(g) 경과분 + 미경과분 모두 영구 삭제 ({r})")
    check(not _exists("S_t_old") and not _exists("S_t_new"), "(g) 휴지통이 비워졌다")
    check(_exists("S_t_live"), "(g) 활성 세션은 건드리지 않는다")

    res = c.post("/pe/admin-pte/api/sessions/purge", headers=_HDR,
                 json={"all_trashed": True, "dry_run": True})
    check((res.get_json() or {}).get("scanned") == 0, "(g) 빈 휴지통은 대상 0건")
    report_db.delete_session("S_t_live")


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
    # 신원 없는 일반 브라우저 — **다른 PC** 에서. 같은 IP 면 "IP 가 같으면 같은 사용자"
    # 규칙으로 위 계정에 합쳐지는 게 정상이라(→ tests/test_identity_merge.py) 여기서
    # 익명 행을 확인하려면 IP 를 다르게 줘야 한다.
    cl.get("/pe/report/", environ_base={"REMOTE_ADDR": "10.9.9.9"})
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

    check(me["input_ago"] is not None and me["input_ago"] < 5,
          f"(e) 실사용 요청은 곧 행동 — input_ago 가 잡힌다 ({me})")

    metrics._active_users["stale"] = {"uid": "x", "ip": "1.1.1.1", "honey": False,
                                      "first": time.time() - 999, "last": time.time() - 999,
                                      "count": 1, "route": "r", "session_id": None}
    check(metrics.active_users(window_sec=60)["count"] == 2, "(e) 윈도우 밖 사용자 제외")
    check("stale" not in metrics._active_users, "(e) 조회 시 오래된 항목 prune")
    metrics._active_users.clear()


# ── (h) 하트비트 — 접속 유지와 실제 행동을 가른다 ────────────────────────────

def _hb_app():
    """my_messages(폴링) · session_full(실사용) 둘만 있는 최소 앱."""
    app = Flask(__name__)

    @app.get("/pe/report/api/my_messages", endpoint="report.my_messages")
    def msgs():
        return "ok"

    @app.get("/pe/report/session/<session_id>/full", endpoint="report.session_full")
    def full(session_id):
        return "ok"

    # init_app 은 프로세스당 1회만 먹는다(_started 가드 — 샘플러 스레드 중복 기동 방지).
    # 위 테스트가 이미 썼으므로 여기서는 훅만 직접 단다.
    app.before_request(metrics._on_request_start)
    app.after_request(metrics._on_response)
    app.teardown_request(metrics._on_request_teardown)
    return app


def test_heartbeat_passive_and_hints():
    metrics._active_users.clear()
    cl = _hb_app().test_client()
    honey = {"User-Agent": "Mozilla/5.0 HoneyUser/HONG.GILDONG"}

    cl.get("/pe/report/session/SID1/full", headers=honey)
    rec = metrics._active_users["hong.gildong"]
    act_ts, act_route = rec["last_input"], rec["route"]
    check(len(rec["recent"]) == 1, "(h) 실사용 요청은 타임라인에 남는다")

    # 폴링만 온다 — 생존(last)은 갱신되지만 '무엇을 하고 있나'는 그대로여야 한다.
    time.sleep(0.01)
    cl.get("/pe/report/api/my_messages?page=index&idle=300", headers=honey)
    check(rec["last"] > act_ts, "(h) 폴링도 접속 유지 신호로는 인정된다")
    check(rec["route"] == act_route, "(h) 폴링이 마지막 행동을 덮지 않는다")
    check(rec["last_input"] == act_ts, "(h) 폴링은 행동 시각을 밀어 올리지 않는다")
    check(len(rec["recent"]) == 1, "(h) 폴링은 타임라인 링버퍼를 채우지 않는다")
    check(rec["page"] == "index", "(h) 보고 있는 화면은 힌트로 받는다")

    # 겸용 라우트의 폴링 표식(boot.js AI 대기) — 엔드포인트가 같아도 hb=1 이면 폴링이다.
    cl.get("/pe/report/session/SID1/full?hb=1", headers=honey)
    check(rec["last_input"] == act_ts, "(h) hb=1 은 실사용으로 세지 않는다")

    # idle 힌트는 '마지막 입력 이후 경과초' — 화면의 초록/노랑이 이 값으로 갈린다.
    cl.get("/pe/report/api/my_messages?page=view&sid=SID9&idle=0", headers=honey)
    check(rec["last_input"] > act_ts, "(h) 입력이 있었으면 행동 시각이 올라간다")
    check(rec["session_id"] == "SID9", "(h) view 하트비트가 보는 세션을 알려준다")
    row = [u for u in metrics.active_users()["users"] if u["key"] == "hong.gildong"][0]
    check(row["input_ago"] is not None and row["input_ago"] < 5 and row["page"] == "view",
          f"(h) 조회 응답에 input_ago·page 가 실린다 ({row})")

    # 세션 상세를 떠나도 유예 안에는 유지 — 목록·상세 두 탭을 열어 둔 사람의 표시가
    # 30초마다 깜빡이지 않게 하기 위한 것.
    cl.get("/pe/report/api/my_messages?page=index&idle=0", headers=honey)
    check(rec["session_id"] == "SID9", "(h) 유예 안에는 보는 세션을 지우지 않는다")
    rec["sid_hint_ts"] = time.time() - metrics._SID_HINT_GRACE_SEC - 1
    cl.get("/pe/report/api/my_messages?page=index&idle=0", headers=honey)
    check(rec["session_id"] is None, "(h) 유예가 지나면 보는 세션을 지운다")

    # 값은 전부 클라가 보낸 것 — 이상하면 조용히 버리고 기존 값을 지킨다.
    rec["page"] = "index"
    cl.get("/pe/report/api/my_messages?page=../etc&sid=a/b&idle=-5", headers=honey)
    check(rec["page"] == "index" and rec["session_id"] is None,
          "(h) 알 수 없는 page·경로 문자 sid 는 버린다")
    metrics._active_users.clear()


def test_active_users_legacy_record():
    """옛 레코드(신규 키 없음) — 재시작 직후·다른 모듈이 만든 행에서도 터지지 않는다."""
    metrics._active_users.clear()
    metrics._active_users["old"] = {"uid": "old", "ip": "10.0.0.1", "honey": False,
                                    "first": time.time(), "last": time.time(),
                                    "count": 1, "route": "report.index", "session_id": None}
    try:
        row = metrics.active_users()["users"][0]
        check(row["input_ago"] is None and row["page"] == "",
              f"(h) 힌트가 없으면 None/빈값 — 화면이 종전 기준으로 폴백한다 ({row})")
    finally:
        metrics._active_users.clear()


# ── (f) api/runtime 계약 ─────────────────────────────────────────────────────

def test_api_runtime_users():
    metrics._active_users.clear()
    metrics._active_users["kim"] = {"uid": "kim", "ip": "10.0.0.9", "honey": True,
                                    "first": time.time(), "last": time.time(),
                                    "count": 3, "route": "report.session_full",
                                    "session_id": "S1"}
    try:
        c = _client()
        res = c.get("/pe/admin-pte/api/runtime")
        data = res.get_json() or {}
        au = data.get("active_users") or {}
        check(res.status_code == 200 and au.get("count") == 1,
              f"(f) api/runtime active_users 반환 ({res.status_code} {au})")
        check((au.get("users") or [{}])[0].get("user") == "kim",
              f"(f) 사용자 행 직렬화 ({au.get('users')})")

        # 사용자 탭 전용 경량 엔드포인트 (10초 폴링용)
        res = c.get("/pe/admin-pte/api/active_users?window=60")
        au2 = res.get_json() or {}
        check(res.status_code == 200 and au2.get("count") == 1
              and au2.get("window_sec") == 60,
              f"(f) api/active_users 단독 조회 ({res.status_code} {au2})")
        row = (au2.get("users") or [{}])[0]
        check(row.get("route") == "report.session_full" and row.get("session_id") == "S1",
              f"(f) 활동 경로·열람 세션 노출 (화면 활동 라벨의 입력) ({row})")
    finally:
        metrics._active_users.clear()


if __name__ == "__main__":
    test_purge_force()
    test_routes()
    test_purge_all_trashed()
    test_active_users()
    test_heartbeat_passive_and_hints()
    test_active_users_legacy_record()
    test_api_runtime_users()
    print()
    if _failures:
        print(f"FAILED {len(_failures)}건: " + " / ".join(_failures))
        sys.exit(1)
    print("ALL OK")

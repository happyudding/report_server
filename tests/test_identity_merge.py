"""관리자 화면 "IP 가 같으면 같은 사용자" 병합 검증 (2026-07-29).

신원 토큰이 없는 접속은 ip:<addr> 로 잡히는데, 같은 PC 에서 Honey 로도 접속하면 한 사람이
계정 행 + IP 행 둘로 갈라져 보였다. 관리자 화면 전체가 같은 규칙으로 합치는지 고정한다.

  (a) ip_to_user — 감사로그 (client_user, client_ip) 짝에서 IP→계정 매핑.
      한 IP 에 계정이 2개 이상이면 **활동이 가장 많은 계정(주 사용자)** 으로 합친다
      (2026-08-12 완화 — 그전에는 매핑하지 않아 한 사람의 행이 갈라졌다).
      admin-panel·system 은 사람이 아니라 제외
  (b) metrics.active_users — 익명 ip: 행이 계정 행에 합쳐지고 요청 수가 합산된다
  (c) stats.user_ranking — IP 이름 행이 계정 행에 합쳐지고, LIMIT 은 병합 후에 걸린다
  (d) stats.usage_ranking — ip:<addr> 행이 계정 행에 합쳐진다
  (e) 감사 로그 — 계정명 검색이 그 계정 IP 의 무신원 기록도 잡고, 행에 resolved_user 가 붙는다
  (f) 활동 동률이면 계정명 사전순으로 고정 — 새로고침마다 대표가 바뀌면 표가 흔들린다
  (g) 계정 기록이 아예 없는 IP 는 익명으로 남는다

실행:
    python tests/test_identity_merge.py

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

_TMP = Path(tempfile.mkdtemp(prefix="ident_merge_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

from flask import Flask  # noqa: E402

import admin_panel  # noqa: E402
from admin_panel import identity_merge, metrics, stats  # noqa: E402
from admin_panel.routes import admin_panel_bp  # noqa: E402
from database import report_db  # noqa: E402

report_db.init_report_db()

_failures = []


def check(ok, label):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        _failures.append(label)


def audit(action, user, ip, when=None):
    report_db.log_audit(action, session_id=None, client_ip=ip, client_user=user,
                        user_agent="test", result="ok")
    if when is not None:
        with report_db.get_conn() as conn:
            conn.execute("UPDATE report_audit_log SET created_at=? "
                         "WHERE id=(SELECT MAX(id) FROM report_audit_log)", (when,))


def seed():
    """hong = 10.0.0.5 전용 / 10.0.0.50 은 park 주 사용자 / 10.0.0.99 는 kim·lee 동률."""
    audit("upload", "hong", "10.0.0.5")
    audit("edit", "hong", "10.0.0.5")
    audit("edit", "", "10.0.0.5")          # 같은 PC 의 무신원 기록 → hong 으로 귀속돼야
    for _ in range(4):                     # park 4건 vs choi 1건 → 주 사용자 park
        audit("upload", "park", "10.0.0.50")
    audit("upload", "choi", "10.0.0.50")
    audit("upload", "kim", "10.0.0.99")
    audit("upload", "lee", "10.0.0.99")    # 활동 동률 → 사전순 kim
    audit("delete", "admin-panel", "10.0.0.7")   # 사람 아님 → 매핑 제외
    identity_merge.invalidate()


# ── (a)(f) IP→계정 매핑 ──────────────────────────────────────────────────────

def test_mapping():
    m = identity_merge.ip_to_user(force=True)
    check(m.get("10.0.0.5") == "hong", f"(a) 단독 계정 IP 는 매핑 ({m})")
    check(m.get("10.0.0.50") == "park", f"(a) 계정 2개면 활동 최다(park 4 vs choi 1) ({m})")
    check(m.get("10.0.0.99") == "kim", f"(f) 동률이면 계정명 사전순 ({m})")
    check("10.0.0.7" not in m, f"(a) admin-panel 은 사람이 아니라 제외 ({m})")
    check("10.0.0.123" not in m, f"(g) 계정 기록 없는 IP 는 매핑 없음 ({m})")

    check(identity_merge.ip_of_name("ip:1.2.3.4") == "1.2.3.4", "(a) ip: 접두 표기 파싱")
    check(identity_merge.ip_of_name("1.2.3.4") == "1.2.3.4", "(a) 순수 IP 표기 파싱")
    check(identity_merge.ip_of_name("hong") is None, "(a) 계정명은 IP 아님")
    check(identity_merge.ip_of_name("PC-DESK01") is None, "(a) 호스트명은 IP 아님")

    check(identity_merge.resolve("ip:10.0.0.5") == ("hong", True), "(a) resolve 병합")
    check(identity_merge.resolve("ip:10.0.0.50") == ("park", True),
          "(a) resolve 는 주 사용자로 합친다")
    check(identity_merge.resolve("ip:10.0.0.123") == ("ip:10.0.0.123", False),
          "(g) 근거 없는 IP 는 그대로")
    check(identity_merge.resolve("hong") == ("hong", False), "(a) 계정은 그대로")
    check(identity_merge.resolve("choi") == ("choi", False),
          "(a) 계정 이름 행은 IP 가 같아도 흡수하지 않는다")


# ── (b) 실시간 접속 사용자 ───────────────────────────────────────────────────

def test_live_merge():
    now = time.time()
    metrics._active_users.clear()
    metrics._active_users["hong"] = {"uid": "hong", "ip": "10.0.0.5", "honey": True,
                                     "first": now - 600, "last": now - 30, "count": 5,
                                     "route": "report.session_full", "session_id": "S_OLD"}
    metrics._active_users["ip:10.0.0.5"] = {"uid": "", "ip": "10.0.0.5", "honey": False,
                                            "first": now - 100, "last": now - 2, "count": 3,
                                            "route": "report.index", "session_id": "S_NEW"}
    metrics._active_users["ip:10.0.0.123"] = {"uid": "", "ip": "10.0.0.123", "honey": False,
                                              "first": now - 50, "last": now - 5, "count": 2,
                                              "route": "report.index", "session_id": None}
    identity_merge.invalidate()
    au = metrics.active_users(window_sec=3600)
    by = {u["key"]: u for u in au["users"]}
    check(au["count"] == 2, f"(b) 3행 → 2명으로 병합 ({[u['key'] for u in au['users']]})")
    h = by.get("hong") or {}
    check(h.get("requests") == 8, f"(b) 요청 수 합산 5+3 ({h.get('requests')})")
    check(h.get("merged") is True and h.get("ips") == ["10.0.0.5"],
          f"(b) 병합 표시 + IP 목록 ({h})")
    check(h.get("session_id") == "S_NEW" and h.get("route") == "report.index",
          f"(b) 마지막 활동은 더 최근 행 기준 ({h})")
    check(round(h.get("since", 0)) >= 600, f"(b) 접속 시작은 더 이른 쪽 ({h.get('since')})")
    check(h.get("honey") is True, "(b) 한쪽이 Honey 면 Honey 로 표시")
    check("ip:10.0.0.123" in by and by["ip:10.0.0.123"]["user"] == "",
          f"(g) 근거 없는 IP 는 익명 유지 ({sorted(by)})")
    metrics._active_users.clear()


# ── (c)(d) 누적 사용량 ───────────────────────────────────────────────────────

def test_rank_merge():
    identity_merge.invalidate()
    r = stats.user_ranking(days=30)
    rows = {x["who"]: x for x in r["rows"]}
    check("10.0.0.5" not in rows, f"(c) IP 이름 행이 남지 않는다 ({sorted(rows)})")
    check(rows["hong"]["total"] == 3, f"(c) hong 합계 = 2 + 무신원 1 ({rows.get('hong')})")
    check(rows["hong"]["merged_from"] == ["10.0.0.5"],
          f"(c) 병합 출처 기록 ({rows['hong'].get('merged_from')})")

    report_db.record_usage("web_index", "hong")
    report_db.record_usage("web_view", "ip:10.0.0.5")
    report_db.record_usage("honey_run", "ip:10.0.0.50")
    report_db.record_usage("honey_run", "ip:10.0.0.123")
    g = stats.usage_ranking(days=30)
    urows = {x["user_id"]: x for x in g["rows"]}
    check("ip:10.0.0.5" not in urows, f"(d) ip: 행이 계정으로 흡수 ({sorted(urows)})")
    check(urows["hong"]["total"] == 2 and urows["hong"]["web_view"] == 1,
          f"(d) 계정 행에 합산 ({urows.get('hong')})")
    check(urows.get("park", {}).get("honey_run") == 1,
          f"(d) 공용 IP 도 주 사용자 행으로 흡수 ({sorted(urows)})")
    check("ip:10.0.0.123" in urows, f"(g) 근거 없는 IP 는 그대로 ({sorted(urows)})")

    # LIMIT 은 병합 후 적용 — 자르고 합치면 조각이 사라진다
    g1 = stats.usage_ranking(days=30, limit=1)
    check(len(g1["rows"]) == 1 and g1["rows"][0]["user_id"] == "hong",
          f"(d) limit 은 병합 후 상위 ({g1['rows']})")


# ── (e) 감사 기록 ────────────────────────────────────────────────────────────

def test_audit_view():
    app = Flask(__name__)
    app.register_blueprint(admin_panel_bp, url_prefix="/pe/admin-pte")
    c = app.test_client()
    c.set_cookie("pe_admin_gate", admin_panel.gate_token())
    identity_merge.invalidate()

    rows = c.get("/pe/admin-pte/api/audit?q=hong&limit=100").get_json() or []
    check(len(rows) == 3, f"(e) 계정 검색이 같은 IP 무신원 기록까지 포함 ({len(rows)})")
    anon = [r for r in rows if not (r.get("client_user") or "").strip()]
    check(len(anon) == 1 and anon[0].get("resolved_user") == "hong",
          f"(e) 무신원 행에 resolved_user 부착 ({anon})")

    rows = c.get("/pe/admin-pte/api/audit?q=kim&limit=100").get_json() or []
    check(all((r.get("client_user") or "") == "kim" for r in rows),
          f"(e) 모호한 IP 계정은 확장 없음 ({[r.get('client_user') for r in rows]})")


if __name__ == "__main__":
    seed()
    test_mapping()
    test_live_merge()
    test_rank_merge()
    test_audit_view()
    print()
    if _failures:
        print(f"FAILED {len(_failures)}건: " + " / ".join(_failures))
        sys.exit(1)
    print("ALL OK")

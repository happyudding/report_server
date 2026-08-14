"""중복 신원 통합 + 실명 한글 규칙 검증 (2026-08-14).

실행:
    python tests/test_identity_dedup.py

배경: 한 사람이 'SECDS\\chumji.kim' · 'chumji.kim' · 'SECDS\\Chumji.Kim' 처럼 표기별로
갈라져 관리자 사용자 현황에 여러 명으로 보이던 문제. 신원 진입점과 관리자 집계를
identity_norm.normalize_uid 하나로 통일하고, 이미 쌓인 행은 병합 도구로 합친다.

시나리오:
  (a) normalize_uid 규칙 (도메인 제거 · 소문자 · ip:/예약계정 통과)
  (b) 신원 provider — UA 에 도메인이 실려 와도 정규화된 uid 로 식별된다
  (c) 관리자 집계 — 감사로그/사용량의 갈라진 표기가 한 행으로 합쳐진다
  (d) 병합 도구 — 카운터 합산 · 즐겨찾기 중복 제거 · 최신 실명 채택 · 계정 정리
  (e) 실명은 한글 2~10자만 저장된다 (영문·자모·공백·1자·11자는 400)

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

_TMP = Path(tempfile.mkdtemp(prefix="identity_dedup_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

from flask import Flask  # noqa: E402

from admin_panel import identity_merge, stats  # noqa: E402
from database import report_db  # noqa: E402
from database.core import get_conn  # noqa: E402
from identity_norm import normalize_uid  # noqa: E402
from report.report_extension import report_bp  # noqa: E402  (라우트 등록 트리거)

app = Flask(__name__)
app.secret_key = "test-secret"
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()

NOW = int(time.time())
TODAY = time.strftime("%Y-%m-%d", time.localtime(NOW))
CSRF = "test-csrf-token"
client.set_cookie("report_csrf", CSRF)
HDRS = {"X-CSRF-Token": CSRF}


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok  {name}")


def post(path, body, ua=None):
    headers = dict(HDRS)
    if ua:
        headers["User-Agent"] = ua
    return client.post(path, json=body, headers=headers)


# ── (a) 정규화 규칙 ──────────────────────────────────────────────────────────

def test_normalize():
    print("[a] normalize_uid 규칙")
    check("도메인 제거", normalize_uid("SECDS\\chumji.kim") == "chumji.kim")
    check("대소문자 무시", normalize_uid("Chumji.Kim") == "chumji.kim")
    check("도메인+대문자", normalize_uid("SECDS\\Chumji.Kim") == "chumji.kim")
    check("공백 제거", normalize_uid("  chumji.kim  ") == "chumji.kim")
    check("이미 정규형은 그대로", normalize_uid("chumji.kim") == "chumji.kim")
    check("무신원 ip 표기 통과", normalize_uid("ip:10.1.2.3") == "ip:10.1.2.3")
    check("예약 계정 통과", normalize_uid("admin-panel") == "admin-panel")
    check("빈 값", normalize_uid(None) == "" and normalize_uid("") == "")


# ── (b) 신원 provider ────────────────────────────────────────────────────────

def test_identity_provider():
    print("[b] 신원 provider — UA 에 도메인이 실려도 한 사람")
    for ua_token, label in (("chumji.kim", "도메인 없음"),
                            ("SECDS%5Cchumji.kim", "도메인 포함(percent-encoded)"),
                            ("SECDS%5CChumji.Kim", "도메인+대문자")):
        j = client.get("/pe/report/api/auth/me",
                       headers={"User-Agent": f"Mozilla/5.0 HoneyUser/{ua_token}"}).get_json()
        check(f"{label} → chumji.kim", j["user_id"] == "chumji.kim")


# ── (c) 관리자 집계 ──────────────────────────────────────────────────────────

def seed_split_identity():
    """같은 사람이 세 가지 표기로 남긴 기록 (통합 전 운영 DB 의 모습)."""
    with get_conn() as conn:
        for who, n in (("SECDS\\chumji.kim", 3), ("chumji.kim", 2), ("SECDS\\Chumji.Kim", 1)):
            for i in range(n):
                conn.execute(
                    "INSERT INTO report_audit_log (action, created_at, client_user, client_ip,"
                    " result) VALUES ('upload', ?, ?, '10.0.0.9', 'ok')",
                    (NOW - 60 - i, who))
        for uid, cnt in (("SECDS\\chumji.kim", 5), ("chumji.kim", 4), ("Chumji.Kim", 1)):
            conn.execute(
                "INSERT INTO report_usage_daily (day, kind, user_id, count, last_at)"
                " VALUES (?, 'web_index', ?, ?, ?)", (TODAY, uid, cnt, NOW))
            conn.execute(
                "INSERT INTO report_usage_hourly (day, hour, kind, user_id, count, last_at)"
                " VALUES (?, 9, 'web_index', ?, ?, ?)", (TODAY, uid, cnt, NOW))
    identity_merge.invalidate()


def test_admin_aggregation():
    print("[c] 관리자 집계 — 갈라진 표기가 한 행으로")
    rows = [r for r in stats.user_ranking(days=30)["rows"] if "chumji" in r["who"].lower()]
    check("활동 순위 1행", len(rows) == 1)
    check("표기는 정규화된 ID", rows[0]["who"] == "chumji.kim")
    check("건수 합산(3+2+1)", rows[0]["upload"] == 6)

    rows = [r for r in stats.usage_ranking(days=30)["rows"]
            if "chumji" in r["user_id"].lower()]
    check("사용량 순위 1행", len(rows) == 1)
    check("접속 합산(5+4+1)", rows[0]["total"] == 10)

    hm = stats.usage_hourly_heatmap(days=30)
    wd = time.localtime(NOW).tm_wday
    check("히트맵 고유 사용자 1명", hm["users"][wd][9] == 1)


# ── (d) 병합 도구 ────────────────────────────────────────────────────────────

def seed_for_merge():
    """DB 병합 대상 — 즐겨찾기·실명·계정까지 갈라진 상태."""
    with get_conn() as conn:
        conn.execute("INSERT INTO report_user_favorite (user_id, session_id, created_at)"
                     " VALUES ('SECDS\\chumji.kim', 's01', ?)", (NOW - 500,))
        conn.execute("INSERT INTO report_user_favorite (user_id, session_id, created_at)"
                     " VALUES ('chumji.kim', 's01', ?)", (NOW - 100,))   # 같은 세션 = 중복
        conn.execute("INSERT INTO report_user_favorite (user_id, session_id, created_at)"
                     " VALUES ('SECDS\\chumji.kim', 's02', ?)", (NOW - 400,))
        conn.execute("INSERT INTO report_web_visitor (user_id, first_seen, last_seen)"
                     " VALUES ('SECDS\\chumji.kim', ?, ?)", (NOW - 900, NOW - 800))
        conn.execute("INSERT INTO report_web_visitor (user_id, first_seen, last_seen)"
                     " VALUES ('chumji.kim', ?, ?)", (NOW - 700, NOW - 10))
        # 실명: 도메인 표기 쪽이 더 최신 → 그 이름이 살아남아야 한다
        conn.execute("INSERT INTO report_user_profile (user_id, display_name, updated_at,"
                     " updated_by) VALUES ('chumji.kim', '김철수', ?, 'self')", (NOW - 900,))
        conn.execute("INSERT INTO report_user_profile (user_id, display_name, updated_at,"
                     " updated_by) VALUES ('SECDS\\chumji.kim', '김첨지', ?, 'self')", (NOW - 10,))
        conn.execute("INSERT INTO report_session_editor (session_id, editor_user, granted_by,"
                     " granted_at) VALUES ('s03', 'SECDS\\chumji.kim', 'SECDS\\boss', ?)",
                     (NOW - 300,))
        conn.execute("INSERT INTO report_user (user_id, password_hash, created_at)"
                     " VALUES ('chumji.kim', 'hash-original', ?)", (NOW - 9000,))
        conn.execute("INSERT INTO report_user (user_id, password_hash, created_at)"
                     " VALUES ('SECDS\\chumji.kim', 'hash-dup', ?)", (NOW - 100,))


def test_merge_tool():
    print("[d] 병합 도구 (tools/merge_duplicate_users.py --apply)")
    seed_for_merge()
    sys.path.insert(0, os.path.join(_ROOT, "server", "tools"))
    import merge_duplicate_users
    check("미리보기는 DB 를 바꾸지 않는다", merge_duplicate_users.main(False) == 0)
    with get_conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM report_user_favorite"
                              " WHERE user_id='SECDS\\chumji.kim'").fetchone()[0]
    check("미리보기 후 원행 유지", before == 2)

    check("병합 실행", merge_duplicate_users.main(True) == 0)
    with get_conn() as conn:
        def one(sql, *p):
            return conn.execute(sql, p).fetchone()

        check("접속 카운터 합산(5+4+1)",
              one("SELECT SUM(count) FROM report_usage_daily WHERE user_id='chumji.kim'")[0] == 10)
        check("접속 카운터 잔여 표기 없음",
              one("SELECT COUNT(*) FROM report_usage_daily WHERE user_id<>'chumji.kim'")[0] == 0)
        check("시간별 카운터도 합산",
              one("SELECT SUM(count) FROM report_usage_hourly WHERE user_id='chumji.kim'")[0] == 10)
        check("즐겨찾기 중복 제거(s01·s02 2건)",
              one("SELECT COUNT(*) FROM report_user_favorite WHERE user_id='chumji.kim'")[0] == 2)
        check("즐겨찾기 created_at 은 더 이른 값",
              one("SELECT created_at FROM report_user_favorite"
                  " WHERE user_id='chumji.kim' AND session_id='s01'")[0] == NOW - 500)
        check("방문자 first_seen 최소·last_seen 최대",
              tuple(one("SELECT first_seen, last_seen FROM report_web_visitor"
                        " WHERE user_id='chumji.kim'")) == (NOW - 900, NOW - 10))
        check("실명은 더 최신 것이 남는다", report_db.get_display_name("chumji.kim") == "김첨지")
        check("실명 행은 1개",
              one("SELECT COUNT(*) FROM report_user_profile")[0] == 1)
        check("편집 위임 editor_user 정규화",
              one("SELECT COUNT(*) FROM report_session_editor"
                  " WHERE editor_user='chumji.kim'")[0] == 1)
        check("편집 위임 granted_by 정규화",
              one("SELECT granted_by FROM report_session_editor"
                  " WHERE session_id='s03'")[0] == "boss")
        check("계정은 먼저 만든 것만 남는다",
              tuple(one("SELECT user_id, password_hash FROM report_user"))
              == ("chumji.kim", "hash-original"))

    check("재실행은 0건 (멱등)", merge_duplicate_users.main(True) == 0)
    with get_conn() as conn:
        check("재실행 후에도 합산값 유지",
              conn.execute("SELECT SUM(count) FROM report_usage_daily"
                           " WHERE user_id='chumji.kim'").fetchone()[0] == 10)


# ── (e) 실명 한글 규칙 ───────────────────────────────────────────────────────

def test_korean_name_rule():
    print("[e] 실명은 한글 2~10자만")
    ua = "Mozilla/5.0 HoneyUser/nametest"
    for bad, label in (("Hong", "영문"), ("홍gil동", "한영 혼용"), ("ㄱㄴ", "자모"),
                       ("김", "1자"), ("가" * 11, "11자"), ("홍 길동", "공백 포함"),
                       ("홍길동!", "특수문자"), ("김철수2", "숫자 혼용")):
        r = post("/pe/report/api/auth/display_name", {"name": bad}, ua=ua)
        check(f"{label} 거부(400)", r.status_code == 400)
    check("거부된 이름은 저장 안 됨", report_db.get_display_name("nametest") is None)

    for good in ("김첨지", "남궁민수", "가" * 10):
        r = post("/pe/report/api/auth/display_name", {"name": good}, ua=ua)
        check(f"'{good}' 저장(200)", r.status_code == 200)
    check("마지막 값 반영", report_db.get_display_name("nametest") == "가" * 10)

    r = post("/pe/report/api/auth/signup",
             {"user_id": "signupkr", "password": "1234", "name": "Hong"})
    check("회원가입도 한글 강제(400)", r.status_code == 400)
    check("가입 실패 시 계정 미생성", report_db.get_user("signupkr") is None)


if __name__ == "__main__":
    test_normalize()
    test_identity_provider()
    seed_split_identity()
    test_admin_aggregation()
    test_merge_tool()
    test_korean_name_rule()
    print("\nALL OK")

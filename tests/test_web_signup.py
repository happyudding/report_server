"""웹 회원가입(/api/auth/signup, /api/auth/signup_hint) 검증.

실행:
    python tests/test_web_signup.py

시나리오:
  (a) 미사용 singleID 자유 가입 → 200 + 즉시 로그인 세션(/api/auth/me), 발급된
      비밀번호로 /api/auth/login 성공 / 틀린 비밀번호 401
  (b) 같은 ID 재가입 → 409
  (c) Honey 사용 이력 차단 — uploaded_by 로 업로드한 계정 / report_web_visitor 방문 계정 → 403
      (uploaded_by 는 'SECDS\\Name' 꼬리·대소문자 무시 비교)
  (d) 입력 검증 — 비밀번호 3자리 400 / singleID 공백 400 / CSRF 헤더 없음 403
  (e) signup_hint — 같은 IP 의 upload 감사기록이 있으면 그 계정, 다른 IP 는 빈 응답,
      오래된(180일 초과) 기록은 제외
  (f) IP 당 가입 횟수 제한 → 6번째 429

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

_TMP = Path(tempfile.mkdtemp(prefix="web_signup_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

from flask import Flask  # noqa: E402

from report.report_extension import report_bp  # noqa: E402  (전체 라우트 등록 트리거)
from database import report_db  # noqa: E402
from database.core import get_conn  # noqa: E402

app = Flask(__name__)
app.secret_key = "test-secret"          # 로그인 세션 쿠키 서명용 (운영은 wsgi.py 가 설정)
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()

NOW = int(time.time())

# double-submit CSRF — 쿠키와 헤더에 같은 값을 넣는다 (security._require_csrf).
CSRF = "test-csrf-token"
client.set_cookie("report_csrf", CSRF)
HDRS = {"X-CSRF-Token": CSRF}


def post(path, body, ip=None):
    headers = dict(HDRS)
    if ip:
        headers["X-Forwarded-For"] = ip
    return client.post(path, json=body, headers=headers)


def seed():
    with get_conn() as conn:
        # Honey 로 업로드한 적 있는 계정 (uploaded_by 꼬리 비교 + 대소문자 무시 확인)
        conn.execute(
            "INSERT INTO report_session (session_id, analysis_key, file_name, product_type,"
            " lot_id, created_at, status, source, uploaded_by)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("s01", "ak1", "a.xlsx", "MDDI", "LOT1", NOW - 100, "done", "web_report",
             "SECDS\\HoneyGuy"))
    # web_report 를 연 적 있는 계정 (편집자 후보 풀)
    report_db.record_web_visitor("visitorguy")
    # signup_hint 용 감사기록 — 같은 IP 의 최근 업로드 / 180일 지난 업로드
    report_db.log_audit("upload", client_ip="9.9.9.9", client_user="SECDS\\Uploader1")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_audit_log (action, client_ip, client_user, result, created_at)"
            " VALUES (?,?,?,?,?)",
            ("upload", "7.7.7.7", "olduser", "ok", NOW - 200 * 86400))


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok  {name}")


def test_signup_basic():
    print("[a] 미사용 ID 자유 가입")
    r = post("/pe/report/api/auth/signup",
             {"user_id": "newbie", "password": "1234", "name": "홍길동"})
    check("가입 200", r.status_code == 200)
    check("응답 user_id/source/이름", r.get_json() == {"ok": True, "user_id": "newbie",
                                                     "source": "login",
                                                     "display_name": "홍길동"})
    check("계정 생성됨", bool(report_db.get_user("newbie")))
    check("실명 저장됨", report_db.get_display_name("newbie") == "홍길동")

    me = client.get("/pe/report/api/auth/me").get_json()
    check("가입 즉시 로그인 세션", me["user_id"] == "newbie" and me["source"] == "login")
    check("me 에 실명 동봉", me["display_name"] == "홍길동")

    r = post("/pe/report/api/auth/login", {"user_id": "newbie", "password": "1234"})
    check("가입한 비밀번호로 로그인 200", r.status_code == 200)
    r = post("/pe/report/api/auth/login", {"user_id": "newbie", "password": "9999"})
    check("틀린 비밀번호 401", r.status_code == 401)

    print("[b] 중복 가입")
    r = post("/pe/report/api/auth/signup", {"user_id": "NEWBIE", "password": "5678"})
    check("재가입 409 (대소문자 정규화)", r.status_code == 409)
    check("비밀번호 안 바뀜",
          post("/pe/report/api/auth/login",
               {"user_id": "newbie", "password": "1234"}).status_code == 200)


def test_honey_history_blocked():
    print("[c] Honey 사용 이력 차단")
    r = post("/pe/report/api/auth/signup", {"user_id": "honeyguy", "password": "1234"})
    check("업로드 이력 계정 403", r.status_code == 403)
    check("가입 안 됨", report_db.get_user("honeyguy") is None)

    r = post("/pe/report/api/auth/signup", {"user_id": "visitorguy", "password": "1234"})
    check("방문 이력 계정 403", r.status_code == 403)

    check("has_honey_history 직접 호출 — 이력 있음",
          report_db.has_honey_history("honeyguy") and report_db.has_honey_history("visitorguy"))
    check("has_honey_history — 이력 없음", not report_db.has_honey_history("nobody"))


def test_validation():
    print("[d] 입력 검증")
    check("비밀번호 3자리 400",
          post("/pe/report/api/auth/signup",
               {"user_id": "who1", "password": "123"}).status_code == 400)
    check("비밀번호 문자 400",
          post("/pe/report/api/auth/signup",
               {"user_id": "who1", "password": "abcd"}).status_code == 400)
    check("singleID 공백 400",
          post("/pe/report/api/auth/signup",
               {"user_id": "a b", "password": "1234"}).status_code == 400)
    check("이름 31자 400",
          post("/pe/report/api/auth/signup",
               {"user_id": "who1", "password": "1234", "name": "가" * 31}).status_code == 400)
    r = client.post("/pe/report/api/auth/signup",
                    json={"user_id": "who2", "password": "1234"})
    check("CSRF 헤더 없음 403", r.status_code == 403)
    check("검증 실패 건은 계정 미생성",
          report_db.get_user("who1") is None and report_db.get_user("who2") is None)
    # 이름 없는 가입은 **막지 않는다** — 브라우저에 캐시된 옛 JS 가 name 없이 보내도
    # 가입 자체는 되어야 한다(이름은 첫 화면 입력창이 뒤늦게 채운다).
    r = post("/pe/report/api/auth/signup", {"user_id": "noname", "password": "1234"})
    check("이름 없이도 가입 200 (하위호환)", r.status_code == 200)
    check("이름은 미등록", report_db.get_display_name("noname") is None)


def test_signup_hint():
    print("[e] signup_hint (자기 IP 만)")
    j = client.get("/pe/report/api/auth/signup_hint",
                   headers={"X-Forwarded-For": "9.9.9.9"}).get_json()
    check("같은 IP 업로드 계정 힌트", j == {"user_id": "uploader1", "honey_seen": True})
    j = client.get("/pe/report/api/auth/signup_hint",
                   headers={"X-Forwarded-For": "8.8.8.8"}).get_json()
    check("다른 IP 는 빈 응답", j == {})
    j = client.get("/pe/report/api/auth/signup_hint",
                   headers={"X-Forwarded-For": "7.7.7.7"}).get_json()
    check("180일 지난 기록 제외", j == {})


def test_rate_limit():
    print("[f] IP 당 가입 제한")
    ip = "5.5.5.5"
    for i in range(5):
        r = post("/pe/report/api/auth/signup",
                 {"user_id": f"rate{i}", "password": "1234"}, ip=ip)
        check(f"{i + 1}번째 가입 200", r.status_code == 200)
    r = post("/pe/report/api/auth/signup", {"user_id": "rate5", "password": "1234"}, ip=ip)
    check("6번째 429", r.status_code == 429)
    check("차단된 건은 계정 미생성", report_db.get_user("rate5") is None)
    r = post("/pe/report/api/auth/signup", {"user_id": "rate5", "password": "1234"},
             ip="6.6.6.6")
    check("다른 IP 는 영향 없음", r.status_code == 200)


if __name__ == "__main__":
    seed()
    test_signup_basic()
    test_honey_history_blocked()
    test_validation()
    test_signup_hint()
    test_rate_limit()
    print("\nALL OK")

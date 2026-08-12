"""사용자 실명(report_user_profile) 검증 — 저장 라우트 · 신원 응답 동봉 · 목록 표기.

실행:
    python tests/test_display_name.py

시나리오:
  (a) 저장 라우트 — 익명 403 / 형식 400(빈값·31자·제어문자) / CSRF 없음 403
  (b) Honey 신원(UA)으로 저장 → report_user 계정이 없어도 저장된다
      (프로필은 로그인 계정과 별개 테이블이라 Honey 전용 사용자도 이름을 가진다)
  (c) 저장한 이름이 /api/auth/me · /api/history viewer 에 실려 온다
  (d) /api/history 의 names 맵 — 업로더 uid('SECDS\\Name' 꼬리) 기준으로 채워진다
  (e) 편집자 후보 검색 — 이름으로 검색되고 후보에 name 이 실린다
  (f) 이름 변경 (같은 uid 재저장 = UPSERT)

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

_TMP = Path(tempfile.mkdtemp(prefix="display_name_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

from flask import Flask  # noqa: E402

from report.report_extension import report_bp  # noqa: E402  (전체 라우트 등록 트리거)
from database import report_db  # noqa: E402
from database.core import get_conn  # noqa: E402

app = Flask(__name__)
app.secret_key = "test-secret"
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()

NOW = int(time.time())
CSRF = "test-csrf-token"
client.set_cookie("report_csrf", CSRF)
HDRS = {"X-CSRF-Token": CSRF}
# Honey 내장 브라우저 신원 — UA 의 HoneyUser/<계정> 토큰 (auth_identity)
HONEY_UA = "Mozilla/5.0 HoneyUser/pteuser1"


def post(path, body, ua=None, csrf=True):
    headers = dict(HDRS) if csrf else {}
    if ua:
        headers["User-Agent"] = ua
    return client.post(path, json=body, headers=headers)


def seed():
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_session (session_id, analysis_key, file_name, product_type,"
            " lot_id, created_at, status, source, uploaded_by)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("s01", "ak1", "a.xlsx", "MDDI", "LOT1", NOW - 100, "done", "web_report",
             "SECDS\\PteUser1"))
    report_db.record_web_visitor("teammate9")


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok  {name}")


def test_guards():
    print("[a] 저장 라우트 가드")
    # 신원 없는 일반 브라우저 — 저장할 대상이 없다
    check("익명 403", post("/pe/report/api/auth/display_name", {"name": "누구"}).status_code == 403)
    check("빈 이름 400",
          post("/pe/report/api/auth/display_name", {"name": "   "}, ua=HONEY_UA).status_code == 400)
    check("31자 400",
          post("/pe/report/api/auth/display_name", {"name": "가" * 31},
               ua=HONEY_UA).status_code == 400)
    check("제어문자 400",
          post("/pe/report/api/auth/display_name", {"name": "홍\n길동"},
               ua=HONEY_UA).status_code == 400)
    r = client.post("/pe/report/api/auth/display_name", json={"name": "홍길동"},
                    headers={"User-Agent": HONEY_UA})
    check("CSRF 헤더 없음 403", r.status_code == 403)
    check("실패 건은 미저장", report_db.get_display_name("pteuser1") is None)


def test_save_without_account():
    print("[b] Honey 신원 저장 - 로그인 계정이 없어도 된다")
    check("로그인 계정 없음(전제)", report_db.get_user("pteuser1") is None)
    r = post("/pe/report/api/auth/display_name", {"name": "홍길동"}, ua=HONEY_UA)
    check("저장 200", r.status_code == 200)
    check("응답 본문", r.get_json() == {"ok": True, "user_id": "pteuser1",
                                      "display_name": "홍길동"})
    check("DB 반영", report_db.get_display_name("pteuser1") == "홍길동")
    check("계정은 여전히 없음(프로필과 별개 테이블)", report_db.get_user("pteuser1") is None)


def test_viewer_payloads():
    print("[c] 신원 응답에 실명 동봉")
    me = client.get("/pe/report/api/auth/me", headers={"User-Agent": HONEY_UA}).get_json()
    check("auth/me display_name", me["display_name"] == "홍길동")
    j = client.get("/pe/report/api/history?limit=50&offset=0",
                   headers={"User-Agent": HONEY_UA}).get_json()
    check("history viewer display_name", j["viewer"]["display_name"] == "홍길동")
    anon = client.get("/pe/report/api/auth/me").get_json()
    check("익명은 빈 문자열", anon["display_name"] == "")


def test_history_names_map():
    print("[d] 목록 names 맵 (업로더 표기용)")
    j = client.get("/pe/report/api/history?limit=50&offset=0",
                   headers={"User-Agent": HONEY_UA}).get_json()
    # uploaded_by 는 'SECDS\PteUser1' — 꼬리를 소문자로 떼어낸 키로 들어간다
    check("names 에 업로더 이름", j["names"].get("pteuser1") == "홍길동")


def test_candidate_search_by_name():
    print("[e] 편집자 후보 - 이름 검색")
    report_db.set_display_name("teammate9", "김철수", "self")
    # 업로더(pteuser1) 본인으로 접속해야 후보 목록을 볼 수 있다(_uploader_guard)
    r = client.get("/pe/report/session/s01/editors/candidates?q=김철",
                   headers={"User-Agent": HONEY_UA})
    check("후보 조회 200", r.status_code == 200)
    cands = r.get_json()["candidates"]
    check("이름으로 검색됨", any(c["user"] == "teammate9" for c in cands))
    check("후보에 name 동봉",
          all(c["name"] == "김철수" for c in cands if c["user"] == "teammate9"))
    r = client.get("/pe/report/session/s01/editors/candidates?q=teammate",
                   headers={"User-Agent": HONEY_UA})
    check("ID 검색도 그대로 동작",
          any(c["user"] == "teammate9" for c in r.get_json()["candidates"]))


def test_rename():
    print("[f] 이름 변경 (UPSERT)")
    r = post("/pe/report/api/auth/display_name", {"name": "홍길순"}, ua=HONEY_UA)
    check("변경 200", r.status_code == 200)
    check("덮어쓰기됨", report_db.get_display_name("pteuser1") == "홍길순")
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM report_user_profile WHERE user_id=?",
                         ("pteuser1",)).fetchone()[0]
    check("행은 1개 (중복 INSERT 아님)", n == 1)
    check("display_names 배치 조회",
          report_db.display_names(["pteuser1", "teammate9", "nobody"])
          == {"pteuser1": "홍길순", "teammate9": "김철수"})
    check("빈 목록은 빈 dict", report_db.display_names([]) == {})


if __name__ == "__main__":
    seed()
    test_guards()
    test_save_without_account()
    test_viewer_payloads()
    test_history_names_map()
    test_candidate_search_by_name()
    test_rename()
    print("\nALL OK")

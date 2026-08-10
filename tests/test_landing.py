"""/pe 랜딩 페이지 + 현황 API 검증.

시나리오:
  (a) 라우팅 — /pe, /pe/ 둘 다 200, 기존 /pe/report/ 무회귀, url_map 에 세 rule 존재
  (b) gzip / ETag — Accept-Encoding: gzip -> Content-Encoding, If-None-Match -> 304
  (c) 세션 카운트 — 완료·미삭제만, **비공개 포함**, 0건 제품군도 키 0, total = 합계
  (d) 오늘자 사용량 합계 — kind 별 SUM, 어제 행 제외
  (e) 개인정보 미노출 — 응답에 계정ID·ip:·users 키가 없다
  (f) CSRF 순서 — GET /pe 는 쿠키를 안 심고, /api/landing 호출이 심는다 (로그아웃 전제)
  (g) TTL 캐시 — 2회 연속 호출은 같은 스냅샷, 캐시 비우면 갱신

실행:
    python tests/test_landing.py

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="landing_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from report.report_extension import report_bp  # noqa: E402  (라우트 등록 트리거)
from report import routes_misc  # noqa: E402  (캐시 리셋용)
from honey_routes import honey_bp  # noqa: E402
from landing import landing_bp  # noqa: E402
from database import report_db  # noqa: E402
from database.core import get_conn  # noqa: E402

app = Flask(__name__)
app.secret_key = "landing-test"      # 운영은 wsgi.py 가 파일에서 로드 — 로그아웃이 세션을 지운다
app.register_blueprint(report_bp)
app.register_blueprint(honey_bp)
app.register_blueprint(landing_bp)
report_db.init_report_db()
client = app.test_client()

_failures = []


def check(ok, label):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        _failures.append(label)


def reset_cache():
    routes_misc._landing_cache = None


def landing_json():
    reset_cache()
    return json.loads(client.get("/pe/report/api/landing").data)


def add_session(sid, product_type, *, status="done", private=0, deleted=False,
                uploaded_by="alice"):
    report_db.create_session(sid, sid + ".csv", None, product_type=product_type,
                             source="web_report", uploaded_by=uploaded_by)
    sets = ["status = ?"]
    args = [status]
    if private:
        sets.append("is_private = 1")
    if deleted:
        sets.append("deleted_at = ?")
        args.append(int(time.time()))
    args.append(sid)
    with get_conn() as conn:
        conn.execute(f"UPDATE report_session SET {', '.join(sets)} WHERE session_id = ?", args)


# ── (a) 라우팅 ────────────────────────────────────────────────────────────────

r_pe = client.get("/pe")
r_pes = client.get("/pe/")
check(r_pe.status_code == 200 and r_pes.status_code == 200,
      "(a) /pe 와 /pe/ 둘 다 200 (슬래시 리다이렉트 홉 없음)")
check("text/html" in (r_pe.headers.get("Content-Type") or ""),
      "(a) Content-Type: text/html")
check(b"LSI PTE REPORT SERVER" in r_pe.data, "(a) 제목이 실려 있다")
check(b'href="/pe/report/?pt=MDDI"' in r_pe.data,
      "(a) 제품군 딥링크는 pt= 파라미터 (product_type= 은 페이지에서 무시된다)")
check(client.get("/pe/report/").status_code == 200,
      "(a) 기존 검색결과 페이지 무회귀")
rules = {str(r) for r in app.url_map.iter_rules()}
check({"/pe", "/pe/", "/pe/report/"} <= rules, "(a) url_map 에 세 rule 모두 존재")

# ── (b) gzip / ETag ──────────────────────────────────────────────────────────

rz = client.get("/pe", headers={"Accept-Encoding": "gzip"})
check(rz.headers.get("Content-Encoding") == "gzip", "(b) gzip 응답")
etag = rz.headers.get("ETag")
r304 = client.get("/pe", headers={"If-None-Match": etag})
check(bool(etag) and r304.status_code == 304, "(b) ETag 재요청은 304")

# ── (c) 세션 카운트 ──────────────────────────────────────────────────────────

add_session("s_mddi_1", "MDDI")
add_session("s_mddi_2", "MDDI")
add_session("s_pmic_1", "PMIC")
add_session("s_priv_1", "PMIC", private=1)                 # 비공개 — 포함되어야 한다
add_session("s_pend_1", "TCON", status="pending")          # 미완료 — 제외
add_session("s_del_1", "TCON", deleted=True)               # 휴지통 — 제외

d = landing_json()
s = d["sessions"]
check(s.get("MDDI") == 2, "(c) MDDI = 2")
check(s.get("PMIC") == 2, "(c) PMIC = 2 (비공개 세션 포함)")
check(s.get("TCON") == 0, "(c) TCON = 0 (pending·휴지통 제외)")
check(s.get("PDDI") == 0 and s.get("SECURITY") == 0,
      "(c) 0건 제품군도 키가 0 으로 존재")
check(s.get("total") == 4, "(c) total = 제품군 합계")

# 익명/신원 요청이 같은 값을 본다 (누가 봐도 같은 숫자)
reset_cache()
anon = json.loads(client.get("/pe/report/api/landing").data)["sessions"]
reset_cache()
named = json.loads(client.get(
    "/pe/report/api/landing",
    headers={"User-Agent": "Mozilla/5.0 HoneyUser/alice"}).data)["sessions"]
check(anon == named, "(c) 신원 유무와 무관하게 같은 숫자")

# ── (d) 오늘자 사용량 합계 ───────────────────────────────────────────────────

now = int(time.time())
today = time.strftime("%Y-%m-%d", time.localtime(now))
yday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
with get_conn() as conn:
    conn.execute("DELETE FROM report_usage_daily")
    conn.executemany(
        "INSERT INTO report_usage_daily (day, kind, user_id, count, last_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(today, "honey_run", "alice", 5, now),
         (today, "honey_run", "ip:10.0.0.9", 2, now),
         (today, "web_index", "alice", 3, now),
         (today, "web_view", "bob", 4, now),
         (yday, "honey_run", "carol", 99, now - 86400)])

u = landing_json()["usage"]
check(u.get("day") == today, "(d) day = 오늘 (localtime)")
check(u.get("honey_run") == 7 and u.get("web_index") == 3 and u.get("web_view") == 4,
      "(d) kind 별 SUM (사용자 축 합산)")
check(u.get("total") == 14, "(d) total = 14 (어제 99건 제외)")

# ── (e) 개인정보 미노출 ──────────────────────────────────────────────────────

raw = client.get("/pe/report/api/landing",
                 headers={"User-Agent": "Mozilla/5.0 HoneyUser/alice"}).data.decode("utf-8")
body = json.loads(raw)
check("users" not in body.get("active", {}), "(e) active 에 users 목록 없음")
check(set(body.get("active", {})) == {"count", "window_sec"},
      "(e) active 는 count/window_sec 만")
stats_only = json.dumps({k: v for k, v in body.items() if k != "viewer"})
check("ip:" not in stats_only and "bob" not in stats_only and "carol" not in stats_only,
      "(e) 집계 값에 계정ID·ip: 문자열 없음")
check(body.get("viewer", {}).get("user_id") == "alice",
      "(e) viewer 는 요청자 본인 신원만 (캐시되지 않는다)")

# ── (f) CSRF 순서 ────────────────────────────────────────────────────────────

c2 = app.test_client()
c2.get("/pe")
check(c2.get_cookie("report_csrf") is None,
      "(f) GET /pe 는 CSRF 쿠키를 심지 않는다 (landing_bp 엔 after_request 없음)")
c2.get("/pe/report/api/landing")
tok = c2.get_cookie("report_csrf")
check(tok is not None, "(f) /api/landing 호출이 CSRF 쿠키를 심는다")
if tok is not None:
    lo = c2.post("/pe/report/api/auth/logout", headers={"X-CSRF-Token": tok.value})
    check(lo.status_code == 200, "(f) 그 토큰으로 로그아웃 POST 가 403 이 아니다")

# ── (g) TTL 캐시 ─────────────────────────────────────────────────────────────

reset_cache()
first = json.loads(client.get("/pe/report/api/landing").data)
add_session("s_mddi_3", "MDDI")                 # 캐시 중에는 반영되지 않아야 한다
second = json.loads(client.get("/pe/report/api/landing").data)
check(second["sessions"] == first["sessions"], "(g) TTL 안에서는 같은 스냅샷")
check(landing_json()["sessions"]["MDDI"] == 3, "(g) 캐시 만료 후 갱신")

# ── (h) 최근 1주 활동 (신규 / 수정) ──────────────────────────────────────────
# 여기까지 만들어진 완료·미삭제 세션은 전부 방금 생성 → created 에 잡힌다.
# 1주 밖에 만든 세션에 최근 편집을 붙이면 updated 로만 잡혀야 한다 (둘은 겹치지 않는다).
fresh_total = landing_json()["sessions"]["total"]
add_session("s_old_1", "TCON")
with get_conn() as conn:
    conn.execute("UPDATE report_session SET created_at = ? WHERE session_id = 's_old_1'",
                 (now - 30 * 86400,))
    conn.execute(
        "INSERT INTO report_webreport_edit (session_id, kind, item_key, value, updated_at, updated_by) "
        "VALUES ('s_old_1', 'issue_comment', 'r1', 'hello', ?, 'alice')", (now,))

rec = landing_json()["recent"]
check(rec.get("days") == 7, "(h) days = 7")
check(rec.get("created") == fresh_total, "(h) created = 최근 1주 안에 생성된 세션만")
check(rec.get("updated") == 1, "(h) updated = 1주 전에 만들어졌고 최근 편집된 세션")

# 편집 시각이 1주 밖이면 updated 에서 빠진다
with get_conn() as conn:
    conn.execute("UPDATE report_webreport_edit SET updated_at = ? WHERE session_id = 's_old_1'",
                 (now - 20 * 86400,))
check(landing_json()["recent"].get("updated") == 0, "(h) 오래된 편집은 제외")

# ── (i) Honey 다운로드 버튼 — 검색결과 페이지와 동일 규약 ────────────────────
with get_conn() as conn:
    conn.execute("DELETE FROM report_usage_daily")
c3 = app.test_client()
ua = {"User-Agent": "Mozilla/5.0 HoneyUser/dana"}
check(c3.get("/honey/version?probe=1", headers=ua).status_code in (200, 404),
      "(i) probe 호출도 같은 응답 (manifest 유무에 따라 200/404)")
with get_conn() as conn:
    n = conn.execute("SELECT COUNT(*) FROM report_usage_daily WHERE kind='honey_run'").fetchone()[0]
check(n == 0, "(i) probe=1 은 'Honey 실행' 집계를 올리지 않는다")
c3.get("/honey/version", headers=ua)             # 실제 클라 호출 경로는 그대로 집계
with get_conn() as conn:
    n = conn.execute("SELECT COUNT(*) FROM report_usage_daily WHERE kind='honey_run'").fetchone()[0]
check(n == 1, "(i) probe 없는 호출은 종전대로 집계 (기존 동작 무회귀)")

pe_html = client.get("/pe").data
check(b'id="honeyDlBtn"' in pe_html and b'href="/honey/download"' in pe_html,
      "(i) 랜딩의 다운로드 버튼도 /honey/download 기본 링크 + 보정용 id")

# ── (k) 랜딩 자체 로그인 (모달) ──────────────────────────────────────────────
# 랜딩의 '로그인' 은 /pe/report 로 넘기지 않고 그 자리에서 모달을 띄운다.
check(b'id="loginModal"' in pe_html and b'id="pwInput"' in pe_html,
      "(k) 랜딩에 로그인 모달 마크업이 있다")
check(b'<button class="chip" id="userChip"' in pe_html,
      "(k) 로그인 칩이 /pe/report 링크가 아니라 버튼")

# 모달이 실제로 쓰는 경로 그대로: /pe/report/api/landing 이 심은 CSRF 로 로그인 POST
c4 = app.test_client()
report_db.create_user("erin", generate_password_hash("1234"))
c4.get("/pe")
c4.get("/pe/report/api/landing")
tok4 = c4.get_cookie("report_csrf")
lg = c4.post("/pe/report/api/auth/login",
             json={"user_id": "erin", "password": "1234"},
             headers={"X-CSRF-Token": tok4.value if tok4 else ""})
body4 = json.loads(lg.data) if lg.status_code == 200 else {}
check(lg.status_code == 200 and body4.get("user_id") == "erin",
      "(k) 랜딩 경로로 받은 CSRF 로 로그인 성공")
me = json.loads(c4.get("/pe/report/api/landing").data)["viewer"]
check(me.get("user_id") == "erin" and me.get("source") == "login",
      "(k) 로그인 후 랜딩 viewer 가 login 신원으로 바뀐다")

bad = c4.post("/pe/report/api/auth/login",
              json={"user_id": "erin", "password": "9999"},
              headers={"X-CSRF-Token": tok4.value if tok4 else ""})
check(bad.status_code != 200 and b"error" in bad.data,
      "(k) 잘못된 비밀번호는 error 를 돌려준다 (모달이 그 문구를 표시)")

# ── (j) 검색결과 페이지 제목 → 랜딩 링크 ─────────────────────────────────────
idx = client.get("/pe/report/").data.decode("utf-8")
check('href="/pe/"' in idx and "page-title-link" in idx,
      "(j) /pe/report 좌상단 제목이 /pe/ 로 가는 링크")

print()
if _failures:
    print(f"FAILED: {len(_failures)}건")
    for f in _failures:
        print("  - " + f)
    sys.exit(1)
print("ALL OK")

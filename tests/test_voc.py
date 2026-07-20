"""VOC 게시판 (별도 voc.db + storage_gateway voc_<id> 네임스페이스) E2E 검증.

실행:
    python tests/test_voc.py

시나리오:
  (a) 스키마 반복 생성 멱등
  (b) 텍스트만 / 이미지 1장 / 3장 등록 + 이미지 서빙(nosniff) + 로컬 파일 실존
  (c) 입력 거부 — 4장·2MB 초과·요청 전체 초과·위장 확장자·빈 파일·필드 위반
  (d) 권한 — 익명 조회 ok / 이름 없는 익명 등록 400 / CSRF 누락 403 / 타인 삭제 403 /
      본인 삭제 200 (메타 CASCADE + 로컬 이미지 디렉토리 정리)
  (e) 이미지 격리 — 타 VOC image_id·형식 위반 id 404
  (f) 페이지네이션 — 25건 → 20+5 최신순
  (g) 감사 로그 voc_create/voc_delete
  (h) XSS 페이로드 원문 무변조 (렌더는 프론트 textContent 책임)
  (i) 검색 — 제목 부분일치 / 번호(id) / LIKE 특수문자 이스케이프
  (j) 수정(PATCH) — 본인 ok / 타인 403 / 익명 401 / CSRF 403 / 필드 검증 400
  (k) 처리 상태 — 신규 open / 비관리자 403 / 관리자 close·open / 잘못된 값 400
  (l) 댓글 — 등록·작성순·길이·익명 401 / 타인 삭제 403 / 관리자 삭제 ok /
      글 삭제 시 CASCADE / 목록 comment_count
  (m) 구 스키마 voc.db 마이그레이션 (status·guest_token 컬럼 추가, 기존 행 'open')
  (n) 게스트(일반 브라우저) — 이름으로 등록·댓글 / 이름 검증 / 게스트 토큰 쿠키로
      본인 글만 수정·삭제 / 다른 브라우저는 같은 이름이어도 403 / Honey 계정 글과 격리

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import io
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="voc_test_"))
os.environ["REPORT_VOC_DB_PATH"] = str(_TMP / "voc" / "voc.db")
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")          # 감사 로그용 메인 DB
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")         # 이미지 로컬 폴백
os.environ["REPORT_S3_BUCKET"] = ""                             # S3 비활성 → 로컬 경로 검증

from flask import Flask  # noqa: E402

import config  # noqa: E402  (마이그레이션 시나리오에서 DB 경로 교체)
from admin_panel import GATE_COOKIE_VOC, voc_gate_token  # noqa: E402  (관리자 쿠키 시뮬레이션)
from report.report_extension import report_bp  # noqa: E402  (전체 라우트 등록 트리거)
from database import report_db, voc_db  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100   # 서버는 매직바이트만 검사
JPG = b"\xff\xd8\xff" + b"\x00" * 100
_MB = 1024 * 1024


def _csrf():
    client.get("/pe/report/voc")             # after_request 가 쿠키 발급
    cookie = client.get_cookie("report_csrf")
    assert cookie is not None, "CSRF 쿠키가 발급되지 않음"
    return cookie.value


def _headers(ua_user="tester", csrf=True):
    h = {}
    if ua_user:
        h["User-Agent"] = f"Mozilla/5.0 HoneyUser/{ua_user}"
    if csrf:
        h["X-CSRF-Token"] = _csrf()
    return h


def _post(files=None, ua_user="tester", csrf=True, **form):
    data = {"category": "버그", "title": "제목", "content": "내용"}
    data.update(form)
    if files:
        data["screenshots"] = [(io.BytesIO(b), name) for b, name in files]
    return client.post("/pe/report/api/voc", data=data,
                       content_type="multipart/form-data",
                       headers=_headers(ua_user, csrf))


def _list(ua_user=None, limit=20, offset=0, q=None):
    h = {"User-Agent": f"Mozilla/5.0 HoneyUser/{ua_user}"} if ua_user else {}
    url = f"/pe/report/api/voc?limit={limit}&offset={offset}"
    if q is not None:
        url += f"&q={q}"
    r = client.get(url, headers=h)
    assert r.status_code == 200, r.data
    return r.get_json()


def _detail(voc_id, ua_user=None, expect=200):
    h = {"User-Agent": f"Mozilla/5.0 HoneyUser/{ua_user}"} if ua_user else {}
    r = client.get(f"/pe/report/api/voc/{voc_id}", headers=h)
    assert r.status_code == expect, (r.status_code, r.data)
    return r.get_json() if r.status_code == 200 else None


def _patch(voc_id, ua_user="tester", csrf=True, **body):
    payload = {"category": "문의", "title": "수정된 제목", "content": "수정된 내용"}
    payload.update(body)
    return client.patch(f"/pe/report/api/voc/{voc_id}", json=payload,
                        headers=_headers(ua_user, csrf))


def _set_status(voc_id, status, ua_user="tester", csrf=True):
    return client.post(f"/pe/report/api/voc/{voc_id}/status", json={"status": status},
                       headers=_headers(ua_user, csrf))


def _comment(voc_id, content="댓글 내용", ua_user="tester", csrf=True):
    return client.post(f"/pe/report/api/voc/{voc_id}/comments", json={"content": content},
                       headers=_headers(ua_user, csrf))


def _comment_delete(voc_id, comment_id, ua_user="tester", csrf=True):
    return client.delete(f"/pe/report/api/voc/{voc_id}/comments/{comment_id}",
                         headers=_headers(ua_user, csrf))


def _delete(voc_id, ua_user="tester", csrf=True):
    return client.delete(f"/pe/report/api/voc/{voc_id}", headers=_headers(ua_user, csrf))


def _admin(on):
    """관리자 게이트 쿠키(admin 로그인이 /pe/report 경로로 발급하는 사본) 시뮬레이션."""
    if on:
        client.set_cookie(GATE_COOKIE_VOC, voc_gate_token(), path="/pe/report")
    else:
        client.delete_cookie(GATE_COOKIE_VOC, path="/pe/report")


def _local_img_dir(voc_id):
    return Path(os.environ["REPORT_UPLOAD_DIR"]) / "note_img" / f"voc_{voc_id}"


# ── (a) 스키마 반복 생성 멱등 ─────────────────────────────────────────────────
with voc_db.open_conn() as conn:
    pass
with voc_db.open_conn() as conn:
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
assert {"report_voc", "report_voc_image", "report_voc_comment"} <= names, names
print("(a) 스키마 멱등 ok")

# ── (b) 등록 — 텍스트만 / 1장 / 3장 + 서빙 ──────────────────────────────────
r = _post(title="텍스트만", content="본문")
assert r.status_code == 201, (r.status_code, r.data)
text_only_id = r.get_json()["id"]

r = _post(files=[(PNG, "one.png")], title="한 장")
assert r.status_code == 201, (r.status_code, r.data)
one_img_id = r.get_json()["id"]

r = _post(files=[(PNG, "a.png"), (JPG, "b.jpg"), (PNG, "c.png")], title="세 장")
assert r.status_code == 201, (r.status_code, r.data)
three_img_id = r.get_json()["id"]

assert len(_detail(text_only_id)["screenshots"]) == 0
one_detail = _detail(one_img_id, ua_user="tester")
assert len(one_detail["screenshots"]) == 1
assert one_detail["voc"]["user_id"] == "tester"
assert one_detail["can_delete"] is True and one_detail["can_edit"] is True
three_detail = _detail(three_img_id)
assert len(three_detail["screenshots"]) == 3

for shot in three_detail["screenshots"]:
    resp = client.get(shot["url"])
    assert resp.status_code == 200, (shot, resp.status_code)
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.mimetype in ("image/png", "image/jpeg"), resp.mimetype
img_dir = _local_img_dir(three_img_id)
stored = [p for p in img_dir.iterdir() if p.suffix in (".png", ".jpg")]
assert len(stored) == 3, list(img_dir.iterdir())
print("(b) 텍스트만/1장/3장 등록 + 서빙 + 로컬 실존 ok")

# ── (c) 입력 거부 ────────────────────────────────────────────────────────────
r = _post(files=[(PNG, f"{i}.png") for i in range(4)])
assert r.status_code == 400, (r.status_code, r.data)          # 4장
r = _post(files=[(PNG + b"\x00" * (2 * _MB), "big.png")])
assert r.status_code == 413, (r.status_code, r.data)          # 장당 2MB 초과
r = _post(content="x" * (8 * _MB))
assert r.status_code == 413, (r.status_code, r.data)          # 요청 전체 선차단
r = _post(files=[(b"just text pretending", "fake.png")])
assert r.status_code == 400, (r.status_code, r.data)          # 위장 확장자(매직 불일치)
r = _post(files=[(b"", "empty.png")])
assert r.status_code == 400, (r.status_code, r.data)          # 빈 파일
r = _post(title="")
assert r.status_code == 400
r = _post(title="x" * 121)
assert r.status_code == 400
r = _post(content="")
assert r.status_code == 400
r = _post(content="x" * 4001)
assert r.status_code == 400
r = _post(category="spam")
assert r.status_code == 400
print("(c) 4장/2MB/전체상한/위장/빈파일/필드 거부 ok")

# ── (d) 권한 ────────────────────────────────────────────────────────────────
data = _list()                                                # 익명 조회
assert data["user"] == "" and data["is_admin"] is False
anon_detail = _detail(one_img_id)
assert anon_detail["can_delete"] is False and anon_detail["can_edit"] is False
r = _post(ua_user=None)                                       # 익명 + 이름 없음
assert r.status_code == 400, (r.status_code, r.data)
r = _post(csrf=False)                                         # CSRF 누락
assert r.status_code == 403, (r.status_code, r.data)
r = _delete(one_img_id, csrf=False)
assert r.status_code == 403, (r.status_code, r.data)
r = _delete(one_img_id, ua_user=None)                         # 익명(게스트 토큰 없음)
assert r.status_code == 403, (r.status_code, r.data)
r = _delete(one_img_id, ua_user="other")                      # 타인 삭제
assert r.status_code == 403, (r.status_code, r.data)
r = _delete(three_img_id)                                     # 본인 삭제
assert r.status_code == 200, (r.status_code, r.data)
assert voc_db.get_voc(three_img_id) is None
with voc_db.open_conn() as conn:
    n = conn.execute("SELECT COUNT(*) FROM report_voc_image WHERE voc_id=?",
                     (three_img_id,)).fetchone()[0]
assert n == 0, n                                              # 메타 CASCADE
assert not _local_img_dir(three_img_id).exists()              # 실파일 정리
r = _delete(three_img_id)                                     # 재삭제
assert r.status_code == 404, r.status_code
_detail(three_img_id, expect=404)                             # 상세도 404
print("(d) 익명/CSRF/타인/본인 삭제 + 정리 ok")

# ── (e) 이미지 격리 ─────────────────────────────────────────────────────────
one_shot = _detail(one_img_id)["screenshots"][0]
r = client.get(f"/pe/report/api/voc/{text_only_id}/screenshots/{one_shot['image_id']}")
assert r.status_code == 404, r.status_code                    # 타 VOC 의 image_id
r = client.get(f"/pe/report/api/voc/{one_img_id}/screenshots/zzz.png")
assert r.status_code == 404, r.status_code                    # 형식 위반
r = client.get(f"/pe/report/api/voc/{one_img_id}/screenshots/{'0' * 32}.exe")
assert r.status_code == 404, r.status_code                    # 허용 외 확장자
print("(e) 타 VOC/임의 id 404 ok")

# ── (f) 페이지네이션 (25건, 최신순) ─────────────────────────────────────────
for i in range(25):
    r = _post(title=f"페이지 {i}", content="p")
    assert r.status_code == 201
page0 = _list(limit=20, offset=0)
page1 = _list(limit=20, offset=20)
assert page0["total"] == page1["total"] >= 25
assert len(page0["items"]) == 20
assert len(page1["items"]) == page0["total"] - 20
assert page0["items"][0]["title"] == "페이지 24"              # 최신이 먼저
ids = [it["id"] for it in page0["items"]]
assert ids == sorted(ids, reverse=True), ids
# 목록은 lean — 본문·스크린샷은 상세에서만
assert "content" not in page0["items"][0], page0["items"][0]
assert "screenshots" not in page0["items"][0], page0["items"][0]
assert page0["items"][0]["comment_count"] == 0
print("(f) 페이지네이션 20+나머지 최신순 + lean 목록 ok")

# ── (g) 감사 로그 ───────────────────────────────────────────────────────────
creates = report_db.get_audit_logs(action="voc_create", limit=100)
deletes = report_db.get_audit_logs(action="voc_delete", limit=100)
assert creates and creates[0]["client_user"] == "tester", creates[:1]
assert deletes and deletes[0]["client_user"] == "tester", deletes[:1]
print("(g) 감사 voc_create/voc_delete ok")

# ── (h) XSS 페이로드 원문 무변조 ────────────────────────────────────────────
payload = '<script>alert(1)</script><img src=x onerror=alert(2)>'
r = _post(title=payload, content=payload)
assert r.status_code == 201
xss_id = r.get_json()["id"]
xss = _detail(xss_id)["voc"]
assert xss["title"] == payload and xss["content"] == payload
print("(h) XSS 원문 무변조 ok")

# ── (i) 검색 ────────────────────────────────────────────────────────────────
r = _post(title="검색표식 100% 할인", content="본문")
assert r.status_code == 201
hit_id = r.get_json()["id"]
assert _list(q="검색표식")["total"] == 1
assert _list(q="검색표식")["items"][0]["id"] == hit_id
assert _list(q=str(hit_id))["total"] >= 1                     # 번호(id) 검색
assert any(it["id"] == hit_id for it in _list(q=str(hit_id))["items"])
assert _list(q="100%")["total"] == 1                          # % 는 리터럴
assert _list(q="_")["total"] == 0                             # _ 도 리터럴(전체 매치 아님)
assert _list(q="존재하지않는제목")["total"] == 0
assert _list(q="9" * 30)["total"] == 0                        # 18자리 초과도 안전
print("(i) 제목/번호/LIKE 이스케이프 검색 ok")

# ── (j) 수정 (PATCH) ────────────────────────────────────────────────────────
r = _patch(hit_id)                                            # 본인
assert r.status_code == 200, (r.status_code, r.data)
edited = _detail(hit_id)["voc"]
assert edited["title"] == "수정된 제목" and edited["content"] == "수정된 내용"
assert edited["category"] == "문의"
r = _patch(hit_id, ua_user="other")                           # 타인
assert r.status_code == 403, (r.status_code, r.data)
r = _patch(hit_id, ua_user=None)                              # 익명(게스트 토큰 없음)
assert r.status_code == 403, (r.status_code, r.data)
r = _patch(hit_id, csrf=False)                                # CSRF 누락
assert r.status_code == 403, (r.status_code, r.data)
r = _patch(hit_id, title="")
assert r.status_code == 400
r = _patch(hit_id, title="x" * 121)
assert r.status_code == 400
r = _patch(hit_id, content="x" * 4001)
assert r.status_code == 400
r = _patch(hit_id, category="spam")
assert r.status_code == 400
r = _patch(999999)                                            # 없는 글
assert r.status_code == 404, r.status_code
assert _detail(hit_id)["voc"]["title"] == "수정된 제목"        # 거부된 요청은 무변경
print("(j) 수정 본인/타인/익명/CSRF/검증 ok")

# ── (k) 처리 상태 (Open / Close) ────────────────────────────────────────────
assert _detail(hit_id)["voc"]["status"] == "open"             # 신규는 항상 open
r = _set_status(hit_id, "close")                              # 비관리자
assert r.status_code == 403, (r.status_code, r.data)
assert _detail(hit_id)["voc"]["status"] == "open"
_admin(True)
assert _detail(hit_id)["is_admin"] is True
r = _set_status(hit_id, "close")
assert r.status_code == 200, (r.status_code, r.data)
assert _detail(hit_id)["voc"]["status"] == "close"
assert _list(q="수정된")["items"][0]["status"] == "close"      # 목록에도 반영
r = _set_status(hit_id, "open")                               # 다시 열기
assert r.status_code == 200 and _detail(hit_id)["voc"]["status"] == "open"
r = _set_status(hit_id, "bogus")                              # 잘못된 값
assert r.status_code == 400, (r.status_code, r.data)
r = _set_status(hit_id, "close", csrf=False)
assert r.status_code == 403, (r.status_code, r.data)
r = _set_status(999999, "close")                              # 없는 글
assert r.status_code == 404, r.status_code
_admin(False)
assert _detail(hit_id)["is_admin"] is False
statuses = report_db.get_audit_logs(action="voc_status", limit=100)
assert statuses, "voc_status 감사 없음"
print("(k) 신규 open / 비관리자 403 / 관리자 close·open / 검증 ok")

# ── (l) 댓글 ────────────────────────────────────────────────────────────────
r = _comment(hit_id, "첫 번째 댓글")
assert r.status_code == 201, (r.status_code, r.data)
first_cid = r.get_json()["id"]
r = _comment(hit_id, "두 번째 댓글", ua_user="other")
assert r.status_code == 201, (r.status_code, r.data)
second_cid = r.get_json()["id"]
comments = _detail(hit_id, ua_user="tester")["comments"]
assert [c["content"] for c in comments] == ["첫 번째 댓글", "두 번째 댓글"], comments
assert comments[0]["can_delete"] is True                      # 본인 댓글
assert comments[1]["can_delete"] is False                     # 타인 댓글
assert _list(q="수정된")["items"][0]["comment_count"] == 2
r = _comment(hit_id, "익명", ua_user=None)                    # 이름 없는 익명
assert r.status_code == 400, (r.status_code, r.data)
r = _comment(hit_id, "", )
assert r.status_code == 400, (r.status_code, r.data)
r = _comment(hit_id, "x" * 1001)
assert r.status_code == 400, (r.status_code, r.data)
r = _comment(hit_id, "csrf 없음", csrf=False)
assert r.status_code == 403, (r.status_code, r.data)
r = _comment(999999, "없는 글")
assert r.status_code == 404, r.status_code
r = _comment_delete(hit_id, second_cid)                       # 타인 댓글 삭제
assert r.status_code == 403, (r.status_code, r.data)
r = _comment_delete(hit_id, first_cid)                        # 본인 댓글 삭제
assert r.status_code == 200, (r.status_code, r.data)
r = _comment_delete(hit_id, first_cid)                        # 재삭제
assert r.status_code == 404, r.status_code
r = _comment_delete(text_only_id, second_cid)                 # 타 VOC 소속 → 404
assert r.status_code == 404, r.status_code
_admin(True)
r = _comment_delete(hit_id, second_cid, ua_user="tester")     # 관리자 강제 삭제
assert r.status_code == 200, (r.status_code, r.data)
_admin(False)
assert _detail(hit_id)["comments"] == []
# Close 된 글에도 댓글 가능 (상태는 잠금이 아니다)
_admin(True)
_set_status(hit_id, "close")
_admin(False)
r = _comment(hit_id, "Close 후 댓글")
assert r.status_code == 201, (r.status_code, r.data)
# 글 삭제 시 댓글 CASCADE
assert _delete(hit_id).status_code == 200
with voc_db.open_conn() as conn:
    n = conn.execute("SELECT COUNT(*) FROM report_voc_comment WHERE voc_id=?",
                     (hit_id,)).fetchone()[0]
assert n == 0, n
assert report_db.get_audit_logs(action="voc_comment_create", limit=10)
assert report_db.get_audit_logs(action="voc_comment_delete", limit=10)
print("(l) 댓글 등록/순서/권한/관리자 삭제/CASCADE ok")

# ── (n) 게스트(일반 브라우저) — 이름 입력으로 등록 ──────────────────────────
# 이름은 표시용이라 그것만으로는 소유권이 안 된다. 서버가 심는 게스트 토큰 쿠키를
# 가진 브라우저에게만 수정·삭제를 허용하는지가 핵심.
def _gcsrf(cl):
    cl.get("/pe/report/voc")
    return cl.get_cookie("report_csrf").value


def _gpost(cl, guest_name=None, **form):
    """게스트 등록 (HoneyUser UA 없음 — Werkzeug 기본 UA)."""
    data = {"category": "기타", "title": "게스트 글", "content": "게스트 본문"}
    data.update(form)
    if guest_name is not None:
        data["guest_name"] = guest_name
    return cl.post("/pe/report/api/voc", data=data,
                   content_type="multipart/form-data",
                   headers={"X-CSRF-Token": _gcsrf(cl)})


def _gjson(cl, method, path, **body):
    return getattr(cl, method)(f"/pe/report{path}", json=body or None,
                               headers={"X-CSRF-Token": _gcsrf(cl)})


g1 = app.test_client()            # 게스트 브라우저 A
g2 = app.test_client()            # 게스트 브라우저 B (토큰이 다르다)

assert _gpost(g1).status_code == 400                      # 이름 없음
assert _gpost(g1, guest_name="  ").status_code == 400      # 공백뿐
assert _gpost(g1, guest_name="가" * 21).status_code == 400  # 20자 초과

r = _gpost(g1, guest_name="홍길동", title="게스트가 쓴 글")
assert r.status_code == 201, (r.status_code, r.data)
g1_voc = r.get_json()["id"]
setc = r.headers.getlist("Set-Cookie")
assert any(c.startswith("report_voc_guest=") and "Path=/pe/report" in c
           and "HttpOnly" in c for c in setc), setc
g1_token = g1.get_cookie("report_voc_guest", path="/pe/report").value
assert len(g1_token) == 32, g1_token

d = _gjson(g1, "get", f"/api/voc/{g1_voc}").get_json()
assert d["voc"]["user_id"] == "홍길동" and d["voc"]["is_guest"] is True
assert d["can_edit"] is True and d["can_delete"] is True    # 쓴 브라우저
assert "guest_token" not in d["voc"], d["voc"]              # 토큰 미노출
assert g1_token not in r.get_data(as_text=True)
listed = {it["id"]: it for it in _list()["items"]}[g1_voc]
assert listed["is_guest"] == 1 and listed["user_id"] == "홍길동"
assert "guest_token" not in listed, listed

# 다른 브라우저(B)는 같은 글을 못 고친다 — 이름을 사칭해도 토큰이 없다
d2 = _gjson(g2, "get", f"/api/voc/{g1_voc}").get_json()
assert d2["can_edit"] is False and d2["can_delete"] is False
r = _gjson(g2, "patch", f"/api/voc/{g1_voc}",
           category="기타", title="가로채기", content="가로채기")
assert r.status_code == 403, (r.status_code, r.data)
assert _gjson(g2, "delete", f"/api/voc/{g1_voc}").status_code == 403
r = _gpost(g2, guest_name="홍길동", title="같은 이름 다른 사람")
assert r.status_code == 201                                 # 이름 중복 자체는 허용
g2_voc = r.get_json()["id"]
assert g2.get_cookie("report_voc_guest", path="/pe/report").value != g1_token

# 본인(A)은 수정 가능
r = _gjson(g1, "patch", f"/api/voc/{g1_voc}",
           category="문의", title="게스트가 고친 글", content="고친 본문")
assert r.status_code == 200, (r.status_code, r.data)
assert _gjson(g1, "get", f"/api/voc/{g1_voc}").get_json()["voc"]["title"] == "게스트가 고친 글"

# Honey 계정 글 ↔ 게스트 글 격리 — 이름이 Honey 계정과 같아도 서로 못 건드린다
r = _gpost(g1, guest_name="tester", title="계정명 사칭 시도")
assert r.status_code == 201
spoof_voc = r.get_json()["id"]
r = _patch(spoof_voc, ua_user="tester")                     # 진짜 tester 도 못 고침
assert r.status_code == 403, (r.status_code, r.data)
assert _delete(spoof_voc, ua_user="tester").status_code == 403
honey_voc = _post(title="Honey 계정 글").get_json()["id"]    # 반대 방향
assert _gjson(g1, "patch", f"/api/voc/{honey_voc}",
              category="기타", title="x", content="y").status_code == 403
assert _gjson(g1, "delete", f"/api/voc/{honey_voc}").status_code == 403

# 게스트 댓글
assert _gjson(g1, "post", f"/api/voc/{honey_voc}/comments",
              content="이름 없음").status_code == 400
r = _gjson(g1, "post", f"/api/voc/{honey_voc}/comments",
           content="게스트 댓글", guest_name="홍길동")
assert r.status_code == 201, (r.status_code, r.data)
g1_cmt = r.get_json()["id"]
cmts = _gjson(g1, "get", f"/api/voc/{honey_voc}").get_json()["comments"]
assert cmts[0]["user_id"] == "홍길동" and cmts[0]["is_guest"] is True
assert cmts[0]["can_delete"] is True and "guest_token" not in cmts[0]
assert _gjson(g2, "get", f"/api/voc/{honey_voc}").get_json()["comments"][0]["can_delete"] is False
assert _gjson(g2, "delete", f"/api/voc/{honey_voc}/comments/{g1_cmt}").status_code == 403
assert _delete(honey_voc, ua_user="tester").status_code == 200   # 글 주인은 글 삭제 가능

# 게스트도 CSRF 는 필요하다
r = g1.post("/pe/report/api/voc",
            data={"category": "기타", "title": "t", "content": "c", "guest_name": "홍길동"},
            content_type="multipart/form-data")
assert r.status_code == 403, (r.status_code, r.data)

# 본인 글 삭제
assert _gjson(g1, "delete", f"/api/voc/{g1_voc}").status_code == 200
assert _gjson(g2, "delete", f"/api/voc/{g2_voc}").status_code == 200
creates = report_db.get_audit_logs(action="voc_create", limit=200)
assert any((c["client_user"] or "").startswith("guest:") for c in creates)   # 감사에 게스트 표기
assert any(c["client_user"] == "tester" for c in creates)                    # Honey 계정과 구분
print("(n) 게스트 등록/이름검증/토큰 소유권/계정 격리/댓글 ok")

# 페이지 자체 서빙 확인
r = client.get("/pe/report/voc")
assert r.status_code == 200 and b"VOC" in r.data
print("페이지 서빙 ok")

# ── (m) 구 스키마 voc.db 마이그레이션 ───────────────────────────────────────
# 실제로 존재하는 두 세대의 DB 를 만들어 두고 open_conn 이 멱등 보정하는지 본다:
#   gen0 = 최초 배포 (status·guest_token·댓글 테이블 모두 없음)
#   gen1 = 게시판 개편 직후 (status 는 있고 guest_token 만 없음 ← 운영 DB 가 이 상태)
_GEN0 = """
CREATE TABLE report_voc (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
    category TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
    created_at INTEGER NOT NULL);
"""
_GEN1 = """
CREATE TABLE report_voc (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
    category TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', created_at INTEGER NOT NULL);
CREATE TABLE report_voc_comment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voc_id INTEGER NOT NULL REFERENCES report_voc(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL, content TEXT NOT NULL, created_at INTEGER NOT NULL);
"""

_saved_path = config.REPORT_VOC_DB_PATH
try:
    for gen, ddl in (("gen0", _GEN0), ("gen1", _GEN1)):
        old_db = _TMP / gen / "voc.db"
        old_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(old_db))
        conn.executescript(ddl)
        conn.execute("INSERT INTO report_voc (user_id, category, title, content, created_at)"
                     " VALUES ('legacy', '버그', '옛 글', '옛 본문', 1)")
        conn.commit()
        conn.close()

        config.REPORT_VOC_DB_PATH = str(old_db)   # db_path() 가 호출 시점에 읽는다
        with voc_db.open_conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(report_voc)")}
            ccols = {r[1] for r in c.execute("PRAGMA table_info(report_voc_comment)")}
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"status", "guest_token"} <= cols, (gen, cols)
        assert "guest_token" in ccols, (gen, ccols)
        assert {"report_voc_comment", "report_voc_image"} <= tables, (gen, tables)
        legacy = voc_db.get_voc(1)
        assert legacy["status"] == "open", (gen, legacy)     # 기존 행은 기본값
        assert legacy["guest_token"] is None, (gen, legacy)  # 옛 글은 게스트 아님
        assert legacy["title"] == "옛 글"
        with voc_db.open_conn() as c:             # 재실행 멱등
            pass
        items, total = voc_db.list_voc()
        assert total == 1 and items[0]["comment_count"] == 0, (gen, items, total)
        assert items[0]["is_guest"] == 0, items[0]
        # 보정된 DB 에 게스트 글이 실제로 써지는지
        gid = voc_db.create_voc("게스트", "기타", "새 글", "본문", guest_token="a" * 32)
        assert voc_db.get_voc(gid)["guest_token"] == "a" * 32
        voc_db.add_comment(gid, "게스트", "댓글", guest_token="a" * 32)
        assert voc_db.list_comments(gid)[0]["guest_token"] == "a" * 32
finally:
    config.REPORT_VOC_DB_PATH = _saved_path
print("(m) 구 스키마 마이그레이션 ok (gen0 / gen1 두 세대)")

print("\n전체 통과")
shutil.rmtree(_TMP, ignore_errors=True)

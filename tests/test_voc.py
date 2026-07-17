"""VOC 게시판 (별도 voc.db + storage_gateway voc_<id> 네임스페이스) E2E 검증.

실행:
    python tests/test_voc.py

시나리오:
  (a) 스키마 반복 생성 멱등
  (b) 텍스트만 / 이미지 1장 / 3장 등록 + 이미지 서빙(nosniff) + 로컬 파일 실존
  (c) 입력 거부 — 4장·2MB 초과·요청 전체 초과·위장 확장자·빈 파일·필드 위반
  (d) 권한 — 익명 조회 ok / 익명 등록 401 / CSRF 누락 403 / 타인 삭제 403 /
      본인 삭제 200 (메타 CASCADE + 로컬 이미지 디렉토리 정리)
  (e) 이미지 격리 — 타 VOC image_id·형식 위반 id 404
  (f) 페이지네이션 — 25건 → 20+5 최신순
  (g) 감사 로그 voc_create/voc_delete
  (h) XSS 페이로드 원문 무변조 (렌더는 프론트 textContent 책임)

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import io
import os
import shutil
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


def _list(ua_user=None, limit=20, offset=0):
    h = {"User-Agent": f"Mozilla/5.0 HoneyUser/{ua_user}"} if ua_user else {}
    r = client.get(f"/pe/report/api/voc?limit={limit}&offset={offset}", headers=h)
    assert r.status_code == 200, r.data
    return r.get_json()


def _delete(voc_id, ua_user="tester", csrf=True):
    return client.delete(f"/pe/report/api/voc/{voc_id}", headers=_headers(ua_user, csrf))


def _local_img_dir(voc_id):
    return Path(os.environ["REPORT_UPLOAD_DIR"]) / "note_img" / f"voc_{voc_id}"


# ── (a) 스키마 반복 생성 멱등 ─────────────────────────────────────────────────
with voc_db.open_conn() as conn:
    pass
with voc_db.open_conn() as conn:
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
assert {"report_voc", "report_voc_image"} <= names, names
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

data = _list(ua_user="tester")
by_id = {it["id"]: it for it in data["items"]}
assert len(by_id[text_only_id]["screenshots"]) == 0
assert len(by_id[one_img_id]["screenshots"]) == 1
assert len(by_id[three_img_id]["screenshots"]) == 3
assert by_id[one_img_id]["can_delete"] is True
assert by_id[one_img_id]["user_id"] == "tester"

for shot in by_id[three_img_id]["screenshots"]:
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
assert data["user"] == ""
assert all(it["can_delete"] is False for it in data["items"])
r = _post(ua_user=None)                                       # 익명 등록
assert r.status_code == 401, (r.status_code, r.data)
r = _post(csrf=False)                                         # CSRF 누락
assert r.status_code == 403, (r.status_code, r.data)
r = _delete(one_img_id, csrf=False)
assert r.status_code == 403, (r.status_code, r.data)
r = _delete(one_img_id, ua_user=None)                         # 익명 삭제
assert r.status_code == 401, (r.status_code, r.data)
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
print("(d) 익명/CSRF/타인/본인 삭제 + 정리 ok")

# ── (e) 이미지 격리 ─────────────────────────────────────────────────────────
one_shot = _list()["items"]
one_shot = {it["id"]: it for it in one_shot}[one_img_id]["screenshots"][0]
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
print("(f) 페이지네이션 20+나머지 최신순 ok")

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
it = {i["id"]: i for i in _list()["items"]}[xss_id]
assert it["title"] == payload and it["content"] == payload
print("(h) XSS 원문 무변조 ok")

# 페이지 자체 서빙 확인
r = client.get("/pe/report/voc")
assert r.status_code == 200 and b"VOC" in r.data
print("페이지 서빙 ok")

print("\n전체 통과")
shutil.rmtree(_TMP, ignore_errors=True)

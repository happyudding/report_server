"""VOC 등록이 메인 report.db 쓰기 경합에 발목 잡히지 않는지 검증.

배경: VOC 본문은 별도 voc.db 에 저장하지만 감사 로그는 메인 report.db 에 기록한다.
report.db 가 업로드/편집으로 쓰기 잠금 중이면, 감사 로그 INSERT 가 busy_timeout 만큼
대기하며 VOC 응답을 붙잡을 수 있다. _audit_voc 는 busy_timeout_ms=100 을 써서 100ms 안에
못 쓰면 포기하도록 돼 있다 — 이 테스트가 그 동작을 고정한다.

실행:
    python tests/test_voc_audit_busy.py

시나리오:
  (a) report.db 에 다른 커넥션이 BEGIN IMMEDIATE 로 쓰기 잠금을 건 상태에서 VOC 등록
      → ① voc.db 에 본문 정상 저장 ② 응답 0.5초 이내 ③ 감사 로그는 누락(best-effort 포기)
  (b) 잠금 해제 후 등록하면 감사 로그가 정상 기록 (대조)

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="voc_busy_test_"))
os.environ["REPORT_VOC_DB_PATH"] = str(_TMP / "voc" / "voc.db")
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")          # 감사 로그용 메인 DB
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""                             # 로컬 폴백

from flask import Flask  # noqa: E402

import config  # noqa: E402
from report.report_extension import report_bp  # noqa: E402
from database import report_db, voc_db  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()


def _csrf():
    client.get("/pe/report/voc")
    cookie = client.get_cookie("report_csrf")
    assert cookie is not None, "CSRF 쿠키가 발급되지 않음"
    return cookie.value


def _post_voc(title, content, ua_user="tester"):
    return client.post(
        "/pe/report/api/voc",
        data={"category": "버그", "title": title, "content": content},
        content_type="multipart/form-data",
        headers={"User-Agent": f"Mozilla/5.0 HoneyUser/{ua_user}",
                 "X-CSRF-Token": _csrf()},
    )


def _voc_create_count():
    return len(report_db.get_audit_logs(action="voc_create", limit=1000))


# ── (a) report.db 쓰기 잠금 중 VOC 등록 ────────────────────────────────────────
assert _voc_create_count() == 0, "사전 감사 로그가 비어 있어야 함"

# 별도 커넥션이 메인 DB 쓰기 잠금을 잡는다 (업로드/편집이 쓰는 중인 상황 재현).
# WAL 이라 읽기는 통과하지만, 감사 INSERT(쓰기)는 busy_timeout 까지 대기한다.
lock_conn = sqlite3.connect(str(config.REPORT_DB_PATH), timeout=30)
lock_conn.execute("PRAGMA busy_timeout = 30000")
lock_conn.execute("BEGIN IMMEDIATE")   # 쓰기 잠금 획득, commit 하지 않아 유지
try:
    t0 = time.perf_counter()
    r = _post_voc("잠금 중 등록", "본문")
    elapsed = time.perf_counter() - t0

    assert r.status_code == 201, (r.status_code, r.data)        # ① 응답 정상
    voc_id = r.get_json()["id"]
    assert elapsed < 0.5, f"응답이 0.5초를 초과: {elapsed:.3f}s"   # ② 빠른 응답

    saved = voc_db.get_voc(voc_id)                              # ③ 본문 저장
    assert saved is not None and saved["content"] == "본문", saved
finally:
    lock_conn.rollback()
    lock_conn.close()

# 잠금 때문에 감사 로그는 포기됐어야 한다 (best-effort — 100ms 초과 시 예외 무시).
assert _voc_create_count() == 0, "잠금 중이면 감사 로그는 기록되지 않아야 함"
print(f"(a) 잠금 중 VOC 등록: 응답 {elapsed*1000:.0f}ms < 500ms, 본문 저장 ok, 감사 누락 ok")

# ── (b) 잠금 해제 후 등록하면 감사 정상 (대조) ─────────────────────────────────
r = _post_voc("잠금 해제 후", "본문2")
assert r.status_code == 201, (r.status_code, r.data)
assert _voc_create_count() == 1, "잠금 없으면 감사 로그가 기록돼야 함"
print("(b) 잠금 해제 후 감사 로그 정상 기록 ok")

print("\n전체 통과")
shutil.rmtree(_TMP, ignore_errors=True)

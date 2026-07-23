"""세션 메타 수정(PATCH /session/<sid>/meta)이 값·기준정보·권한 계약을 지키는지.

실행:
    python tests/test_session_meta.py

Honey 세션 페이지의 ✏️ 버튼 → Honey 편집창 → 이 라우트다. 여기서 고정하는 계약:

  (a) X-Honey-Agent 헤더가 없으면 403 — "수정은 Honey 에서만" 을 서버가 강제한다
  (b) 신원 없음 401 / 남의 세션 403 (_editor_guard)
  (c) 세션 이름(file_name)·Product·LOT·Process·Family 가 세션 행에 반영된다
  (d) Product 를 바꾸면 product_info.db 를 다시 lookup 해 기준정보 14컬럼이 갱신되고,
      **미등록 Part ID 면 비워진다** (옛 제품 값이 남으면 상단바가 틀린 정보를 보여준다)
  (e) 이름 빈 값·Product/LOT 빈 값은 400, 경로문자는 제거된다
  (f) analysis_key 는 재산출하지 않는다 (산출물이 그 키로 저장돼 있음 — 불변 규칙 #3)
  (g) 감사로그에 edit 1행

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="session_meta_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["PRODUCT_INFO_DB_PATH"] = str(_TMP / "product_info.db")

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp  # noqa: E402

SID = "s-meta-test"
AKEY = "e" * 64
USER = "tester"
OTHER = "someone-else"


def _make_product_info_db():
    """서버가 읽기 전용으로 여는 기준정보 DB — AB123 만 등록해 둔다."""
    cols = ["part_id", "sub_part_id", "product_group", "wf_size", "chip_size_x",
            "chip_size_y", "gross_die", "pkg_type", "e2f_fab_site", "step",
            "temperature", "equip", "para", "flat_zone"]
    conn = sqlite3.connect(os.environ["PRODUCT_INFO_DB_PATH"])
    conn.execute("CREATE TABLE report_product_info (row_no INTEGER NOT NULL, %s)"
                 % ", ".join(f"{c} TEXT" for c in cols))
    conn.execute(
        "INSERT INTO report_product_info (row_no, %s) VALUES (%s)"
        % (", ".join(cols), ", ".join("?" * (len(cols) + 1))),
        [1, "AB123", "", "GRP", "12inch", "5.2", "4.8", "1200", "QFN", "FAB1", "S1",
         "25", "{6,6,6}", "{4,4}", "UP"])
    conn.commit()
    conn.close()


_make_product_info_db()

app = Flask(__name__)
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()


def _headers(user=USER, honey=True):
    h = {"User-Agent": f"Mozilla/5.0 HoneyUser/{user}"} if user else {}
    if honey:
        h["X-Honey-Agent"] = "1"
    return h


def _patch(payload, user=USER, honey=True):
    return client.patch(f"/pe/report/session/{SID}/meta", json=payload,
                        headers=_headers(user, honey))


def _reset_session():
    """기준정보가 이미 채워진 세션으로 초기화 (Product 변경 시 갱신/비움 확인용)."""
    report_db.delete_session(SID)
    report_db.create_session(
        SID, "old_name", None, product_type="MDDI", lot_id="LOT-OLD", product="OLD1",
        process="PROC-OLD", family_product="MX", source="web_report", uploaded_by=USER,
        product_info={"part_id": "OLD1", "wf_size": "8inch", "gross_die": "999",
                      "pkg_type": "OLDPKG"})
    report_db.update_session(SID, analysis_key=AKEY, status="done")


BASE = {"file_name": "7월 2차 재측정", "family_product": "AQUA", "product": "AB123",
        "lot_id": "LOT-NEW", "process": "PROC-NEW"}

# ── (a) Honey 헤더 강제 ───────────────────────────────────────────────────────
_reset_session()
r = _patch(BASE, honey=False)
assert r.status_code == 403, ("X-Honey-Agent 없이 통과", r.status_code)
assert report_db.get_session(SID)["file_name"] == "old_name"

# ── (b) 신원/권한 ────────────────────────────────────────────────────────────
r = _patch(BASE, user="")
assert r.status_code == 401, ("신원 없이 통과", r.status_code, r.data[:200])
r = _patch(BASE, user=OTHER)
assert r.status_code == 403, ("남의 세션 수정 통과", r.status_code, r.data[:200])

# ── (e) 값 검증 ──────────────────────────────────────────────────────────────
r = _patch({**BASE, "file_name": "   "})
assert r.status_code == 400, ("빈 이름 통과", r.status_code)
r = _patch({**BASE, "product": ""})
assert r.status_code == 400, ("빈 Product 통과", r.status_code)
r = _patch({**BASE, "lot_id": ""})
assert r.status_code == 400, ("빈 LOT 통과", r.status_code)

# ── (c)(d)(f) 정상 수정 ──────────────────────────────────────────────────────
r = _patch(BASE)
assert r.status_code == 200, (r.status_code, r.data[:300])
body = r.get_json()
assert set(body["changed"]) == {"file_name", "family_product", "product", "lot_id",
                                "process"}, body["changed"]

s = report_db.get_session(SID)
assert s["file_name"] == "7월 2차 재측정", s["file_name"]      # 한글 이름 보존
assert (s["product"], s["lot_id"], s["process"], s["family_product"]) == \
       ("AB123", "LOT-NEW", "PROC-NEW", "AQUA"), dict(s)
assert s["product_type"] == "MDDI", "product_type 은 편집 대상이 아니다"
assert s["analysis_key"] == AKEY, "analysis_key 는 재산출하지 않는다 (규칙 #3)"
# 등록 Part ID → 기준정보 갱신. 중괄호 다중값은 첫 값만.
assert (s["wf_size"], s["gross_die"], s["pkg_type"]) == ("12inch", "1200", "QFN"), dict(s)
assert (s["equip"], s["para"], s["flat_zone"]) == ("6", "4", "UP"), dict(s)

# ── (d) 미등록 Part ID → 기준정보 비움 ───────────────────────────────────────
r = _patch({**BASE, "product": "NOPE999"})
assert r.status_code == 200, (r.status_code, r.data[:300])
s = report_db.get_session(SID)
assert s["product"] == "NOPE999"
for col in ("wf_size", "gross_die", "pkg_type", "equip", "para", "flat_zone"):
    assert not s[col], (f"미등록 Part ID 인데 {col} 이 남았다", s[col])

# ── (e) 경로/제어문자 제거 ───────────────────────────────────────────────────
r = _patch({**BASE, "file_name": 'a/b\\c:d*e?f"g<h>i|j'})
assert r.status_code == 200, (r.status_code, r.data[:300])
assert report_db.get_session(SID)["file_name"] == "abcdefghij", \
    report_db.get_session(SID)["file_name"]

# ── 바뀐 게 없으면 changed 가 빈 목록 ────────────────────────────────────────
cur = report_db.get_session(SID)
r = _patch({"file_name": cur["file_name"], "family_product": cur["family_product"],
            "product": cur["product"], "lot_id": cur["lot_id"], "process": cur["process"]})
assert r.status_code == 200 and r.get_json()["changed"] == [], r.get_json()

# ── (g) 감사로그 ─────────────────────────────────────────────────────────────
logs = [x for x in report_db.get_audit_logs(limit=50)
        if x["session_id"] == SID and str(x["changed_fields"] or "").startswith("meta:")]
assert logs, "감사로그에 meta 편집 기록이 없다"
assert all(x["action"] == "edit" for x in logs), logs[0]

# ── 폴백 안내 페이지 (가드 없는 브라우저가 액션 URL 로 실제 이동했을 때) ─────
r = client.get(f"/pe/report/honey/session_meta/{SID}")
assert r.status_code == 200 and "Honey" in r.get_data(as_text=True), r.status_code

print("OK - session meta 수정 계약 통과")

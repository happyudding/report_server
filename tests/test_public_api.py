"""공개 REST API(/pe/api/v1) 검증 — product_info 조회.

실행:
    python tests/test_public_api.py

시나리오:
  (a) DB 파일 부재 → candidates 빈 목록 200 (예외 아님)
  (b) product_info.db 시드 후 candidates — part_id + sub_part_id flatten, 정렬·중복제거
  (c) lookup — 존재 part_id 200 + 14컬럼 / sub_part_id 도 같은 행 / 미존재 404 /
      part_id 누락 400
  (d) report_bp 와 동시 등록해도 URL 충돌 없음 + 기존 /pe/report/api/part_ids 무변경

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
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

_TMP = Path(tempfile.mkdtemp(prefix="public_api_test_"))
_PI_DB = _TMP / "product_info.db"
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["PRODUCT_INFO_DB_PATH"] = str(_PI_DB)

from flask import Flask  # noqa: E402

from product_info import INFO_COLUMNS  # noqa: E402
from public_api import URL_PREFIX, register_public_api  # noqa: E402
from report.report_extension import report_bp  # noqa: E402  (충돌 확인용 동시 등록)
from database import report_db  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
register_public_api(app)
report_db.init_report_db()
client = app.test_client()

API = URL_PREFIX


def seed_product_info():
    """임포터와 같은 스키마로 product_info.db 생성 (tools/product_info_import 참조)."""
    cols = ",\n    ".join(f"{c} TEXT" for c in INFO_COLUMNS)
    conn = sqlite3.connect(str(_PI_DB))
    try:
        conn.executescript(
            f"CREATE TABLE report_product_info (row_no INTEGER NOT NULL,\n    {cols});\n"
            "CREATE TABLE report_product_info_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
        rows = [
            # part_id, sub_part_id, product_group, wf_size, x, y, gross_die, pkg,
            # fab, step, temp, equip, para, flat_zone
            (1, "ABC123", "{ABC123-1, ABC123-2}", "MDDI", "12", "5.12", "4.08",
             "1234", "COF", "FAB1", "S1", "25", "{6,6,6}", "{2,2}", "{0,0}"),
            (2, "XYZ999", "", "PDDI", "8", "3.00", "3.00",
             "555", "BGA", "FAB2", "S2", "85", "4", "1", "90"),
        ]
        conn.executemany(
            "INSERT INTO report_product_info (row_no, %s) VALUES (%s)"
            % (", ".join(INFO_COLUMNS), ", ".join("?" * (len(INFO_COLUMNS) + 1))),
            rows)
        conn.commit()
    finally:
        conn.close()


def check(label, cond):
    assert cond, f"FAIL: {label}"
    print(f"  ok  {label}")


# ── (a) DB 부재 ───────────────────────────────────────────────────────────────
print("(a) product_info.db 부재")
r = client.get(f"{API}/product-info/candidates")
check("candidates 200", r.status_code == 200)
check("빈 목록", r.get_json() == {"candidates": [], "count": 0})

r = client.get(f"{API}/product-info/lookup?part_id=ABC123")
check("lookup 404", r.status_code == 404 and r.get_json() == {"error": "not_found"})

# ── (b) 시드 후 candidates ────────────────────────────────────────────────────
print("(b) candidates")
seed_product_info()
r = client.get(f"{API}/product-info/candidates")
body = r.get_json()
check("200", r.status_code == 200)
check("part_id + sub_part_id flatten 정렬",
      body["candidates"] == ["ABC123", "ABC123-1", "ABC123-2", "XYZ999"])
check("count 일치", body["count"] == len(body["candidates"]))

# ── (c) lookup ───────────────────────────────────────────────────────────────
print("(c) lookup")
r = client.get(f"{API}/product-info/lookup?part_id=ABC123")
info = r.get_json()
check("200", r.status_code == 200)
check("14컬럼 전부", set(info) == set(INFO_COLUMNS))
check("product_group", info["product_group"] == "MDDI")
check("중괄호 첫값만 (equip)", info["equip"] == "6")

r2 = client.get(f"{API}/product-info/lookup?part_id=ABC123-2")
check("sub_part_id 도 같은 행", r2.status_code == 200 and r2.get_json() == info)

r = client.get(f"{API}/product-info/lookup?part_id=NOPE")
check("미존재 404", r.status_code == 404 and r.get_json()["error"] == "not_found")

r = client.get(f"{API}/product-info/lookup")
check("파라미터 누락 400",
      r.status_code == 400 and r.get_json()["error"] == "bad_request")

r = client.get(f"{API}/product-info/lookup?part_id=%20%20")
check("공백만 400", r.status_code == 400)

# ── (d) 기존 라우트 무변경 ────────────────────────────────────────────────────
print("(d) 기존 라우트 공존")
r = client.get("/pe/report/api/part_ids")
check("/pe/report/api/part_ids 무변경",
      r.status_code == 200 and r.get_json()["part_ids"] == body["candidates"])

rules = {str(rule) for rule in app.url_map.iter_rules()}
check("v1 라우트 2개 등록",
      f"{API}/product-info/candidates" in rules
      and f"{API}/product-info/lookup" in rules)

print("\nALL PASS")

"""Honey 'DB Input' 라우트 — POST /pe/report/api/eval/labels_import.

실행:
    python tests/test_eval_db_input.py

관리자 Eval DB 탭의 CSV 내보내기(GET /api/eval/labels.csv)의 반대 방향이다. 고정하는 계약:

  (a) X-Honey-Agent 없으면 403 / Honey 신원 없으면 401 (브라우저 차단)
  (b) mode 불량·파일 없음·빈 파일 400, 5MB 초과 413
  (c) mode=validate 는 **DB 를 만들지도 열지도 않는다** (dry-run 증명)
  (d) mode=commit 이 실제로 적재되고 응답에 서버 내부 경로(db_path)가 새지 않는다
  (e) 같은 파일명으로 재적재해도 case/label 은 1건씩 유지되고 ingest_run 도 늘지 않는다
      (staged CSV 경로가 파일명 기반 고정이라는 결정을 고정)
  (f) 모르는 unit(dB)은 200 + ok=false + errors, DB 무변경 (부분 적재 없음)
  (g) 레거시 20컬럼은 쓰기 전에 거부 (부분 적재 위험 회피)
  (h) 감사 로그 action=eval_db_input 이 시도마다 1행, client_user 가 채워진다
  (i) 관리자 CSV 내보내기 → 되돌려 올리기 왕복이 성립한다

⚠ 이 테스트는 실제로 import_csv.py 를 subprocess 로 띄운다 — pyyaml 이 필요하다
  (tests/test_eval_export.py 와 같은 조건).

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
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

_TMP = Path(tempfile.mkdtemp(prefix="eval_db_input_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")
os.environ["REPORT_S3_BUCKET"] = ""

from flask import Flask  # noqa: E402

from admin_panel import eval_admin  # noqa: E402
from database import report_db  # noqa: E402
from report.report_extension import report_bp  # noqa: E402
from web_report import eval_export  # noqa: E402

USER = "tester"
HEADER = "Product type,Family Product,unit,Item,comment\n"
GOOD = HEADER + ("PMIC,SOC,VOLTS,VREF_TRIM,전압 마진 부족\n"
                 "PMIC,SOC,HERTZ,OSC_FREQ,주파수 산포 큼\n"
                 "MDDI,MX,PCT,LEAK_RATIO,비율 항목\n")

app = Flask(__name__)
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()


def _post(body, mode="validate", user=USER, honey=True, name="labels.csv"):
    headers = {"User-Agent": f"Mozilla/5.0 HoneyUser/{user}"} if user else {}
    if honey:
        headers["X-Honey-Agent"] = "1"
    data = body if isinstance(body, bytes) else body.encode("utf-8")
    return client.post(
        "/pe/report/api/eval/labels_import",
        data={"mode": mode, "file": (io.BytesIO(data), name)},
        content_type="multipart/form-data", headers=headers)


def _label_count():
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return 0
    try:
        return conn.execute("SELECT COUNT(*) FROM label").fetchone()[0]
    finally:
        conn.close()


def _table_count(table):
    conn = eval_export.open_conn(create=False)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _audits():
    return report_db.get_audit_logs(action="eval_db_input", limit=100)


# ── (a) 가드 ─────────────────────────────────────────────────────────────────
r = _post(GOOD, honey=False)
assert r.status_code == 403, ("X-Honey-Agent 없이 통과", r.status_code)
r = _post(GOOD, user=None)
assert r.status_code == 401, ("신원 없이 통과", r.status_code)
assert eval_export.open_conn(create=False) is None, "가드 단계에서 eval DB 가 생성됐다"

# ── (b) 입력 검증 ────────────────────────────────────────────────────────────
assert _post(GOOD, mode="bogus").status_code == 400
r = client.post("/pe/report/api/eval/labels_import", data={"mode": "validate"},
                content_type="multipart/form-data",
                headers={"User-Agent": f"HoneyUser/{USER}", "X-Honey-Agent": "1"})
assert r.status_code == 400, ("파일 없음이 통과", r.status_code)
assert _post("").status_code == 400, "빈 파일이 통과"
assert _post(b"x" * (5 * 1024 * 1024 + 1)).status_code == 413, "5MB 초과가 통과"

# ── (c) validate 는 DB 를 열지 않는다 ────────────────────────────────────────
r = _post(GOOD, mode="validate")
assert r.status_code == 200, r.status_code
body = r.get_json()
assert body["ok"] and body["mode"] == "validate" and body["format"] == "simple"
assert body["rows"] == 3, body
assert body["groups"] == [
    {"product_type": "MDDI", "family_product": "MX", "rows": 1},
    {"product_type": "PMIC", "family_product": "SOC", "rows": 2},
], body["groups"]
assert body["file_name"] == "labels.csv"
assert eval_export.open_conn(create=False) is None, "dry-run 이 eval DB 를 만들었다"

# ── (d) commit ───────────────────────────────────────────────────────────────
r = _post(GOOD, mode="commit")
assert r.status_code == 200, r.status_code
body = r.get_json()
assert body["ok"] and body["mode"] == "commit" and body["rows"] == 3, body
assert "db_path" not in body, "서버 내부 경로가 응답에 샜다"
assert all("db_path" not in g for g in body["groups"]), "group 에 db_path 가 샜다"
assert all(g.get("session_id") is None for g in body["groups"]), body["groups"]
assert _label_count() == 3, _label_count()
assert eval_admin.list_labels()["total"] == 3
# % 가 value_type 으로 저장된다 (db_input 전용 확장 어휘)
conn = eval_export.open_conn(create=False)
try:
    vts = {r[0] for r in conn.execute("SELECT value_type FROM item_master")}
finally:
    conn.close()
assert vts == {"V", "Hz", "%"}, vts

# ── (e) 같은 파일명 재적재 = 갱신, ingest_run 증식 없음 ──────────────────────
runs_before = _table_count("ingest_run")
r = _post(GOOD.replace("전압 마진 부족", "수정된 코멘트"), mode="commit")
assert r.get_json()["ok"], r.get_json()
assert _label_count() == 3, ("재적재로 label 이 늘었다", _label_count())
assert _table_count("ingest_run") == runs_before, "재적재로 ingest_run 이 늘었다"
conn = eval_export.open_conn(create=False)
try:
    comments = {c[0] for c in conn.execute("SELECT human_comment FROM label")}
finally:
    conn.close()
assert "수정된 코멘트" in comments, comments

# ── (f) 모르는 unit ──────────────────────────────────────────────────────────
before = _label_count()
r = _post(HEADER + "PMIC,SOC,V,OK_ITEM,정상\nPMIC,SOC,dB,GAIN_TEST,모르는 단위\n",
          mode="commit", name="bad.csv")
assert r.status_code == 200, r.status_code
body = r.get_json()
assert not body["ok"] and body["errors"], body
assert any("dB" in e for e in body["errors"]), body["errors"]
assert _label_count() == before, "오류 CSV 인데 일부가 적재됐다"

# ── (g) 레거시 20컬럼 거부 ───────────────────────────────────────────────────
legacy = ("product_name,product_type,family_product,item_name,value_type,bin\n"
          "S5E_TEST_1,PMIC,SOC,VREF_TRIM,V,18\n")
r = _post(legacy, mode="commit", name="legacy.csv")
assert r.status_code == 200, r.status_code
body = r.get_json()
assert not body["ok"] and body["format"] == "legacy", body
assert "단순 5컬럼" in body["errors"][0], body["errors"]
assert _label_count() == before, "레거시 CSV 가 적재됐다"

# ── (h) 감사 ─────────────────────────────────────────────────────────────────
logs = _audits()
# 여기까지 라우트 본문에 도달한 시도 5건 (validate 1 + commit 2 + 오류 CSV 1 + 레거시 1).
# 가드에서 끊긴 403/401/400/413 은 감사하지 않는다.
assert len(logs) == 5, ("시도마다 감사 1행이 아니다", len(logs))
assert all(l["client_user"] == USER for l in logs), [l["client_user"] for l in logs]
assert any(l["result"] == "error" for l in logs), "실패 시도가 ok 로 기록됐다"
assert any(l["result"] == "ok" and "mode=commit" in (l["changed_fields"] or "")
           for l in logs), "성공 commit 감사가 없다"

# ── (i) 관리자 CSV 왕복 ──────────────────────────────────────────────────────
exported = "".join(eval_admin.labels_csv_iter())
r = _post(exported.encode("utf-8-sig") if not exported.startswith("﻿")
          else exported, mode="commit", name="roundtrip.csv")
assert r.status_code == 200, r.status_code
assert r.get_json()["ok"], r.get_json()
assert _label_count() == 3, ("왕복으로 라벨이 늘었다", _label_count())

# staged CSV 는 남지 않는다
staged = Path(os.environ["REPORT_UPLOAD_DIR"]) / "eval_input"
assert not list(staged.glob("*.csv")), list(staged.glob("*.csv"))

shutil.rmtree(_TMP, ignore_errors=True)
print("OK - DB Input 라우트: 가드 / 검증 dry-run / 적재 / 재적재 / 오류격리 / 감사 / 왕복")

"""Temperature 세션 서버 왕복 검증 — TEMP 편집 반영 + temp_map 라우트/캐시 (2026-08-05).

실행:
    python tests/test_temperature_session_e2e.py

고정하는 계약:
  (a) 업로드 → /full 에 sheets["Issue Table Temp"] 가 실리고, Yield 시트는 RT 소스만 갖는다.
  (b) TEMP 행 편집(comment / Status / 행 숨김)이 저장되고 재조회 payload 에 반영된다
      — Issue Table 과 **같은 채널**(report_webreport_edit, kind 공유, 키 접두 TEMP|).
  (c) report 콜드 빌드가 temp_map 을 미리 채운다(seed_temp_map) — 라우트가 재계산 없이
      캐시/디스크에서 응답한다. 캐시를 전부 비워도 디스크에서 복구된다.
  (d) temp_map 인덱스가 map_analysis 의 dies 배열과 정합한다(n == len(dies)).
  (e) 비 Temperature 세션은 temp_map 이 빈 목록이고 Temp 시트도 비어 있다.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert). Excel/Qt 불필요.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="wr_temp_e2e_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 인라인 — 테스트 결정성

from database import report_db  # noqa: E402
from web_report import cache as wr_cache  # noqa: E402
from web_report import ingest as wr_ingest  # noqa: E402
from web_report import service as wr_service  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)

from flask import Flask  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()

UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])
ITEMS = ["ItemA", "ItemB", "ItemC"]
N_ROWS = 24

_failures = []


def check(ok, label):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        _failures.append(label)


def make_parquet(fails=None) -> bytes:
    """RT limit [0,10] 3항목. fails={item: 이탈 die 수} 이면 앞에서부터 15 로 채운다."""
    fails = fails or {}
    rows = []
    meta = {"TSEQ": [1, 2, 3], "TNO": [100, 200, 300], "STEP": ["P1"] * 3,
            "UNIT": ["V"] * 3, "HILIM": [10] * 3, "LOLIM": [0] * 3}
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row[META_COLUMNS[0]] = label
        for j, it in enumerate(ITEMS):
            row[it] = meta[label][j]
        rows.append(row)
    for i in range(N_ROWS):
        row = {c: "" for c in META_COLUMNS}
        row.update({"SERIAL": f"S{i}", "SHOT": 1, "DUT": 1,
                    "XPOS": i % 6, "YPOS": i // 6, "BIN": "1", "FAILTNO": ""})
        for it in ITEMS:
            row[it] = 15 if i < int(fails.get(it, 0)) else 5
        rows.append(row)
    return encode_honeyform_parquet(pd.DataFrame(rows, columns=META_COLUMNS + ITEMS))


def create_session(mode="Temperature"):
    files = [
        {"name": "W_RT", "filename": "rt.parquet", "data": make_parquet()},
        {"name": "W_CT", "filename": "ct.parquet",
         "data": make_parquet({"ItemA": 6, "ItemC": 4})},
        {"name": "W_HT", "filename": "ht.parquet",
         "data": make_parquet({"ItemA": 3, "ItemB": 2})},
    ]
    options = {}
    if mode == "Temperature":
        options["temperature"] = {"groups": [{"rt": "W_RT", "members": ["W_CT", "W_HT"],
                                              "member_roles": ["CT", "HT"]}]}
    manifest = {
        "meta": {"product_type": "PMIC", "product": "P1", "lot_id": "L1"},
        "mode": mode,
        "sources": [{"name": f["name"], "file_name": f["filename"]} for f in files],
        "selected_items": [],
        "options": options,
        "client": {"user": "tester", "host": "testhost"},
    }
    res = wr_ingest.ingest_webreport(
        manifest, files, report_db=report_db, upload_root=UPLOAD_ROOT,
        client_ip="127.0.0.1", user_agent="Mozilla/5.0 HoneyUser/tester")
    return res["session_id"]


def load(session_id):
    _sess, report = wr_service.load_webreport(
        session_id, report_db=report_db, upload_root=UPLOAD_ROOT)
    return report


def temp_rows(report):
    return [r for r in (report["sheets"].get("Issue Table Temp") or []) if r.get("Item")]


def clear_ram_caches():
    for name in dir(wr_cache):
        obj = getattr(wr_cache, name)
        if name.endswith("_CACHE") and hasattr(obj, "clear"):
            obj.clear()


# ── (a) 업로드 → Temp 시트 + Yield RT-only ────────────────────────────────────
sid = create_session()
report = load(sid)
rows = temp_rows(report)
by_item = {r["Item"]: r for r in rows}
check(sorted(by_item) == ["ItemA", "ItemB", "ItemC"],
      f"(a) Temp 시트에 이탈 항목 전부: {sorted(by_item)}")
check(by_item["ItemA"]["W_CT_yield"] == 25.0 and by_item["ItemA"]["W_HT_yield"] == 12.5,
      f"(a) 소스별 fail% (CT 6/24, HT 3/24): {by_item['ItemA']}")
pass_row = report["sheets"]["Yield"][0]
check("W_CT_yield" not in pass_row and "W_RT_yield" in pass_row,
      "(a) Yield 시트는 RT 소스만 (CT/HT 컬럼 부재)")
check([r["Category"] for r in report["sheets"]["Issue Table"] if r.get("Category")]
      == ["Yield", "CPK", "ETC"],
      "(a) Issue Table 에 TEMP 섹션 없음 (별도 시트로 분리)")

# ── (b) TEMP 편집 왕복 — comment / Status / 숨김 ──────────────────────────────
wr_service.update_issue_comments(
    sid, [{"key": "TEMP|ItemA", "col": "PTE comment", "value": "CT 확인 필요"}],
    report_db=report_db, upload_root=UPLOAD_ROOT,
    client_ip="127.0.0.1", user_agent="HoneyUser/tester")
wr_service.update_issue_status(
    sid, report_db=report_db, upload_root=UPLOAD_ROOT,
    key="TEMP|ItemA", value="Close",
    client_ip="127.0.0.1", user_agent="HoneyUser/tester")
wr_service.update_issue_hidden(
    sid, report_db=report_db, upload_root=UPLOAD_ROOT,
    action="hide", key="TEMP|ItemB",
    client_ip="127.0.0.1", user_agent="HoneyUser/tester")
after = {r["Item"]: r for r in temp_rows(load(sid))}
check(after.get("ItemA", {}).get("PTE comment") == "CT 확인 필요",
      f"(b) TEMP comment 저장·반영: {after.get('ItemA', {}).get('PTE comment')!r}")
check(after.get("ItemA", {}).get("Status") == "Close",
      f"(b) TEMP Status 저장·반영: {after.get('ItemA', {}).get('Status')!r}")
check("ItemB" not in after, f"(b) TEMP 행 숨김 반영: {sorted(after)}")
# 저장 채널이 Issue Table 과 같은 테이블·kind 인지 (접두만 다름)
saved = report_db.get_webreport_edits(sid)
kinds = {(k, key.split("\x1f")[0]) for k, key, _v in
         [(e["kind"], e["item_key"], e["value"]) for e in saved]}
check(("issue_comment", "TEMP|ItemA") in kinds and ("issue_status", "TEMP|ItemA") in kinds
      and ("issue_hidden", "TEMP|ItemB") in kinds,
      f"(b) 기존 Issue 편집과 동일 kind 로 저장: {sorted(k for k in kinds if 'TEMP' in k[1])}")
# 복원 — 이후 검증에 영향 없게
wr_service.update_issue_hidden(
    sid, report_db=report_db, upload_root=UPLOAD_ROOT, action="reset_all",
    client_ip="127.0.0.1", user_agent="HoneyUser/tester")
check("ItemB" in {r["Item"] for r in temp_rows(load(sid))}, "(b) reset_all 로 숨김 복원")

# ── (c) 콜드 빌드가 temp_map 을 시딩 / 디스크 복구 ────────────────────────────
sid2 = create_session()
load(sid2)                                   # 콜드 빌드 1회 (여기서 시딩)
session = report_db.get_session(sid2)
key = wr_service.cache_policy.temp_map_key(session, "")
check(wr_cache.cache_get(wr_cache.TEMP_MAP_CACHE, key) is not None,
      "(c) report 콜드 빌드가 TEMP_MAP_CACHE 를 미리 채움")
from web_report import disk_cache as wr_disk  # noqa: E402
check(wr_disk.load_temp_map(UPLOAD_ROOT, key) is not None,
      "(c) 디스크 캐시에도 저장됨 (재시작 후 콜드 없음)")

clear_ram_caches()                           # 서버 재시작 상당
blob = wr_service.get_temp_map_gzip(sid2, report_db=report_db, upload_root=UPLOAD_ROOT)
import gzip  # noqa: E402
payload = json.loads(gzip.decompress(blob))
check(payload.get("format") == "temp-map-v1" and payload.get("sources"),
      f"(c) 캐시 전멸 후에도 디스크에서 응답: {payload.get('format')}")

# ── (d) 인덱스 ↔ map dies 정합 ───────────────────────────────────────────────
maps = wr_service.get_map_analysis(sid2, report_db=report_db,
                                   upload_root=UPLOAD_ROOT)["maps"]
dies_of = {m["source"]: len(m["dies"]) for m in maps}
ok = all(p["n"] == dies_of.get(p["source"]) for p in payload["sources"])
check(ok, f"(d) temp_map n == map dies 길이: "
          f"{[(p['source'], p['n'], dies_of.get(p['source'])) for p in payload['sources']]}")
idx_items = {e["item"] for p in payload["sources"] for e in p["items"]}
check(idx_items == {"ItemA", "ItemB", "ItemC"}, f"(d) 항목별 인덱스 존재: {sorted(idx_items)}")

# ── (e) 비 Temperature 세션 ──────────────────────────────────────────────────
sid3 = create_session(mode="Normal")
normal = load(sid3)
check(normal["sheets"].get("Issue Table Temp") == [],
      "(e) Normal 세션은 Temp 시트가 빈 배열")
empty = wr_service.get_temp_map(sid3, report_db=report_db, upload_root=UPLOAD_ROOT)
check(empty == {"format": "temp-map-v1", "sources": []},
      f"(e) Normal 세션 temp_map 은 빈 목록: {empty}")
check("W_CT_yield" in normal["sheets"]["Yield"][0],
      "(e) Normal 은 종전대로 전 소스 Yield")

print()
if _failures:
    print(f"FAILED {len(_failures)}건: {_failures}")
    sys.exit(1)
print("ALL PASS")

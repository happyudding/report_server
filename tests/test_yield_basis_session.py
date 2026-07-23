"""수율 분모 기준(Gross Die ↔ Test data 개수) 세션 저장·적용 E2E.

실행:
    python tests/test_yield_basis_session.py

Honey 의 Rawdata 허브 체크박스("Yield 계산 기준 - Test data 개수")가 보내는 값이
세션 편집 DB 에 남고, /full payload 의 수율 분모에 그대로 반영되는지를 고정한다:

  (a) 기본(옵션 미저장) = 제품 기준정보 Gross Die 분모
  (b) 체크(basis=test) 저장 → rawdata 개수 분모, 캐시를 전부 비우고 다시 열어도 유지
  (c) 해제(basis=gross) 저장 → (a) 의 payload 와 정준 JSON 완전 일치 (되돌리기)
  (d) Gross Die 가 없는 세션은 옵션과 무관하게 rawdata 분모(폴백)

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="yield_basis_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""             # S3 비활성 → 로컬 폴백
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 워커 오프로드 없이 인라인 계산

import pandas as pd  # noqa: E402
from flask import Flask  # noqa: E402

import storage_gateway  # noqa: E402
from database import report_db  # noqa: E402
from report.report_extension import report_bp  # noqa: E402
from web_report import cache as wr_cache  # noqa: E402
from web_report import edits as wr_edits  # noqa: E402
from web_report.honeyform import META_COLUMNS, encode_honeyform_parquet  # noqa: E402
from web_report.validation import canon  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()

USER = "tester"
UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])
GROSS_DIE = "40"     # 측정 die 20개 → Gross Die 분모면 수율이 절반


def _make_parquet():
    """측정 die 20개 = pass 18 + ItemA fail 1 + ItemB fail 1."""
    cols = META_COLUMNS + ["ItemA", "ItemB"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P2"],
        ["UNIT", "", "", "", "", "", "", "V", "V"],
        ["HILIM", "", "", "", "", "", "", 12, 12],
        ["LOLIM", "", "", "", "", "", "", 8, 8],
    ]
    for i in range(20):
        a, b, bin_code, failtno = 10 + (i % 5) * 0.1, 10 + (i % 7) * 0.1, 1, ""
        if i == 18:
            a, bin_code, failtno = 11.9, 5, 100
        if i == 19:
            b, bin_code, failtno = 11.9, 6, 200
        rows.append([f"s{i}", 1, 1, i % 5, i // 5, bin_code, failtno, a, b])
    return encode_honeyform_parquet(pd.DataFrame(rows, columns=cols))


def _setup(sid, akey, product_info):
    blob = _make_parquet()
    chash = hashlib.sha256(canon({"files": [hashlib.sha256(blob).hexdigest()]})).hexdigest()
    report_db.create_session(sid, "x.parquet", None, product_type="MDDI", lot_id="LOT1",
                             product="P1", source="web_report", uploaded_by=USER,
                             product_info=product_info)
    report_db.update_session(sid, analysis_key=akey, content_hash=chash, status="done")
    storage_gateway.save_webreport_sources(
        akey, chash, [blob],
        {"sources": [{"name": "Lot1", "file_name": "lot1.csv"}],
         "selected_items": [], "mode": "Normal"},
        upload_root=UPLOAD_ROOT)


def _headers(sid):
    client.get(f"/pe/report/view/{sid}")     # after_request 가 CSRF 쿠키 발급
    cookie = client.get_cookie("report_csrf")
    return {"User-Agent": f"Mozilla/5.0 HoneyUser/{USER}",
            "X-CSRF-Token": cookie.value if cookie else ""}


def _full(sid):
    import gzip
    import time

    for _ in range(20):
        r = client.get(f"/pe/report/session/{sid}/full", headers=_headers(sid))
        if r.status_code == 200:
            body = r.data if r.headers.get("Content-Encoding") != "gzip" \
                else gzip.decompress(r.data)
            return json.loads(body)["web_report"]
        time.sleep(0.3)
    raise AssertionError(f"/full 이 200 이 아님: {r.status_code} {r.data[:200]}")


def _get_opts(sid):
    r = client.get(f"/pe/report/session/{sid}/web_report/preprocess", headers=_headers(sid))
    assert r.status_code == 200, (r.status_code, r.data[:200])
    return r.get_json()


def _save_opts(sid, body):
    r = client.post(f"/pe/report/session/{sid}/web_report/preprocess",
                    json=body, headers=_headers(sid))
    assert r.status_code == 200, (r.status_code, r.data[:200])
    return r.get_json()


def _clear_all_caches(akey):
    wr_cache.invalidate_caches(akey)
    for sub in ("report", "dist", "map"):
        shutil.rmtree(UPLOAD_ROOT / "web_report" / akey / sub, ignore_errors=True)
    shutil.rmtree(UPLOAD_ROOT / "web_report" / "_cache", ignore_errors=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    sid, akey = "s-basis", "a" * 64
    _setup(sid, akey, {"part_id": "P1", "gross_die": GROSS_DIE})

    # (a) 기본 = Gross Die 분모
    gross = _full(sid)
    ov = gross["yield_summary"]
    assert gross["yield_basis"] == {"basis": "gross", "gross_die": 40}, gross["yield_basis"]
    assert (ov["total"], ov["tested"], ov["pass"], ov["yield_pct"]) == (40, 20, 18, 45.0), ov
    assert _get_opts(sid)["yield_basis"] == "gross", _get_opts(sid)
    assert _get_opts(sid)["gross_die"] == 40, _get_opts(sid)
    gross_canon = json.dumps(gross, sort_keys=True, ensure_ascii=False, default=str)
    print(f"(a) 기본 — Gross Die {GROSS_DIE} 분모, 수율 {ov['yield_pct']}% (측정 {ov['tested']})")

    # (b) 체크(Test data 개수) → rawdata 분모 + 세션 재오픈에도 유지
    result = _save_opts(sid, {"exclude_items": [], "yield_basis": "test"})
    assert result["yield_basis"] == "test", result
    test_basis = _full(sid)
    ov2 = test_basis["yield_summary"]
    assert test_basis["yield_basis"] == {"basis": "test", "gross_die": None}, test_basis["yield_basis"]
    assert (ov2["total"], ov2["tested"], ov2["yield_pct"]) == (20, 20, 90.0), ov2
    _clear_all_caches(akey)
    assert _full(sid)["yield_summary"]["yield_pct"] == 90.0, "캐시 비운 뒤 옵션이 사라졌다"
    assert wr_edits.load_yield_basis(report_db, sid) == "test"
    print(f"(b) 체크 — rawdata 분모, 수율 {ov2['yield_pct']}% (캐시 비운 뒤에도 유지)")

    # (c) 해제 → (a) 와 정준 JSON 완전 일치
    _save_opts(sid, {"exclude_items": [], "yield_basis": "gross"})
    _clear_all_caches(akey)
    restored = _full(sid)
    assert json.dumps(restored, sort_keys=True, ensure_ascii=False, default=str) == gross_canon, \
        "해제 후 Gross Die 기준 payload 로 돌아오지 않음"
    print("(c) 해제 — 원래(Gross Die 기준) payload 와 정준 JSON 완전 일치")

    # (d) Gross Die 없는 세션 = 옵션과 무관하게 rawdata 폴백
    sid2, akey2 = "s-nogross", "b" * 64
    _setup(sid2, akey2, None)
    nog = _full(sid2)
    assert nog["yield_basis"] == {"basis": "test", "gross_die": None}, nog["yield_basis"]
    assert nog["yield_summary"]["yield_pct"] == 90.0, nog["yield_summary"]
    assert _get_opts(sid2)["gross_die"] is None, _get_opts(sid2)
    print("(d) Gross Die 없는 세션 — rawdata 분모로 폴백")

    print("\nPASS — 분모 기준 저장·적용·되돌리기·폴백")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

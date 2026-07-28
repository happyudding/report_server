"""빠른 수정(셀 패치)·조건 일괄 규칙 저장 E2E — 라우트 → 세션 편집 DB → /full 반영.

실행:
    python tests/test_preprocess_patch_e2e.py

원본 parquet 을 바꾸지 않는 "패치 계층"이 실제로 리포트에 반영되고 되돌려지는지,
그리고 저장 계약(merge 의미론·검증·상한)이 지켜지는지를 고정한다:

  (a) 셀 패치 저장 → 수율·CPK 가 그 값 기준으로 바뀌고 원본 content_hash 는 불변
  (b) 조건 일괄 규칙(BIN ∉ [1] → die 제외 = 'Bin1 only') 저장 → 수율 100%
  (c) merge 의미론 — 구버전 허브처럼 edits/rules 없이 저장해도 패치가 유지된다
      (빈 리스트를 명시하면 해제)
  (d) 검증 — 없는 source/잘못된 값/적중 0 규칙/전멸 규칙은 400 이고 아무것도 저장 안 됨
  (e) 되돌리기 — 전부 해제하면 최초 payload 와 정준 JSON 완전 일치
  (f) 원본 수정(웹 셀 편집)이 들어오면 셀 패치만 해제되고 규칙은 남는다

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

_TMP = Path(tempfile.mkdtemp(prefix="prep_patch_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""              # S3 비활성 → 로컬 폴백
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
SID, AKEY = "s-patch", "a" * 64
SOURCE = "Lot1"


def _make_parquet():
    """측정 die 20개 = pass 18 + ItemA fail 1(BIN 5) + ItemB fail 1(BIN 6)."""
    cols = META_COLUMNS + ["ItemA", "ItemB"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P1"],
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


def _setup():
    blob = _make_parquet()
    chash = hashlib.sha256(canon({"files": [hashlib.sha256(blob).hexdigest()]})).hexdigest()
    report_db.create_session(SID, "x.parquet", None, product_type="MDDI", lot_id="LOT1",
                             product="P1", source="web_report", uploaded_by=USER)
    report_db.update_session(SID, analysis_key=AKEY, content_hash=chash, status="done")
    storage_gateway.save_webreport_sources(
        AKEY, chash, [blob],
        {"sources": [{"name": SOURCE, "file_name": "lot1.csv"}],
         "selected_items": [], "mode": "Normal"},
        upload_root=UPLOAD_ROOT)
    return chash


def _headers():
    client.get(f"/pe/report/view/{SID}")     # after_request 가 CSRF 쿠키 발급
    cookie = client.get_cookie("report_csrf")
    return {"User-Agent": f"Mozilla/5.0 HoneyUser/{USER}",
            "X-CSRF-Token": cookie.value if cookie else ""}


def _full():
    import gzip
    import time

    for _ in range(20):
        r = client.get(f"/pe/report/session/{SID}/full", headers=_headers())
        if r.status_code == 200:
            body = r.data if r.headers.get("Content-Encoding") != "gzip" \
                else gzip.decompress(r.data)
            return json.loads(body)["web_report"]
        time.sleep(0.3)
    raise AssertionError(f"/full 이 200 이 아님: {r.status_code} {r.data[:200]}")


def _save(body):
    return client.post(f"/pe/report/session/{SID}/web_report/preprocess",
                       json=body, headers=_headers())


def _save_ok(body):
    r = _save(body)
    assert r.status_code == 200, (r.status_code, r.data[:300])
    return r.get_json()


def _save_400(body):
    r = _save(body)
    assert r.status_code == 400, f"400 이 아님: {r.status_code} {r.data[:300]}"
    return (r.get_json() or {}).get("error", "")


def _spec():
    r = client.get(f"/pe/report/session/{SID}/web_report/preprocess", headers=_headers())
    assert r.status_code == 200
    return r.get_json()["spec"]


def _chash():
    return report_db.get_session(SID).get("content_hash")


def _clear_all_caches():
    wr_cache.invalidate_caches(AKEY)
    for sub in ("report", "dist", "map"):
        shutil.rmtree(UPLOAD_ROOT / "web_report" / AKEY / sub, ignore_errors=True)
    shutil.rmtree(UPLOAD_ROOT / "web_report" / "_cache", ignore_errors=True)


_BIN1_RULE = {"where": {"conds": [{"field": "BIN", "op": "not_in", "values": ["1"]}]},
              "action": {"op": "exclude_rows"}}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    chash0 = _setup()
    base = _full()
    base_canon = json.dumps(base, sort_keys=True, ensure_ascii=False, default=str)
    ov = base["yield_summary"]
    assert (ov["tested"], ov["pass"], ov["yield_pct"]) == (20, 18, 90.0), ov
    print(f"(0) 최초 — 측정 {ov['tested']}, pass {ov['pass']}, 수율 {ov['yield_pct']}%")

    # (a) 셀 패치 — fail die 2건의 BIN 을 1(pass)로 고치면 수율이 100% 가 된다.
    #     원본 parquet 은 그대로여야 한다(content_hash 불변 = Excel export ETag 캐시 유효).
    result = _save_ok({"edits": [
        {"source": SOURCE, "row_idx": 18, "column": "BIN", "value": "1"},
        {"source": SOURCE, "row_idx": 19, "column": "BIN", "value": "1"},
    ]})
    assert result["stats"]["edited_cells"] == 2, result
    patched = _full()
    ov2 = patched["yield_summary"]
    assert (ov2["tested"], ov2["pass"], ov2["yield_pct"]) == (20, 20, 100.0), ov2
    assert _chash() == chash0, "패치가 원본 content_hash 를 바꿨다 (원본 불변 위반)"
    _clear_all_caches()
    assert _full()["yield_summary"]["yield_pct"] == 100.0, "캐시 비운 뒤 패치가 사라졌다"
    print(f"(a) 셀 패치 2건 — 수율 {ov2['yield_pct']}%, content_hash 불변, 캐시 비워도 유지")

    # (b) 조건 일괄 규칙 — Bin1 only (fail die 제외). 셀 패치는 위에서 이미 BIN 을 1로
    #     바꿨으므로, 먼저 패치를 해제해 원래 fail die 2개가 살아 있는 상태로 확인한다.
    _save_ok({"edits": [], "rules": [_BIN1_RULE]})
    ruled = _full()
    ov3 = ruled["yield_summary"]
    assert (ov3["tested"], ov3["pass"], ov3["yield_pct"]) == (18, 18, 100.0), ov3
    assert _spec()["rules"] and "edits" not in _spec(), _spec()
    print(f"(b) Bin1 only 규칙 — 측정 {ov3['tested']}(fail 2 제외), 수율 {ov3['yield_pct']}%")

    # (c) merge 의미론 — 구버전 허브처럼 edits/rules 키 없이 저장해도 규칙이 살아 있어야 한다.
    _save_ok({"exclude_items": [], "yield_basis": "test"})
    assert _spec().get("rules"), "구클라 저장이 규칙을 지웠다 (merge 의미론 위반)"
    #     빈 리스트를 명시하면 해제된다.
    _save_ok({"rules": []})
    assert not _spec().get("rules"), "빈 리스트로 해제되지 않았다"
    print("(c) merge — 키 부재=유지 / 빈 리스트=해제")

    # (d) 검증 — 저장 시점에 400 으로 막고 아무것도 남기지 않는다.
    before = _spec()
    cases = [
        ("없는 source", {"edits": [{"source": "nope", "row_idx": 0,
                                    "column": "ItemA", "value": "1"}]}),
        ("범위 밖 행", {"edits": [{"source": SOURCE, "row_idx": 999,
                                   "column": "ItemA", "value": "1"}]}),
        ("잘못된 값", {"edits": [{"source": SOURCE, "row_idx": 0,
                                  "column": "ItemA", "value": "abc"}]}),
        ("BIN 빈값", {"edits": [{"source": SOURCE, "row_idx": 0,
                                 "column": "BIN", "value": ""}]}),
        ("적중 0 규칙", {"rules": [{"where": {"conds": [
            {"field": "BIN", "op": "in", "values": ["9999"]}]},
            "action": {"op": "exclude_rows"}}]}),
        ("die 전멸", {"rules": [{"where": {"conds": [
            {"field": "SERIAL", "op": "not_in", "values": ["없는값"]}]},
            "action": {"op": "exclude_rows"}}]}),
    ]
    for label, body in cases:
        err = _save_400(body)
        assert err, label
        print(f"    · {label} → 400 ({err[:40]}…)")
    assert _spec() == before, "400 인데 spec 이 바뀌었다"
    print("(d) 검증 — 6종 모두 400, spec 무변경")

    # (e) 되돌리기 — 전부 해제하면 최초 payload 와 정준 JSON 완전 일치
    _save_ok({"exclude_items": [], "edits": [], "rules": [], "yield_basis": "gross"})
    _clear_all_caches()
    restored = _full()
    assert json.dumps(restored, sort_keys=True, ensure_ascii=False,
                      default=str) == base_canon, "해제 후 최초 payload 로 안 돌아옴"
    print("(e) 전체 해제 — 최초 payload 와 정준 JSON 완전 일치")

    # (f) 원본 수정(웹 셀 편집)이 들어오면 셀 패치만 해제되고 규칙은 남는다.
    _save_ok({"edits": [{"source": SOURCE, "row_idx": 0, "column": "ItemA", "value": "9.5"}],
              "rules": [_BIN1_RULE]})
    assert _spec().get("edits") and _spec().get("rules")
    r = client.post(f"/pe/report/session/{SID}/web_report/raw_data/edit",
                    json={"edits": [{"source": SOURCE, "row_idx": 1,
                                     "column": "ItemA", "value": "10.5"}]},
                    headers=_headers())
    assert r.status_code == 200, (r.status_code, r.data[:300])
    after = _spec()
    assert "edits" not in after, f"원본 수정 후에도 셀 패치가 남았다: {after}"
    assert after.get("rules"), "조건 규칙까지 지워졌다 (조건 기반이라 유지돼야 함)"
    assert _chash() != chash0, "원본 수정인데 content_hash 가 그대로다"
    assert wr_edits.load_preprocess(report_db, SID).get("rules"), "DB 에도 규칙이 남아야 한다"
    print("(f) 원본 수정 — 셀 패치만 해제, 규칙 유지, content_hash 갱신")

    print("\nPASS — 패치 계층 저장·적용·merge·검증·되돌리기·원본충돌")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

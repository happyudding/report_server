"""조회 전처리(Item Select / Outlier)가 **전 탭에 반영**되고 세션 재오픈에도 유지되는지 E2E.

실행:
    python tests/test_preprocess_tabs.py

Honey 의 Rawdata 허브에서 항목을 빼거나 outlier 를 걸면, 원본 parquet 은 그대로 두고
Summary/Yield/CPK/Issue Table/Distribution/Trim/Map 이 그 기준으로 다시 계산돼야 한다.
여기서 고정하는 계약:

  (a) 제외 항목은 CPK·Distribution 에서 사라진다
  (b) **Yield 표는 그대로 유지된다** — 제외한 항목의 fail die 도 BIN 상으로는 여전히
      fail 이라, 표에서 빼면 행 합(90+5+5=100%)과 수율이 어긋난다. 제외는 "그 항목을
      분석에서 뺀다"이지 "그 die 를 없앤다"가 아니다 (selected_items 필터와 같은 의미론).
  (c) outlier 마스킹은 측정값만 바꾸므로 수율·행 수가 불변이고 CPK/분포만 달라진다
  (d) 필터는 세션 편집 DB 에 있어 **캐시를 전부 비우고 다시 열어도** 그대로 적용된다
  (e) 해제하면 원래 payload 와 정준 JSON 이 완전히 일치한다 (되돌리기)

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

_TMP = Path(tempfile.mkdtemp(prefix="prep_tabs_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""            # S3 비활성 → 로컬 폴백
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"  # 워커 오프로드 없이 인라인 계산

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

SID = "s-prep-tabs"
AKEY = "d" * 64
USER = "tester"
UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])


def _make_parquet():
    """ItemA/ItemB 각각 fail die 1개 + ItemA 에 극단값 1개(outlier 대상)."""
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
        if i == 18:                       # ItemA fail (BIN 5)
            a, bin_code, failtno = 100.0, 5, 100
        if i == 19:                       # ItemB fail (BIN 6)
            b, bin_code, failtno = 100.0, 6, 200
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
        {"sources": [{"name": "Lot1", "file_name": "lot1.csv"}],
         "selected_items": [], "mode": "Normal"},
        upload_root=UPLOAD_ROOT)


def _headers():
    client.get(f"/pe/report/view/{SID}")     # after_request 가 CSRF 쿠키 발급
    cookie = client.get_cookie("report_csrf")
    return {"User-Agent": f"Mozilla/5.0 HoneyUser/{USER}",
            "X-CSRF-Token": cookie.value if cookie else ""}


def _full():
    """/full payload (콜드면 202 → 백그라운드 빌드 후 재요청)."""
    import gzip
    import time

    for _ in range(20):
        r = client.get(f"/pe/report/session/{SID}/full", headers=_headers())
        if r.status_code == 200:
            body = r.data if r.headers.get("Content-Encoding") != "gzip" \
                else gzip.decompress(r.data)
            return json.loads(body)
        time.sleep(0.3)
    raise AssertionError(f"/full 이 200 이 아님: {r.status_code} {r.data[:200]}")


def _save_spec(spec):
    r = client.post(f"/pe/report/session/{SID}/web_report/preprocess",
                    json=spec, headers=_headers())
    assert r.status_code == 200, (r.status_code, r.data[:200])
    return r.get_json()


def _sheet_json(payload, name):
    return json.dumps(payload["web_report"]["sheets"].get(name), ensure_ascii=False,
                      sort_keys=True, default=str)


def _dist_items(payload):
    return [r["subject"] for r in payload["web_report"].get("distribution_index") or []]


def _canon_report(payload):
    """계산 콘텐츠 정준화 — 되돌리기 비교용."""
    return json.dumps(payload["web_report"], sort_keys=True, ensure_ascii=False, default=str)


def _clear_all_caches():
    """세션을 '다시 여는' 상황 — RAM + 디스크 캐시를 전부 버리고 DB 만 남긴다."""
    wr_cache.invalidate_caches(AKEY)
    for sub in ("report", "dist", "map"):
        shutil.rmtree(UPLOAD_ROOT / "web_report" / AKEY / sub, ignore_errors=True)
    shutil.rmtree(UPLOAD_ROOT / "web_report" / "_cache", ignore_errors=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    _setup()
    base = _full()
    base_canon = _canon_report(base)
    assert _dist_items(base) == ["ItemA", "ItemB"], _dist_items(base)
    assert base["preprocess"]["summary"] == "", base["preprocess"]
    yield_before = _sheet_json(base, "Yield")
    cpk_before = _sheet_json(base, "CPK")
    assert "ItemB" in cpk_before and "ItemB" in yield_before
    print(f"(a) 기준 payload — dist {_dist_items(base)}, "
          f"수율 {base['web_report']['yield_summary']['yield_pct']}%")

    # ── 항목 제외 ────────────────────────────────────────────────────────────
    result = _save_spec({"exclude_items": ["ItemB"]})
    assert result["summary"], result
    after = _full()
    assert _dist_items(after) == ["ItemA"], _dist_items(after)
    assert "ItemB" not in _sheet_json(after, "CPK"), "CPK 에 제외 항목이 남았다"
    assert after["preprocess"]["summary"], "배지용 summary 가 비었다"
    # Yield 는 그대로 — 제외 항목의 fail die 도 여전히 fail 이다 (표 합 = 수율)
    assert _sheet_json(after, "Yield") == yield_before, "제외가 Yield 표를 바꿨다 (행 합 불일치)"
    assert after["web_report"]["yield_summary"] == base["web_report"]["yield_summary"]
    for tab in ("Summary", "Issue Table", "Trim Analysis", "Map Analysis"):
        assert tab in after["web_report"]["sheets"], f"{tab} 시트가 사라졌다"
    print(f"(b) 제외 후 — dist {_dist_items(after)}, CPK 에서 ItemB 제거, Yield 표 불변")

    # ── 세션 다시 열기 (캐시 전부 버림 → DB 의 spec 으로 재계산) ──────────────
    _clear_all_caches()
    reopened = _full()
    assert _dist_items(reopened) == ["ItemA"], _dist_items(reopened)
    assert reopened["preprocess"]["spec"] == {"exclude_items": ["ItemB"]}, reopened["preprocess"]
    assert wr_edits.load_preprocess(report_db, SID) == {"exclude_items": ["ItemB"]}
    print("(c) 캐시 전부 비우고 재조회 — 필터가 DB 에서 복원됨")

    # ── outlier 추가 ─────────────────────────────────────────────────────────
    _save_spec({"exclude_items": ["ItemB"], "outlier": {"mode": "stdev", "k": 2}})
    masked = _full()
    # 측정값만 결측 → 수율·행 수 불변, CPK 통계만 변화
    assert masked["web_report"]["yield_summary"] == base["web_report"]["yield_summary"], \
        "outlier 제거가 수율을 바꿨다"
    assert _sheet_json(masked, "Yield") == yield_before, "outlier 제거가 Yield 표를 바꿨다"
    assert _sheet_json(masked, "CPK") != _sheet_json(after, "CPK"), "outlier 가 CPK 에 반영 안 됨"
    print("(d) outlier k=2 — 수율·Yield 표 불변, CPK 만 변화")

    # ── 해제 → 원래대로 ──────────────────────────────────────────────────────
    _save_spec({})
    _clear_all_caches()
    restored = _full()
    assert restored["preprocess"]["summary"] == ""
    assert _canon_report(restored) == base_canon, "해제 후 원래 payload 로 돌아오지 않음"
    print("(e) 해제 — 원래 payload 와 정준 JSON 완전 일치")

    print("\nPASS — 전 탭 재계산 + 세션 재오픈 유지 + 되돌리기")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

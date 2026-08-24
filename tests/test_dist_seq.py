"""Distribution "Serial 순"(rawdata 누적 순) 배치 응답 계약 스모크.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_dist_seq.py

왜 이 파일이 필요한가 (2026-08-24 신설):
    이 기능이 깨지는 방식은 전부 **조용하다** — 화면에는 점이 그대로 찍히고 순서만 틀린다.
      · 서버가 값을 정렬해 보내면(또는 pack 지름길을 타면) 사용자는 "rawdata 순"이라 믿고
        정렬된 그림을 본다. 순서 보존이 이 응답의 존재 이유다.
      · ECDF 배치와 캐시 키·ETag 를 공유하면 토글 직후 **서로의 304** 로 다른 축의 데이터가
        나간다.
      · Item_detail(scatter)과 값 집합이 갈리면 같은 항목이 갤러리와 상세에서 달라 보인다
        (CLAUDE.md 규칙 #13).

검증 항목:
  (a) order=seq → format "seq-columnar-v1", 요청한 항목만, **행 순서 그대로**
  (b) order 없음/ecdf → 종전 ECDF 응답 그대로 (회귀 방지)
  (c) 같은 항목 집합인데 **ETag 가 갈린다** (seq ↔ ecdf 교차 304 차단) + 각 변형은 304 재검증
  (d) order=bogus → 400 (조용한 ECDF 폴백 금지)
  (e) bin1=1 → 양품(BIN==1) & 규격내만, 그래도 **순서 보존**
  (f) seq 값 == /scatter values (Item_detail 과 같은 점 집합 — 규칙 #13)
  (g) 다운샘플 없음 — 전 행이 그대로 온다

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# config 는 import 시점에 env 를 읽는다 — 반드시 import 앞에서 지정할 것.
_TMP = Path(tempfile.mkdtemp(prefix="dist_seq_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import compute as wr_compute  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()

UA = {"User-Agent": "Mozilla/5.0 HoneyUser/owner"}
ITEMS = ["IT00", "IT01"]
SOURCES = ["WF1", "WF2"]
N_ROWS = 8

# 값을 **정렬과 다른 순서**로 박는 것이 이 테스트의 핵심이다 — 서버가 어디선가 정렬하면
# (또는 pack 지름길을 타면) 기대 배열과 즉시 어긋난다.
#   BIN: 인덱스 3,6 만 2(fail), 나머지 1(pass)  → bin1 필터 기대값이 결정적으로 갈린다
#   IT00 규격: -100 ~ 100 → 인덱스 5 의 500 은 **양품이지만 규격 밖**(bin1 에서 제외돼야 한다)
VALUES = {
    "WF1": {"IT00": [7, 3, 9, 1, 5, 500, 2, 8],
            "IT01": [20, 19, 18, 17, 16, 15, 14, 13]},
    "WF2": {"IT00": [-4, 6, -9, 0, 11, 3, -1, 2],
            "IT01": [1, 3, 2, 5, 4, 7, 6, 9]},
}
FAIL_ROWS = (3, 6)
LIMITS = {"IT00": (-100.0, 100.0), "IT01": (-1000.0, 1000.0)}


def make_parquet(source: str) -> bytes:
    """합성 7-meta honeyform → parquet bytes (계약 정본은 CLAUDE.md 규칙 #9)."""
    rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row[META_COLUMNS[0]] = label
        for j, it in enumerate(ITEMS):
            row[it] = {"TSEQ": j + 1, "TNO": 1000 + j, "STEP": "P1", "UNIT": "V",
                       "HILIM": LIMITS[it][1], "LOLIM": LIMITS[it][0]}[label]
        rows.append(row)
    for i in range(N_ROWS):
        bin_v = 2 if i in FAIL_ROWS else 1
        row = {"SERIAL": f"{source}-{i:03d}", "SHOT": 0, "DUT": 0,
               "XPOS": i + 1, "YPOS": 1, "BIN": bin_v,
               "FAILTNO": 1000 if bin_v == 2 else ""}
        for it in ITEMS:
            row[it] = float(VALUES[source][it][i])
        rows.append(row)
    df = pd.DataFrame(rows, columns=META_COLUMNS + list(ITEMS))
    return encode_honeyform_parquet(df)


def upload_session() -> str:
    manifest = {
        "meta": {"product_type": "MDDI", "product": "SMOKE", "lot_id": "DISTSEQ"},
        "mode": "Normal",
        "sources": [{"name": s, "file_name": f"{s}.csv"} for s in SOURCES],
        "selected_items": [],
        "client": {"user": "owner", "host": "smokehost"},
    }
    data = {"manifest": json.dumps(manifest)}
    for idx, src in enumerate(SOURCES):
        data[f"webreport_{idx}"] = (io.BytesIO(make_parquet(src)), f"{src}.csv")
    resp = client.post("/pe/report/upload_webreport", data=data, headers=UA,
                       content_type="multipart/form-data")
    assert resp.status_code == 200, \
        f"업로드 실패 {resp.status_code}: {resp.get_data(as_text=True)[:400]}"
    sid = resp.get_json()["session_id"]
    return sid


def get_full(sid: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/pe/report/session/{sid}/full", headers=UA)
        if resp.status_code == 200:
            return resp.get_json()
        time.sleep(0.1)
    raise AssertionError("/full 이 200 으로 돌아오지 않았다")


def batch(sid, subjects, order=None, bin1=False, headers=None):
    q = f"?subjects={','.join(subjects)}"
    if order is not None:
        q += f"&order={order}"
    if bin1:
        q += "&bin1=1"
    return client.get(f"/pe/report/session/{sid}/web_report/distribution_batch{q}",
                      headers={**UA, **(headers or {})})


def settle(timeout=120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)


# ── 검증 ─────────────────────────────────────────────────────────────────────

def test_seq_row_order(sid) -> None:
    """(a)(g) 행 순서 그대로 · 요청 항목만 · 전 행(다운샘플 없음)."""
    resp = batch(sid, ["IT00"], order="seq")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["format"] == "seq-columnar-v1", body.get("format")
    assert set(body["items"]) == {"IT00"}, list(body["items"])
    it = body["items"]["IT00"]
    assert (it["lo"], it["hi"]) == LIMITS["IT00"], it
    for src in SOURCES:
        got = it["sources"][src]["v"]
        want = [float(v) for v in VALUES[src]["IT00"]]
        assert got == want, f"{src} 순서/값 불일치\n  got  {got}\n  want {want}"
        assert len(got) == N_ROWS, f"{src} 행이 잘렸다 {len(got)} != {N_ROWS}"
    print("  [ok] Serial 순 = rawdata 행 순서 그대로 (전 행, 요청 항목만)")


def test_ecdf_unchanged(sid) -> None:
    """(b) order 미지정/ecdf 는 종전 ECDF 응답 — 회귀 방지."""
    for order in (None, "ecdf"):
        resp = batch(sid, ["IT00"], order=order)
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        body = resp.get_json()
        assert body["format"] == "ecdf-columnar-v1", (order, body.get("format"))
        xs = body["items"]["IT00"]["sources"]["WF1"]["x"]
        assert xs == sorted(xs), "ECDF x 가 오름차순이 아니다"
        assert body["items"]["IT00"]["sources"]["WF1"]["y"][-1] == 100.0, "누적 100% 아님"
    print("  [ok] order 미지정·ecdf → 종전 ECDF (오름차순·누적 100%)")


def test_etag_split(sid) -> None:
    """(c) **핵심** — seq ↔ ecdf ETag 가 갈리고, 각 변형은 자기 ETag 로 304."""
    e_seq = batch(sid, ["IT00", "IT01"], order="seq").headers["ETag"]
    e_ecdf = batch(sid, ["IT00", "IT01"]).headers["ETag"]
    assert e_seq and e_ecdf and e_seq != e_ecdf, f"ETag 가 같다: {e_seq}"
    r304 = batch(sid, ["IT00", "IT01"], order="seq",
                 headers={"If-None-Match": e_seq})
    assert r304.status_code == 304, r304.status_code
    # 교차: ecdf ETag 를 들고 seq 를 요청하면 304 가 아니어야 한다(옛 축 데이터 방지).
    rx = batch(sid, ["IT00", "IT01"], order="seq", headers={"If-None-Match": e_ecdf})
    assert rx.status_code == 200, f"교차 304 발생 — 캐시 키가 섞였다 ({rx.status_code})"
    assert rx.get_json()["format"] == "seq-columnar-v1"
    print("  [ok] ETag 분리 (자기 변형만 304, 교차는 200)")


def test_bad_order(sid) -> None:
    """(d) 알 수 없는 order 는 400 — 조용한 ECDF 폴백 금지."""
    resp = batch(sid, ["IT00"], order="serial")
    assert resp.status_code == 400, resp.status_code
    print("  [ok] order=serial(오타) → 400")


def test_seq_batch_cap(sid) -> None:
    """(d-2) seq 는 ECDF 보다 **작은 항목 수 상한**을 쓴다.

    seq 응답은 동일값을 접지 않아 항목당 payload 가 ECDF 의 한 자릿수 배 이상이다
    (5 source × 25,000 die 면 항목 1개가 125,000 값). ECDF 상한(40)을 그대로 쓰면 한 요청이
    수십 MB 가 되므로 seq 만 따로 자른다. 점을 버리는 게 아니라 요청을 나누는 것이라
    규칙 #5(다운샘플 금지)와 무관하다 — 프런트 DIST_BATCH.SEQ_SIZE 와 짝이다."""
    from report.routes_webreport import _DIST_BATCH_MAX, _DIST_SEQ_BATCH_MAX
    assert _DIST_SEQ_BATCH_MAX < _DIST_BATCH_MAX, "seq 상한이 ECDF 상한보다 작아야 합니다"
    many = [f"IT{i:02d}" for i in range(_DIST_SEQ_BATCH_MAX + 1)]
    assert batch(sid, many, order="seq").status_code == 400, "seq 상한 초과가 400 이 아닙니다"
    # 같은 개수라도 ECDF 는 종전 상한(40)까지 통과해야 한다 — 회귀 방지
    assert batch(sid, many).status_code == 200, "ECDF 배치 상한이 함께 줄었습니다(회귀)"
    print(f"  [ok] seq 배치 상한 {_DIST_SEQ_BATCH_MAX} 초과 → 400 (ECDF 는 무영향)")


def test_bin1_filter(sid) -> None:
    """(e) bin1 = 양품 & 규격내, 순서는 보존."""
    body = batch(sid, ["IT00"], order="seq", bin1=True).get_json()
    lo, hi = LIMITS["IT00"]
    for src in SOURCES:
        want = [float(v) for i, v in enumerate(VALUES[src]["IT00"])
                if i not in FAIL_ROWS and lo <= v <= hi]
        got = body["items"]["IT00"]["sources"][src]["v"]
        assert got == want, f"{src} bin1 불일치\n  got  {got}\n  want {want}"
    # WF1 인덱스5(500) 는 양품이지만 규격 밖 → 빠져야 한다
    assert 500.0 not in body["items"]["IT00"]["sources"]["WF1"]["v"], \
        "규격 밖 양품 die 가 bin1 에 남았다"
    print("  [ok] bin1 = 양품 ∩ 규격내, 순서 보존 (규격 밖 양품 제외)")


def test_matches_scatter(sid) -> None:
    """(f) **핵심** — seq 값 집합·순서가 /scatter(Item_detail)과 같다."""
    for bin1 in (False, True):
        seq = batch(sid, ["IT01"], order="seq", bin1=bin1).get_json()
        url = f"/pe/report/session/{sid}/web_report/scatter/IT01" + ("?bin1=1" if bin1 else "")
        sc = client.get(url, headers=UA)
        assert sc.status_code == 200, sc.status_code
        for s in sc.get_json()["sources"]:
            got = seq["items"]["IT01"]["sources"][s["name"]]["v"]
            assert got == s["values"], \
                f"bin1={bin1} {s['name']} 갤러리≠상세\n  seq {got}\n  sca {s['values']}"
    print("  [ok] seq == /scatter values (갤러리·상세 같은 점 집합, bin1 포함)")


def main():
    print("[dist_seq (Serial 순) 스모크]")
    try:
        sid = upload_session()
        get_full(sid)
        test_seq_row_order(sid)
        test_ecdf_unchanged(sid)
        test_etag_split(sid)
        test_bad_order(sid)
        test_seq_batch_cap(sid)
        test_bin1_filter(sid)
        test_matches_scatter(sid)
        settle()
    finally:
        try:
            settle(timeout=10)
        except Exception:
            pass
        shutil.rmtree(_TMP, ignore_errors=True)
    print("[통과] Serial 순 배치 응답 계약 정상")


if __name__ == "__main__":
    main()

"""대용량 대응 3종 라우트 검증 — Distribution 배치 / 콜드 202 / Map 경량 payload.

실행:
    python tests/test_webreport_batch_and_async.py

검증 항목:
  (a) GET .../web_report/distribution_batch?subjects=... — 요청 항목만 반환하고,
      전체 /distribution payload 에서 그 항목만 뽑은 것과 값이 완전히 같다.
  (b) subjects 파싱·상한·검증 (빈 값 400 / 41개 400 / 순서 무관 같은 ETag / 304)
  (c) bin1=1 변형이 전체 기준과 다른 캐시·다른 ETag 를 쓴다.
  (d) 콜드 미스 /full 이 202 {"building":true} 를 즉시 반환하고, 백그라운드 빌드가
      끝난 뒤 재요청하면 200 이 온다 (요청 스레드가 빌드를 기다리지 않는다).
  (e) Map: /full 의 Map Analysis 시트에 dies 가 없고(경량), 지연 라우트는 dies 를 준다.
      콜드 map 라우트도 202 → 완료 후 200.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# config 는 import 시점에 env 를 읽는다 — 반드시 import 앞에서 지정할 것.
_TMP = Path(tempfile.mkdtemp(prefix="wr_batch_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""              # S3 비활성 → 로컬 폴백
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 워커 오프로드 없이 인라인(테스트 결정성)

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import cache as wr_cache  # noqa: E402
from web_report import compute as wr_compute  # noqa: E402
from web_report import ingest as wr_ingest  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()

UA = {"User-Agent": "Mozilla/5.0 HoneyUser/tester"}
N_ITEMS = 5
N_ROWS = 30


def make_parquet(seed: int) -> bytes:
    """합성 7-meta honeyform → parquet bytes."""
    rng = np.random.default_rng(seed)
    items = [f"IT{j:02d}" for j in range(N_ITEMS)]
    rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row[META_COLUMNS[0]] = label
        for j, it in enumerate(items):
            row[it] = {"TSEQ": j + 1, "TNO": 1000 + j, "STEP": ("P1", "P2")[j % 2],
                       "UNIT": "V", "HILIM": 10.0, "LOLIM": -10.0}[label]
        rows.append(row)
    for i in range(N_ROWS):
        bin_v = 1 if i % 4 else 2
        row = {"SERIAL": f"S{i:04d}", "SHOT": 0, "DUT": 0,
               "XPOS": i % 6, "YPOS": i // 6, "BIN": bin_v,
               "FAILTNO": "" if bin_v == 1 else 1000 + (i % N_ITEMS)}
        for it in items:
            row[it] = round(float(rng.normal(0, 2)), 4)
        rows.append(row)
    df = pd.DataFrame(rows, columns=META_COLUMNS + items)
    return encode_honeyform_parquet(df)


def create_session() -> str:
    """web_report 세션 1건 ingest (업로드 라우트를 거치지 않는 직접 호출)."""
    files = [{"name": f"Lot{i}", "filename": f"Lot{i}.csv", "data": make_parquet(i)}
             for i in range(2)]
    manifest = {
        "meta": {"product_type": "MDDI", "product": "P1", "lot_id": "L1"},
        "mode": "Normal",
        "sources": [{"name": f"Lot{i}", "file_name": f"Lot{i}.csv"} for i in range(2)],
        "selected_items": [],
        "client": {"user": "tester", "host": "testhost"},
    }
    result = wr_ingest.ingest_webreport(
        manifest, files,
        report_db=report_db,
        upload_root=Path(os.environ["REPORT_UPLOAD_DIR"]),
        client_ip="127.0.0.1", user_agent="Mozilla/5.0 HoneyUser/tester",
    )
    return result["session_id"]


def settle(timeout=120) -> None:
    """백그라운드 빌드(업로드 프리웜 / 온디맨드)가 끝날 때까지 기다린다.

    안 기다리고 캐시를 비우면 그 직후 프리웜이 다시 채워 넣어 '콜드' 상태가 만들어지지
    않는다(테스트 플레이크의 원인).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("백그라운드 빌드가 끝나지 않음")


def drop_caches(session_id: str) -> None:
    """RAM+디스크 캐시를 비워 '진짜 콜드' 상태를 만든다."""
    settle()
    session = report_db.get_session(session_id)
    wr_cache.invalidate_caches(session.get("analysis_key"))
    root = Path(os.environ["REPORT_UPLOAD_DIR"]) / "web_report"
    for cache_dir in root.glob("*/cache"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def get_json(url, **kw):
    r = client.get(url, headers=UA, **kw)
    return r, (r.get_json() if r.data and r.status_code < 300 else None)


def wait_until_200(url, timeout=60):
    """202(building) 이 200 이 될 때까지 폴링 — 프런트 재시도 흐름 재현."""
    deadline = time.monotonic() + timeout
    polls = 0
    while time.monotonic() < deadline:
        r = client.get(url, headers=UA)
        if r.status_code == 200:
            return r, polls
        assert r.status_code == 202, f"예상 밖 상태 {r.status_code}: {r.data[:200]}"
        polls += 1
        # build_status 는 빌드 중에도 즉시 응답해야 한다(요청 스레드 비블록의 핵심)
        st = client.get(f"/pe/report/session/{SID}/web_report/build_status", headers=UA)
        assert st.status_code == 200, st.data
        time.sleep(0.1)
    raise AssertionError(f"{url} 이 {timeout}s 안에 200 이 되지 않음")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    global SID
    SID = create_session()
    base = f"/pe/report/session/{SID}/web_report"
    ok = 0

    # ── (d) 콜드 /full 이 202 를 즉시 반환하고, 완료 후 200 ────────────────────
    drop_caches(SID)
    t0 = time.monotonic()
    r = client.get(f"/pe/report/session/{SID}/full", headers=UA)
    first_elapsed = time.monotonic() - t0
    assert r.status_code == 202, f"콜드 /full 이 202 가 아님: {r.status_code}"
    body = r.get_json()
    assert body.get("building") is True, body
    r, polls = wait_until_200(f"/pe/report/session/{SID}/full")
    full = r.get_json()
    assert full.get("web_report"), "완료 후 /full 에 web_report 가 없음"
    print(f"(d) 콜드 /full: 202 즉시 반환({first_elapsed*1000:.0f}ms) → "
          f"{polls}회 폴링 후 200")
    ok += 1

    # warm 재요청은 202 가 아니라 바로 200
    r = client.get(f"/pe/report/session/{SID}/full", headers=UA)
    assert r.status_code == 200, f"warm /full 이 200 이 아님: {r.status_code}"

    # ── (e) Map: /full 은 경량, 지연 라우트는 dies 포함 ────────────────────────
    map_rows = full["web_report"]["sheets"]["Map Analysis"]
    assert map_rows, "Map Analysis 시트가 비어 있음"
    assert all("dies" not in row for row in map_rows), "/full 에 dies 가 실림(경량 위반)"
    assert all("bin_counts" in row and "total" in row for row in map_rows), \
        "/full 경량 메타에 total/bin_counts 가 없음"

    drop_caches(SID)
    r = client.get(f"{base}/map_analysis", headers=UA)
    assert r.status_code == 202, f"콜드 map 이 202 가 아님: {r.status_code}"
    r, polls = wait_until_200(f"{base}/map_analysis")
    maps = r.get_json()["maps"]
    assert all("dies" in m for m in maps), "지연 라우트에 dies 가 없음"
    assert len(maps) == len(map_rows), "경량/지연 맵 개수 불일치"
    # total 은 그 STEP 의 결과 die 수 — 앞 STEP 에서 이미 fail 한 회색 die({..,"g":1})는
    # 모양만 남기는 것이라 제외된다(기존 의미). 경량 경로가 이 셈을 바꾸지 않았는지 확인.
    for lean_row, full_row in zip(map_rows, maps):
        counted = sum(1 for d in full_row["dies"] if "g" not in d)
        assert lean_row["total"] == full_row["total"] == counted, (
            f"total 불일치: 경량={lean_row['total']} 지연={full_row['total']} "
            f"비회색die={counted}")
        assert lean_row["bin_counts"] == full_row["bin_counts"], "bin_counts 불일치"
    total_dies = sum(len(m["dies"]) for m in maps)
    print(f"(e) Map: /full 경량 {len(map_rows)}맵(dies 없음) / 지연 {total_dies}die, "
          f"콜드 202 → {polls}회 폴링 후 200")
    ok += 1

    # ── (a) 배치 == 전체 payload 의 부분집합 ──────────────────────────────────
    r, _ = get_json(f"{base}/distribution")
    assert r.status_code == 200, r.status_code
    full_dist = json.loads(r.data)
    all_subjects = sorted(full_dist["items"])
    picked = all_subjects[:3]

    r = client.get(f"{base}/distribution_batch?subjects={','.join(picked)}", headers=UA)
    assert r.status_code == 200, f"배치 요청 실패: {r.status_code} {r.data[:200]}"
    batch = json.loads(r.data)
    assert sorted(batch["items"]) == picked, f"항목 집합 불일치: {sorted(batch['items'])}"
    expect = {"format": full_dist["format"],
              "items": {k: full_dist["items"][k] for k in picked}}
    assert json.dumps(batch, sort_keys=True) == json.dumps(expect, sort_keys=True), \
        "배치 결과가 전체 payload 의 부분집합과 다르다"
    print(f"(a) 배치 {len(picked)}/{len(all_subjects)}항목 — 전체 payload 부분집합과 값 일치")
    ok += 1

    # ── (b) 파싱·상한·ETag ────────────────────────────────────────────────────
    assert client.get(f"{base}/distribution_batch", headers=UA).status_code == 400
    assert client.get(f"{base}/distribution_batch?subjects=", headers=UA).status_code == 400
    many = ",".join(f"X{i}" for i in range(41))
    assert client.get(f"{base}/distribution_batch?subjects={many}",
                      headers=UA).status_code == 400, "항목 수 상한 미적용"
    assert client.get(f"{base}/distribution_batch?subjects={'x' * 201}",
                      headers=UA).status_code == 400, "항목명 길이 상한 미적용"

    r1 = client.get(f"{base}/distribution_batch?subjects={','.join(picked)}", headers=UA)
    r2 = client.get(f"{base}/distribution_batch?subjects={','.join(reversed(picked))}",
                    headers=UA)
    assert r1.headers["ETag"] == r2.headers["ETag"], "순서만 다른 요청의 ETag 가 다름"
    r3 = client.get(f"{base}/distribution_batch?subjects={','.join(picked)}",
                    headers={**UA, "If-None-Match": r1.headers["ETag"]})
    assert r3.status_code == 304, f"조건부 요청이 304 가 아님: {r3.status_code}"
    # 미존재 항목은 400 이 아니라 빈 결과 (스크롤 중 필터로 사라진 항목 대비)
    r4 = client.get(f"{base}/distribution_batch?subjects=__nope__", headers=UA)
    assert r4.status_code == 200 and json.loads(r4.data)["items"] == {}, r4.data[:200]
    print("(b) subjects 파싱·상한(40개/200자)·순서 무관 ETag·304·미존재 항목 OK")
    ok += 1

    # ── (c) bin1 변형 분리 ────────────────────────────────────────────────────
    rb = client.get(f"{base}/distribution_batch?subjects={','.join(picked)}&bin1=1",
                    headers=UA)
    assert rb.status_code == 200, rb.data[:200]
    assert rb.headers["ETag"] != r1.headers["ETag"], "bin1 변형이 같은 ETag 를 씀"
    bin1 = json.loads(rb.data)
    assert sorted(bin1["items"]) == picked
    r, _ = get_json(f"{base}/distribution?bin1=1")
    full_bin1 = json.loads(r.data)
    expect_b = {"format": full_bin1["format"],
                "items": {k: full_bin1["items"][k] for k in picked}}
    assert json.dumps(bin1, sort_keys=True) == json.dumps(expect_b, sort_keys=True), \
        "bin1 배치가 bin1 전체 payload 의 부분집합과 다르다"
    n_all = sum(len(s["x"]) for it in batch["items"].values()
                for s in it["sources"].values())
    n_b1 = sum(len(s["x"]) for it in bin1["items"].values()
               for s in it["sources"].values())
    assert n_b1 < n_all, "bin1(양품만)이 전체와 포인트 수가 같다 — 필터 미적용 의심"
    print(f"(c) bin1 변형 분리 OK (전체 {n_all}pt vs 양품 {n_b1}pt, 별도 ETag)")
    ok += 1

    print(f"\n전체 통과: {ok}개 그룹")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

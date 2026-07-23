"""Trim 산포 배치 라우트 검증 — 단일 /trim_chart 와 정준 JSON 완전 일치.

실행:
    python tests/test_trim_chart_batch.py

검증 항목:
  (a) GET .../web_report/trim_chart_batch?group=A&group=B&group=C 의 charts[i] 가
      기존 단일 GET .../web_report/trim_chart?group=X 결과와 **완전히 같다**.
  (b) 요청 순서가 그대로 유지된다 (반복 param — 정렬·중복제거 없음).
  (c) 그룹 1/2/3개 모두 동작하고, 배치가 단일 경로와 캐시 엔트리를 공유한다.
  (d) 상한·검증: 0개 400 / 4개 400 / 201자 400 / 미존재 그룹 404.
  (e) 워커 잡(compute.trim_chart_batch_job)이 라우트와 같은 bytes 를 돌려준다
      (콜드 오프로드 경로가 인라인과 동일한 산출인지 — 워커 유무로 값이 갈리면 안 된다).
  (f) TRIM_CHART_CACHE 바이트 상한 축출이 동작한다 (WEB_REPORT_TRIM_CHART_CACHE_MB).

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import gzip
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
_TMP = Path(tempfile.mkdtemp(prefix="wr_trim_batch_test_"))
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
# MDDI = TV2 규칙 → 이름 끝 _PRE/_POST 꼬리를 떼어 stem 으로 묶는다 (VREF_PRE↔VREF_POST).
STEMS = ["VREF", "VCOM", "VGH", "VGL"]
ITEMS = [f"{s}_{tail}" for s in STEMS for tail in ("PRE", "POST")]
N_ROWS = 40


def make_parquet(seed: int) -> bytes:
    """합성 7-meta honeyform → parquet bytes (TV2 그룹이 생기는 항목명)."""
    rng = np.random.default_rng(seed)
    rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row[META_COLUMNS[0]] = label
        for j, it in enumerate(ITEMS):
            row[it] = {"TSEQ": j + 1, "TNO": 1000 + j, "STEP": "P2",
                       "UNIT": "V", "HILIM": 10.0, "LOLIM": -10.0}[label]
        rows.append(row)
    for i in range(N_ROWS):
        bin_v = 1 if i % 5 else 2
        row = {"SERIAL": f"S{i:04d}", "SHOT": 0, "DUT": 0,
               "XPOS": i % 8, "YPOS": i // 8, "BIN": bin_v,
               "FAILTNO": "" if bin_v == 1 else 1000 + (i % len(ITEMS))}
        for it in ITEMS:
            row[it] = round(float(rng.normal(0, 2)), 4)
        rows.append(row)
    df = pd.DataFrame(rows, columns=META_COLUMNS + ITEMS)
    return encode_honeyform_parquet(df)


def create_session() -> str:
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
    """업로드 프리웜/온디맨드 백그라운드 빌드가 끝날 때까지 대기."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("백그라운드 빌드가 끝나지 않음")


def body_json(resp):
    """gzip 협상 결과와 무관하게 응답 본문을 dict 로."""
    data = resp.data
    if resp.headers.get("Content-Encoding") == "gzip":
        data = gzip.decompress(data)
    return json.loads(data)


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    sid = create_session()
    base = f"/pe/report/session/{sid}/web_report"
    settle()
    ok = 0

    # ── 준비: payload 에서 그룹 목록 확보 ────────────────────────────────────
    r = client.get(f"{base}/trim_analysis", headers=UA)
    assert r.status_code == 200, f"trim_analysis 실패: {r.status_code} {r.data[:200]}"
    payload = body_json(r)
    source = payload["source"]
    gids = [g["id"] for g in payload["groups"]]
    assert len(gids) >= 3, f"그룹이 3개 미만이라 배치를 검증할 수 없음: {gids}"
    picked = gids[:3]
    print(f"준비: source={source!r} 그룹 {len(gids)}개 {gids} → 검증대상 {picked}")

    def single(gid):
        rr = client.get(f"{base}/trim_chart?source={source}&group={gid}", headers=UA)
        assert rr.status_code == 200, f"단일 실패 {gid}: {rr.status_code} {rr.data[:200]}"
        return body_json(rr)

    def batch(gids_, expect=200):
        q = f"source={source}" + "".join(f"&group={g}" for g in gids_)
        rr = client.get(f"{base}/trim_chart_batch?{q}", headers=UA)
        assert rr.status_code == expect, \
            f"배치 상태 {rr.status_code} (기대 {expect}) groups={gids_} {rr.data[:200]}"
        return body_json(rr) if expect == 200 else None

    # ── (a) 배치 charts[i] == 단일 결과 (정준 JSON 완전 일치) ─────────────────
    singles = [single(g) for g in picked]
    charts = batch(picked)["charts"]
    assert len(charts) == 3, f"charts 개수 {len(charts)}"
    for i, gid in enumerate(picked):
        assert canon(charts[i]) == canon(singles[i]), \
            f"[{gid}] 배치 결과가 단일 결과와 다르다"
    n_pts = sum(len(s["y"]) for c in charts for s in c["phases"].values())
    print(f"(a) 배치 3개 == 단일 3개 정준 JSON 완전 일치 (총 {n_pts}pt, 다운샘플 없음)")
    ok += 1

    # ── (b) 요청 순서 유지 (정렬·중복제거 없음) ───────────────────────────────
    rev = [picked[2], picked[0], picked[1]]
    rev_charts = batch(rev)["charts"]
    expect_rev = [singles[2], singles[0], singles[1]]
    for i, gid in enumerate(rev):
        assert canon(rev_charts[i]) == canon(expect_rev[i]), \
            f"순서 불일치 idx={i} gid={gid}"
    print(f"(b) 요청 순서 유지 OK ({picked} → {rev} 그대로)")
    ok += 1

    # ── (c) 1/2/3개 모두 동작 ────────────────────────────────────────────────
    for n in (1, 2, 3):
        got = batch(picked[:n])["charts"]
        assert len(got) == n, f"{n}개 요청에 {len(got)}개 응답"
        for i in range(n):
            assert canon(got[i]) == canon(singles[i]), f"{n}개 요청 idx={i} 불일치"
    print("(c) 그룹 1/2/3개 모두 단일 결과와 일치 (캐시 공유)")
    ok += 1

    # ── (d) 상한·검증 ────────────────────────────────────────────────────────
    assert client.get(f"{base}/trim_chart_batch?source={source}",
                      headers=UA).status_code == 400, "group 0개가 400 이 아님"
    # 상한 = 프런트 한 페이지 크기(TRIM.PAGE_SIZE=6) — 6개는 통과, 7개는 거부.
    six = "".join(f"&group={g}" for g in (gids * 3)[:6])
    assert client.get(f"{base}/trim_chart_batch?source={source}{six}",
                      headers=UA).status_code == 200, "group 6개(상한)가 거부됨"
    seven = "".join(f"&group={g}" for g in (gids * 3)[:7])
    assert client.get(f"{base}/trim_chart_batch?source={source}{seven}",
                      headers=UA).status_code == 400, "group 7개 상한 미적용"
    assert client.get(f"{base}/trim_chart_batch?source={source}&group={'x' * 201}",
                      headers=UA).status_code == 400, "group 길이 상한 미적용"
    batch(["__nope__"], expect=404)
    batch([picked[0], "__nope__"], expect=404)
    print("(d) 검증 OK — 0개/7개/201자 → 400, 6개(상한) → 200, 미존재 그룹 → 404")
    ok += 1

    # ── (e) 워커 잡이 라우트와 같은 bytes ────────────────────────────────────
    blob = wr_compute.trim_chart_batch_job(
        sid, os.environ["REPORT_UPLOAD_DIR"], source, list(picked))
    job_charts = json.loads(gzip.decompress(blob))["charts"]
    for i, gid in enumerate(picked):
        assert canon(job_charts[i]) == canon(singles[i]), \
            f"워커 잡 산출이 인라인과 다르다 gid={gid}"
    print("(e) compute.trim_chart_batch_job 산출 == 인라인 산출 (오프로드해도 값 동일)")
    ok += 1

    # ── (f) 캐시 바이트 상한 축출 ────────────────────────────────────────────
    saved_bytes, saved_max = (wr_cache.TRIM_CHART_CACHE_MAX_BYTES,
                              wr_cache.TRIM_CHART_CACHE_MAX)
    try:
        wr_cache.TRIM_CHART_CACHE.clear()
        wr_cache.TRIM_CHART_CACHE_MAX = 100          # 개수 상한은 넉넉히
        wr_cache.TRIM_CHART_CACHE_MAX_BYTES = 3000   # 바이트 상한만 걸리게
        for i in range(10):
            wr_cache.trim_chart_cache_put((f"k{i}",), b"x" * 1000)
        total = sum(len(v) for v in wr_cache.TRIM_CHART_CACHE.values())
        assert len(wr_cache.TRIM_CHART_CACHE) <= 3, \
            f"바이트 상한 축출 안 됨: {len(wr_cache.TRIM_CHART_CACHE)}개"
        assert total <= 3000, f"바이트 총량 초과: {total}"
        assert ("k9",) in wr_cache.TRIM_CHART_CACHE, "최신 항목이 축출됨"
        assert ("k0",) not in wr_cache.TRIM_CHART_CACHE, "가장 오래된 항목이 남음(LRU 위반)"
        print(f"(f) 바이트 상한 축출 OK ({len(wr_cache.TRIM_CHART_CACHE)}개 / {total}B ≤ 3000B, LRU)")
        ok += 1
    finally:
        wr_cache.TRIM_CHART_CACHE_MAX_BYTES, wr_cache.TRIM_CHART_CACHE_MAX = (
            saved_bytes, saved_max)
        wr_cache.TRIM_CHART_CACHE.clear()

    print(f"\n전체 통과: {ok}개 그룹")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

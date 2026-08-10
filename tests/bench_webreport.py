"""web_report 성능 벤치테스트 — 세션 열기/탭 조회/ingest 를 합성 대용량으로 실측.

기본 스케일 21 소스 × 1000 항목 × 1000 행(honeyform)으로 web_report 파이프라인의
주요 경로를 측정하고, 이전 실행(같은 params) 대비 delta% 리포트를 한국어 markdown 으로
출력·저장한다. 운영 서버/DB 무접촉 — DB/업로드/로그 전부 임시 디렉토리에 격리된다.

실행 (repo 루트에서):
    server\\.venv\\Scripts\\python.exe tests\\bench_webreport.py            # full (수 분)
    server\\.venv\\Scripts\\python.exe tests\\bench_webreport.py --quick    # 5x200x200 스모크
옵션:
    --quick           5 소스 × 200 항목 × 200 행 (스모크 — full 과 비교되지 않음)
    --baseline PATH   비교 기준 JSON 명시 (기본: params 일치하는 최신 이전 실행)
    --label TEXT      실행에 붙일 짧은 메모 (리포트 헤더에 표시)

결과: tests/bench_results/bench_<ts>.json + bench_<ts>.md + latest.md (gitignore 대상).
판정: 이전 대비 p50 +15% 초과 [주의], +30% 초과 [회귀] (bytes 는 +10%/+30%).

절대 기준 판정 1건 — **Map 3초 SLA (CLAUDE.md §5-11)**: gross die 10,000 × 7 소스 ×
STEP 3종 세션에서 Map 첫 조회(서버 응답 + gunzip + JSON 파싱)가 3초를 넘으면 기준선
유무와 무관하게 [SLA위반] 이 뜬다. full 실행에서만 돈다(--quick 제외).

측정 범위 밖(리포트 말미에도 명시): 실 네트워크/waitress 동시성(→ load_test_10users.py),
브라우저 JS 렌더링, S3 경로, workers>0 프로세스 풀(결정성 위해 workers=0 인라인 고정).

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import argparse
import gzip as _gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 한국어 Windows 콘솔(cp949) 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# config 는 import 시점에 env 를 읽는다 — 반드시 import 앞에서 지정할 것.
_TMP = Path(tempfile.mkdtemp(prefix="wr_bench_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval.db")
os.environ["REPORT_VOC_DB_PATH"] = str(_TMP / "voc.db")
os.environ["REPORT_S3_BUCKET"] = ""               # S3 비활성 → 로컬 폴백
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"    # 인라인 고정 (측정 결정성)
os.environ["REPORT_CLEANUP_ENABLED"] = "0"        # 백그라운드 스케줄러 전부 정지
os.environ["REPORT_TIER_ENABLED"] = "0"
os.environ["REPORT_DB_BACKUP_ENABLED"] = "0"
os.environ["REPORT_METRICS_ENABLED"] = "0"

import psutil  # noqa: E402
from flask import Flask  # noqa: E402

import config  # noqa: E402
config.ROOT_DIR = _TMP   # build_log 등 ROOT_DIR 파생 경로(server/log)를 임시로 격리

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import build_log  # noqa: E402
from web_report import cache as wr_cache  # noqa: E402
from web_report import compute as wr_compute  # noqa: E402
from web_report import dist_pack  # noqa: E402
from web_report import ingest as wr_ingest  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, decode_split_honeyform_parquet,
    encode_honeyform_parquet)

UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])
RESULTS_DIR = Path(_ROOT) / "tests" / "bench_results"
UA = {"User-Agent": "Mozilla/5.0 HoneyUser/bench", "Accept-Encoding": "gzip"}

SCALE_FULL = (21, 1000, 1000)    # (sources, items, rows)
SCALE_QUICK = (5, 200, 200)
# CLAUDE.md §5-11 Map 3초 SLA 시나리오 — gross die 10,000 × 7 source.
# 항목 수는 map 산출에 영향이 없어 100 으로 줄인다(픽스처 생성 시간만 아낌).
SCALE_SLA = (7, 100, 10000)
SLA_MAP_SECONDS = 3.0            # 서버 응답 + gunzip + JSON 파싱 합산 상한
SLA_STEPS = 3                    # P1/P2/P3 — STEP 분리(die×STEP 증폭) 경로를 실제로 태운다
N_COLD, N_DISK, N_WARM, N_TAB = 3, 5, 10, 5
BATCH_N = 30                     # distribution_batch 요청 항목 수 (프런트 DIST_BATCH.SIZE)

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()

_warnings: list[str] = []


def warn(msg: str) -> None:
    _warnings.append(msg)
    print(f"  [경고] {msg}")


# ── 합성 honeyform (load_test_10users.py 이식) ───────────────────────────────

def make_honeyform_df(n_items: int, n_rows: int, seed: int = 0, n_steps: int = 1):
    """n_steps>1 이면 항목을 P1..Pn STEP 으로 나눠 배분한다 (Map STEP 분리 경로용).

    기본값 1 은 종전과 문자 그대로 같은 df 를 만든다 — 기존 지표의 기준선 연속성 유지.
    """
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    items = [f"item_{i:04d}" for i in range(1, n_items + 1)]

    meta_rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row["SERIAL"] = label
        for i, it in enumerate(items, start=1):
            row[it] = {"TSEQ": str(i), "TNO": str(i),
                       "STEP": "FT" if n_steps <= 1 else f"P{(i - 1) % n_steps + 1}",
                       "UNIT": "V", "HILIM": "1.5", "LOLIM": "0.5"}[label]
        meta_rows.append(row)

    values = rng.normal(1.0, 0.1, size=(n_rows, n_items))
    side = int(n_rows ** 0.5) + 1
    data_rows = []
    for d in range(n_rows):
        is_fail = rng.random() < 0.05
        row = {"SERIAL": f"S{d:06d}", "SHOT": str(d // 4 + 1), "DUT": str(d % 4 + 1),
               "XPOS": str(d % side), "YPOS": str(d // side),
               "BIN": "2" if is_fail else "1", "FAILTNO": ""}
        if is_fail:
            fi = int(rng.integers(0, n_items))
            values[d, fi] = 2.5  # HILIM(1.5) 밖
            row["FAILTNO"] = str(fi + 1)
        for i, it in enumerate(items):
            row[it] = f"{values[d, i]:.6f}"
        data_rows.append(row)

    df = pd.DataFrame(meta_rows + data_rows, columns=META_COLUMNS + items)
    return df, items


def make_files(parquet_bytes: bytes, n_sources: int) -> list[dict]:
    """동일 parquet bytes 를 소스 n개로 재사용 — 인코딩 x21 회피 (analysis_key 는
    file hash+meta 산출이라 중복 bytes 무해). 인코딩 시간은 #1 에서 1회 별도 측정."""
    return [{"name": f"BENCH_{i:02d}", "filename": f"BENCH_{i:02d}.csv",
             "data": parquet_bytes} for i in range(n_sources)]


def build_manifest(files: list[dict], lot_id: str) -> dict:
    return {
        "meta": {"product_type": "PMIC", "product": "BENCHTEST",
                 "lot_id": lot_id, "file_name": "benchtest"},
        "mode": "Normal",
        "sources": [{"index": i, "name": f["name"], "file_name": f["filename"]}
                    for i, f in enumerate(files)],
        "selected_items": [],
        "sheets": [],
        "options": {},
        "client": {"user": "bench", "host": "benchhost", "domain": ""},
    }


def build_client_pack(files: list[dict], mode: str = "Normal") -> dict:
    """Honey 클라가 하는 일 그대로 — parquet 디코드 → dist pack 생성 (클라 부담 실측)."""
    tables = [decode_split_honeyform_parquet(
        f["data"], source=f["name"], file_name=f["filename"], keep_df=False)
        for f in files]
    index, chunk_iter = dist_pack.build_dist_pack(tables, [], mode, chunk_items=BATCH_N)
    chunks = {cid: dist_pack.gzip_pack_chunk(c, level=1) for cid, c in chunk_iter}
    return {"index": dist_pack.dumps_pack_index(index), "chunks": chunks}


# ── 측정 프리미티브 ──────────────────────────────────────────────────────────

def summarize(vals: list[float], unit: str = "s") -> dict:
    if not vals:
        return {"n": 0, "unit": unit}
    s = sorted(vals)
    p = lambda q: s[min(len(s) - 1, int(len(s) * q))]  # noqa: E731
    return {"n": len(s), "mean": round(sum(s) / len(s), 4), "p50": round(p(.50), 4),
            "p95": round(p(.95), 4), "min": round(s[0], 4), "max": round(s[-1], 4),
            "unit": unit}


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / 2**20


def settle(timeout: float = 600) -> None:
    """백그라운드 빌드(프리웜/온디맨드)가 끝날 때까지 대기 — 캐시 상태 제어의 전제."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("백그라운드 빌드가 끝나지 않음 (settle timeout)")


def drop_ram(akey: str) -> None:
    """RAM 캐시만 비움 (tables 포함) → 디스크 캐시 히트 상태."""
    settle()
    wr_cache.invalidate_caches(akey)


def drop_all(akey: str) -> None:
    """RAM + 디스크 캐시 전부 비움 → 완전 콜드 상태. dist_pack 은 영구 데이터라 유지."""
    drop_ram(akey)
    for cache_dir in (UPLOAD_ROOT / "web_report").glob("*/cache"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def timed_get(url: str, headers: dict | None = None):
    t0 = time.perf_counter()
    r = client.get(url, headers=headers or UA)
    return time.perf_counter() - t0, r


def open_until_200(url: str, timeout: float = 900):
    """콜드 열기 체감치: 최초 GET(202)부터 폴링으로 200 받을 때까지 총 시간.

    프런트(boot.js)의 202 → build_status 폴링 → 재요청 흐름과 같다.
    workers=0 이어도 온디맨드 소비자 스레드가 인라인 빌드하므로 202 규약은 동일하다.
    """
    t0 = time.perf_counter()
    r = client.get(url, headers=UA)
    if r.status_code == 200:
        return time.perf_counter() - t0, r, 0
    assert r.status_code == 202, f"콜드 {url} 이 202/200 이 아님: {r.status_code}"
    polls = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.1)
        polls += 1
        r = client.get(url, headers=UA)
        if r.status_code == 200:
            return time.perf_counter() - t0, r, polls
        assert r.status_code == 202, f"{url} 폴링 중 예상 밖 상태 {r.status_code}"
    raise AssertionError(f"{url} 이 {timeout}s 안에 200 이 되지 않음")


# ── 시나리오 ─────────────────────────────────────────────────────────────────

def bench_client_prep(scale) -> tuple[dict, dict, bytes, list[str], dict]:
    """#1 클라이언트 부담: df 생성 / parquet 인코딩(1소스) / dist_pack 생성(전 소스)."""
    n_sources, n_items, n_rows = scale
    metrics, sizes = {}, {}

    t0 = time.perf_counter()
    df, items = make_honeyform_df(n_items, n_rows)
    metrics["client.df_gen"] = summarize([time.perf_counter() - t0])

    t0 = time.perf_counter()
    parquet_bytes = encode_honeyform_parquet(df)
    metrics["client.parquet_encode_1src"] = summarize([time.perf_counter() - t0])
    sizes["parquet_one_source"] = len(parquet_bytes)

    files = make_files(parquet_bytes, n_sources)
    t0 = time.perf_counter()
    pack = build_client_pack(files)
    metrics["client.dist_pack_build"] = summarize([time.perf_counter() - t0])
    sizes["pack_total"] = len(pack["index"]) + sum(len(c) for c in pack["chunks"].values())
    print(f"#1 클라 준비: parquet {sizes['parquet_one_source']/2**20:.1f}MB/소스, "
          f"pack {sizes['pack_total']/2**20:.1f}MB "
          f"(인코딩 {metrics['client.parquet_encode_1src']['p50']}s, "
          f"pack {metrics['client.dist_pack_build']['p50']}s)")
    return metrics, sizes, parquet_bytes, items, pack


def bench_ingest(files: list[dict], lot_id: str, pack: dict | None) -> tuple[dict, str]:
    """#2/#3 ingest — 직접 호출(HTTP 캡 미적용). prewarm 은 별도 스레드라 settle 로 분리 측정."""
    manifest = build_manifest(files, lot_id)
    t0 = time.perf_counter()
    result = wr_ingest.ingest_webreport(
        manifest, files, report_db=report_db, upload_root=UPLOAD_ROOT,
        client_ip="127.0.0.1", user_agent=UA["User-Agent"], dist_pack=pack)
    ingest_sec = time.perf_counter() - t0
    t0 = time.perf_counter()
    settle()
    metrics = {"ingest": summarize([ingest_sec]),
               "ingest.prewarm_settle": summarize([time.perf_counter() - t0])}
    return metrics, result["session_id"]


def bench_session_open(sid: str, akey: str) -> tuple[dict, dict]:
    """#4~#6 세션 열기 3상태 + 콜드 stage 분해(build_log)."""
    metrics = {}
    full_url = f"/pe/report/session/{sid}/full"

    cold = []
    for i in range(N_COLD):
        drop_all(akey)
        sec, r, polls = open_until_200(full_url)
        assert r.status_code == 200
        cold.append(sec)
        print(f"#4 콜드 /full [{i+1}/{N_COLD}]: {sec:.1f}s ({polls}회 폴링)")
    metrics["open.cold_full"] = summarize(cold)

    # build_log 에서 이 세션의 report 콜드 빌드 stage 분해 추출 (최신순 → 평균)
    stages: dict = {}
    recs = [r for r in build_log.history(hours=1, limit=200)
            if r.get("session") == sid and r.get("kind") == "report"
            and r.get("stages")][:N_COLD]
    if recs:
        for rec in recs:
            for k, v in rec["stages"].items():
                stages[k] = stages.get(k, 0.0) + float(v)
        stages = {k: round(v / len(recs), 3) for k, v in stages.items()}
    else:
        warn("build_log 에서 콜드 빌드 stage 기록을 찾지 못함")

    disk = []
    for _ in range(N_DISK):
        drop_ram(akey)
        sec, r = timed_get(full_url)
        assert r.status_code == 200, f"디스크 캐시 /full 이 200 이 아님: {r.status_code}"
        disk.append(sec)
    metrics["open.disk_full"] = summarize(disk)

    warm = []
    for _ in range(N_WARM):
        sec, r = timed_get(full_url)
        assert r.status_code == 200
        warm.append(sec)
    metrics["open.warm_full"] = summarize(warm)

    view = []
    for _ in range(N_TAB):
        sec, r = timed_get(f"/pe/report/view/{sid}")
        assert r.status_code == 200
        view.append(sec)
    metrics["open.view_html"] = summarize(view)
    print(f"#5/#6 디스크 p50 {metrics['open.disk_full']['p50']}s / "
          f"재오픈(RAM) p50 {metrics['open.warm_full']['p50']}s")
    return metrics, stages


def bench_payload_sizes(sid: str) -> tuple[dict, dict]:
    """#12 클라 부담 프록시 — 전송(gzip) bytes vs 브라우저가 파싱할 해제 bytes."""
    sizes, checks = {}, {}
    r = client.get(f"/pe/report/session/{sid}/full", headers=UA)
    assert r.status_code == 200
    gz = r.data if r.headers.get("Content-Encoding") == "gzip" else _gzip.compress(r.data)
    raw = _gzip.decompress(gz)
    sizes["full_gz"], sizes["full_json"] = len(gz), len(raw)
    payload = json.loads(raw)
    sheets = (payload.get("web_report") or {}).get("sheets") or {}
    checks["tabs"] = sorted(sheets.keys())
    assert sheets, "/full 에 web_report.sheets 가 없음"
    return sizes, checks


def bench_dist(sid_pack: str, akey_pack: str, sid_nopack: str, akey_nopack: str,
               items: list[str]) -> tuple[dict, dict]:
    """#7~#9 Distribution 단건(pack/폴백) + 배치 30건(pack vs 폴백)."""
    metrics, sizes = {}, {}
    subjects = ",".join(items[:BATCH_N])

    for label, sid, akey in (("pack", sid_pack, akey_pack),
                             ("fallback", sid_nopack, akey_nopack)):
        url = f"/pe/report/session/{sid}/web_report/distribution"
        drop_all(akey)
        sec, r = timed_get(url)
        assert r.status_code == 200, f"dist({label}) 콜드 실패: {r.status_code}"
        metrics[f"tab.dist_single.{label}.cold"] = summarize([sec])
        metrics[f"tab.dist_single.{label}.warm"] = summarize(
            [timed_get(url)[0] for _ in range(N_TAB)])

        burl = (f"/pe/report/session/{sid}/web_report/distribution_batch"
                f"?subjects={subjects}")
        drop_ram(akey)
        sec, r = timed_get(burl)
        assert r.status_code == 200, f"batch({label}) 실패: {r.status_code} {r.data[:200]}"
        if label == "pack":
            sizes["dist_batch_resp_gz"] = len(r.data)
        metrics[f"tab.dist_batch.{label}.cold"] = summarize([sec])
        metrics[f"tab.dist_batch.{label}.warm"] = summarize(
            [timed_get(burl)[0] for _ in range(N_TAB)])
        print(f"#7~9 dist({label}): 단건 콜드 "
              f"{metrics[f'tab.dist_single.{label}.cold']['p50']}s / "
              f"배치{BATCH_N} 콜드 {metrics[f'tab.dist_batch.{label}.cold']['p50']}s")

    if (metrics["tab.dist_batch.pack.cold"]["p50"]
            > metrics["tab.dist_batch.fallback.cold"]["p50"]):
        warn("dist_batch pack 콜드가 폴백보다 느림 — pack 경로 미적용 의심")
    return metrics, sizes


def bench_tabs(sid: str, akey: str, items: list[str]) -> dict:
    """#10~#11 Map(콜드 202+웜) + 기타 탭 엔드포인트 (웜)."""
    metrics = {}
    base = f"/pe/report/session/{sid}/web_report"

    drop_all(akey)
    sec, r, polls = open_until_200(f"{base}/map_analysis")
    body = r.data
    if r.headers.get("Content-Encoding") == "gzip":
        body = _gzip.decompress(body)
    assert json.loads(body).get("maps"), "map_analysis 에 maps 가 없음"
    metrics["tab.map.cold"] = summarize([sec])
    metrics["tab.map.warm"] = summarize(
        [timed_get(f"{base}/map_analysis")[0] for _ in range(N_TAB)])
    print(f"#10 map: 콜드 {sec:.1f}s ({polls}회 폴링)")

    endpoints = {
        "tab.scatter": f"{base}/scatter/{items[0]}",
        "tab.raw_columns": f"{base}/raw_data/columns",
        "tab.raw_page": f"{base}/raw_data?columns={','.join(items[:5])}",
        "tab.trim": f"{base}/trim_analysis",
    }
    for name, url in endpoints.items():
        sec, r = timed_get(url)   # 1회차 = 자체 콜드(캐시 미적재)일 수 있음
        assert r.status_code == 200, f"{name} 실패: {r.status_code} {r.data[:200]}"
        metrics[f"{name}.first"] = summarize([sec])
        metrics[f"{name}.warm"] = summarize(
            [timed_get(url)[0] for _ in range(N_TAB)])
    return metrics


def bench_sla_map(run_id: str) -> tuple[dict, dict, dict]:
    """#13 Map 3초 SLA (CLAUDE.md §5-11) — gross die 10,000 × 7 source × STEP 3종.

    측정 대상은 **사용자가 세션을 연 뒤 Map 탭을 클릭한 순간**이다 — 서버 응답 +
    gunzip + JSON 파싱까지 합산한다. Issue Table Map 컬럼은 같은 map_analysis 응답을
    소비하므로(static/webreport/issue_dist.js) 별도 지표가 없다.

    세션 열기(/full)를 먼저 200 까지 완료시키는 것이 시나리오의 핵심이다. 그 콜드
    빌드가 map dies 를 함께 시딩하고(`service.seed_map`), 실사용에서도 Map 탭 클릭은
    항상 세션 열기 뒤에 온다. 프리웜(업로드 직후)으로 대신할 수 없다 —
    `compute.status()["prewarm_pending"]` 은 **큐 길이만** 세어 settle 이 빌드 완료를
    보장하지 못한다(빌드는 소비자 스레드에서 계속 돈다).

    이 판정은 이전 실행 대비 상대 판정과 **독립인 절대 기준**이다 — 기준선이 없어도
    3초를 넘으면 [SLA위반] 이 뜬다.
    """
    n_sources, n_items, n_rows = SCALE_SLA
    print(f"#13 SLA 픽스처 생성: {n_sources} 소스 × {n_rows} die × STEP {SLA_STEPS}종")
    df, _items = make_honeyform_df(n_items, n_rows, n_steps=SLA_STEPS)
    files = make_files(encode_honeyform_parquet(df), n_sources)
    _m, sid = bench_ingest(files, f"BENCH_{run_id}_SLA", None)
    akey = report_db.get_session(sid).get("analysis_key")
    metrics = {}
    url = f"/pe/report/session/{sid}/web_report/map_analysis"
    sizes = {}

    # ① 세션 열기 — 이 콜드 빌드가 map dies 를 함께 시딩한다.
    open_sec, r, open_polls = open_until_200(f"/pe/report/session/{sid}/full")
    assert r.status_code == 200
    metrics["sla.session_open"] = summarize([open_sec])
    print(f"#13 세션 열기(/full): {open_sec:.2f}s ({open_polls}회 폴링)")

    # ② Map 탭 클릭. 서버 재시작 직후와 같은 최악의 웜 상태(RAM 비고 디스크만)에서도
    #    시딩이 동작하면 202 없이 바로 200 이어야 한다.
    drop_ram(akey)
    t0 = time.perf_counter()
    _sec, r, polls = open_until_200(url)
    body = r.data
    if r.headers.get("Content-Encoding") == "gzip":
        body = _gzip.decompress(body)
    payload = json.loads(body)
    first_sec = time.perf_counter() - t0
    if polls:
        warn(f"Map 첫 조회가 콜드 202 ({polls}회 폴링) — seed_map 시딩 미동작 의심")
    sizes["sla_map_gz"] = len(r.data)
    sizes["sla_map_json"] = len(body)
    metrics["sla.map.first"] = summarize([first_sec])

    # 데이터 소실 없음(규칙 §5-5) — STEP 분리는 모든 die 가 모든 STEP 맵에 등장한다.
    maps = payload.get("maps") or []
    dies = sum(len(m2.get("dies") or ()) for m2 in maps)
    expected = n_rows * n_sources * SLA_STEPS
    assert dies == expected, f"die 소실/증식: {dies} != {expected} (맵 {len(maps)}장)"
    print(f"#13 SLA 첫 조회: {first_sec:.2f}s ({len(maps)}맵 / {dies:,}die / "
          f"gz {sizes['sla_map_gz']/2**20:.1f}MB → json {sizes['sla_map_json']/2**20:.1f}MB)")

    # 시딩 도입 전 세션(map 캐시만 없음) → /full 200 이 백그라운드 백필을 건다.
    settle()
    wr_cache.invalidate_caches(akey)
    for path in (UPLOAD_ROOT / "web_report").glob("*/cache/map-*.gz"):
        path.unlink()
    t0 = time.perf_counter()
    _sec, r = timed_get(f"/pe/report/session/{sid}/full")
    assert r.status_code == 200, f"레거시 /full 이 200 이 아님: {r.status_code}"
    _sec, _r, back_polls = open_until_200(url)
    backfill_sec = time.perf_counter() - t0
    metrics["sla.map.legacy_backfill"] = summarize([backfill_sec])
    print(f"#13 레거시 백필: {backfill_sec:.2f}s ({back_polls}회 폴링)")

    ok = first_sec <= SLA_MAP_SECONDS
    if not ok:
        warn(f"Map 3초 SLA 위반 — 첫 조회 {first_sec:.2f}s > {SLA_MAP_SECONDS}s "
             f"(CLAUDE.md §5-11)")
    sla = {"target": SLA_MAP_SECONDS, "measured": round(first_sec, 3), "ok": ok,
           "maps": len(maps), "dies": dies, "polls": polls,
           "params": {"sources": n_sources, "rows": n_rows, "steps": SLA_STEPS}}
    return metrics, sizes, sla


# ── 환경/카운터/저장/비교/리포트 ─────────────────────────────────────────────

def collect_env(scale, quick: bool, label: str) -> dict:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_ROOT,
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        rev = ""
    return {"git_rev": rev, "python": sys.version.split()[0],
            "cpu": os.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
            "workers": 0, "label": label,
            "params": {"sources": scale[0], "items": scale[1], "rows": scale[2],
                       "quick": quick}}


def load_previous(baseline_path: str | None, params: dict) -> dict | None:
    if baseline_path:
        try:
            return json.loads(Path(baseline_path).read_text(encoding="utf-8"))
        except Exception as exc:
            warn(f"--baseline 로드 실패: {exc}")
            return None
    candidates = sorted(RESULTS_DIR.glob("bench_*.json"), reverse=True)
    for p in candidates:
        try:
            prev = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if prev.get("env", {}).get("params") == params:
            return prev
    return None


def _verdict(delta_pct: float, warn_th: float, bad_th: float) -> str:
    if delta_pct > bad_th:
        return "[회귀]"
    if delta_pct > warn_th:
        return "[주의]"
    return "OK"


def _fmt_sec(v: float) -> str:
    return f"{v*1000:.0f}ms" if v < 1 else f"{v:.2f}s"


def _fmt_bytes(n: int) -> str:
    return f"{n/2**20:.2f}MB" if n >= 2**20 else f"{n/2**10:.0f}KB"


_SECTION_ORDER = [
    ("세션 열기/닫기", ["open.cold_full", "open.disk_full", "open.warm_full",
                        "open.view_html"]),
    ("업로드 (ingest)", ["ingest", "ingest.prewarm_settle",
                         "ingest_nopack", "ingest_nopack.prewarm_settle"]),
    ("클라이언트 준비 부담", ["client.df_gen", "client.parquet_encode_1src",
                              "client.dist_pack_build"]),
    ("탭: Distribution", ["tab.dist_single.pack.cold", "tab.dist_single.pack.warm",
                          "tab.dist_single.fallback.cold", "tab.dist_single.fallback.warm",
                          "tab.dist_batch.pack.cold", "tab.dist_batch.pack.warm",
                          "tab.dist_batch.fallback.cold", "tab.dist_batch.fallback.warm"]),
    ("탭: Map/기타", ["tab.map.cold", "tab.map.warm", "tab.scatter.first",
                      "tab.scatter.warm", "tab.raw_columns.first", "tab.raw_columns.warm",
                      "tab.raw_page.first", "tab.raw_page.warm", "tab.trim.first",
                      "tab.trim.warm"]),
    ("SLA: Map 3초 (§5-11)", ["sla.map.first", "sla.map.legacy_backfill",
                              "sla.session_open"]),
]

_NAME_KO = {
    "open.cold_full": "완전 콜드 열기 (/full 202→200)",
    "open.disk_full": "디스크 캐시 열기",
    "open.warm_full": "재오픈 (RAM, 닫기→열기 체감)",
    "open.view_html": "세션 페이지 HTML",
    "ingest": "ingest (pack 포함)",
    "ingest.prewarm_settle": "└ 업로드 후 프리웜 빌드",
    "ingest_nopack": "ingest (pack 없음)",
    "ingest_nopack.prewarm_settle": "└ 업로드 후 프리웜 빌드",
    "client.df_gen": "honeyform df 생성",
    "client.parquet_encode_1src": "parquet 인코딩 (1소스)",
    "client.dist_pack_build": "dist_pack 생성 (전 소스)",
    "tab.dist_single.pack.cold": "Distribution 전체 — pack, 콜드",
    "tab.dist_single.pack.warm": "Distribution 전체 — pack, 웜",
    "tab.dist_single.fallback.cold": "Distribution 전체 — 폴백, 콜드",
    "tab.dist_single.fallback.warm": "Distribution 전체 — 폴백, 웜",
    "tab.dist_batch.pack.cold": f"배치 {BATCH_N}건 — pack, 콜드",
    "tab.dist_batch.pack.warm": f"배치 {BATCH_N}건 — pack, 웜",
    "tab.dist_batch.fallback.cold": f"배치 {BATCH_N}건 — 폴백, 콜드",
    "tab.dist_batch.fallback.warm": f"배치 {BATCH_N}건 — 폴백, 웜",
    "tab.map.cold": "Map Analysis — 콜드 (202→200)",
    "tab.map.warm": "Map Analysis — 웜",
    "tab.scatter.first": "Scatter 단건 — 1회차",
    "tab.scatter.warm": "Scatter 단건 — 웜",
    "tab.raw_columns.first": "Raw Data 컬럼 목록 — 1회차",
    "tab.raw_columns.warm": "Raw Data 컬럼 목록 — 웜",
    "tab.raw_page.first": "Raw Data 페이지 조회 — 1회차",
    "tab.raw_page.warm": "Raw Data 페이지 조회 — 웜",
    "tab.trim.first": "Trim Analysis — 1회차",
    "tab.trim.warm": "Trim Analysis — 웜",
    "sla.map.first": "Map 첫 조회 (응답+파싱, 목표 3초)",
    "sla.map.legacy_backfill": "└ 시딩 전 세션 백필 경로",
    "sla.session_open": "(선행) 세션 열기 — 이때 시딩됨",
}

_SIZE_KO = {
    "parquet_one_source": "parquet 1소스 (업로드 전송)",
    "pack_total": "dist_pack 전체 (업로드 전송)",
    "full_gz": "/full 응답 gzip (네트워크)",
    "full_json": "/full 해제 JSON (브라우저 파싱)",
    "dist_batch_resp_gz": f"배치 {BATCH_N}건 응답 gzip",
    "sla_map_gz": "SLA Map 응답 gzip (네트워크)",
    "sla_map_json": "SLA Map 해제 JSON (브라우저 파싱)",
}


def render_report_md(cur: dict, prev: dict | None, total_sec: float) -> str:
    env, params = cur["env"], cur["env"]["params"]
    lines = []
    lines.append(f"# web_report 벤치마크 리포트 ({cur['ts']})")
    lines.append("")
    scale_txt = f"{params['sources']} 소스 × {params['items']} 항목 × {params['rows']} 행"
    mode_txt = "quick" if params["quick"] else "full"
    lines.append(f"- 규모: {scale_txt} ({mode_txt}) | git `{env.get('git_rev') or '?'}` | "
                 f"workers=0(인라인) | 총 소요 {total_sec/60:.1f}분"
                 + (f" | label: {env['label']}" if env.get("label") else ""))
    if prev:
        lines.append(f"- 비교 대상: {prev.get('run_id')} (git `"
                     f"{prev.get('env', {}).get('git_rev') or '?'}`)")
    else:
        lines.append("- 비교 대상 없음 (같은 params 의 이전 실행 없음 — 이번이 기준선)")
    lines.append("")

    prev_m = (prev or {}).get("metrics", {})
    flagged = []
    for title, keys in _SECTION_ORDER:
        rows = [(k, cur["metrics"][k]) for k in keys if k in cur["metrics"]]
        if not rows:
            continue
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| 항목 | p50 | p95 | 이전 p50 | 변화 | 판정 |")
        lines.append("|---|---|---|---|---|---|")
        for k, m in rows:
            name = _NAME_KO.get(k, k)
            p50, p95 = m.get("p50"), m.get("p95")
            pm = prev_m.get(k, {})
            if pm.get("p50"):
                delta = (p50 - pm["p50"]) / pm["p50"] * 100
                verdict = _verdict(delta, 15, 30)
                if verdict != "OK":
                    flagged.append((verdict, name, delta))
                prev_txt, delta_txt = _fmt_sec(pm["p50"]), f"{delta:+.0f}%"
            else:
                prev_txt = delta_txt = verdict = "—"
            lines.append(f"| {name} | {_fmt_sec(p50)} | {_fmt_sec(p95)} | "
                         f"{prev_txt} | {delta_txt} | {verdict} |")
        lines.append("")

    if cur.get("stages"):
        lines.append("## 콜드 빌드 단계별 (build_log, 콜드 3회 평균)")
        lines.append("")
        items = sorted(cur["stages"].items(), key=lambda kv: -kv[1])
        prev_st = (prev or {}).get("stages", {})
        parts = []
        for k, v in items:
            if k == "payload":
                continue   # payload = tab 단계 합 (관리자 패널과 같은 기준)
            d = f" ({(v - prev_st[k]) / prev_st[k] * 100:+.0f}%)" \
                if prev_st.get(k) else ""
            parts.append(f"`{k}` {_fmt_sec(v)}{d}")
        lines.append(" | ".join(parts))
        lines.append("")

    lines.append("## 페이로드 크기 (클라이언트 부담 프록시)")
    lines.append("")
    lines.append("| 항목 | 크기 | 이전 | 변화 | 판정 |")
    lines.append("|---|---|---|---|---|")
    prev_sz = (prev or {}).get("sizes", {})
    for k, v in cur["sizes"].items():
        name = _SIZE_KO.get(k, k)
        pv = prev_sz.get(k)
        if pv:
            delta = (v - pv) / pv * 100
            verdict = _verdict(delta, 10, 30)
            if verdict != "OK":
                flagged.append((verdict, name, delta))
            prev_txt, delta_txt = _fmt_bytes(pv), f"{delta:+.0f}%"
        else:
            prev_txt = delta_txt = verdict = "—"
        lines.append(f"| {name} | {_fmt_bytes(v)} | {prev_txt} | {delta_txt} | {verdict} |")
    lines.append("")

    c = cur.get("counters", {})
    cs = c.get("cache_stats", {})
    lines.append("## 리소스")
    lines.append("")
    lines.append(f"- 벤치 프로세스 RSS: 시작 {c.get('rss_start_mb', 0):.0f}MB → "
                 f"종료 {c.get('rss_end_mb', 0):.0f}MB (Δ{c.get('rss_delta_mb', 0):+.0f}MB)")
    lines.append(f"- 캐시: hit {cs.get('hit')} / miss {cs.get('miss')} / "
                 f"disk_hit {cs.get('disk_hit')} / disk_miss {cs.get('disk_miss')}")
    if cs.get("lock_waits"):
        lines.append(f"- lock_waits: {json.dumps(cs['lock_waits'], ensure_ascii=False)}")
    lines.append("")

    sla = cur.get("sla")
    if sla:
        p = sla["params"]
        lines.append("## SLA: Map 3초 (CLAUDE.md §5-11)")
        lines.append("")
        lines.append(f"- **{'[SLA충족]' if sla['ok'] else '[SLA위반]'}** "
                     f"{p['sources']} 소스 × {p['rows']:,} die × STEP {p['steps']}종 "
                     f"→ Map 첫 조회 **{sla['measured']:.2f}s** (목표 {sla['target']}s)")
        lines.append(f"- 맵 {sla['maps']}장 / die {sla['dies']:,}개 "
                     f"(다운샘플 없음 — 규칙 §5-5) / 콜드 폴링 {sla['polls']}회")
        lines.append("- 이전 실행 대비가 아니라 **절대 기준** 판정이다. Issue Table 의 Map "
                     "컬럼도 같은 응답을 소비하므로 이 수치가 함께 적용된다.")
        lines.append("")

    lines.append("## 종합")
    lines.append("")
    if sla and not sla["ok"]:
        lines.append(f"- [SLA위반] Map 첫 조회 {sla['measured']:.2f}s "
                     f"> 목표 {sla['target']}s")
    if flagged:
        for verdict, name, delta in sorted(flagged, key=lambda x: -abs(x[2])):
            lines.append(f"- {verdict} {name}: {delta:+.0f}%")
    elif prev:
        lines.append("- 이전 실행 대비 [주의]/[회귀] 항목 없음.")
    else:
        lines.append("- 기준선 실행 — 다음 실행부터 비교가 표시된다.")
    for w in _warnings:
        lines.append(f"- [경고] {w}")
    lines.append("")
    lines.append("## 이번 벤치가 다루지 않는 것")
    lines.append("")
    lines.append("실제 네트워크/waitress 동시성(→ `tests/load_test_10users.py`), "
                 "브라우저 JS 렌더링, S3 경로, workers>0 프로세스 풀(운영은 3), "
                 "Compare/Commonality/DUT 모드 세션.")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="web_report 성능 벤치테스트")
    ap.add_argument("--quick", action="store_true", help="5x200x200 스모크")
    ap.add_argument("--baseline", default=None, help="비교 기준 JSON 경로")
    ap.add_argument("--label", default="", help="실행 메모 (리포트 헤더)")
    args = ap.parse_args()

    scale = SCALE_QUICK if args.quick else SCALE_FULL
    run_id = time.strftime("%Y%m%d_%H%M%S")
    t_start = time.perf_counter()
    rss_start = rss_mb()
    print(f"web_report 벤치 시작 — {scale[0]} 소스 × {scale[1]} 항목 × {scale[2]} 행 "
          f"(임시 디렉토리: {_TMP})")

    metrics, sizes = {}, {}

    m, sz, parquet_bytes, items, pack = bench_client_prep(scale)
    metrics.update(m); sizes.update(sz)

    files = make_files(parquet_bytes, scale[0])
    m, sid_a = bench_ingest(files, f"BENCH_{run_id}_A", pack)
    metrics.update(m)
    m, sid_b = bench_ingest(files, f"BENCH_{run_id}_B", None)
    metrics.update({f"ingest_nopack{k[6:]}" if k.startswith("ingest") else k: v
                    for k, v in m.items()})
    akey_a = report_db.get_session(sid_a).get("analysis_key")
    akey_b = report_db.get_session(sid_b).get("analysis_key")
    print(f"#2/#3 ingest: pack {metrics['ingest']['p50']}s / "
          f"no-pack {metrics['ingest_nopack']['p50']}s (A={sid_a}, B={sid_b})")

    m, stages = bench_session_open(sid_a, akey_a)
    metrics.update(m)

    sz, checks = bench_payload_sizes(sid_a)
    sizes.update(sz)

    m, sz = bench_dist(sid_a, akey_a, sid_b, akey_b, items)
    metrics.update(m); sizes.update(sz)

    metrics.update(bench_tabs(sid_a, akey_a, items))

    # #13 Map 3초 SLA — 픽스처 규모가 달라 full 실행에서만 돈다(quick 스모크 제외).
    sla = None
    if not args.quick:
        m, sz, sla = bench_sla_map(run_id)
        metrics.update(m); sizes.update(sz)

    cur = {
        "run_id": run_id,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "env": collect_env(scale, args.quick, args.label),
        "metrics": metrics,
        "stages": stages,
        "sizes": sizes,
        "checks": checks,
        "sla": sla,
        "counters": {
            "cache_stats": wr_cache.cache_stats(),
            "compute_stats": wr_compute.status().get("stats"),
            "rss_start_mb": round(rss_start, 1),
            "rss_end_mb": round(rss_mb(), 1),
            "rss_delta_mb": round(rss_mb() - rss_start, 1),
        },
        "warnings": list(_warnings),
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    prev = load_previous(args.baseline, cur["env"]["params"])
    total_sec = time.perf_counter() - t_start
    report_md = render_report_md(cur, prev, total_sec)

    (RESULTS_DIR / f"bench_{run_id}.json").write_text(
        json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    (RESULTS_DIR / f"bench_{run_id}.md").write_text(report_md, encoding="utf-8")
    (RESULTS_DIR / "latest.md").write_text(report_md, encoding="utf-8")

    print("\n" + "=" * 78)
    print(report_md)
    print(f"저장: tests/bench_results/bench_{run_id}.json / .md (+ latest.md)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)

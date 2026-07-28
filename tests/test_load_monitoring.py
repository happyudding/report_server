"""동시 사용 병목 완화 + 관리자 부하 모니터링 검증 (2026-07-28).

배경: 2~3명이 대형 세션(10소스 x 1500항목 x 2000 rawdata)을 동시에 열 때의 병목을
줄이고, 그 상태를 관리자 화면에서 볼 수 있게 한 변경들을 고정한다.

  (a) 콜드 202 폴링이 빌드 중인 세션의 single-flight 락에 **막히지 않는다**
      (수정 전엔 락에 들어간 뒤 판정해서 빌드가 끝날 때까지 요청 스레드가 묶였다)
  (b) response_cache 3종의 바이트 상한이 실제로 축출한다 (개수 상한만 있던 구멍)
  (c) keyed_lock 경합이 계측된다 — 무경합은 카운터를 건드리지 않는다
  (d) cache_stats 가 response_cache 통계(provider 콜백)와 chunk 캐시를 노출한다
  (e) build_status.snapshot_all 이 진행 중 콜드 빌드를 경과순으로 준다
  (f) 동시 열람 세션(viewers) 계측 — 화이트리스트 endpoint 만, 윈도우 밖은 제외
  (g) runtime 로그 type:"load" 기록 → file_history 가 부하 시계열로 되돌린다
      (캐시 히트율은 누적값의 구간 증분, 재시작 리셋 구간은 None)
  (h) GET api/runtime 이 화면이 읽는 키(viewers/builds/compute 대기/cache 확장)를
      실제로 직렬화해 돌려준다 — 항목 하나가 실패해도 나머지를 막지 않는다
  (i) 컴퓨트 워커(자식 프로세스) RSS 가 집계된다 — 부모 RSS 만 보면 워커가 쓰는 RAM 이
      통째로 안 보이고, 시스템 전체 RAM 은 같은 박스의 다른 서비스와 섞여 분리가 안 된다

실행:
    python tests/test_load_monitoring.py

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="load_mon_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["REPORT_RUNTIME_LOG_INTERVAL_SEC"] = "60"

from flask import Flask  # noqa: E402

import config  # noqa: E402
from admin_panel import metrics  # noqa: E402
from web_report import build_status, cache as wr_cache, disk_cache  # noqa: E402
from web_report import response_cache, service as wr_service  # noqa: E402

config.ROOT_DIR = _TMP
LOG_DIR = _TMP / "server" / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_failures = []


def check(ok, label):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        _failures.append(label)


# ── (a) 콜드 202 는 빌드 중인 락을 기다리지 않는다 ───────────────────────────

class _FakeDB:
    """load_webreport 가 콜드 판정까지 도달하는 데 필요한 최소 세션 stub."""

    def get_session(self, session_id):
        return {"analysis_key": "AKEY_COLD", "content_hash": "chash",
                "mode": "Normal", "webreport_options": ""}

    def get_webreport_edit_rev(self, session_id):
        return 1


def test_cold_poll_not_blocked():
    db = _FakeDB()
    upload_root = _TMP / "uploads"
    kwargs = dict(report_db=db, upload_root=upload_root, build_if_cold=False)

    # 온디맨드 소비자가 빌드 중인 상황 재현: 같은 키의 락을 다른 스레드가 붙잡는다.
    from web_report import cache_policy
    session = db.get_session("S1")
    key = ("report",) + cache_policy.report_key(session, "S1", 1)
    lock = wr_cache.keyed_lock(key)
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with lock:
            holding.set()
            release.wait(10)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    holding.wait(5)

    t0 = time.perf_counter()
    try:
        wr_service.load_webreport("S1", **kwargs)
        raised = None
    except wr_service.ColdBuildRequired:
        raised = "cold"
    except Exception as exc:                       # 다른 예외 = 판정 지점이 바뀐 것
        raised = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - t0
    release.set()
    t.join(5)

    check(raised == "cold", f"(a) 빌드 중 폴링이 ColdBuildRequired 즉시 반환 ({raised})")
    check(elapsed < 1.0, f"(a) 락 대기 없이 반환 ({elapsed * 1000:.0f}ms — 수정 전엔 빌드 종료까지 대기)")

    # 디스크 산출물이 있으면(=빌드 완료) 종전대로 락 안 경로를 탄다 — 존재 확인이
    # 콜드 판정만 앞당겼을 뿐 웜 경로 규약은 그대로임을 고정.
    from web_report import cache_policy as cp
    cache_key = cp.report_key(session, "S1", 1)
    check(not disk_cache.report_exists(upload_root, cache_key),
          "(a) 산출물 없음 = report_exists False")
    disk_cache.save_report(upload_root, cache_key, {"sheets": {}})
    check(disk_cache.report_exists(upload_root, cache_key),
          "(a) 저장 후 report_exists True (같은 키 규약)")
    loaded = wr_service.load_webreport("S1", **kwargs)[1]
    check(loaded == {"sheets": {}}, "(a) 디스크 히트는 종전대로 200 경로")


# ── (b) response_cache 바이트 상한 ───────────────────────────────────────────

def test_response_cache_bytes_cap():
    cache = response_cache._FULL_CACHE
    cache.clear()
    blob = b"x" * 1024
    for i in range(6):
        wr_cache._bytes_capped_put(cache, ("AK", "c", f"k{i}"), blob,
                                   100, 3 * 1024)   # 개수 100 / 바이트 3KB
    total = sum(len(v) for v in cache.values())
    check(len(cache) == 3 and total <= 3 * 1024,
          f"(b) 바이트 상한으로 축출 (보유 {len(cache)}건 / {total}B)")
    check(("AK", "c", "k5") in cache, "(b) 최근 항목은 유지")

    cache.clear()
    wr_cache._bytes_capped_put(cache, ("AK", "c", "big"), b"y" * 4096, 100, 1024)
    check(len(cache) == 1, "(b) 상한보다 큰 단일 blob 도 최소 1개는 남긴다")
    cache.clear()


# ── (c) keyed_lock 경합 계측 ─────────────────────────────────────────────────

def test_lock_wait_metering():
    wr_cache.LOCK_WAITS.clear()
    with wr_cache.keyed_lock_ctx(("t_free", "x")):
        pass
    check(not wr_cache.LOCK_WAITS, "(c) 무경합은 카운터를 건드리지 않음")

    key = ("t_busy", "x")
    entered = threading.Event()
    done = threading.Event()

    def holder():
        with wr_cache.keyed_lock_ctx(key):
            entered.set()
            time.sleep(0.15)
        done.set()

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    entered.wait(5)
    with wr_cache.keyed_lock_ctx(key):       # 여기서 대기 발생
        pass
    t.join(5)

    ent = wr_cache.LOCK_WAITS.get("t_busy")
    check(ent is not None and ent[0] == 1, f"(c) 경합 1회 계측 ({ent})")
    check(ent and ent[1] >= 100, f"(c) 대기 시간(ms) 누적 ({ent})")
    stats = wr_cache.cache_stats()
    check(stats["lock_waits"].get("t_busy", {}).get("count") == 1,
          "(c) cache_stats 에 lock_waits 노출")
    wr_cache.LOCK_WAITS.clear()


# ── (d) cache_stats 확장 ─────────────────────────────────────────────────────

def test_cache_stats_exposure():
    wr_cache.DIST_CHUNK_CACHE.clear()
    wr_cache._DIST_CHUNK_SIZES.clear()
    wr_cache.dist_chunk_cache_put(("AK", "c", "Normal", 0), {"IT": {}}, 12345)
    stats = wr_cache.cache_stats()
    check(stats["sizes"].get("dist_chunk") == 1, "(d) dist_chunk 건수 노출")
    check(stats["chunk_bytes"] == 12345, f"(d) chunk_bytes 노출 ({stats['chunk_bytes']})")
    check(set(stats.get("response") or {}) == {"full", "scatter", "dist_batch"},
          f"(d) response_cache 3종 provider 노출 ({sorted(stats.get('response') or {})})")

    # akey 무효화에 자동 편입 + 크기 기록도 함께 회수
    wr_cache.evict_akey_caches("AK")
    check(not wr_cache.DIST_CHUNK_CACHE and not wr_cache._DIST_CHUNK_SIZES,
          "(d) evict_akey_caches 가 chunk 캐시와 크기 기록을 함께 회수")


# ── (e) 진행 중 콜드 빌드 전역 목록 ──────────────────────────────────────────

def test_build_snapshot_all():
    build_status._ACTIVE.clear()
    build_status.begin("S_old", "report")
    time.sleep(0.05)
    build_status.begin("S_new", "map")
    rows = build_status.snapshot_all()
    check(len(rows) == 2, f"(e) 진행 중 빌드 2건 ({rows})")
    check(rows[0]["session_id"] == "S_old", "(e) 경과 내림차순 정렬")
    check(rows[0]["stage"] == "report" and rows[1]["stage"] == "map",
          "(e) stage 구분 유지")
    build_status.end("S_old", "report")
    build_status.end("S_new", "map")
    check(build_status.snapshot_all() == [], "(e) 종료 후 비어 있음")


# ── (f) 동시 열람 세션 계측 ──────────────────────────────────────────────────

def test_viewers():
    metrics._viewers.clear()
    app = Flask(__name__)

    @app.get("/pe/report/session/<session_id>/full", endpoint="report.session_full")
    def full(session_id):
        return "ok"

    @app.get("/other/<session_id>", endpoint="report.web_report_preprocess")
    def other(session_id):
        return "ok"

    metrics.init_app(app)
    client = app.test_client()
    client.get("/pe/report/session/SID1/full")
    client.get("/pe/report/session/SID2/full")
    client.get("/other/SID3")                      # 화이트리스트 밖 = 열람으로 안 셈

    v = metrics.viewers()
    ids = {s["session_id"] for s in v["sessions"]}
    check(v["count"] == 2 and ids == {"SID1", "SID2"}, f"(f) 열람 세션 2건 ({v})")

    metrics._viewers["SID_OLD"] = time.time() - 999
    check(metrics.viewers(window_sec=60)["count"] == 2, "(f) 윈도우 밖 세션 제외")
    check("SID_OLD" not in metrics._viewers, "(f) 조회 시 오래된 항목 prune")
    metrics._viewers.clear()


# ── (g) 부하 시계열 기록/파싱 ────────────────────────────────────────────────

def test_load_timeseries():
    for p in list(LOG_DIR.glob("runtime_*.log")) + list(LOG_DIR.glob("metrics_*.log")):
        p.unlink()
    metrics._rt_last_write = time.time() - 999
    metrics._runtime_record(time.time())
    recs = [json.loads(ln) for ln in
            (LOG_DIR / f"runtime_{time.strftime('%Y%m%d')}.log").read_text(
                encoding="utf-8").splitlines() if ln.strip()]
    load = [r for r in recs if r.get("type") == "load"]
    check(len(load) == 1, f"(g) load 라인 1개 기록 ({[r.get('type') for r in recs]})")
    check("viewers" in load[0] and "builds" in load[0] and "hit" in load[0],
          f"(g) 열람/빌드/캐시 필드 포함 ({sorted(load[0])})")

    # 누적 히트/미스 → 구간 증분(%) 환산. 첫 줄은 이전 값이 없어 None,
    # 재시작으로 카운터가 줄어든 구간도 None(음수 증분 무시).
    now = time.time()
    date = time.strftime("%Y%m%d", time.localtime(now))
    lines = []
    for i, (hit, miss) in enumerate([(100, 100), (190, 110), (10, 10)]):
        lines.append(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - (3 - i) * 60)),
            "type": "load", "viewers": i, "builds": i, "ondemand": 0, "distpack": 0,
            "hit": hit, "miss": miss, "disk_hit": 0, "disk_miss": 0}))
    (LOG_DIR / f"runtime_{date}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    h = metrics.file_history(hours=1)
    ld = h["load"]
    check(ld["ts"] and len(ld["ts"]) == 3, f"(g) load 시계열 3구간 ({ld['ts']})")
    check(ld["viewers"] == [0, 1, 2], f"(g) 열람 세션 시계열 ({ld['viewers']})")
    check(ld["hit_rate"][0] is None, "(g) 첫 구간은 증분 불가 → None")
    check(ld["hit_rate"][1] == 90.0, f"(g) 구간 히트율 90% ({ld['hit_rate'][1]})")
    check(ld["hit_rate"][2] is None, "(g) 카운터 리셋 구간(음수 증분)은 None")


# ── (h) api/runtime 응답 계약 ────────────────────────────────────────────────

def test_api_runtime_contract():
    import admin_panel
    from admin_panel.routes import admin_panel_bp

    app = Flask(__name__)
    app.register_blueprint(admin_panel_bp, url_prefix="/pe/admin-pte")
    client = app.test_client()
    client.set_cookie("pe_admin_gate", admin_panel.gate_token())

    build_status._ACTIVE.clear()
    build_status.begin("S_live", "report")
    metrics._viewers.clear()
    metrics._viewers["S_live"] = time.time()
    try:
        res = client.get("/pe/admin-pte/api/runtime")
        check(res.status_code == 200, f"(h) api/runtime 200 ({res.status_code})")
        data = res.get_json() or {}
        check(data.get("viewers", {}).get("count") == 1,
              f"(h) viewers 반환 ({data.get('viewers')})")
        check([b["session_id"] for b in data.get("builds") or []] == ["S_live"],
              f"(h) builds 반환 ({data.get('builds')})")
        cp = data.get("compute") or {}
        check("ondemand_pending" in cp and "distpack_pending" in cp,
              f"(h) compute 큐 대기 노출 ({sorted(cp)[:6]})")
        ca = data.get("cache") or {}
        check("response" in ca and "chunk_bytes" in ca and "lock_waits" in ca,
              f"(h) cache 확장 필드 노출 ({sorted(ca)})")
    finally:
        build_status.end("S_live", "report")
        metrics._viewers.clear()


# ── (i) 컴퓨트 워커 RSS 집계 ─────────────────────────────────────────────────

def _idle():
    return 1


def test_children_rss():
    from concurrent.futures import ProcessPoolExecutor

    from admin_panel import sysinfo

    sysinfo._ch_ts = 0.0                      # TTL 캐시 무시하고 즉시 측정
    base_rss, base_n = sysinfo.children_rss()

    pool = ProcessPoolExecutor(max_workers=2)
    try:
        [f.result(timeout=60) for f in [pool.submit(_idle), pool.submit(_idle)]]
        sysinfo._ch_ts = 0.0
        rss, n = sysinfo.children_rss()
        check(n >= base_n + 2, f"(i) 자식 프로세스 2개 집계 ({base_n} → {n})")
        check(rss > base_rss, f"(i) 자식 RSS 합이 잡힘 ({rss / 1048576:.0f}MB)")

        h = sysinfo.health()
        check(h["total_rss"] == h["proc_rss"] + h["workers_rss"],
              "(i) health.total_rss = 부모 + 워커")
        check(h["workers_n"] >= 2, f"(i) health.workers_n ({h['workers_n']})")
    finally:
        pool.shutdown(wait=True)

    sysinfo._ch_ts = 0.0
    _rss2, n2 = sysinfo.children_rss()
    check(n2 <= base_n, f"(i) 풀 종료 후 자식 수 복귀 ({n2})")

    # 시계열/파일 이력도 워커 RSS 를 함께 남긴다 (7번째 컬럼, 구파일은 0 으로 읽힘)
    for p in LOG_DIR.glob("metrics_*.log"):
        p.unlink()
    metrics._fr_last_minute = None
    metrics._flight_record(time.time(), 10.0, 1000, 2000, 1, 2, 4096)
    line = (LOG_DIR / f"metrics_{time.strftime('%Y%m%d')}.log").read_text(
        encoding="utf-8").strip()
    check(line.split(",")[-1] == "4096", f"(i) metrics 로그 7번째 컬럼 = 워커 RSS ({line})")

    now = time.time()
    old = "%s,10.0,2000,1000,1,2" % time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 120))
    new = "%s,10.0,2000,1000,1,2,4096" % time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 60))
    (LOG_DIR / f"metrics_{time.strftime('%Y%m%d')}.log").write_text(
        old + "\n" + new + "\n", encoding="utf-8")
    res = metrics.file_history(hours=1)["resource"]
    check(res["total_rss_max"] == [2000, 2000 + 4096],
          f"(i) 구파일(6컬럼)은 0 으로, 신파일은 합산 ({res['total_rss_max']})")


if __name__ == "__main__":
    try:
        test_cold_poll_not_blocked()
        test_response_cache_bytes_cap()
        test_lock_wait_metering()
        test_cache_stats_exposure()
        test_build_snapshot_all()
        test_viewers()
        test_load_timeseries()
        test_api_runtime_contract()
        test_children_rss()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if _failures:
        print(f"FAILED {len(_failures)}건:")
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")

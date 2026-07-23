"""runtime 이력(파일 기반) 기록·조회 검증 — 서버 재시작과 무관한 부하 이력.

배경: '무엇이 서버에 부담을 주는가'(느린 경로·응답시간·리소스)는 지금까지 전부
in-memory 라 재기동마다 초기화됐다. watchdog 재기동이 잦을수록 정작 필요한 순간에
비어 있으므로, flight recorder(metrics_*.log) 패턴을 확장해 runtime_*.log 에 남기고
admin 이 파일을 읽어 이력을 보여준다. 이 테스트가 다음을 고정한다:
  (a) 느린 요청(SLOW_REQ_MS 초과)이 요청 훅 → 샘플러 경유로 파일에 남는지
      (요청 경로에서 직접 파일 IO 를 하지 않는다는 계약 포함)
  (b) 주기 응답시간 스냅샷(lat) 라인 기록
  (c) file_history 가 metrics_/runtime_ 파일을 읽어 시계열·느린 요청·top route 구성

실행:
    python tests/test_runtime_history.py

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="runtime_hist_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["REPORT_SLOW_REQ_MS"] = "100"          # 테스트용 낮은 임계
os.environ["REPORT_RUNTIME_LOG_INTERVAL_SEC"] = "60"

from flask import Flask  # noqa: E402

import config  # noqa: E402
from admin_panel import metrics  # noqa: E402

config.ROOT_DIR = _TMP
LOG_DIR = _TMP / "server" / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _runtime_lines(date=None):
    date = date or time.strftime("%Y%m%d")
    path = LOG_DIR / f"runtime_{date}.log"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_slow_request_recorded():
    """느린 요청은 훅에서 큐에만 쌓이고, 샘플러가 파일로 내린다."""
    app = Flask(__name__)

    @app.get("/slow")
    def slow():
        return "ok"

    metrics.init_app(app)
    client = app.test_client()

    real_pc = time.perf_counter
    calls = {"n": 0}

    def fake_pc():                      # 시작/종료 사이를 0.5초로 위조
        calls["n"] += 1
        return real_pc() + (0.5 if calls["n"] % 2 == 0 else 0.0)

    time.perf_counter = fake_pc
    try:
        client.get("/slow")
    finally:
        time.perf_counter = real_pc

    # 요청 경로는 파일을 만들지 않는다 (IO 는 샘플러 몫)
    assert not _runtime_lines(), "요청 훅이 직접 파일에 기록함"
    assert len(metrics._slow_pending) == 1, metrics._slow_pending

    metrics._runtime_record(time.time())
    recs = _runtime_lines()
    slow = [r for r in recs if r["type"] == "slow"]
    assert len(slow) == 1, recs
    assert slow[0]["route"] == "slow", slow[0]
    assert slow[0]["ms"] >= 100, slow[0]
    assert not metrics._slow_pending, "기록 후 큐가 비지 않음"
    print("[a] 느린 요청 큐잉 → 샘플러 기록 OK (요청 경로 파일 IO 없음)")


def test_lat_snapshot():
    """주기 스냅샷(lat) 은 interval 이 지나야 기록된다."""
    before = len([r for r in _runtime_lines() if r["type"] == "lat"])
    metrics._rt_last_write = time.time()           # 방금 쓴 것으로 간주
    metrics._runtime_record(time.time())
    assert len([r for r in _runtime_lines() if r["type"] == "lat"]) == before, "주기 전에 기록됨"

    metrics._rt_last_write = time.time() - 999     # interval 경과
    metrics._runtime_record(time.time())
    lats = [r for r in _runtime_lines() if r["type"] == "lat"]
    assert len(lats) == before + 1, lats
    assert "p95" in lats[-1] and "top" in lats[-1], lats[-1]
    print("[b] 주기 응답시간 스냅샷 기록 OK")


def test_file_history():
    """이틀치 파일을 시드해 구간 필터·다운샘플·집계를 확인."""
    now = time.time()
    for p in list(LOG_DIR.glob("runtime_*.log")) + list(LOG_DIR.glob("metrics_*.log")):
        p.unlink()      # 앞선 테스트가 남긴 기록과 섞이지 않게 초기화
    for day_ago in (0, 1):
        date = time.strftime("%Y%m%d", time.localtime(now - day_ago * 86400))
        rows = []
        for i in range(120):                        # 분당 1줄 x 2시간
            ts = now - day_ago * 86400 - i * 60
            rows.append("%s,%.1f,%d,%d,%d,%d" % (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
                10.0 + i % 5, 1000 + i, 2000 + i, i % 3, i % 4))
        (LOG_DIR / f"metrics_{date}.log").write_text("\n".join(rows) + "\n", encoding="utf-8")

        lines = [json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(now - day_ago * 86400 - 600)),
            "type": "lat", "n": 10, "p50": 5, "p95": 50 + day_ago, "p99": 90, "max": 120,
            "top": [{"route": "report.view", "count": 3, "avg_ms": 40, "max_ms": 300 + day_ago}]})]
        lines.append(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(now - day_ago * 86400 - 300)),
            "type": "slow", "route": "report.full", "ms": 15000}))
        (LOG_DIR / f"runtime_{date}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    h6 = metrics.file_history(hours=6)
    assert h6["resource"]["ts"], "리소스 시계열이 비어 있음"
    assert len(h6["slow"]) == 1, h6["slow"]         # 어제 건은 구간 밖
    assert h6["resource"]["ts"] == sorted(h6["resource"]["ts"]), "시계열 정렬 아님"
    assert min(h6["resource"]["ts"]) >= now - 6 * 3600 - 60, "구간 밖 샘플 포함"

    h48 = metrics.file_history(hours=48)
    assert len(h48["slow"]) == 2, h48["slow"]        # 이틀치 모두
    assert h48["slow"][0]["ts"] >= h48["slow"][1]["ts"], "느린 요청 최신 먼저 정렬 아님"
    assert h48["top_routes"] and h48["top_routes"][0]["route"] == "report.view", h48["top_routes"]
    assert len(h48["lat"]["p95"]) == 2, h48["lat"]
    assert len(h48["files"]) >= 3, h48["files"]

    small = metrics.file_history(hours=48, max_points=10)
    assert len(small["resource"]["ts"]) <= 10, len(small["resource"]["ts"])
    assert max(small["resource"]["cpu_max"]) == max(h48["resource"]["cpu_max"]), "다운샘플이 피크를 잃음"

    assert metrics.file_history(hours=99999)["hours"] == metrics.FILE_HISTORY_MAX_HOURS
    print("[c] file_history 구간 필터·정렬·집계·다운샘플(피크 보존) OK")


def main():
    try:
        test_slow_request_recorded()
        test_lat_snapshot()
        test_file_history()
        print("\nALL OK")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

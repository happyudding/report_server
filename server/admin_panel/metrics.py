"""서버 리소스 시계열 샘플러 + in-flight 요청 카운터 (admin 패널 모니터링).

오버헤드 원칙: 요청 경로에는 lock 1회 + 정수 증감만 얹는다. 샘플링은 10초 간격
데몬 스레드 1개가 psutil 순간값을 링버퍼(24h, deque maxlen)에 쌓는 것이 전부.
CPU 는 sysinfo._cpu_percent() 를 재사용한다 — psutil.cpu_percent(interval=None) 은
프로세스 전역 baseline 을 공유하므로 call-site 를 1개로 유지해야 기존 /api/health
값과 서로 구간을 잘라먹지 않는다.

구간 피크(5분/1시간/24시간)는 조회 시점에 링버퍼에서 계산하고(관리자 조회 시에만
수 ms), 기동 이후 최고치만 running max 로 상시 유지한다.

REPORT_METRICS_ENABLED=0 이면 init_app 이 no-op (kill-switch).
"""
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

import psutil
from flask import g, request

from admin_panel.sysinfo import _cpu_percent

_log = logging.getLogger(__name__)

METRICS_ENABLED = os.getenv("REPORT_METRICS_ENABLED", "1") != "0"
SAMPLE_INTERVAL = max(1.0, float(os.getenv("REPORT_METRICS_INTERVAL_SEC", "10")))
RETENTION_SEC = 24 * 3600
# wsgi.py 와 동일 규칙 — in-flight 점유율 분모용
WAITRESS_THREADS = int(os.getenv("WAITRESS_THREADS", "13"))

# flight recorder — 링버퍼(_samples)는 메모리 전용이라 프로세스가 죽으면 크래시 직전
# 리소스 추이가 함께 사라진다. 그 부검을 위해 1분에 1줄씩 metrics_YYYYMMDD.log 에 남긴다.
# REPORT_METRICS_FILE_KEEP_DAYS=0 이면 비활성.
METRICS_FILE_KEEP_DAYS = float(os.getenv("REPORT_METRICS_FILE_KEEP_DAYS", "14"))
_fr_last_minute = None   # 마지막 기록한 분 — 분이 바뀔 때만 append
_fr_last_date = None     # 마지막 기록 날짜 — 롤오버 시 오래된 파일 prune
_fr_warned = False       # 기록 실패 반복 경고 억제 (디스크 풀 시 로그 폭주 방지)

_proc = psutil.Process()
_lock = threading.Lock()  # 카운터·링버퍼 공용 (임계구역은 정수 연산·append 뿐)
# 샘플: (ts, cpu%, mem_used, proc_rss, inflight, inflight_window_peak)
_samples = deque(maxlen=int(RETENTION_SEC / SAMPLE_INTERVAL))
_inflight = 0
_inflight_window_peak = 0  # 샘플 구간 내 순간 최대 동시 요청 (샘플러가 읽고 리셋)
_boot_peaks = {"cpu": (0.0, 0.0), "rss": (0, 0.0), "mem": (0, 0.0), "inflight": (0, 0.0)}
_started = False

# 응답시간 — 최근 요청 소요(ms) 링버퍼(백분위용) + endpoint 별 누적(느린 경로 식별용).
# endpoint 수는 라우트 수만큼이라 상한이 자연스럽다.
_lat_recent = deque(maxlen=2000)
_lat_by_route = {}


def _on_request_start():
    global _inflight, _inflight_window_peak
    with _lock:
        _inflight += 1
        if _inflight > _inflight_window_peak:
            _inflight_window_peak = _inflight
    g._mx_counted = True
    g._mx_t0 = time.perf_counter()


def _on_request_teardown(exc=None):
    # 다른 before_request 가 먼저 abort 하면 우리 훅이 안 돌았을 수 있다 — 플래그 확인
    global _inflight
    if not g.pop("_mx_counted", None):
        return
    t0 = g.pop("_mx_t0", None)
    ms = (time.perf_counter() - t0) * 1000.0 if t0 is not None else None
    try:
        route = request.endpoint or request.path
    except Exception:
        route = "?"
    with _lock:
        _inflight -= 1
        if ms is not None:
            _lat_recent.append(ms)
            n, total, mx = _lat_by_route.get(route, (0, 0.0, 0.0))
            _lat_by_route[route] = (n + 1, total + ms, max(mx, ms))


def _bump_boot_peak(key, value, ts):
    if value > _boot_peaks[key][0]:
        _boot_peaks[key] = (value, ts)


def _prune_flight_files(log_dir):
    """오래된 metrics_*.log 정리 (best-effort)."""
    try:
        cutoff = time.time() - METRICS_FILE_KEEP_DAYS * 86400
        for p in log_dir.glob("metrics_*.log"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _flight_record(ts, cpu, mem_used, rss, inflight, win_peak):
    """분이 바뀔 때만 metrics_YYYYMMDD.log 에 1줄 append (기록마다 open/close — 1회/분이라
    비용 무시 가능, 핸들 상시 보유 없이 외부 삭제·수집과 충돌 없음)."""
    global _fr_last_minute, _fr_last_date, _fr_warned
    if METRICS_FILE_KEEP_DAYS <= 0:
        return
    minute = int(ts // 60)
    if minute == _fr_last_minute:
        return
    _fr_last_minute = minute
    try:
        import config
        log_dir = Path(config.ROOT_DIR) / "server" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        lt = time.localtime(ts)
        date = time.strftime("%Y%m%d", lt)
        if date != _fr_last_date:      # 날짜 롤오버(+ 샘플러 시작 첫 기록) 시 prune
            _fr_last_date = date
            _prune_flight_files(log_dir)
        line = "%s,%.1f,%d,%d,%d,%d\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%S", lt), cpu, rss, mem_used, inflight, win_peak)
        with (log_dir / f"metrics_{date}.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        if not _fr_warned:
            _fr_warned = True
            _log.warning("[metrics] flight recorder write failed (further warnings suppressed)", exc_info=True)


def _sample():
    ts = time.time()
    cpu = _cpu_percent()
    mem_used = psutil.virtual_memory().used
    rss = _proc.memory_info().rss
    with _lock:
        global _inflight_window_peak
        inflight = _inflight
        win_peak = _inflight_window_peak
        _inflight_window_peak = inflight
        _samples.append((ts, cpu, mem_used, rss, inflight, win_peak))
        _bump_boot_peak("cpu", cpu, ts)
        _bump_boot_peak("mem", mem_used, ts)
        _bump_boot_peak("rss", rss, ts)
        _bump_boot_peak("inflight", win_peak, ts)
    # 파일 IO 는 락 밖에서 (요청 경로의 in-flight 카운터를 막지 않도록)
    _flight_record(ts, cpu, mem_used, rss, inflight, win_peak)


def _loop():
    time.sleep(SAMPLE_INTERVAL)  # 서버 초기화(priming 직후 cpu 0.0)와 겹치지 않게 지연
    while True:
        try:
            _sample()
        except Exception:
            _log.exception("[metrics] sample failed")
        time.sleep(SAMPLE_INTERVAL)


def init_app(app):
    """app 전역 in-flight 훅 등록 + 샘플러 데몬 기동. 중복 기동은 _started 로 방지."""
    global _started
    if not METRICS_ENABLED:
        _log.info("[metrics] disabled (REPORT_METRICS_ENABLED=0)")
        return
    if _started:
        return
    _started = True
    app.before_request(_on_request_start)
    app.teardown_request(_on_request_teardown)
    threading.Thread(target=_loop, name="admin-metrics-sampler", daemon=True).start()
    _log.info("[metrics] sampler started: interval=%.0fs retention=%dh buf=%d",
              SAMPLE_INTERVAL, RETENTION_SEC // 3600, _samples.maxlen)


def current_inflight():
    """현재 처리 중인 요청 수(호출자 자신 포함). 비활성/미기동이면 None.

    "모름(None)" 과 "0건" 을 반드시 구분해야 한다 — terminate.bat 의 종료 drain 이
    이 값을 보고 "지금 내려도 되는가" 를 판정하므로, 카운터가 없는 상태를 0건으로
    오인하면 진행 중인 업로드를 끊게 된다 (→ ops.healthz).
    """
    if not METRICS_ENABLED or not _started:
        return None
    with _lock:
        return _inflight


def _pct(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q)))
    return round(sorted_vals[idx], 1)


def latency_snapshot(top=8):
    """최근 요청 응답시간 백분위 + 평균이 느린 endpoint 상위 목록.

    p95/p99 는 최근 _lat_recent(최대 2000건) 기준이라 "지금 느려졌는지"를 보고,
    route 별 평균/최대는 기동 이후 누적이라 "원래 무거운 경로"를 본다.
    """
    with _lock:
        recent = sorted(_lat_recent)
        routes = [(r, n, total / n, mx) for r, (n, total, mx) in _lat_by_route.items() if n]
    routes.sort(key=lambda x: x[2], reverse=True)
    return {
        "samples": len(recent),
        "p50": _pct(recent, 0.50), "p95": _pct(recent, 0.95), "p99": _pct(recent, 0.99),
        "max": round(recent[-1], 1) if recent else 0.0,
        "slowest": [{"route": r, "count": n, "avg_ms": round(avg, 1), "max_ms": round(mx, 1)}
                    for r, n, avg, mx in routes[:top]],
    }


def _window_peaks(rows, now, window_sec):
    cut = now - window_sec
    cpu = rss = mem = infl = 0
    for ts, c, m, r, _i, wp in rows:
        if ts < cut:
            continue
        if c > cpu:
            cpu = c
        if m > mem:
            mem = m
        if r > rss:
            rss = r
        if wp > infl:
            infl = wp
    return {"cpu": cpu, "mem_used": mem, "rss": rss, "inflight": infl}


def snapshot_history(window_sec, max_points=360):
    """window_sec 구간 시계열 + 피크 요약. 초과 시 버킷 다운샘플(버킷별 max/avg —
    max 를 보존해 피크가 차트에서 사라지지 않게 함)."""
    now = time.time()
    with _lock:
        rows = list(_samples)
        current = {"inflight": _inflight, "cpu": _cpu_percent(),
                   "mem_used": psutil.virtual_memory().used,
                   "rss": _proc.memory_info().rss}
        boot = {k: {"v": v, "ts": ts} for k, (v, ts) in _boot_peaks.items()}

    peaks = {"w300": _window_peaks(rows, now, 300),
             "w3600": _window_peaks(rows, now, 3600),
             "w86400": _window_peaks(rows, now, 86400),
             "boot": boot}

    cut = now - window_sec
    win = [r for r in rows if r[0] >= cut]
    series = {"ts": [], "cpu_avg": [], "cpu_max": [],
              "mem_used_max": [], "rss_max": [], "inflight_max": []}
    if win:
        bucket = max(1, (len(win) + max_points - 1) // max_points)
        for i in range(0, len(win), bucket):
            chunk = win[i:i + bucket]
            series["ts"].append(round(chunk[-1][0]))
            cpus = [c[1] for c in chunk]
            series["cpu_avg"].append(round(sum(cpus) / len(cpus), 1))
            series["cpu_max"].append(max(cpus))
            series["mem_used_max"].append(max(c[2] for c in chunk))
            series["rss_max"].append(max(c[3] for c in chunk))
            series["inflight_max"].append(max(c[5] for c in chunk))

    return {"now": round(now), "interval": SAMPLE_INTERVAL, "threads": WAITRESS_THREADS,
            "enabled": METRICS_ENABLED, "current": current,
            "series": series, "peaks": peaks}

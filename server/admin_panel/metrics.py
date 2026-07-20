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

import psutil
from flask import g

from admin_panel.sysinfo import _cpu_percent

_log = logging.getLogger(__name__)

METRICS_ENABLED = os.getenv("REPORT_METRICS_ENABLED", "1") != "0"
SAMPLE_INTERVAL = max(1.0, float(os.getenv("REPORT_METRICS_INTERVAL_SEC", "10")))
RETENTION_SEC = 24 * 3600
# wsgi.py 와 동일 규칙 — in-flight 점유율 분모용
WAITRESS_THREADS = int(os.getenv("WAITRESS_THREADS", "8"))

_proc = psutil.Process()
_lock = threading.Lock()  # 카운터·링버퍼 공용 (임계구역은 정수 연산·append 뿐)
# 샘플: (ts, cpu%, mem_used, proc_rss, inflight, inflight_window_peak)
_samples = deque(maxlen=int(RETENTION_SEC / SAMPLE_INTERVAL))
_inflight = 0
_inflight_window_peak = 0  # 샘플 구간 내 순간 최대 동시 요청 (샘플러가 읽고 리셋)
_boot_peaks = {"cpu": (0.0, 0.0), "rss": (0, 0.0), "mem": (0, 0.0), "inflight": (0, 0.0)}
_started = False


def _on_request_start():
    global _inflight, _inflight_window_peak
    with _lock:
        _inflight += 1
        if _inflight > _inflight_window_peak:
            _inflight_window_peak = _inflight
    g._mx_counted = True


def _on_request_teardown(exc=None):
    # 다른 before_request 가 먼저 abort 하면 우리 훅이 안 돌았을 수 있다 — 플래그 확인
    global _inflight
    if g.pop("_mx_counted", None):
        with _lock:
            _inflight -= 1


def _bump_boot_peak(key, value, ts):
    if value > _boot_peaks[key][0]:
        _boot_peaks[key] = (value, ts)


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

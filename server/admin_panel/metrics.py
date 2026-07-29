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
import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

import psutil
from flask import g, request

import auth_identity
from admin_panel.sysinfo import _cpu_percent, children_rss as _children_rss

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

# runtime 이력 — "무엇이 서버에 부담을 주는가"(느린 경로/느린 요청)는 지금까지 in-memory
# 라 재기동마다 사라졌다. watchdog 재기동이 잦을수록 정작 필요한 순간에 비어 있으므로
# runtime_YYYYMMDD.log(JSON lines)에 남긴다. 보관은 metrics_*.log 와 같은 정책.
RUNTIME_LOG_INTERVAL = max(60.0, float(os.getenv("REPORT_RUNTIME_LOG_INTERVAL_SEC", "300")))
SLOW_REQ_MS = float(os.getenv("REPORT_SLOW_REQ_MS", "10000"))
_slow_pending = deque(maxlen=200)  # (ts, route, ms) — 요청 훅이 넣고 샘플러가 비운다
_rt_last_write = 0.0
_rt_warned = False

_proc = psutil.Process()
_lock = threading.Lock()  # 카운터·링버퍼 공용 (임계구역은 정수 연산·append 뿐)
# 샘플: (ts, cpu%, mem_used, proc_rss, inflight, inflight_window_peak, workers_rss)
# workers_rss = 컴퓨트 워커(자식 프로세스) RSS 합 — proc_rss 에는 안 잡힌다.
_samples = deque(maxlen=int(RETENTION_SEC / SAMPLE_INTERVAL))
_inflight = 0
_inflight_window_peak = 0  # 샘플 구간 내 순간 최대 동시 요청 (샘플러가 읽고 리셋)
_boot_peaks = {"cpu": (0.0, 0.0), "rss": (0, 0.0), "mem": (0, 0.0), "inflight": (0, 0.0),
               "total_rss": (0, 0.0)}   # total_rss = 부모 + 컴퓨트 워커
_started = False

# 응답시간 — 최근 요청 소요(ms) 링버퍼(백분위용) + endpoint 별 누적(느린 경로 식별용).
# endpoint 수는 라우트 수만큼이라 상한이 자연스럽다.
_lat_recent = deque(maxlen=2000)
_lat_by_route = {}

# 동시 열람 세션 — "지금 몇 명이 어떤 세션을 보고 있나"가 부하 원인 파악의 출발점인데
# 지금까지 계측이 없었다. 별도 heartbeat 엔드포인트를 두는 대신(프런트 배포 + 상시
# 트래픽이 필요) **세션 데이터를 실제로 요청한 흔적**을 endpoint 화이트리스트로 줍는다.
# 화면을 열어만 두고 아무 요청도 안 하는 열람자는 안 잡히지만, 그런 세션은 부하가 아니다.
_VIEWER_ENDPOINTS = frozenset((
    "report.session_full", "report.web_report_distribution",
    "report.web_report_distribution_batch", "report.web_report_scatter",
    "report.web_report_map_analysis", "report.web_report_raw_data",
    "report.web_report_trim_analysis", "report.web_report_trim_chart_batch"))
_VIEWERS_MAX = 500          # 상한 — 초과 시 가장 오래된 것부터 버린다
VIEWER_WINDOW_SEC = 300     # "최근 N초 안에 요청이 있었으면 열람 중"
_viewers: OrderedDict = OrderedDict()   # session_id -> 마지막 요청 ts (_lock 공유)

# 실시간 접속 사용자 — _viewers 는 "어떤 세션이 열려 있나"라서 사람 수를 모른다. 여기서는
# 요청 신원(auth_identity.current_user — Honey UA / SSO 헤더 / 웹 로그인)을 키로 최근 활동을
# 모은다. 신원이 없는 일반 브라우저는 ip:<addr> 로 묶어 "누군지는 몰라도 접속 중"은 보이게 한다.
# 오버헤드는 요청당 UA 정규식 1회 + dict 갱신 1회 (기존 teardown 훅 안에서 처리).
ACTIVE_USER_WINDOW_SEC = max(30, int(os.getenv("REPORT_ACTIVE_USER_WINDOW_SEC", "300")))
_ACTIVE_USERS_MAX = 300
_active_users: OrderedDict = OrderedDict()   # key -> dict(uid, ip, honey, first, last, count, route, session_id)


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
    sid = None
    if route in _VIEWER_ENDPOINTS:      # 락 밖에서 뽑는다 (dict 조회 2회)
        try:
            sid = (request.view_args or {}).get("session_id")
        except Exception:
            sid = None
    # 접속 사용자 — 세션 데이터 요청이 아니어도(목록·편집 API 등) 사람은 접속 중이므로
    # _VIEWER_ENDPOINTS 보다 넓게 잡는다. 지금 보고 있는 세션은 참고용으로만 곁들이므로
    # 열람 세션 계측(sid)과 변수를 분리한다 — 섞으면 viewers 화이트리스트가 무너진다.
    ident = user_sid = None
    if not _skip_user_track(route):
        ident = _identity_for_track()
        if ident is not None:
            try:
                user_sid = (request.view_args or {}).get("session_id")
            except Exception:
                user_sid = None
    with _lock:
        _inflight -= 1
        if ident is not None:
            key, uid, ip, honey = ident
            now = time.time()
            rec = _active_users.get(key)
            if rec is None:
                rec = {"uid": uid, "ip": ip, "honey": honey, "first": now, "count": 0,
                       "session_id": None}
                _active_users[key] = rec
            rec["last"] = now
            rec["count"] += 1
            rec["ip"] = ip
            rec["honey"] = honey
            rec["route"] = route
            if user_sid:
                rec["session_id"] = user_sid
            _active_users.move_to_end(key)
            while len(_active_users) > _ACTIVE_USERS_MAX:
                _active_users.popitem(last=False)
        if sid:
            _viewers[sid] = time.time()
            _viewers.move_to_end(sid)
            while len(_viewers) > _VIEWERS_MAX:
                _viewers.popitem(last=False)
        if ms is not None:
            _lat_recent.append(ms)
            n, total, mx = _lat_by_route.get(route, (0, 0.0, 0.0))
            _lat_by_route[route] = (n + 1, total + ms, max(mx, ms))
            # 느린 요청은 큐에만 넣는다 — 파일 IO 는 샘플러 스레드가 락 밖에서 처리
            if SLOW_REQ_MS > 0 and ms >= SLOW_REQ_MS:
                _slow_pending.append((time.time(), route, ms))


def _skip_user_track(route):
    """접속 사용자 집계에서 뺄 요청 — 관리자 자신·healthz(watchdog 폴링)·정적 파일.

    이것들을 빼야 "지금 몇 명이 쓰고 있나"가 실사용자 수에 가까워진다."""
    return (not route or route == "healthz" or route.endswith("static")
            or route.startswith("admin_panel."))


def _identity_for_track():
    """요청 신원 → (key, uid, ip, honey). 요청 컨텍스트 문제가 생기면 None (집계 생략)."""
    try:
        ip = request.remote_addr or "?"
        ua = request.headers.get("User-Agent") or ""
    except Exception:
        return None
    try:
        uid = auth_identity.current_user()
    except Exception:
        uid = ""     # SECRET_KEY 미설정 등으로 로그인 세션 조회가 터져도 IP 로는 잡는다
    return (uid or f"ip:{ip}", uid, ip, "HoneyUser/" in ua)


def _bump_boot_peak(key, value, ts):
    if value > _boot_peaks[key][0]:
        _boot_peaks[key] = (value, ts)


def _prune_flight_files(log_dir):
    """오래된 metrics_*.log / runtime_*.log 정리 (best-effort)."""
    try:
        cutoff = time.time() - METRICS_FILE_KEEP_DAYS * 86400
        for pattern in ("metrics_*.log", "runtime_*.log"):
            for p in log_dir.glob(pattern):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def _flight_record(ts, cpu, mem_used, rss, inflight, win_peak, workers_rss=0):
    """분이 바뀔 때만 metrics_YYYYMMDD.log 에 1줄 append (기록마다 open/close — 1회/분이라
    비용 무시 가능, 핸들 상시 보유 없이 외부 삭제·수집과 충돌 없음).

    workers_rss 는 **7번째 컬럼으로 뒤에 붙인다** — 6컬럼짜리 기존 파일도 그대로 파싱되게
    하기 위함이다(file_history 가 7번째를 옵셔널로 읽는다)."""
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
        line = "%s,%.1f,%d,%d,%d,%d,%d\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%S", lt), cpu, rss, mem_used, inflight, win_peak,
            workers_rss)
        with (log_dir / f"metrics_{date}.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        if not _fr_warned:
            _fr_warned = True
            _log.warning("[metrics] flight recorder write failed (further warnings suppressed)", exc_info=True)


def _runtime_record(ts):
    """runtime_YYYYMMDD.log 에 (a) 쌓인 느린 요청 이벤트 (b) 주기적 응답시간 스냅샷을
    JSON line 으로 append. 샘플러 스레드에서 락 밖으로 호출한다 (요청 경로 무영향 —
    느린 요청은 최대 SAMPLE_INTERVAL 만큼 늦게 기록되고 종료 시 마지막 구간은 유실 허용)."""
    global _rt_last_write, _rt_warned
    if METRICS_FILE_KEEP_DAYS <= 0:
        return
    with _lock:
        pending = list(_slow_pending)
        _slow_pending.clear()
    due = (ts - _rt_last_write) >= RUNTIME_LOG_INTERVAL
    if not pending and not due:
        return
    lines = []
    for s_ts, route, ms in pending:
        lines.append(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s_ts)),
                                 "type": "slow", "route": route, "ms": round(ms, 1)},
                                ensure_ascii=False))
    if due:
        _rt_last_write = ts
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
        lat = latency_snapshot(top=5)
        lines.append(json.dumps({"ts": stamp,
                                 "type": "lat", "n": lat["samples"], "p50": lat["p50"],
                                 "p95": lat["p95"], "p99": lat["p99"], "max": lat["max"],
                                 "top": lat["slowest"]}, ensure_ascii=False))
        # 부하 스냅샷 — 캐시 히트/미스는 **기동 이후 누적**이라 구간 증분은 화면에서
        # 환산한다(재시작으로 카운터가 리셋되면 음수가 되므로 그때는 0 취급).
        load = dict(load_snapshot())
        load["ts"] = stamp
        load["type"] = "load"
        lines.append(json.dumps(load, ensure_ascii=False))
    try:
        import config
        log_dir = Path(config.ROOT_DIR) / "server" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        date = time.strftime("%Y%m%d", time.localtime(ts))
        with (log_dir / f"runtime_{date}.log").open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        if not _rt_warned:
            _rt_warned = True
            _log.warning("[metrics] runtime history write failed (further warnings suppressed)",
                         exc_info=True)


def _sample():
    ts = time.time()
    cpu = _cpu_percent()
    mem_used = psutil.virtual_memory().used
    rss = _proc.memory_info().rss
    wrss, _wn = _children_rss()      # 컴퓨트 워커 RSS 합 (부모 RSS 에는 안 잡힌다)
    with _lock:
        global _inflight_window_peak
        inflight = _inflight
        win_peak = _inflight_window_peak
        _inflight_window_peak = inflight
        _samples.append((ts, cpu, mem_used, rss, inflight, win_peak, wrss))
        _bump_boot_peak("cpu", cpu, ts)
        _bump_boot_peak("mem", mem_used, ts)
        _bump_boot_peak("rss", rss, ts)
        _bump_boot_peak("inflight", win_peak, ts)
        _bump_boot_peak("total_rss", rss + wrss, ts)
    # 파일 IO 는 락 밖에서 (요청 경로의 in-flight 카운터를 막지 않도록)
    _flight_record(ts, cpu, mem_used, rss, inflight, win_peak, wrss)
    _runtime_record(ts)


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


def viewers(window_sec=VIEWER_WINDOW_SEC):
    """최근 window_sec 안에 세션 데이터를 요청한 세션 목록 (동시 열람 근사).

    prune 은 관리자 조회 시점에만 한다 — 요청 경로에 O(n) 을 얹지 않기 위함이다.
    """
    now = time.time()
    cut = now - window_sec
    with _lock:
        for sid in [s for s, ts in _viewers.items() if ts < cut]:
            _viewers.pop(sid, None)
        rows = [(sid, ts) for sid, ts in _viewers.items() if ts >= cut]
    rows.sort(key=lambda r: r[1], reverse=True)
    return {"count": len(rows), "window_sec": window_sec,
            "sessions": [{"session_id": sid, "ago": round(now - ts, 1)} for sid, ts in rows]}


def live_identity_pairs():
    """지금 추적 중인 (계정, IP) 짝 — identity_merge 가 매핑 근거로 쓴다.

    감사 기록이 아직 없는 새 PC 도 접속하는 순간 매핑에 잡히게 하는 용도라, 윈도우와
    무관하게 보유 중인 항목 전부를 준다."""
    with _lock:
        return [(v["uid"], v["ip"]) for v in _active_users.values() if v.get("uid")]


def active_users(window_sec=ACTIVE_USER_WINDOW_SEC):
    """최근 window_sec 안에 요청을 보낸 접속 사용자 목록 (실시간 현황).

    신원(Honey UA/SSO/웹 로그인)이 있으면 계정으로, 없으면 ip:<addr> 로 묶는다. 단
    **IP 가 같으면 같은 사람**이므로, 그 IP 가 계정 하나로 확정되면(identity_merge)
    익명 행을 그 계정 행에 합친다 — 한 사람이 Honey 와 일반 브라우저를 같이 쓸 때
    두 줄로 갈라져 보이던 문제를 없앤다.

    viewers() 와 같은 이유로 prune 은 관리자 조회 시점에만 한다 (요청 경로에 O(n) 을
    얹지 않는다).
    """
    try:
        window_sec = max(30, min(int(window_sec), 24 * 3600))
    except (TypeError, ValueError):
        window_sec = ACTIVE_USER_WINDOW_SEC
    now = time.time()
    cut = now - window_sec
    with _lock:
        for key in [k for k, v in _active_users.items() if v["last"] < cut]:
            _active_users.pop(key, None)
        rows = [(k, dict(v)) for k, v in _active_users.items() if v["last"] >= cut]
    rows.sort(key=lambda r: r[1]["last"], reverse=True)

    from admin_panel import identity_merge
    mapping = identity_merge.ip_to_user()

    merged = OrderedDict()   # 표시 키 -> 누적 행 (최근 활동 순서 유지)
    for key, v in rows:
        name, was_merged = identity_merge.resolve(v["uid"] or key, v["ip"], mapping)
        cur = merged.get(name)
        if cur is None:
            merged[name] = {
                "key": name, "user": name if (v["uid"] or was_merged) else "",
                "ip": v["ip"], "ips": [v["ip"]], "honey": bool(v["honey"]),
                "requests": v["count"], "last": v["last"], "first": v["first"],
                "route": v.get("route") or "", "session_id": v.get("session_id"),
                "merged": was_merged,
            }
            continue
        # 합치기 — 요청 수는 더하고, 마지막 활동/보는 세션은 더 최근 쪽을 남긴다.
        cur["requests"] += v["count"]
        cur["first"] = min(cur["first"], v["first"])
        cur["honey"] = cur["honey"] or bool(v["honey"])
        cur["merged"] = True
        if v["ip"] not in cur["ips"]:
            cur["ips"].append(v["ip"])
        if v["last"] > cur["last"]:
            cur["last"] = v["last"]
            cur["route"] = v.get("route") or ""
            cur["ip"] = v["ip"]
            if v.get("session_id"):
                cur["session_id"] = v["session_id"]
        elif not cur.get("session_id") and v.get("session_id"):
            cur["session_id"] = v["session_id"]

    out = []
    for rec in sorted(merged.values(), key=lambda r: r["last"], reverse=True):
        out.append({"key": rec["key"], "user": rec["user"], "ip": rec["ip"],
                    "ips": rec["ips"], "honey": rec["honey"], "merged": rec["merged"],
                    "requests": rec["requests"],
                    "ago": round(now - rec["last"], 1),
                    "since": round(now - rec["first"], 1),
                    "route": rec["route"], "session_id": rec["session_id"]})
    return {"count": len(out), "window_sec": window_sec,
            "named": sum(1 for r in out if r["user"]),
            "honey": sum(1 for r in out if r["honey"]),
            "users": out}


def load_snapshot():
    """부하 요약 — 동시 열람 + 컴퓨트 큐 + 진행 중 콜드 빌드 + 캐시 누적 히트/미스.

    api/runtime 집계와 시계열 기록(_runtime_record)이 같은 함수를 쓴다. web_report 는
    지연 import + 개별 try — 미기동/kill-switch 여도 나머지 값은 살린다.
    """
    out = {"viewers": viewers()["count"]}
    try:
        from web_report import compute
        st = compute.status()
        out.update(ondemand=st.get("ondemand_pending"), distpack=st.get("distpack_pending"),
                   prewarm=st.get("prewarm_pending"))
    except Exception:
        pass
    try:
        from web_report import build_status
        out["builds"] = len(build_status.snapshot_all())
    except Exception:
        pass
    try:
        from web_report import cache as wr_cache
        cs = wr_cache.cache_stats()
        out.update(hit=cs.get("hit"), miss=cs.get("miss"),
                   disk_hit=cs.get("disk_hit"), disk_miss=cs.get("disk_miss"))
    except Exception:
        pass
    return out


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
    cpu = rss = mem = infl = total = 0
    for row in rows:
        ts, c, m, r, _i, wp = row[:6]
        wrss = row[6] if len(row) > 6 else 0
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
        if r + wrss > total:
            total = r + wrss
    return {"cpu": cpu, "mem_used": mem, "rss": rss, "inflight": infl,
            "total_rss": total}


def snapshot_history(window_sec, max_points=360):
    """window_sec 구간 시계열 + 피크 요약. 초과 시 버킷 다운샘플(버킷별 max/avg —
    max 를 보존해 피크가 차트에서 사라지지 않게 함)."""
    now = time.time()
    wrss, wn = _children_rss()
    with _lock:
        rows = list(_samples)
        rss_now = _proc.memory_info().rss
        current = {"inflight": _inflight, "cpu": _cpu_percent(),
                   "mem_used": psutil.virtual_memory().used,
                   "rss": rss_now, "workers_rss": wrss, "workers_n": wn,
                   "total_rss": rss_now + wrss}
        boot = {k: {"v": v, "ts": ts} for k, (v, ts) in _boot_peaks.items()}

    peaks = {"w300": _window_peaks(rows, now, 300),
             "w3600": _window_peaks(rows, now, 3600),
             "w86400": _window_peaks(rows, now, 86400),
             "boot": boot}

    cut = now - window_sec
    win = [r for r in rows if r[0] >= cut]
    series = {"ts": [], "cpu_avg": [], "cpu_max": [],
              "mem_used_max": [], "rss_max": [], "inflight_max": [], "total_rss_max": []}
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
            series["total_rss_max"].append(
                max(c[3] + (c[6] if len(c) > 6 else 0) for c in chunk))

    return {"now": round(now), "interval": SAMPLE_INTERVAL, "threads": WAITRESS_THREADS,
            "enabled": METRICS_ENABLED, "current": current,
            "series": series, "peaks": peaks}


# ── 파일 기반 이력 (재시작과 무관) ────────────────────────────────────────────

FILE_HISTORY_MAX_HOURS = 24 * 14  # 파일 보관 기간(14일) 과 동일


def _hist_epoch(text):
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError, TypeError):
        return 0


def _hist_files(log_dir, prefix, hours, now):
    """구간에 걸치는 날짜 파일만 고른다 (일별 파일 — 경계 하루를 여유로 포함)."""
    days = int(hours // 24) + 2
    names = [time.strftime(f"{prefix}_%Y%m%d.log", time.localtime(now - d * 86400))
             for d in range(days)]
    return [log_dir / n for n in reversed(names) if (log_dir / n).exists()]


def file_history(hours=24, max_points=500):
    """metrics_*.log(1분 해상도 리소스) + runtime_*.log(응답시간·느린 요청)를 읽어
    재시작과 무관한 이력을 돌려준다. in-memory snapshot_history 를 대체하지 않고 병행한다
    (현황 탭 = 10초 해상도 실시간 / 이력 탭 = 1분 해상도 최대 14일)."""
    try:
        hours = max(1, min(int(hours), FILE_HISTORY_MAX_HOURS))
    except (TypeError, ValueError):
        hours = 24
    now = time.time()
    cutoff = now - hours * 3600
    import config
    log_dir = Path(config.ROOT_DIR) / "server" / "log"
    used = []

    rows = []
    for path in _hist_files(log_dir, "metrics", hours, now):
        used.append(path.name)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    parts = ln.strip().split(",")
                    if len(parts) < 6:
                        continue
                    ts = _hist_epoch(parts[0])
                    if ts < cutoff:
                        continue
                    try:
                        # 7번째(워커 RSS)는 2026-07-28 추가 — 없는 구파일은 0 으로 읽는다
                        rows.append((ts, float(parts[1]), int(parts[2]),
                                     int(parts[3]), int(parts[4]), int(parts[5]),
                                     int(parts[6]) if len(parts) > 6 else 0))
                    except ValueError:
                        pass
        except OSError:
            pass
    rows.sort(key=lambda r: r[0])

    # 버킷 다운샘플 — 버킷별 max 를 보존해 피크가 사라지지 않게 (snapshot_history 와 동일 규칙)
    resource = {"ts": [], "cpu_max": [], "mem_used_max": [], "rss_max": [],
                "inflight_max": [], "total_rss_max": []}
    if rows:
        bucket = max(1, (len(rows) + max_points - 1) // max_points)
        for i in range(0, len(rows), bucket):
            chunk = rows[i:i + bucket]
            resource["ts"].append(int(chunk[-1][0]))
            resource["cpu_max"].append(max(c[1] for c in chunk))
            resource["rss_max"].append(max(c[2] for c in chunk))
            resource["mem_used_max"].append(max(c[3] for c in chunk))
            resource["inflight_max"].append(max(c[5] for c in chunk))
            resource["total_rss_max"].append(max(c[2] + c[6] for c in chunk))

    lat = {"ts": [], "p95": [], "p99": []}
    # 부하 시계열 — 큐/빌드/열람은 순간값 그대로, 캐시 히트율만 구간 증분으로 환산한다
    # (기록값이 기동 이후 누적이라, 재시작 리셋 구간은 증분이 음수가 되므로 건너뛴다).
    load = {"ts": [], "ondemand": [], "distpack": [], "builds": [], "viewers": [],
            "hit_rate": []}
    _prev_hit = _prev_miss = None
    slow = []
    routes = {}
    for path in _hist_files(log_dir, "runtime", hours, now):
        used.append(path.name)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        continue
                    ts = _hist_epoch(rec.get("ts"))
                    if ts < cutoff:
                        continue
                    if rec.get("type") == "lat":
                        lat["ts"].append(int(ts))
                        lat["p95"].append(rec.get("p95") or 0)
                        lat["p99"].append(rec.get("p99") or 0)
                        for t in rec.get("top") or []:
                            r = t.get("route") or "?"
                            n, mx = routes.get(r, (0, 0.0))
                            routes[r] = (n + (t.get("count") or 0),
                                         max(mx, t.get("max_ms") or 0))
                    elif rec.get("type") == "slow":
                        slow.append({"ts": int(ts), "route": rec.get("route"),
                                     "ms": rec.get("ms")})
                    elif rec.get("type") == "load":
                        load["ts"].append(int(ts))
                        for key in ("ondemand", "distpack", "builds", "viewers"):
                            load[key].append(rec.get(key) or 0)
                        hit, miss = rec.get("hit"), rec.get("miss")
                        rate = None
                        if hit is not None and miss is not None:
                            if _prev_hit is not None:
                                d_hit, d_miss = hit - _prev_hit, miss - _prev_miss
                                if d_hit >= 0 and d_miss >= 0 and (d_hit + d_miss) > 0:
                                    rate = round(d_hit / (d_hit + d_miss) * 100, 1)
                            _prev_hit, _prev_miss = hit, miss
                        load["hit_rate"].append(rate)
        except OSError:
            pass
    slow.sort(key=lambda d: d["ts"], reverse=True)
    top_routes = sorted(({"route": r, "count": n, "max_ms": mx}
                         for r, (n, mx) in routes.items()),
                        key=lambda d: d["max_ms"], reverse=True)[:10]

    return {"hours": hours, "now": round(now), "resource": resource, "lat": lat,
            "load": load, "slow": slow[:100], "top_routes": top_routes,
            "slow_threshold_ms": SLOW_REQ_MS, "files": used,
            "coverage_from": int(rows[0][0]) if rows else None}

"""공개 API(/pe/api/v1) 호출 계측 — 관리자 패널 'public API' 탭 전용.

**admin_panel/metrics.py 와 분리한 이유**: 그쪽 지표(응답시간 p50/p95, 실시간 접속
사용자)는 "사람이 쓰기에 서버가 괜찮은가"를 본다. 무인증 폴러(사내 대시보드·배치
스크립트)가 그 표본에 섞이면, 빠르고 잦은 API 호출이 표본 대부분을 차지해 **사람의
체감 악화를 가린다**. 그래서 public_api 요청은 그쪽 표본에서 빼고
(→ admin_panel/metrics.py `_on_request_teardown`) 여기로 따로 모은다.

식별 키는 Flask endpoint 이름의 `public_api_` 접두다. 기능별 Blueprint 이름 규약
(README '기능 추가 규칙')이 깨지면 그 기능은 계측에서 통째로 빠지므로,
`register_public_api()` 가 등록 시점에 경고한다.

**부담 판정 지표**는 분당 호출수가 아니라 `busy_pct` 다 — 구간 내 총 소요시간을
`WAITRESS_THREADS × 구간` 으로 나눈 값, 즉 공개 API 가 요청 스레드를 몇 % 점유했는가.
호출이 아무리 잦아도 응답이 짧으면 이 값은 0 에 가깝다.

오버헤드 원칙은 admin_panel/metrics.py 와 동일: 요청 경로에는 lock 1회 + 정수 연산만
얹는다. 파일 기록은 그쪽 샘플러 스레드가 분당 1회 flush_file() 을 부르는 방식이라
요청 경로에 파일 IO 가 없다. 오래된 로그 정리도 그쪽 _prune_flight_files() 가
같은 보관 정책(REPORT_METRICS_FILE_KEEP_DAYS)으로 함께 처리한다.
"""
import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

_log = logging.getLogger(__name__)

ENABLED = os.getenv("PUBLIC_API_METRICS_ENABLED", "1") != "0"

# Blueprint 이름 = endpoint 접두. public_api/__init__.py 의 검증과 같은 값을 쓴다.
ENDPOINT_PREFIX = "public_api_"

# 느린 호출 기준 — 공개 API 는 단순 조회만 노출하므로 사람 요청(10초)보다 훨씬 낮게 잡는다.
SLOW_MS = float(os.getenv("PUBLIC_API_SLOW_MS", "1000"))
# 파일 보관은 metrics_*.log / runtime_*.log 와 같은 정책 (같은 env 를 읽는다)
FILE_KEEP_DAYS = float(os.getenv("REPORT_METRICS_FILE_KEEP_DAYS", "14"))
# busy_pct 분모 — wsgi.py / admin_panel.metrics 와 동일 규칙
WAITRESS_THREADS = int(os.getenv("WAITRESS_THREADS", "13"))

_RECENT_MAX = 200        # 느린/에러 호출 링버퍼
_CALLERS_MAX = 200       # 호출자(IP) 상한 — 초과 시 가장 오래된 것부터 버린다
_MINUTES_MAX = 24 * 60   # 분 버킷 24시간
_LAT_MAX = 2000          # 백분위 표본

_lock = threading.Lock()
_by_route = {}            # endpoint -> [count, err4xx, err5xx, total_ms, max_ms, last_ts]
_callers = OrderedDict()  # ip -> [count, last_ts, last_route]
_minutes = OrderedDict()  # 분(epoch//60) -> [count, err, total_ms, max_ms]
_recent = deque(maxlen=_RECENT_MAX)   # (ts, route, ms, status, ip) — 느린/에러만
_lat = deque(maxlen=_LAT_MAX)
_started_at = time.time()

_fr_last_minute = None    # 마지막으로 파일에 기록한 분
_fr_warned = False        # 기록 실패 반복 경고 억제 (디스크 풀 시 로그 폭주 방지)


def record(endpoint, ms, status, ip):
    """요청 1건 계측. **절대 예외를 올리지 않는다** — 전역 teardown 훅에서 불린다."""
    if not ENABLED:
        return
    try:
        now = time.time()
        minute = int(now // 60)
        ip = ip or "?"
        with _lock:
            row = _by_route.get(endpoint)
            if row is None:
                row = [0, 0, 0, 0.0, 0.0, 0.0]
                _by_route[endpoint] = row
            row[0] += 1
            if status >= 500:
                row[2] += 1
            elif status >= 400:
                row[1] += 1
            row[3] += ms
            if ms > row[4]:
                row[4] = ms
            row[5] = now

            cal = _callers.get(ip)
            if cal is None:
                cal = [0, 0.0, ""]
                _callers[ip] = cal
            cal[0] += 1
            cal[1] = now
            cal[2] = endpoint
            _callers.move_to_end(ip)
            while len(_callers) > _CALLERS_MAX:
                _callers.popitem(last=False)

            # 분은 단조 증가로 들어오므로 삽입 순서 = 시간 순 (popitem(last=False)=최오래된 것)
            mrow = _minutes.get(minute)
            if mrow is None:
                mrow = [0, 0, 0.0, 0.0]
                _minutes[minute] = mrow
                while len(_minutes) > _MINUTES_MAX:
                    _minutes.popitem(last=False)
            mrow[0] += 1
            if status >= 400:
                mrow[1] += 1
            mrow[2] += ms
            if ms > mrow[3]:
                mrow[3] = ms

            _lat.append(ms)
            if status >= 400 or (SLOW_MS > 0 and ms >= SLOW_MS):
                _recent.append((now, endpoint, round(ms, 1), int(status), ip))
    except Exception:
        pass


def _pct(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q)))
    return round(sorted_vals[idx], 1)


def snapshot(window_sec=3600, recent_limit=50):
    """탭 1회 조회용 스냅샷. prune·정렬은 여기서만 한다 (요청 경로에 O(n) 을 얹지 않는다).

    window_sec 구간 집계는 분 버킷에서, 백분위는 최근 _LAT_MAX 건 표본에서 구한다
    (구간과 표본 범위가 다르다 — 화면에 그대로 표기한다).
    """
    if not ENABLED:
        return {"enabled": False}
    now = time.time()
    cut_min = int((now - window_sec) // 60)
    cut_ts = now - window_sec
    with _lock:
        routes = [(r, list(v)) for r, v in _by_route.items()]
        callers = [(ip, list(v)) for ip, v in _callers.items() if v[1] >= cut_ts]
        minutes = [(m, list(v)) for m, v in _minutes.items() if m >= cut_min]
        recent = list(_recent)
        lat = sorted(_lat)
        started = _started_at

    w_count = sum(v[0] for _m, v in minutes)
    w_err = sum(v[1] for _m, v in minutes)
    w_ms = sum(v[2] for _m, v in minutes)
    # 요청 스레드 점유율 — "부담인가"의 핵심 숫자 (호출수가 아니라 총 소요시간 기준)
    busy_pct = w_ms / (window_sec * 1000.0 * max(1, WAITRESS_THREADS)) * 100

    routes.sort(key=lambda x: x[1][0], reverse=True)
    callers.sort(key=lambda x: x[1][1], reverse=True)
    recent.sort(key=lambda r: r[0], reverse=True)

    series = {"ts": [], "count": [], "err": [], "avg_ms": []}
    for m, v in minutes:
        series["ts"].append(m * 60)
        series["count"].append(v[0])
        series["err"].append(v[1])
        series["avg_ms"].append(round(v[2] / v[0], 1) if v[0] else 0.0)

    return {
        "enabled": True,
        "now": round(now),
        "since": round(started),
        "window_sec": window_sec,
        "threads": WAITRESS_THREADS,
        "slow_ms": SLOW_MS,
        "window": {
            "count": w_count,
            "err": w_err,
            "avg_ms": round(w_ms / w_count, 1) if w_count else 0.0,
            "rpm": round(w_count / (window_sec / 60.0), 2) if window_sec else 0.0,
            "busy_pct": round(busy_pct, 3),
        },
        "latency": {
            "samples": len(lat),
            "p50": _pct(lat, 0.50), "p95": _pct(lat, 0.95), "p99": _pct(lat, 0.99),
            "max": round(lat[-1], 1) if lat else 0.0,
        },
        "routes": [{"route": r, "count": v[0], "err4xx": v[1], "err5xx": v[2],
                    "avg_ms": round(v[3] / v[0], 1) if v[0] else 0.0,
                    "max_ms": round(v[4], 1), "last_ago": round(now - v[5], 1)}
                   for r, v in routes],
        "callers": [{"ip": ip, "count": v[0], "last_ago": round(now - v[1], 1),
                     "route": v[2]} for ip, v in callers],
        "series": series,
        "recent": [{"ts": round(ts), "route": r, "ms": ms, "status": st, "ip": ip}
                   for ts, r, ms, st, ip in recent[:recent_limit]],
    }


def _log_dir():
    import config
    return Path(config.ROOT_DIR) / "server" / "log"


def flush_file(ts=None):
    """완료된 분 버킷을 publicapi_YYYYMMDD.log 에 JSON line 으로 append.

    admin_panel/metrics 샘플러 스레드가 부른다 — 요청 경로에 파일 IO 를 얹지 않기 위함.
    현재 진행 중인 분은 아직 집계가 끝나지 않았으므로 제외한다.
    """
    global _fr_last_minute, _fr_warned
    if not ENABLED or FILE_KEEP_DAYS <= 0:
        return
    now = ts or time.time()
    cur = int(now // 60)
    with _lock:
        pending = [(m, list(v)) for m, v in _minutes.items()
                   if m < cur and (_fr_last_minute is None or m > _fr_last_minute)]
    if not pending:
        return
    _fr_last_minute = pending[-1][0]
    # 자정을 걸치면 파일이 갈리므로 날짜별로 묶어서 쓴다
    by_date = OrderedDict()
    for m, v in pending:
        lt = time.localtime(m * 60)
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", lt),
                           "n": v[0], "err": v[1],
                           "avg": round(v[2] / v[0], 1) if v[0] else 0.0,
                           "max": round(v[3], 1)}, ensure_ascii=False)
        by_date.setdefault(time.strftime("%Y%m%d", lt), []).append(line)
    try:
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        for date, lines in by_date.items():
            with (log_dir / f"publicapi_{date}.log").open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    except Exception:
        if not _fr_warned:
            _fr_warned = True
            _log.warning("[public-api] metrics file write failed "
                         "(further warnings suppressed)", exc_info=True)


def file_history(hours=24, max_points=500):
    """publicapi_*.log 를 읽어 재시작과 무관한 분 단위 이력을 돌려준다.

    in-memory snapshot 을 대체하지 않고 병행한다 (탭 상단 = 실시간 / 이력 차트 = 최대 14일).
    다운샘플은 버킷별 max 를 보존한다 — 피크가 차트에서 사라지지 않게
    (admin_panel/metrics.file_history 와 동일 규칙).
    """
    try:
        hours = max(1, min(int(hours), 24 * 14))
    except (TypeError, ValueError):
        hours = 24
    now = time.time()
    cutoff = now - hours * 3600
    rows = []
    used = []
    try:
        log_dir = _log_dir()
        days = int(hours // 24) + 2
        names = [time.strftime("publicapi_%Y%m%d.log", time.localtime(now - d * 86400))
                 for d in range(days)]
        for name in reversed(names):
            path = log_dir / name
            if not path.exists():
                continue
            used.append(name)
            try:
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    for ln in f:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            rec = json.loads(ln)
                            ts = time.mktime(time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%S"))
                        except (ValueError, KeyError, OverflowError, TypeError):
                            continue
                        if ts < cutoff:
                            continue
                        rows.append((ts, int(rec.get("n") or 0), int(rec.get("err") or 0),
                                     float(rec.get("avg") or 0), float(rec.get("max") or 0)))
            except OSError:
                pass
    except Exception:
        pass
    rows.sort(key=lambda r: r[0])

    series = {"ts": [], "count": [], "err": [], "avg_ms": [], "max_ms": []}
    if rows:
        bucket = max(1, (len(rows) + max_points - 1) // max_points)
        for i in range(0, len(rows), bucket):
            chunk = rows[i:i + bucket]
            n = sum(c[1] for c in chunk)
            series["ts"].append(int(chunk[-1][0]))
            series["count"].append(n)
            series["err"].append(sum(c[2] for c in chunk))
            # 버킷 평균은 호출수 가중 (분마다 호출수가 다르므로 단순평균은 왜곡)
            series["avg_ms"].append(
                round(sum(c[1] * c[3] for c in chunk) / n, 1) if n else 0.0)
            series["max_ms"].append(max(c[4] for c in chunk))

    total = sum(r[1] for r in rows)
    return {"hours": hours, "now": round(now), "series": series, "files": used,
            "total": total, "err": sum(r[2] for r in rows),
            "coverage_from": int(rows[0][0]) if rows else None}

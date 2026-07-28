"""서버 현황 수집 — CPU/RAM/디스크, DB·업로드 디렉토리 크기, S3 연결 상태.

waitress 멀티스레드에서 요청마다 psutil.cpu_percent(interval=0.5) 를 부르면
워커가 블록되므로, interval=None 재샘플을 2초 캐시로 감싼다 (import 시 1회 priming —
priming 없이 연속 interval=None 호출은 0.0 에 가까운 값이 나온다).
디렉토리 크기 재귀 스캔은 느릴 수 있어 경로별 TTL 캐시(기본 60초) + refresh 우회를 둔다.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

import psutil

import config

_log = logging.getLogger(__name__)

# ── CPU 샘플러 (2초 캐시) ────────────────────────────────────────────────────
psutil.cpu_percent(interval=None)  # priming — 다음 호출부터 구간 평균이 나온다
_cpu_lock = threading.Lock()
_cpu_cached = 0.0
_cpu_ts = 0.0
_CPU_TTL = 2.0


def _cpu_percent():
    global _cpu_cached, _cpu_ts
    with _cpu_lock:
        now = time.time()
        if now - _cpu_ts >= _CPU_TTL:
            _cpu_cached = psutil.cpu_percent(interval=None)
            _cpu_ts = now
        return _cpu_cached


# ── 컴퓨트 워커 RSS (2초 캐시) ───────────────────────────────────────────────
# 부모 프로세스 RSS 만 보면 컴퓨트 워커(ProcessPoolExecutor 자식)가 쓰는 RAM 이 통째로
# 안 보인다. 시스템 전체 RAM(virtual_memory)에는 잡히지만 같은 박스의 다른 서비스와
# 섞여 "report_server 가 얼마나 쓰는지"를 분리할 수 없다 — 워커 수를 늘릴 때 판단
# 근거가 되는 값이라 자식 RSS 합을 따로 집계한다.
# 자식 열거는 Windows 에서 전체 프로세스 스캔이라 값싸지 않다 → CPU 와 같은 TTL 캐시.
_ch_lock = threading.Lock()
_ch_cached = (0, 0)   # (rss 합, 자식 수)
_ch_ts = 0.0
_CHILDREN_TTL = 2.0


def children_rss():
    """컴퓨트 워커(자식 프로세스) RSS 합계와 개수. 실패 시 (0, 0)."""
    global _ch_cached, _ch_ts
    with _ch_lock:
        now = time.time()
        if now - _ch_ts < _CHILDREN_TTL:
            return _ch_cached
        total = n = 0
        try:
            for child in psutil.Process().children(recursive=True):
                try:
                    total += child.memory_info().rss
                    n += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue    # 리사이클(max_tasks_per_child)로 방금 죽은 워커
        except Exception:
            total = n = 0
        _ch_cached = (total, n)
        _ch_ts = now
        return _ch_cached


def health():
    """현황 카드용 스냅샷. 무거운 디스크 스캔 없이 즉시 응답."""
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(config.ROOT_DIR))
    proc = psutil.Process()
    proc_rss = proc.memory_info().rss
    workers_rss, workers_n = children_rss()
    return {
        "cpu_percent": _cpu_percent(),
        "cpu_count": psutil.cpu_count(),
        "mem_total": mem.total,
        "mem_used": mem.used,
        "mem_percent": mem.percent,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_percent": disk.percent,
        "disk_path": str(config.ROOT_DIR),
        "uptime_sec": int(time.time() - proc.create_time()),
        "proc_rss": proc_rss,
        "proc_threads": proc.num_threads(),
        # 서버 전체(부모+워커) — 같은 박스의 다른 서비스와 섞이지 않은 우리 몫
        "workers_rss": workers_rss,
        "workers_n": workers_n,
        "total_rss": proc_rss + workers_rss,
    }


# ── 디렉토리 크기 (TTL 캐시) ─────────────────────────────────────────────────
_size_lock = threading.Lock()
_size_cache = {}  # str(path) -> (ts, bytes, files)
_SIZE_TTL = 60.0


def _dir_size(path: Path):
    """os.scandir 재귀 합산. (bytes, file_count). 접근 실패 항목은 건너뛴다."""
    total = 0
    files = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        pass
        except OSError:
            pass
    return total, files


def _dir_size_cached(path: Path, refresh=False):
    key = str(path)
    now = time.time()
    with _size_lock:
        cached = _size_cache.get(key)
        if cached and not refresh and now - cached[0] < _SIZE_TTL:
            return cached[1], cached[2]
    if not path.exists():
        size, files = 0, 0
    else:
        size, files = _dir_size(path)
    with _size_lock:
        _size_cache[key] = (now, size, files)
    return size, files


def _file_size(path: Path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def storage(refresh=False):
    """DB 파일(-wal/-shm 포함)·백업·업로드 하위 디렉토리 크기."""
    db = Path(config.REPORT_DB_PATH)
    upload_root = Path(config.REPORT_UPLOAD_DIR)
    dirs = []
    for label, p in (
        ("uploads/report (전체)", upload_root),
        ("└ web_report", upload_root / "web_report"),
        ("└ issue_img", upload_root / "issue_img"),
        ("└ dist_combined", upload_root / "dist_combined"),
        ("DB 백업", Path(config.REPORT_DB_BACKUP_DIR)),
    ):
        size, files = _dir_size_cached(p, refresh=refresh)
        dirs.append({"label": label, "path": str(p), "bytes": size, "files": files})
    return {
        "db_file": _file_size(db),
        "db_wal": _file_size(db.with_name(db.name + "-wal")),
        "db_shm": _file_size(db.with_name(db.name + "-shm")),
        "db_path": str(db),
        "dirs": dirs,
    }


def s3_status():
    """S3 설정·연결 확인. head_bucket 이 connect-timeout 만큼 블록될 수 있어
    수동 새로고침 전용 (자동 폴링 금지). facade 공개 API 만 사용(내부 _s3 직접 import 금지)."""
    import storage_gateway
    return storage_gateway.s3_health()


def _wd_epoch(rec):
    """watchdog 로그의 ts('2026-07-23T13:50:25') → epoch. 파싱 실패는 0."""
    try:
        return time.mktime(time.strptime(rec.get("ts", ""), "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return 0


def _read_json_lines(path, tail):
    """watchdog 이 남긴 JSON lines 파일의 마지막 tail 줄을 파싱 (best-effort)."""
    out = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-tail:]
    except OSError:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def watchdog_status(limit=10):
    """watchdog(server/watchdog.ps1) 상태 요약 — 현황 탭 타일용.

    - watchdog.state 의 mtime = 마지막 점검 시각 (파일 없으면 미등록/미실행).
    - watchdog_events.log(JSON lines) 에서 재기동 이력 집계. 이벤트 파일은
      watchdog 이 1MB 캡으로 자체 관리하므로 전량 읽어도 가볍다.
    - reason 분포는 "왜 재기동했나"(healthz_503=DB 체크 실패 / healthz_timeout=지연 /
      not_listening=프로세스 사망)를 대시보드에서 바로 가르기 위한 것이다."""
    log_dir = config.ROOT_DIR / "server" / "log"
    last_check = None
    try:
        last_check = int((log_dir / "watchdog.state").stat().st_mtime)
    except OSError:
        pass

    # backoff_skip 이벤트가 늘어 500줄로는 24h 를 못 덮을 수 있어 2000줄까지 읽는다
    # (파일 자체가 1MB 캡이라 전량 읽어도 가볍다).
    events = _read_json_lines(log_dir / "watchdog_events.log", 2000)

    restarts = [e for e in events if e.get("event") in ("restart", "restart_fail")]
    cutoff = time.time() - 86400
    reasons = {}
    for e in restarts:
        if _wd_epoch(e) >= cutoff:
            r = e.get("reason") or "?"
            reasons[r] = reasons.get(r, 0) + 1
    skips = [e for e in events if e.get("event") == "backoff_skip"]
    return {
        "registered": last_check is not None,
        "last_check": last_check,
        "restarts_24h": sum(1 for e in restarts if _wd_epoch(e) >= cutoff),
        "restarts_total": len(restarts),
        "reasons_24h": reasons,
        "backoff_skips_24h": sum(1 for e in skips if _wd_epoch(e) >= cutoff),
        "last_backoff": skips[-1] if skips else None,
        "events": events[-limit:][::-1],  # 최신 먼저
    }


def watchdog_checks(hours=24, max_points=300):
    """watchdog_checks.log(매 점검 1줄) 요약 — 재기동 원인 추적용.

    events 가 '재기동했다'만 남기는 데 비해 checks 는 매 5분 점검 결과 전부를 남긴다.
    ok 가 한 건도 없으면 폭주(healthz 상시 실패), mutex_busy 가 많으면 태스크 겹침이다.
    checks 도 1MB 캡이라 요청 구간이 파일보다 길 수 있어 coverage_from(가장 오래된 ts)을
    함께 돌려준다."""
    log_dir = config.ROOT_DIR / "server" / "log"
    recs = _read_json_lines(log_dir / "watchdog_checks.log", 4000)
    cutoff = time.time() - max(1, hours) * 3600
    win = [r for r in recs if _wd_epoch(r) >= cutoff]

    counts = {}
    for r in win:
        k = r.get("result") or "?"
        counts[k] = counts.get(k, 0) + 1

    # healthz 응답시간 추이 — 리스닝 상태에서 실제로 healthz 를 호출한 점검만
    hz = [r for r in win if r.get("ms") is not None]
    if len(hz) > max_points:  # 스트라이드 다운샘플 (추이만 보면 되므로 균등 간격)
        step = (len(hz) + max_points - 1) // max_points
        hz = hz[::step]
    series = {
        "ts": [int(_wd_epoch(r)) for r in hz],
        "ms": [r.get("ms") or 0 for r in hz],
        "code": [r.get("code") or 0 for r in hz],
    }
    return {
        "hours": hours,
        "total": len(win),
        "counts": counts,
        "hz_series": series,
        "recent": win[-30:][::-1],  # 최신 먼저
        "coverage_from": int(_wd_epoch(recs[0])) if recs else None,
    }

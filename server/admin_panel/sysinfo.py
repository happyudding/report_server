"""서버 현황 수집 — CPU/RAM/디스크, DB·업로드 디렉토리 크기, S3 연결 상태.

waitress 멀티스레드에서 요청마다 psutil.cpu_percent(interval=0.5) 를 부르면
워커가 블록되므로, interval=None 재샘플을 2초 캐시로 감싼다 (import 시 1회 priming —
priming 없이 연속 interval=None 호출은 0.0 에 가까운 값이 나온다).
디렉토리 크기 재귀 스캔은 느릴 수 있어 경로별 TTL 캐시(기본 60초) + refresh 우회를 둔다.
"""
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


def health():
    """현황 카드용 스냅샷. 무거운 디스크 스캔 없이 즉시 응답."""
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(config.ROOT_DIR))
    proc = psutil.Process()
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
        "proc_rss": proc.memory_info().rss,
        "proc_threads": proc.num_threads(),
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

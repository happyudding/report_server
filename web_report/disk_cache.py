"""계산 산출물(report dict JSON, distribution gzip bytes)의 로컬 디스크 캐시.

인메모리 LRU(cache.py)는 서버 재시작·슬롯 퇴출로 사라져 콜드 재계산(수 초)이
재발한다. 업로드 직후 prewarm 이 만드는 최종 산출물을
``<upload_root>/web_report/<analysis_key>/cache/`` 에 파일로도 남겨, 이후 콜드
미스가 디스크 읽기(수십~수백 ms)로 끝나게 한다.

- 파일명 = ``<kind>-<content_hash 앞12>-<나머지 키 digest 16>`` — stale 키는 이름이
  달라 자연 배제된다. 같은 kind 의 다른 content_hash 세대(raw_data 편집 후 구세대)는
  쓰기 시점에 같은 디렉토리에서 정리한다.
- 총량 상한: env ``WEB_REPORT_DISK_CACHE_MAX_GB`` (기본 500, 0 이하 = 디스크 캐시
  비활성). 쓰기 후 백그라운드 스레드가 전체 cache/ 파일을 mtime 오래된 순으로 상한
  이하가 될 때까지 삭제한다. 캐시 히트는 mtime 을 갱신해 최근 사용을 표시한다.
- 세션 삭제는 storage_gateway.delete_analysis_artifacts 가 akey 디렉토리째 지우므로
  별도 삭제 훅이 필요 없다.
- 캐시는 재계산 가능한 파생물 — 모든 실패는 조용히 무시(best-effort)하고 언제
  지워져도 무해하다.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
from pathlib import Path

_log = logging.getLogger(__name__)

_MAX_GB = float(os.getenv("WEB_REPORT_DISK_CACHE_MAX_GB", "500") or 500)
_MAX_BYTES = int(_MAX_GB * (1024 ** 3))

_EVICT_LOCK = threading.Lock()


def _enabled() -> bool:
    return _MAX_BYTES > 0


def _cache_dir(upload_root: Path, analysis_key) -> Path:
    return Path(upload_root) / "web_report" / str(analysis_key) / "cache"


def _chash12(content_hash) -> str:
    return (str(content_hash) or "none")[:12] or "none"


def _rest_digest(rest_parts) -> str:
    canon = repr(tuple(str(p) for p in rest_parts)).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()[:16]


def _path_for(upload_root: Path, kind: str, cache_key: tuple, ext: str) -> Path:
    analysis_key, content_hash, *rest = cache_key
    name = f"{kind}-{_chash12(content_hash)}-{_rest_digest(rest)}{ext}"
    return _cache_dir(upload_root, analysis_key) / name


def _read(path: Path) -> bytes | None:
    from . import cache as _cache
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        _cache.STATS["disk_miss"] += 1
        return None
    except Exception:
        _cache.STATS["disk_miss"] += 1
        _log.warning("disk cache read failed: %s", path, exc_info=True)
        return None
    _cache.STATS["disk_hit"] += 1
    try:
        os.utime(path, None)  # LRU 신호 — 최근 사용 파일은 총량 퇴출에서 뒤로 밀린다
    except OSError:
        pass
    return blob


def _write(path: Path, data: bytes) -> None:
    # tmp 이름에 pid+스레드 id 를 박는다 — 프리웜 워커/온디맨드 워커/부모가 서로 다른
    # 프로세스에서 같은 report/map 키를 동시에 쓸 때 고정 ".tmp" 를 공유하면 os.replace
    # 경쟁·간헐 write 실패가 난다(dist_pack_store 가 이미 pid 접미사로 해결한 것과 동일).
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except Exception:
        _log.warning("disk cache write failed: %s", path, exc_info=True)
        try:
            tmp.unlink(missing_ok=True)   # 유니크 tmp 라 실패 시 잔여를 직접 회수
        except OSError:
            pass
        return
    _cleanup_stale_generations(path)
    threading.Thread(target=_enforce_cap, args=(path.parent.parent.parent,),
                     name="webreport-diskcache-evict", daemon=True).start()


def _cleanup_stale_generations(current: Path) -> None:
    """같은 kind 의 다른 content_hash 세대 파일 정리 (raw_data 편집 후 구세대 회수).

    파일명 규약 ``<kind>-<chash12>-<digest>`` 에서 kind 가 같고 chash12 가 다른 것만
    지운다 — 같은 chash12 의 다른 digest(모드/옵션 변형)는 세션별로 유효하므로 남긴다.
    """
    try:
        kind, chash12 = current.name.split("-", 2)[:2]
        for p in current.parent.iterdir():
            if p == current or not p.is_file():
                continue
            parts = p.name.split("-", 2)
            if len(parts) >= 2 and parts[0] == kind and parts[1] != chash12:
                p.unlink(missing_ok=True)
    except Exception:
        pass


def _enforce_cap(webreport_root: Path) -> None:
    """webreport_root(=<upload_root>/web_report) 아래 전체 cache/ 총량을 상한으로 유지.

    쓰기 후 백그라운드 스레드에서만 호출 — 이미 다른 스레드가 정리 중이면 건너뛴다.
    """
    if not _EVICT_LOCK.acquire(blocking=False):
        return
    try:
        entries = []
        total = 0
        for cache_dir in Path(webreport_root).glob("*/cache"):
            try:
                for p in cache_dir.iterdir():
                    if not p.is_file():
                        continue
                    st = p.stat()
                    entries.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
            except OSError:
                continue
        if total <= _MAX_BYTES:
            return
        entries.sort()  # mtime 오래된 순
        for _, size, p in entries:
            try:
                p.unlink()
                total -= size
            except OSError:
                continue
            if total <= _MAX_BYTES:
                break
        _log.info("disk cache evicted down to %.1f GB", total / (1024 ** 3))
    except Exception:
        _log.warning("disk cache eviction failed", exc_info=True)
    finally:
        _EVICT_LOCK.release()


def load_report(upload_root: Path, cache_key: tuple) -> dict | None:
    """report dict 디스크 캐시 조회. cache_key 는 service.load_webreport 의
    REPORT_CACHE 키 (analysis_key, content_hash, manifest_digest, opts_raw, mode)."""
    if not _enabled():
        return None
    path = _path_for(upload_root, "report", cache_key, ".json.gz")
    blob = _read(path)
    if blob is None:
        return None
    try:
        return json.loads(gzip.decompress(blob).decode("utf-8"))
    except Exception:
        _log.warning("disk cache decode failed: %s", path, exc_info=True)
        try:
            path.unlink(missing_ok=True)  # 손상 파일은 재계산으로 대체
        except OSError:
            pass
        return None


def report_exists(upload_root: Path, cache_key: tuple) -> bool:
    """report 디스크 캐시 파일이 있는지만 stat 1회로 확인 (읽지 않는다).

    콜드 판정을 single-flight 락 **밖에서** 하기 위한 용도다. 파일이 없으면 = 아직
    콜드(빌드 중이거나 시작 전) → 호출자가 락을 기다리지 않고 즉시 202 로 응답한다.
    있으면 종전대로 락 안에서 한 스레드만 디코드한다(중복 디코드 방지 유지).
    """
    if not _enabled():
        return False
    try:
        return _path_for(upload_root, "report", cache_key, ".json.gz").is_file()
    except OSError:
        return False


def map_exists(upload_root: Path, cache_key: tuple) -> bool:
    """map 디스크 캐시 파일 존재 확인 (report_exists 와 같은 용도)."""
    if not _enabled():
        return False
    try:
        return _path_for(upload_root, "map", cache_key, ".gz").is_file()
    except OSError:
        return False


def dumps_report(report: dict) -> bytes:
    """report dict → 캐시 직렬화 bytes. 콜드 경로가 이 bytes 를 gzip 저장(save_report_gz)과
    RAM 캐시 크기추정(cache.report_cache_put size=)에 **함께** 재사용해, 같은 payload 를
    레이어마다 다시 직렬화하던 낭비(콜드 1회당 3중 → 1중)를 없앤다."""
    return json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def save_report_gz(upload_root: Path, cache_key: tuple, report_bytes: bytes) -> None:
    """이미 직렬화된 report bytes(dumps_report 결과)를 gzip 해 디스크에 저장."""
    if not _enabled():
        return
    try:
        data = gzip.compress(report_bytes, compresslevel=1)
    except Exception:
        _log.warning("disk cache serialize failed", exc_info=True)
        return
    _write(_path_for(upload_root, "report", cache_key, ".json.gz"), data)


def save_report(upload_root: Path, cache_key: tuple, report: dict) -> None:
    """dict 를 직렬화해 저장 (bytes 를 이미 가진 콜드 경로는 dumps_report+save_report_gz 사용)."""
    if not _enabled():
        return
    try:
        report_bytes = dumps_report(report)
    except Exception:
        _log.warning("disk cache serialize failed", exc_info=True)
        return
    save_report_gz(upload_root, cache_key, report_bytes)


def load_dist(upload_root: Path, cache_key: tuple) -> bytes | None:
    """distribution gzip bytes 디스크 캐시 조회. cache_key 는
    service.get_distribution_gzip 의 DIST_CACHE 키 (analysis_key, content_hash, mode)."""
    if not _enabled():
        return None
    return _read(_path_for(upload_root, "dist", cache_key, ".gz"))


def save_dist(upload_root: Path, cache_key: tuple, blob: bytes) -> None:
    if not _enabled():
        return
    _write(_path_for(upload_root, "dist", cache_key, ".gz"), blob)


def load_map(upload_root: Path, cache_key: tuple) -> bytes | None:
    """Map Analysis dies gzip bytes 디스크 캐시 조회. cache_key 는
    service.get_map_gzip 의 MAP_CACHE 키 (analysis_key, content_hash, mode)."""
    if not _enabled():
        return None
    return _read(_path_for(upload_root, "map", cache_key, ".gz"))


def save_map(upload_root: Path, cache_key: tuple, blob: bytes) -> None:
    if not _enabled():
        return
    _write(_path_for(upload_root, "map", cache_key, ".gz"), blob)

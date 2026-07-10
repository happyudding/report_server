"""web_report 인메모리 캐시 인프라 (LRU 레지스트리 + 락 + 무효화).

service.py 에 있던 캐시 프리미티브를 분리한 모듈. 규약:
- 캐시 키의 첫 요소가 analysis_key 인 캐시는 AKEY_CACHES 에 등록(register_akey_cache)
  하면 편집(evict_akey_caches)·세션삭제(invalidate_caches) 무효화에 자동 편입된다.
- 모든 캐시 조작은 CACHE_LOCK 하나를 공유한다 (조작 시간이 짧아 경합 무시 가능).
- 단일 프로세스(waitress 1 process) 전제 — manifest write-through 일관성의 근거.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path

from .validation import canon

# ── decoded tables 인메모리 LRU 캐시 ──────────────────────────────────────────
# parquet decode+split 이 요청당 ~2.4s 로 /full·raw_data·scatter 등 모든 조회의 고정비라
# (analysis_key, content_hash) 키로 캐시한다. raw_data 편집은 content_hash 를 갱신하므로
# 키 자체가 바뀌어 자연 무효화되고, etc/comments 편집은 manifest 만 바꾸므로 캐시가 유효하다
# (manifest 는 아래 MANIFEST_CACHE 에 별도 캐시, 편집 시 write-through 갱신).
TABLES_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_TABLES_CACHE", "4") or 4))
TABLES_CACHE: OrderedDict = OrderedDict()   # (analysis_key, content_hash) -> list[HoneyformTable]
CACHE_LOCK = threading.Lock()               # 모든 캐시가 이 락을 공유 (조작 시간 짧음)

# 파생 결과 캐시 — 동시 사용자 대비 핵심. CPU-bound 재계산(distribution compact 수 초,
# /full payload ~2s)이 GIL 을 잡고 다른 요청까지 밀리게 하므로, 세션당 첫 1회만 계산한다.
DIST_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_DIST_CACHE", "4") or 4))
DIST_CACHE: OrderedDict = OrderedDict()     # (analysis_key, content_hash) -> gzip bytes
REPORT_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_REPORT_CACHE", "8") or 8))
REPORT_CACHE: OrderedDict = OrderedDict()   # (akey, chash, manifest_digest, incl_dist) -> report dict

# Commonality 인덱스 캐시 — chip 검색(키스트로크)·백분위(chip 클릭)가 매번 전 item 컬럼을
# 재변환하던 유일한 무캐시 heavy 경로였다. 메타 리스트 + item별 정렬 배열을 세션 단위로 보관.
COMMONALITY_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_COMMONALITY_CACHE", "2") or 2))
COMMONALITY_CACHE: OrderedDict = OrderedDict()  # (analysis_key, content_hash) -> build_index 결과

# Trim Analysis 파생 캐시. 매칭+통계 payload(TRIM_CACHE)는 manifest(trim_overrides) 의존이라
# manifest digest 를 키에 포함해 편집 시 자연 무효화한다. 그룹 차트(TRIM_CHART_CACHE)는
# 슬롯 구성(items)만의 함수라 items digest 로 키를 잡는다 — overrides 편집이 그룹 구성을
# 바꾸지 않은 차트는 캐시가 그대로 살아있다.
TRIM_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_TRIM_CACHE", "4") or 4))
TRIM_CACHE: OrderedDict = OrderedDict()      # (akey, chash, mdigest, mode, source) -> gzip bytes
TRIM_CHART_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_TRIM_CHART_CACHE", "64") or 64))
TRIM_CHART_CACHE: OrderedDict = OrderedDict()  # (akey, chash, mode, source, items_digest) -> gzip bytes

# manifest 인메모리 캐시 — warm 조회(/full·raw_data 등)마다 발생하던 S3 manifest GET 왕복 제거.
# 단일 프로세스(waitress 1 process) 전제: manifest 를 바꾸는 코드가 전부 web_report 패키지라
# 저장 성공 직후 write-through(manifest_cache_put) 로 일관성이 유지된다. 값은 canonical
# JSON bytes 로 보관하고 조회마다 json.loads 로 새 dict 를 만들어 호출자의 in-place 수정이
# 캐시를 오염시키지 않는다.
MANIFEST_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_MANIFEST_CACHE", "16") or 16))
MANIFEST_CACHE: OrderedDict = OrderedDict()  # analysis_key -> (canonical bytes, sha256 digest)

# analysis_key 를 키 첫 요소로 쓰는 캐시 레지스트리 — 무효화(invalidate_caches,
# evict_akey_caches)가 이 리스트를 순회한다. 파생 캐시를 새로 만들면 register_akey_cache
# 로 등록만 하면 무효화에 자동 편입된다 (response_cache.py 가 import 시 자기 캐시를 등록).
AKEY_CACHES: list = [TABLES_CACHE, DIST_CACHE, REPORT_CACHE, COMMONALITY_CACHE,
                     TRIM_CACHE, TRIM_CHART_CACHE]

# 콜드 캐시 동시 진입(stampede) 방지 single-flight 락 — 캐시에 없는 같은 세션을 여러
# 사용자가 동시에 열면 수 초짜리 CPU-bound 계산이 중복 실행되며 GIL 로 서로 밀어내므로,
# 같은 (종류, akey, chash) 계산은 한 스레드만 수행하고 나머지는 대기 후 캐시를 재확인한다.
_KEYED_LOCKS: OrderedDict = OrderedDict()
_KEYED_LOCKS_MAX = 32


def register_akey_cache(cache: OrderedDict) -> None:
    """akey-first 키 규약을 지키는 파생 캐시를 무효화 레지스트리에 등록."""
    AKEY_CACHES.append(cache)


def cache_get(cache: OrderedDict, key):
    with CACHE_LOCK:
        value = cache.get(key)
        if value is not None:
            cache.move_to_end(key)
    return value


def cache_put(cache: OrderedDict, key, value, max_size: int):
    with CACHE_LOCK:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > max_size:
            cache.popitem(last=False)


def keyed_lock(key) -> threading.Lock:
    """(종류, ...캐시키) 단위 락을 돌려준다. 레지스트리는 LRU 로 상한 유지."""
    with CACHE_LOCK:
        lock = _KEYED_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEYED_LOCKS[key] = lock
        _KEYED_LOCKS.move_to_end(key)
        while len(_KEYED_LOCKS) > _KEYED_LOCKS_MAX:
            _KEYED_LOCKS.popitem(last=False)
    return lock


def evict_akey_caches(analysis_key) -> None:
    """AKEY_CACHES 에서 해당 analysis_key 키 엔트리를 전부 제거 (manifest 캐시는 유지).

    raw_data 편집처럼 content_hash 만 바뀌어 구 키가 더 이상 조회되지 않을 때의
    메모리 회수용 — manifest 는 그대로 유효하므로 건드리지 않는다.
    """
    if not analysis_key:
        return
    with CACHE_LOCK:
        for cache in AKEY_CACHES:
            for key in [k for k in cache if k[0] == analysis_key]:
                cache.pop(key, None)


def invalidate_caches(analysis_key) -> None:
    """akey 산출물이 삭제됐을 때(세션 삭제 등) 인메모리 캐시 전부 정리 — 메모리 회수 +
    stale manifest 재사용 방지."""
    if not analysis_key:
        return
    with CACHE_LOCK:
        for cache in AKEY_CACHES:
            for key in [k for k in cache if k[0] == analysis_key]:
                cache.pop(key, None)
        MANIFEST_CACHE.pop(analysis_key, None)


def manifest_cache_put(analysis_key, manifest: dict) -> None:
    if analysis_key:
        blob = canon(manifest)
        cache_put(MANIFEST_CACHE, analysis_key,
                  (blob, hashlib.sha256(blob).hexdigest()), MANIFEST_CACHE_MAX)


def load_manifest_with_digest(analysis_key, upload_root: Path) -> tuple[dict, str]:
    """(manifest dict, canonical digest) 를 단일 캐시 읽기로 반환.

    digest 는 캐시 엔트리에 동봉돼 있어 warm 요청마다 canon+sha256 을 재계산하지 않고,
    manifest 와 digest 가 항상 같은 엔트리에서 나와 편집 경합 시에도 짝이 어긋나지 않는다.
    """
    entry = cache_get(MANIFEST_CACHE, analysis_key)
    if entry is None:
        import storage_gateway
        manifest = storage_gateway.load_webreport_manifest(analysis_key, upload_root=upload_root)
        blob = canon(manifest)
        entry = (blob, hashlib.sha256(blob).hexdigest())
        cache_put(MANIFEST_CACHE, analysis_key, entry, MANIFEST_CACHE_MAX)
        return manifest, entry[1]
    return json.loads(entry[0].decode("utf-8")), entry[1]


def load_manifest_cached(analysis_key, upload_root: Path) -> dict:
    return load_manifest_with_digest(analysis_key, upload_root)[0]

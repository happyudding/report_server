"""web_report 인메모리 캐시 인프라 (LRU 레지스트리 + 락 + 무효화).

service.py 에 있던 캐시 프리미티브를 분리한 모듈. 규약:
- 캐시 키의 첫 요소가 analysis_key 인 캐시는 AKEY_CACHES 에 등록(register_akey_cache)
  하면 편집(evict_akey_caches)·세션삭제(invalidate_caches) 무효화에 자동 편입된다.
- 모든 캐시 조작은 CACHE_LOCK 하나를 공유한다 (조작 시간이 짧아 경합 무시 가능).
- 단일 프로세스(waitress 1 process) 전제 — manifest write-through 일관성의 근거.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .validation import canon

# ── decoded tables 인메모리 LRU 캐시 ──────────────────────────────────────────
# parquet decode+split 이 요청당 ~2.4s 로 /full·raw_data·scatter 등 모든 조회의 고정비라
# (analysis_key, content_hash) 키로 캐시한다. raw_data 편집은 content_hash 를 갱신하므로
# 키 자체가 바뀌어 자연 무효화되고, comment/override 편집은 세션 편집 DB 만 바꾸므로
# 캐시가 유효하다. 항목은 슬림 테이블(df=None, loader keep_df=False) 전제.
# 개수(TABLES_CACHE_MAX)와 추정 바이트 총량(TABLES_CACHE_MAX_MB) 이중 상한 —
# 대형 세션 몇 개로 OOM 나지 않게 바이트 기준으로도 축출한다 (Phase 5).
TABLES_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_TABLES_CACHE", "4") or 4))
TABLES_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_TABLES_CACHE_MB", "4096")
                                    or 4096)) * 1024 * 1024   # 0 = 바이트 상한 비활성
TABLES_CACHE: OrderedDict = OrderedDict()   # (analysis_key, content_hash) -> list[HoneyformTable]
_TABLES_SIZES: dict = {}                    # 키 -> 추정 바이트 (TABLES_CACHE 와 동기)
CACHE_LOCK = threading.Lock()               # 모든 캐시가 이 락을 공유 (조작 시간 짧음)

# 파생 결과 캐시 — 동시 사용자 대비 핵심. CPU-bound 재계산(distribution compact 수 초,
# /full payload ~2s)이 GIL 을 잡고 다른 요청까지 밀리게 하므로, 세션당 첫 1회만 계산한다.
# dist blob 은 worst case(전 값 고유 10k행×1500항목×7소스) 실측 ~505MB/개라 개수 상한만으론
# RAM 이 GB 급으로 부풀 수 있어 바이트 상한을 이중 적용한다(tables 와 동일 패턴).
# 축출돼도 디스크 캐시 재읽기(수십 ms)로 복구되므로 공격적으로 잘라도 무해하다.
DIST_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_DIST_CACHE", "4") or 4))
DIST_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_DIST_CACHE_MB", "1024")
                                  or 1024)) * 1024 * 1024   # 0 = 바이트 상한 비활성
DIST_CACHE: OrderedDict = OrderedDict()     # (analysis_key, content_hash) -> gzip bytes
# Map Analysis dies gzip 캐시 — /full 에서 분리한 die 전량 payload (schema v8).
# dist 와 같은 bytes 캐시라 개수+바이트 이중 상한을 같은 헬퍼로 적용한다.
MAP_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_MAP_CACHE", "4") or 4))
MAP_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_MAP_CACHE_MB", "512")
                                 or 512)) * 1024 * 1024   # 0 = 바이트 상한 비활성
MAP_CACHE: OrderedDict = OrderedDict()      # (akey, chash, mode) -> gzip bytes
# report payload dict 캐시. dist/map 과 달리 값이 bytes 가 아니라 dict 라 len() 으로
# 크기를 알 수 없어 개수 상한만 있었는데, 대형 세션(항목×소스 수천 행의 cpk_rows)이
# 8개 쌓이면 RAM 이 예측 불가로 커진다 → tables 와 같은 "크기 기록 + 이중 상한" 방식으로
# 바꾼다. 크기는 put 시 1회 직렬화 길이로 추정한다(콜드 빌드당 1회라 비용이 묻힌다).
REPORT_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_REPORT_CACHE", "8") or 8))
REPORT_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_REPORT_CACHE_MB", "256")
                                    or 256)) * 1024 * 1024   # 0 = 바이트 상한 비활성
REPORT_CACHE: OrderedDict = OrderedDict()   # (akey, chash, manifest_digest, incl_dist) -> report dict
_REPORT_SIZES: dict = {}                    # 키 -> 추정 바이트 (REPORT_CACHE 와 동기)

# dist pack chunk **디코드 결과** 캐시 — distribution_batch 는 요청마다 chunk 파일을
# read+gunzip+json.loads 로 되풀이 디코드했다(대형 세션은 chunk 1개가 비압축 15~20MB).
# 갤러리 스크롤 중 같은 chunk 를 여러 배치가 반복해서 건드리므로, 디코드 결과를 캐시하면
# 그 GIL 점유가 첫 1회로 줄어든다. 값은 dict 라 크기를 len() 으로 못 재므로 report 와
# 같은 "크기 기록 + 이중 상한" 방식(크기는 디코드 시 얻은 비압축 길이).
# ⚠ 캐시 값은 **읽기 전용 공유** — 소비자(dist_pack.ecdf_from_pack_items)가 입력을
# 변경하지 않는다는 계약 위에서만 성립한다.
DIST_CHUNK_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_DIST_CHUNK_CACHE", "64") or 64))
DIST_CHUNK_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_DIST_CHUNK_CACHE_MB", "512")
                                        or 512)) * 1024 * 1024   # 0 = 바이트 상한 비활성
DIST_CHUNK_CACHE: OrderedDict = OrderedDict()  # (akey, chash[, prep], mode, chunk_id) -> items dict
_DIST_CHUNK_SIZES: dict = {}                   # 키 -> 비압축 바이트 (DIST_CHUNK_CACHE 와 동기)

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
# 그룹 차트는 전 die 전 포인트라 세션에 따라 1건이 수 MB 가 되고, 개수 상한(64)만으로는
# RAM 이 예측 불가로 부푼다 → dist/map 과 같은 바이트 이중 상한을 적용한다.
TRIM_CHART_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_TRIM_CHART_CACHE", "64") or 64))
TRIM_CHART_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_TRIM_CHART_CACHE_MB", "256")
                                        or 256)) * 1024 * 1024   # 0 = 바이트 상한 비활성
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
AKEY_CACHES: list = [TABLES_CACHE, DIST_CACHE, MAP_CACHE, REPORT_CACHE,
                     COMMONALITY_CACHE, TRIM_CACHE, TRIM_CHART_CACHE, DIST_CHUNK_CACHE]

# 콜드 캐시 동시 진입(stampede) 방지 single-flight 락 — 캐시에 없는 같은 세션을 여러
# 사용자가 동시에 열면 수 초짜리 CPU-bound 계산이 중복 실행되며 GIL 로 서로 밀어내므로,
# 같은 (종류, akey, chash) 계산은 한 스레드만 수행하고 나머지는 대기 후 캐시를 재확인한다.
_KEYED_LOCKS: OrderedDict = OrderedDict()
# 상한이 동시 진행 키 수보다 작으면 락 보유 중인 키가 축출돼 같은 키에 새 락이 생기며
# 상호배제가 깨진다(편집 직렬화 rawedit 키 포함) — 넉넉하게 잡는다. 락 객체는 경량.
_KEYED_LOCKS_MAX = 256


def register_akey_cache(cache: OrderedDict) -> None:
    """akey-first 키 규약을 지키는 파생 캐시를 무효화 레지스트리에 등록."""
    AKEY_CACHES.append(cache)


# 히트/미스 누적 (관리자 패널 노출용). 캐시 객체를 키로 못 쓰므로 전역 2개만 센다 —
# 캐시별 분해는 필요해질 때 추가한다. 카운터는 CACHE_LOCK 안에서만 만진다.
STATS = {"hit": 0, "miss": 0, "disk_hit": 0, "disk_miss": 0}

# single-flight 락 경합 누적 (관리자 패널 노출용) — 종류(키 첫 요소) -> [횟수, 누적 ms].
# **경합이 실제로 난 경우에만** 기록한다(무경합은 시간 측정조차 하지 않음 — keyed_lock_ctx).
LOCK_WAITS: dict = {}

# cache_stats() 에 자기 통계를 얹고 싶은 외부 모듈(response_cache 등)이 등록하는 콜백.
# response_cache → cache 단방향 import 를 유지하기 위한 장치 — cache 가 response_cache 를
# import 하면 순환이 된다.
STATS_PROVIDERS: list = []


def register_stats_provider(fn) -> None:
    """cache_stats() 의 ``response`` 항목에 병합할 dict 를 돌려주는 콜백 등록.

    콜백은 **CACHE_LOCK 밖에서** 호출된다 (콜백 내부가 CACHE_LOCK 을 다시 잡는 것이
    정상 — 비재진입 Lock 이라 락 안에서 부르면 데드락).
    """
    STATS_PROVIDERS.append(fn)


def cache_get(cache: OrderedDict, key):
    with CACHE_LOCK:
        value = cache.get(key)
        if value is not None:
            cache.move_to_end(key)
            STATS["hit"] += 1
        else:
            STATS["miss"] += 1
    return value


def cache_stats():
    """캐시별 보유 건수 + 히트/미스 누적 + 락 경합 + 등록된 외부 캐시 통계."""
    names = (("tables", TABLES_CACHE), ("dist", DIST_CACHE), ("map", MAP_CACHE),
             ("report", REPORT_CACHE), ("commonality", COMMONALITY_CACHE),
             ("trim", TRIM_CACHE), ("trim_chart", TRIM_CHART_CACHE),
             ("manifest", MANIFEST_CACHE), ("dist_chunk", DIST_CHUNK_CACHE))
    with CACHE_LOCK:
        sizes = {name: len(c) for name, c in names}
        tables_bytes = sum(_TABLES_SIZES.values())
        report_bytes = sum(_REPORT_SIZES.values())
        chunk_bytes = sum(_DIST_CHUNK_SIZES.values())
        stats = dict(STATS)
        lock_waits = {kind: {"count": n, "total_ms": round(ms, 1)}
                      for kind, (n, ms) in LOCK_WAITS.items()}
    # provider 는 락 밖에서 — 콜백 내부가 CACHE_LOCK 을 다시 잡는다(비재진입).
    response = {}
    for fn in STATS_PROVIDERS:
        try:
            response.update(fn() or {})
        except Exception:
            pass
    total = stats["hit"] + stats["miss"]
    disk_total = stats["disk_hit"] + stats["disk_miss"]
    return {"sizes": sizes, "tables_bytes": tables_bytes, "report_bytes": report_bytes,
            "chunk_bytes": chunk_bytes, "lock_waits": lock_waits, "response": response,
            **stats,
            "hit_rate": round(stats["hit"] / total * 100, 1) if total else None,
            "disk_hit_rate": round(stats["disk_hit"] / disk_total * 100, 1) if disk_total else None}


def cache_put(cache: OrderedDict, key, value, max_size: int):
    with CACHE_LOCK:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > max_size:
            cache.popitem(last=False)


def _estimate_tables_bytes(tables) -> int:
    """HoneyformTable 리스트의 RAM 추정치 (바이트 상한 축출용 — 근사면 충분).

    수치 블록은 memory_usage 그대로, object dtype 컬럼(SERIAL 등 메타)은 셀당
    파이썬 객체 오버헤드(~56B)를 더한다. df 는 슬림 규약(None)이라 계산하지 않는다."""
    total = 0
    for t in tables:
        try:
            df = t.data
            total += int(df.memory_usage(index=False).sum())
            obj_cols = df.select_dtypes(include="object").shape[1]
            total += obj_cols * len(df) * 56
        except Exception:
            pass
    return total


def tables_cache_put(key, tables) -> None:
    """TABLES_CACHE 전용 put — 개수 + 추정 바이트 이중 상한으로 축출한다.

    최소 1개는 남긴다 (방금 넣은 세션은 곧바로 조회되므로)."""
    size = _estimate_tables_bytes(tables)
    with CACHE_LOCK:
        TABLES_CACHE[key] = tables
        TABLES_CACHE.move_to_end(key)
        _TABLES_SIZES[key] = size
        while len(TABLES_CACHE) > 1 and (
                len(TABLES_CACHE) > TABLES_CACHE_MAX
                or (TABLES_CACHE_MAX_BYTES
                    and sum(_TABLES_SIZES.values()) > TABLES_CACHE_MAX_BYTES)):
            old_key, _ = TABLES_CACHE.popitem(last=False)
            _TABLES_SIZES.pop(old_key, None)


def _prune_tables_sizes_locked() -> None:
    """크기 기록을 두는 캐시(TABLES/REPORT/DIST_CHUNK)에서 빠진 키의 기록 제거.

    (CACHE_LOCK 보유 상태에서 호출 — 무효화 경로가 캐시에서 직접 pop 하므로 크기 기록만
    남아 상한 계산이 실제보다 커지는 것을 막는다.)"""
    for key in [k for k in _TABLES_SIZES if k not in TABLES_CACHE]:
        _TABLES_SIZES.pop(key, None)
    for key in [k for k in _REPORT_SIZES if k not in REPORT_CACHE]:
        _REPORT_SIZES.pop(key, None)
    for key in [k for k in _DIST_CHUNK_SIZES if k not in DIST_CHUNK_CACHE]:
        _DIST_CHUNK_SIZES.pop(key, None)


def _bytes_capped_put(cache: OrderedDict, key, blob: bytes,
                      max_n: int, max_bytes: int) -> None:
    """bytes 값 캐시 공용 put — 개수 + 바이트(len 합산) 이중 상한으로 축출한다.

    최소 1개는 남긴다 (방금 넣은 blob 은 곧바로 조회되므로). 값이 bytes 라 크기
    측정이 len() 으로 끝난다 — tables 처럼 별도 크기 기록이 필요 없다.
    (dist/map/trim_chart 외에 response_cache 의 full/scatter/dist_batch 도 사용)"""
    with CACHE_LOCK:
        cache[key] = blob
        cache.move_to_end(key)
        while len(cache) > 1 and (
                len(cache) > max_n
                or (max_bytes and sum(len(v) for v in cache.values()) > max_bytes)):
            cache.popitem(last=False)


def report_cache_put(key, report: dict, size: int | None = None) -> None:
    """REPORT_CACHE 전용 put — 개수 + 추정 바이트 이중 상한으로 축출한다.

    크기는 dist 캐시와 같은 직렬화 규약(separators, ensure_ascii=False)의 JSON 길이로
    잡는다 — 실제 파이썬 dict RAM 은 이보다 크지만 세션 간 상대 크기가 목적이라 충분하다.
    최소 1개는 남긴다 (방금 넣은 세션은 곧바로 조회되므로).

    size 를 주면(콜드 경로가 disk_cache.dumps_report 로 이미 직렬화한 bytes 길이) 여기서
    재직렬화하지 않는다 — 같은 payload 를 크기추정용으로 또 dumps 하던 낭비를 없앤다."""
    if size is None:
        try:
            size = len(json.dumps(report, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError):
            size = 0   # 직렬화 불가는 크기 미상 취급 — 개수 상한만 적용된다
    with CACHE_LOCK:
        REPORT_CACHE[key] = report
        REPORT_CACHE.move_to_end(key)
        _REPORT_SIZES[key] = size
        while len(REPORT_CACHE) > 1 and (
                len(REPORT_CACHE) > REPORT_CACHE_MAX
                or (REPORT_CACHE_MAX_BYTES
                    and sum(_REPORT_SIZES.values()) > REPORT_CACHE_MAX_BYTES)):
            old_key, _ = REPORT_CACHE.popitem(last=False)
            _REPORT_SIZES.pop(old_key, None)


def dist_chunk_cache_put(key, items: dict, size: int) -> None:
    """DIST_CHUNK_CACHE 전용 put — 개수 + 비압축 바이트 이중 상한으로 축출한다.

    size 는 디코드 시 이미 알고 있는 비압축 JSON 길이 (실제 dict RAM 은 그보다 크지만
    chunk 간 상대 크기가 목적이라 충분 — report_cache_put 과 같은 관례).
    최소 1개는 남긴다 (방금 넣은 chunk 는 곧바로 조회되므로)."""
    with CACHE_LOCK:
        DIST_CHUNK_CACHE[key] = items
        DIST_CHUNK_CACHE.move_to_end(key)
        _DIST_CHUNK_SIZES[key] = int(size)
        while len(DIST_CHUNK_CACHE) > 1 and (
                len(DIST_CHUNK_CACHE) > DIST_CHUNK_CACHE_MAX
                or (DIST_CHUNK_CACHE_MAX_BYTES
                    and sum(_DIST_CHUNK_SIZES.values()) > DIST_CHUNK_CACHE_MAX_BYTES)):
            old_key, _ = DIST_CHUNK_CACHE.popitem(last=False)
            _DIST_CHUNK_SIZES.pop(old_key, None)


def dist_cache_put(key, blob: bytes) -> None:
    _bytes_capped_put(DIST_CACHE, key, blob, DIST_CACHE_MAX, DIST_CACHE_MAX_BYTES)


def map_cache_put(key, blob: bytes) -> None:
    _bytes_capped_put(MAP_CACHE, key, blob, MAP_CACHE_MAX, MAP_CACHE_MAX_BYTES)


def trim_chart_cache_put(key, blob: bytes) -> None:
    _bytes_capped_put(TRIM_CHART_CACHE, key, blob,
                      TRIM_CHART_CACHE_MAX, TRIM_CHART_CACHE_MAX_BYTES)


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


@contextlib.contextmanager
def keyed_lock_ctx(key):
    """keyed_lock + **경합 계측**. ``with cache.keyed_lock(k):`` 의 대체재.

    무경합(= 곧바로 획득)이면 시도 1회로 끝나고 시간 측정도 카운터 조작도 하지 않는다 —
    관측 때문에 정상 경로가 느려지지 않게 하기 위함. 획득에 실패한 경우에만 대기 시간을
    재서 LOCK_WAITS 에 종류별로 누적한다(어느 캐시가 stampede 를 겪는지 = 콜드 빌드가
    사용자 대기로 이어지는 지점이 관리자 화면에 드러난다).
    """
    lock = keyed_lock(key)
    if not lock.acquire(blocking=False):
        t0 = time.perf_counter()
        lock.acquire()
        waited = (time.perf_counter() - t0) * 1000.0
        kind = str(key[0]) if isinstance(key, tuple) and key else "?"
        with CACHE_LOCK:
            entry = LOCK_WAITS.setdefault(kind, [0, 0.0])
            entry[0] += 1
            entry[1] += waited
    try:
        yield
    finally:
        lock.release()


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
        _prune_tables_sizes_locked()


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
        _prune_tables_sizes_locked()


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
        from . import runtime
        manifest = runtime.storage().load_webreport_manifest(
            analysis_key, upload_root=upload_root)
        blob = canon(manifest)
        entry = (blob, hashlib.sha256(blob).hexdigest())
        cache_put(MANIFEST_CACHE, analysis_key, entry, MANIFEST_CACHE_MAX)
        return manifest, entry[1]
    return json.loads(entry[0].decode("utf-8")), entry[1]


def load_manifest_cached(analysis_key, upload_root: Path) -> dict:
    return load_manifest_with_digest(analysis_key, upload_root)[0]

"""무거운 빌드 오프로드 — ProcessPoolExecutor (Phase 6, 2026-07-11).

GIL 직렬화 제거: **콜드 세션**의 report payload / distribution compact / trim payload
빌드(디코드 포함 수 초 CPU)를 워커 프로세스로 보낸다. 서로 다른 콜드 세션 N개가
동시에 열려도 웹 프로세스의 GIL 을 잡지 않아 값싼 요청(/api/history 등)이 밀리지
않는다.

오프로드 규칙 (should_offload):
- 부모의 TABLES_CACHE 가 이미 따뜻한 세션은 **인라인** — 짧은 GIL 점유(~2s)가
  워커 재디코드+왕복보다 싸다 (편집 직후 재빌드가 이 경우).
- 워커 내부에서는 항상 인라인 (재귀 오프로드 방지 — _IN_WORKER).
- WEB_REPORT_COMPUTE_WORKERS=0 이면 전부 인라인 (종전 동작).

워커는 세션을 스스로 로드/디코드하고(자기 프로세스 tables 캐시 재사용, 바이트 상한
동일 적용) disk_cache 도 채운다 — 부모는 반환된 결과만 RAM 캐시에 넣는다.
대화형 소규모 조회(raw_data/chips/scatter)는 오프로드하지 않는다 (플랜 결정).
"""
from __future__ import annotations

import collections
import logging
import os
import threading
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

_log = logging.getLogger(__name__)

# 관리자 패널 노출용 누적 카운터 (프로세스 생존 기간). 락 없이 증가시키지만 GIL 하의
# int 증가라 통계 용도로는 충분하다.
STATS = {"submitted": 0, "inline": 0, "ok": 0, "timeout": 0, "broken": 0, "error": 0,
         "prewarm_queued": 0, "prewarm_dropped": 0, "prewarm_done": 0,
         "ondemand_queued": 0, "ondemand_done": 0, "ondemand_error": 0}

_WORKERS = max(0, int(os.getenv("WEB_REPORT_COMPUTE_WORKERS", "2") or 2))
# 워커 N개 태스크 처리 후 프로세스 재기동 — 워커 프로세스 내 TABLES_CACHE(최대 4GB)로
# RSS 가 단조 증가하는 것을 막는다. 재기동 비용(모듈 재임포트)은 백그라운드라 무해.
_TASKS_PER_CHILD = max(1, int(os.getenv("WEB_REPORT_COMPUTE_TASKS_PER_CHILD", "32") or 32))
# 워커 hang 시 waitress 스레드가 .result() 에서 영구 대기하는 것을 막는 상한.
_TIMEOUT_SEC = float(os.getenv("WEB_REPORT_COMPUTE_TIMEOUT_SEC", "300") or 300)
_IN_WORKER = False
_pool = None
_pool_lock = threading.Lock()


def _init_worker():
    """워커 프로세스 초기화 — 재귀 오프로드 방지 플래그."""
    global _IN_WORKER
    _IN_WORKER = True


def _get_pool():
    global _pool
    if _WORKERS <= 0 or _IN_WORKER:
        return None
    with _pool_lock:
        if _pool is None:
            from concurrent.futures import ProcessPoolExecutor
            _pool = ProcessPoolExecutor(max_workers=_WORKERS, initializer=_init_worker,
                                        max_tasks_per_child=_TASKS_PER_CHILD)
    return _pool


def should_offload(tables_key) -> bool:
    """이 세션의 콜드 빌드를 워커로 보낼지 — tables 캐시가 차가울 때만 True."""
    if _IN_WORKER or _WORKERS <= 0:
        return False
    from . import cache
    with cache.CACHE_LOCK:
        warm = tables_key in cache.TABLES_CACHE
    return not warm


def _reset_pool(shutdown=False):
    """현재 풀을 버린다. shutdown=True 면 워커 프로세스도 즉시 정리한다.

    hang 된 워커는 태스크를 끝내지 못해 max_tasks_per_child 재기동에도 걸리지 않으므로,
    풀을 통째로 버리지 않으면 그 슬롯이 영구히 죽는다(워커 2개면 2번이면 전멸).
    """
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None and shutdown:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            _log.exception("compute pool shutdown failed")


def run(job, *args):
    """job 을 워커에서 실행하고 결과를 반환. 풀 비활성/워커 내부면 인라인 실행.

    워커 프로세스 붕괴(BrokenProcessPool — 대개 워커 OOM)와 타임아웃(워커 hang) 모두
    풀을 버리고 raise 한다. 인라인 폴백은 하지 않는다 — 붕괴 원인이 메모리/데이터면
    같은 작업을 웹 프로세스에서 다시 돌려 GIL 과 RAM 을 그대로 태우고 웹 프로세스까지
    같이 죽을 수 있다. 호출부는 이 예외를 503 으로 돌려준다.
    """
    pool = _get_pool()
    if pool is None:
        STATS["inline"] += 1
        return job(*args)
    STATS["submitted"] += 1
    try:
        fut = pool.submit(job, *args)
        result = fut.result(timeout=_TIMEOUT_SEC)
        STATS["ok"] += 1
        return result
    except BrokenProcessPool:
        STATS["broken"] += 1
        _log.error("compute worker pool broken — 풀 폐기 후 실패 반환: %s%r",
                   getattr(job, "__name__", job), args, exc_info=True)
        _reset_pool(shutdown=True)
        raise
    except TimeoutError:
        STATS["timeout"] += 1
        # fut.cancel() 은 이미 실행 중인 작업을 못 멈춘다(선급행분은 RUNNING 마킹).
        # hang 워커를 계속 안고 가면 슬롯이 영구 소모되므로 풀 자체를 버린다.
        _log.error("compute worker timeout (%ss) — 풀 폐기: %s%r", _TIMEOUT_SEC,
                   getattr(job, "__name__", job), args)
        _reset_pool(shutdown=True)
        raise
    except Exception:
        STATS["error"] += 1
        raise


def status():
    """관리자 패널용 컴퓨트 풀 상태 스냅샷."""
    pool = _pool
    alive = 0
    if pool is not None:
        try:
            alive = len(getattr(pool, "_processes", {}) or {})
        except Exception:
            alive = 0
    with _prewarm_lock:
        pending = len(_prewarm_queue)
    with _ondemand_lock:
        ondemand_pending = len(_ondemand_pending)
    return {"workers": _WORKERS, "pool_alive": pool is not None, "processes": alive,
            "timeout_sec": _TIMEOUT_SEC, "tasks_per_child": _TASKS_PER_CHILD,
            "prewarm_pending": pending, "prewarm_max": _PREWARM_MAX_PENDING,
            "ondemand_pending": ondemand_pending, "ondemand_workers": _ONDEMAND_WORKERS,
            "stats": dict(STATS)}


# ── 워커 잡 (모듈 최상위 — spawn pickling 요건). service 를 재사용하므로 값이
#    인라인 계산과 동일하고, 워커 안에서는 should_offload=False 라 재귀하지 않는다. ──

def report_job(session_id: str, upload_root_str: str) -> dict:
    from database import report_db
    from . import service
    _, report = service.load_webreport(
        session_id, report_db=report_db, upload_root=Path(upload_root_str))
    return report


def dist_job(session_id: str, upload_root_str: str, bin1: bool = False) -> bytes:
    from database import report_db
    from . import service
    return service.get_distribution_gzip(
        session_id, report_db=report_db, upload_root=Path(upload_root_str),
        bin1=bool(bin1))


def map_job(session_id: str, upload_root_str: str) -> bytes:
    from database import report_db
    from . import service
    return service.get_map_gzip(
        session_id, report_db=report_db, upload_root=Path(upload_root_str))


def trim_job(session_id: str, upload_root_str: str, source: str) -> bytes:
    from database import report_db
    from . import service
    blob, _ = service.get_trim_analysis_gzip(
        session_id, report_db=report_db, upload_root=Path(upload_root_str), source=source)
    return blob


def prewarm_job(session_id: str, upload_root_str: str, dist_seeded: bool = False) -> None:
    """프리웜 전용 잡 — report(+시딩된 dist)를 빌드하고 **None 을 반환**한다.

    report_job 결과(payload dict, 수 MB)는 부모 RAM 캐시 적재 외엔 쓸모가 없어 IPC
    반송(pickle) 비용만 크다. 워커가 disk_cache 를 채우므로 부모의 첫 조회는 디스크
    캐시로 열린다. dist 는 시딩된 blob 이 disk_cache 에 이미 있어 값싼 작업이다.
    """
    report_job(session_id, upload_root_str)
    if dist_seeded:
        dist_job(session_id, upload_root_str)
    return None


# ── 프리웜 큐 ────────────────────────────────────────────────────────────────
# 업로드당 스레드를 띄우면 폭주 시 스레드가 무한히 쌓인다(세마포어는 동시 실행만 제한할 뿐
# 대기 스레드를 막지 못한다). 단일 소비자 스레드 + 상한 있는 큐로 바꾸고, 넘치면 가장
# 오래된 대기분을 버린다 — 프리웜은 실패해도 첫 조회가 다시 계산하므로 손실이 무해하다.
_PREWARM_MAX_PENDING = max(2, int(os.getenv("WEB_REPORT_PREWARM_QUEUE", "8") or 8))
_prewarm_queue = collections.deque()
_prewarm_lock = threading.Lock()
_prewarm_wake = threading.Event()
_prewarm_thread = None


def _prewarm_one(session_id: str, upload_root_str: str, dist_seeded: bool) -> None:
    """업로드 직후 워밍업 — **report payload 만** 만든다.

    종전에는 dist/map/trim 풀 payload 까지 전부 만들었다. 열어보지도 않을 탭까지
    업로드마다 빌드하느라 CPU·디스크 캐시를 태우는 비용이 컸다. report payload 는
    세션을 열면 반드시 쓰이고(/full), 그 과정에서 tables 가 웜이 되어 콜드의 지배
    비용(재다운로드+디코드)이 사라지므로 나머지 탭의 첫 진입도 충분히 빨라진다.
    dist 는 클라가 프리컴퓨트 blob 을 붙여준 경우에만 만든다 — 이미 시딩돼 있어
    gzip 직렬화만 하면 되는 값싼 작업이다.

    실행은 run(prewarm_job) 경유 (2026-07-22): 업로드 직후엔 ingest 가 부모
    TABLES_CACHE 를 시딩해 둬 should_offload 가 False 라, 직접 호출하면 수 초 CPU 가
    waitress 프로세스에서 돌며 GIL 로 세션 밖 요청(홈·VOC)까지 지연시켰다. 운영
    (WEB_REPORT_COMPUTE_WORKERS=2)에서는 워커 프로세스가 계산하고,
    =0(테스트)이면 run() 인라인 폴백으로 종전과 동일하다.
    """
    try:
        run(prewarm_job, session_id, upload_root_str, bool(dist_seeded))
        STATS["prewarm_done"] += 1
    except Exception:
        # 프리웜 실패는 조회 시 재계산으로 복구되지만, 조용히 삼키면 상시 실패를
        # 아무도 모른다 — 로그로는 남긴다.
        _log.warning("[prewarm] failed session=%s", session_id, exc_info=True)


def _prewarm_loop() -> None:
    while True:
        with _prewarm_lock:
            item = _prewarm_queue.popleft() if _prewarm_queue else None
        if item is None:
            _prewarm_wake.wait()
            _prewarm_wake.clear()
            continue
        _prewarm_one(*item)


def prewarm(session_id: str, upload_root_str: str, dist_seeded: bool = False) -> None:
    """업로드 직후 프리웜 요청을 큐에 넣는다 (소비자 스레드가 순차로 run() 에 넘긴다).

    계산은 run(prewarm_job) 이 워커 프로세스로 보낸다 (2026-07-22 변경) — 종전에는 부모
    스레드가 직접 돌려 TABLES_CACHE 재사용(재디코드 0회)·부모 RAM 히트 이점이 있었지만,
    그 수 초 CPU 가 waitress 프로세스 GIL 을 잡아 세션 밖 값싼 요청까지 밀렸다. 지금은
    워커가 재디코드해 disk_cache 를 채우고, 부모 첫 조회는 디스크 캐시로 열린다
    (약간 느린 대신 GIL 해방 — 승인된 트레이드오프). 큐잉은 즉시 반환하므로 업로드
    응답을 블록하지 않는다."""
    global _prewarm_thread
    with _prewarm_lock:
        if len(_prewarm_queue) >= _PREWARM_MAX_PENDING:
            dropped = _prewarm_queue.popleft()
            STATS["prewarm_dropped"] += 1
            _log.warning("[prewarm] 큐 포화(%d) — 가장 오래된 요청 폐기: session=%s",
                         _PREWARM_MAX_PENDING, dropped[0])
        _prewarm_queue.append((session_id, upload_root_str, bool(dist_seeded)))
        STATS["prewarm_queued"] += 1
        if _prewarm_thread is None:
            _prewarm_thread = threading.Thread(target=_prewarm_loop,
                                               name="webreport-prewarm", daemon=True)
            _prewarm_thread.start()
    _prewarm_wake.set()


# ── 온디맨드 콜드 빌드 큐 (라우트 202 경로) ──────────────────────────────────────
# 콜드 미스 조회는 지금까지 요청 스레드가 빌드 완료까지 기다렸다(수 초~수십 초). waitress
# 스레드는 8개뿐이라 서로 다른 신규 세션을 여러 명이 동시에 열면 그 스레드들이 전부 묶여
# 검색·health 같은 값싼 요청까지 밀린다. 여기서는 라우트가 202 를 즉시 반환하고 빌드는
# 이 큐의 소비자 스레드가 맡는다 — 프런트는 build_status 폴링 후 재요청한다.
#
# 프리웜 큐와 분리한 이유: 프리웜은 포화 시 가장 오래된 요청을 버리는데(무해), 여기 요청은
# 사용자가 화면에서 대기 중이라 버리면 그 사용자만 영영 로드되지 않는다. 대신 (session,
# kind) 중복 등록을 막아 재요청 폭주에도 큐가 자라지 않게 한다.
_ONDEMAND_WORKERS = max(1, int(os.getenv("WEB_REPORT_ONDEMAND_WORKERS", "2") or 2))
_ondemand_queue = collections.deque()
_ondemand_pending: set = set()      # (session_id, kind) — 큐 대기 + 실행 중
_ondemand_lock = threading.Lock()
_ondemand_wake = threading.Event()
_ondemand_threads: list = []

_ONDEMAND_JOBS = {
    "report": lambda sid, root: report_job(sid, root),
    "map": lambda sid, root: map_job(sid, root),
}


def _ondemand_loop() -> None:
    while True:
        with _ondemand_lock:
            item = _ondemand_queue.popleft() if _ondemand_queue else None
            if item is None:
                _ondemand_wake.clear()
        if item is None:
            _ondemand_wake.wait()
            continue
        session_id, upload_root_str, kind = item
        try:
            _ONDEMAND_JOBS[kind](session_id, upload_root_str)
            STATS["ondemand_done"] += 1
        except Exception:
            # 실패해도 다음 요청이 다시 큐에 넣는다(pending 해제 후). 조용히 삼키면
            # 프런트가 무한 폴링하므로 로그로는 남긴다.
            STATS["ondemand_error"] += 1
            _log.warning("[ondemand] %s build failed session=%s", kind, session_id,
                         exc_info=True)
        finally:
            with _ondemand_lock:
                _ondemand_pending.discard((session_id, kind))


def request_build(session_id: str, upload_root_str: str, kind: str = "report") -> None:
    """콜드 빌드를 백그라운드에 요청한다 (이미 대기/실행 중이면 무시).

    라우트가 202 를 반환하기 직전에 부른다. 같은 세션을 여러 명이 동시에 열어도 등록은
    1건이고, 실제 빌드 안에서 keyed_lock single-flight 가 한 번 더 중복을 막는다.
    """
    if kind not in _ONDEMAND_JOBS:
        raise ValueError(f"unknown build kind: {kind}")
    with _ondemand_lock:
        key = (session_id, kind)
        if key in _ondemand_pending:
            return
        _ondemand_pending.add(key)
        _ondemand_queue.append((session_id, upload_root_str, kind))
        STATS["ondemand_queued"] += 1
        while len(_ondemand_threads) < _ONDEMAND_WORKERS:
            th = threading.Thread(target=_ondemand_loop,
                                  name=f"webreport-ondemand-{len(_ondemand_threads)}",
                                  daemon=True)
            _ondemand_threads.append(th)
            th.start()
    _ondemand_wake.set()

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

import logging
import os
import threading
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

_log = logging.getLogger(__name__)

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


def run(job, *args):
    """job 을 워커에서 실행하고 결과를 반환. 풀 비활성/워커 내부면 인라인 실행.

    워커 프로세스 붕괴(BrokenProcessPool — OOM, main 가드 없는 스크립트 등)는
    풀을 리셋하고 인라인으로 폴백한다 — 요청이 500 으로 죽지 않는다.
    타임아웃(워커 hang)은 raise — 인라인 폴백하면 hang 원인이 데이터일 때 부모
    GIL 까지 태우므로 요청만 실패시킨다(워커 태스크 자체는 계속 돌 수 있음)."""
    global _pool
    pool = _get_pool()
    if pool is None:
        return job(*args)
    try:
        fut = pool.submit(job, *args)
        return fut.result(timeout=_TIMEOUT_SEC)
    except BrokenProcessPool:
        _log.error("compute worker pool broken — 풀 리셋 후 인라인 폴백: %s%r",
                   getattr(job, "__name__", job), args, exc_info=True)
        with _pool_lock:
            _pool = None
        return job(*args)
    except TimeoutError:
        # 요청자는 이미 실패했으므로 큐 대기 작업 회수 시도. 풀이 워커 feed 큐에
        # max_workers+1 개를 선급행하며 그 시점에 RUNNING 마킹되므로, 그보다 뒤에
        # 대기 중인 작업만 실제로 취소된다 — 선급행/실행 중이면 False 반환(무해).
        fut.cancel()
        _log.error("compute worker timeout (%ss): %s%r", _TIMEOUT_SEC,
                   getattr(job, "__name__", job), args)
        raise


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


# 동시 프리웜 스레드 상한 (연속 업로드 폭주 방지 — 종전 풀 제출 시절의 워커 수 상한과 동등).
_PREWARM_SLOTS = threading.BoundedSemaphore(max(1, _WORKERS))


def _prewarm_job(session_id: str, upload_root_str: str) -> None:
    with _PREWARM_SLOTS:
        try:
            report_job(session_id, upload_root_str)
            dist_job(session_id, upload_root_str)
            map_job(session_id, upload_root_str)
            # 기본 source("") — Trim 탭 첫 진입 요청과 같은 캐시 키라 그대로 히트한다.
            # scatter 는 subject 단위(수백~수천)라 전량 프리웜이 비싸고, 위 report_job 이
            # tables 를 웜으로 만들어 콜드의 지배 비용(재다운로드+디코드)은 이미 사라진다.
            trim_job(session_id, upload_root_str, "")
        except Exception:
            pass


def prewarm(session_id: str, upload_root_str: str) -> None:
    """업로드 직후 프리웜 — 부모 데몬 스레드에서 실행 (2026-07-12 워커 제출에서 복귀).

    워커 제출 시절엔 워커 프로세스가 부모 TABLES_CACHE 시딩을 못 봐 storage 재다운로드+
    재디코드(업로드당 디코드 2회)가 났고, 업로더가 곧바로 페이지를 열면 부모 인라인 빌드와
    중복 계산됐다. 부모 스레드면 ingest 가 방금 시딩한 캐시를 그대로 쓰고(재디코드 0회),
    keyed_lock single-flight 로 직후 /full·/distribution 과도 중복되지 않으며, 결과가 부모
    RAM 캐시에 직접 들어가 첫 조회가 RAM 히트다. 시딩이 이미 축출된 세션은 load 경로의
    should_offload 가 자동으로 워커 오프로드를 택한다(자기교정). 세마포어 acquire 는 스레드
    안에서 하므로 업로드 응답을 블록하지 않는다. 실패는 무해 — 첫 조회가 다시 계산할 뿐이다."""
    threading.Thread(target=_prewarm_job, args=(session_id, upload_root_str),
                     name=f"webreport-prewarm-{session_id}", daemon=True).start()

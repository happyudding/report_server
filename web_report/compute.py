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

import os
import threading
from pathlib import Path

_WORKERS = max(0, int(os.getenv("WEB_REPORT_COMPUTE_WORKERS", "2") or 2))
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
            _pool = ProcessPoolExecutor(max_workers=_WORKERS, initializer=_init_worker)
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
    """job 을 워커에서 실행하고 결과를 반환. 풀 비활성/워커 내부면 인라인 실행."""
    pool = _get_pool()
    if pool is None:
        return job(*args)
    return pool.submit(job, *args).result()


# ── 워커 잡 (모듈 최상위 — spawn pickling 요건). service 를 재사용하므로 값이
#    인라인 계산과 동일하고, 워커 안에서는 should_offload=False 라 재귀하지 않는다. ──

def report_job(session_id: str, upload_root_str: str) -> dict:
    from database import report_db
    from . import service
    _, report = service.load_webreport(
        session_id, report_db=report_db, upload_root=Path(upload_root_str))
    return report


def dist_job(session_id: str, upload_root_str: str) -> bytes:
    from database import report_db
    from . import service
    return service.get_distribution_gzip(
        session_id, report_db=report_db, upload_root=Path(upload_root_str))


def trim_job(session_id: str, upload_root_str: str, source: str) -> bytes:
    from database import report_db
    from . import service
    blob, _ = service.get_trim_analysis_gzip(
        session_id, report_db=report_db, upload_root=Path(upload_root_str), source=source)
    return blob


def _prewarm_job(session_id: str, upload_root_str: str) -> None:
    try:
        report_job(session_id, upload_root_str)
        dist_job(session_id, upload_root_str)
    except Exception:
        pass


def prewarm(session_id: str, upload_root_str: str) -> None:
    """업로드 직후 프리웜 — 풀에 제출 (동시성 상한 = 워커 수, 연속 업로드 폭주 방지).

    풀 비활성이면 종전 데몬 스레드 방식으로 폴백한다. 실패는 무해 — 첫 조회가
    다시 계산할 뿐이다."""
    pool = _get_pool()
    if pool is not None:
        try:
            pool.submit(_prewarm_job, session_id, upload_root_str)
            return
        except Exception:
            pass
    threading.Thread(target=_prewarm_job, args=(session_id, upload_root_str),
                     name=f"webreport-prewarm-{session_id}", daemon=True).start()

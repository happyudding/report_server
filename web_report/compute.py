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
import faulthandler
import functools
import logging
import os
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from . import build_log, build_status

_log = logging.getLogger(__name__)

# 관리자 패널 노출용 누적 카운터 (프로세스 생존 기간). 락 없이 증가시키지만 GIL 하의
# int 증가라 통계 용도로는 충분하다.
STATS = {"submitted": 0, "inline": 0, "ok": 0, "timeout": 0, "broken": 0, "error": 0,
         "worker_killed": 0,
         "prewarm_queued": 0, "prewarm_dropped": 0, "prewarm_done": 0,
         "ondemand_queued": 0, "ondemand_done": 0, "ondemand_error": 0,
         "distpack_queued": 0, "distpack_done": 0, "distpack_error": 0,
         "rewarm_queued": 0}

_WORKERS = max(0, int(os.getenv("WEB_REPORT_COMPUTE_WORKERS", "2") or 2))
# 워커 N개 태스크 처리 후 프로세스 재기동 — 워커 프로세스 내 TABLES_CACHE(최대 4GB)로
# RSS 가 단조 증가하는 것을 막는다. 재기동 비용(모듈 재임포트)은 백그라운드라 무해.
_TASKS_PER_CHILD = max(1, int(os.getenv("WEB_REPORT_COMPUTE_TASKS_PER_CHILD", "32") or 32))
# 워커 hang 시 waitress 스레드가 .result() 에서 영구 대기하는 것을 막는 상한.
_TIMEOUT_SEC = float(os.getenv("WEB_REPORT_COMPUTE_TIMEOUT_SEC", "300") or 300)
_IN_WORKER = False
_pool = None
_pool_lock = threading.Lock()
_WORKER_FAULT_FILE = None  # 워커 faulthandler 파일 핸들 (전역 보관 — GC 로 fd 가 닫히면 덤프 유실)


def _init_worker():
    """워커 프로세스 초기화 — 재귀 오프로드 방지 플래그 + 네이티브 크래시 로깅 + 로깅."""
    global _IN_WORKER
    _IN_WORKER = True
    _enable_worker_faulthandler()
    _enable_worker_logging()


def _enable_worker_logging():
    """워커의 logging 을 파일로 — 지금까지 워커 안의 _log.warning/exception 은
    무포맷 stderr 로 나가 부모 tee(server_*.txt)에 잡히지 않고 증발했다(wsgi 의
    __mp_main__ 가드가 워커에서 tee 설치를 건너뛰기 때문).

    날짜 파일 공유 append — 워커 2~8개가 인터리브해도 라인 단위라 실용상 읽을 수 있다."""
    try:
        import config
        d = Path(config.ROOT_DIR) / "server" / "log"
        d.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(d / f"compute_worker_{time.strftime('%Y%m%d')}.log",
                                encoding="utf-8", delay=True)
        h.setFormatter(logging.Formatter(
            f"%(asctime)s %(levelname)s [pid {os.getpid()}] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(h)
    except Exception:
        pass


def _job(kind: str):
    """워커 잡 데코레이터 — 실행 중 체크포인트(sidecar) + hang 스택 덤프 예약.

    타임아웃으로 terminate 되는 워커는 아무 흔적도 남기지 못했다. 여기서 두 겹을 건다:
    ① build_log sidecar 에 현재 단계를 계속 남겨 부모가 사후에 읽고,
    ② 타임아웃 10초 전 faulthandler 가 자기 스택을 워커 덤프 파일에 찍는다
       (부모가 자식 스택을 뜨는 방법은 Windows 에 없다 — 자식이 스스로 찍어야 한다).
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(session_id, *args, **kw):
            if not _IN_WORKER:
                return fn(session_id, *args, **kw)
            build_log.begin_job(kind, session_id)
            dumping = False
            try:
                if _TIMEOUT_SEC > 20 and _WORKER_FAULT_FILE is not None:
                    faulthandler.dump_traceback_later(_TIMEOUT_SEC - 10,
                                                      file=_WORKER_FAULT_FILE)
                    dumping = True
            except Exception:
                pass
            try:
                return fn(session_id, *args, **kw)
            finally:
                if dumping:
                    try:
                        faulthandler.cancel_dump_traceback_later()
                    except Exception:
                        pass
                build_log.end_job()
        return wrapper
    return deco


def _pool_pids():
    """현재 풀의 워커 PID 목록 (sidecar 회수용)."""
    try:
        return [p.pid for p in (getattr(_pool, "_processes", None) or {}).values()]
    except Exception:
        return []


def _dead_worker_state(session_id):
    """이 세션을 돌고 있던 워커의 마지막 체크포인트 (없으면 None).

    타임아웃 시 **풀을 버리기 전에** 부른다 — terminate 후에는 sidecar 가 남아 있어도
    어느 pid 가 살아 있었는지 알 수 없다."""
    try:
        for st in build_log.read_states(_pool_pids()):
            if st.get("session") == session_id:
                return st
    except Exception:
        pass
    return None


def _enable_worker_faulthandler():
    """워커의 네이티브 크래시(OOM/세그폴트)를 per-PID 파일에 기록한다. 워커 stdout 은
    부모 tee(server_*.txt)로 흐르지 않아(__mp_main__ 가드) 크래시 흔적이 사라지므로
    별도 파일이 필요하다. 공유 append 대신 per-PID 파일 — 동시 크래시 시 인터리브·PID
    귀속 불가 문제를 피한다. 전체 best-effort(실패해도 워커 초기화는 계속)."""
    global _WORKER_FAULT_FILE
    try:
        import config
        log_dir = Path(config.ROOT_DIR) / "server" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        _prune_worker_fault_files(log_dir)
        path = log_dir / f"faulthandler_worker_{os.getpid()}.txt"
        _WORKER_FAULT_FILE = path.open("a", encoding="utf-8")
        faulthandler.enable(file=_WORKER_FAULT_FILE)
    except Exception:
        pass


def _prune_worker_fault_files(log_dir):
    """빈 워커 덤프 파일(리사이클로 PID 가 계속 바뀌어 다발) + LOG_KEEP_DAYS 경과분 정리.
    살아있는 워커의 파일은 Windows 공유 위반으로 unlink 실패 = in-use 보호로 스킵된다."""
    try:
        keep_days = float(os.getenv("LOG_KEEP_DAYS", "14"))
        cutoff = time.time() - keep_days * 86400
        for p in log_dir.glob("faulthandler_worker_*.txt"):
            try:
                st = p.stat()
                if st.st_size == 0 or st.st_mtime < cutoff:
                    p.unlink()
            except Exception:
                pass
    except Exception:
        pass


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

    ⚠️ shutdown(wait=False, cancel_futures=True) 은 아직 RUNNING 인 워커 프로세스를
    끝내지 못한다 — hang(병리적 입력으로 인한 무한 루프/데드락) 워커는 최대 4GB(TABLES_CACHE)
    를 쥔 채 영구 잔존하고, 타임아웃이 반복되면 그 프로세스가 누적돼 RAM 을 고갈시킨다.
    그래서 풀을 버리기 전에 자식 프로세스 핸들을 확보해 terminate 로 강제 회수한다.
    (워커 잡은 순수 pandas/numpy CPU 연산이라 손자 프로세스가 없어 terminate 로 충분.)
    """
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is None or not shutdown:
        return
    # shutdown 이 _processes 를 비우기 전에 프로세스 핸들을 먼저 확보한다.
    try:
        procs = list((getattr(pool, "_processes", None) or {}).values())
    except Exception:
        procs = []
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        _log.exception("compute pool shutdown failed")
    killed = 0
    for p in procs:
        try:
            if p.is_alive():
                p.terminate()          # Windows: TerminateProcess / POSIX: SIGTERM
                p.join(timeout=2)       # best-effort — 좀비(defunct) 회수, 블록 최소화
                killed += 1
        except Exception:
            pass
    if killed:
        STATS["worker_killed"] += killed
        _log.warning("compute pool: %d 워커 프로세스 강제 종료(terminate)", killed)


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
    name = getattr(job, "__name__", str(job))
    t0 = time.time()
    try:
        fut = pool.submit(job, *args)
        result = fut.result(timeout=_TIMEOUT_SEC)
        STATS["ok"] += 1
        return result
    except BrokenProcessPool as exc:
        STATS["broken"] += 1
        _log.error("compute worker pool broken — 풀 폐기 후 실패 반환: %s%r",
                   name, args, exc_info=True)
        # 실패 기록은 여기서만 가능하다 — 어느 세션이 몇 초 만에 죽었는지 아는 유일한 지점.
        # sidecar 는 풀을 버리기 전에 읽어야 어느 pid 가 이 잡을 돌았는지 알 수 있다.
        state = _dead_worker_state(args[0] if args else "")
        build_log.record_failure(name, args, "broken", time.time() - t0, repr(exc), state)
        _emit_build_failure(name, args, "broken", time.time() - t0, repr(exc), state)
        _reset_pool(shutdown=True)
        raise
    except TimeoutError:
        STATS["timeout"] += 1
        # ⚠️ 이 타임아웃은 **풀 큐 대기까지 포함**한 시간이다 — 큐에서 대기만 하다 시간을
        # 소진한 잡은 cancel 이 성공한다(아직 미시작 = 워커 무결). 이때 풀을 버리면 무고한
        # 동시 빌드까지 전멸하므로(전 워커 terminate) 실패만 기록하고 풀은 보존한다.
        if fut.cancel():
            _log.error("compute queue-wait timeout (%ss) — 미시작 잡 취소, 풀 보존: %s%r",
                       _TIMEOUT_SEC, name, args)
            err = f"TimeoutError(queued, {_TIMEOUT_SEC}s)"
            build_log.record_failure(name, args, "timeout", time.time() - t0, err)
            _emit_build_failure(name, args, "timeout", time.time() - t0, err, None)
            raise
        # cancel 실패 = 실행 중(선급행 RUNNING 포함) — fut.cancel() 은 이미 실행 중인
        # 작업을 못 멈춘다. hang 워커를 계속 안고 가면 슬롯이 영구 소모되므로 풀 자체를
        # 버린다.
        _log.error("compute worker timeout (%ss) — 풀 폐기: %s%r", _TIMEOUT_SEC,
                   name, args)
        doomed_pids = _pool_pids()          # 풀을 버리면 pid 를 알 수 없으므로 먼저 확보
        state = _dead_worker_state(args[0] if args else "")
        if state:
            _log.error("  ↳ 마지막 단계: %s%s (%.1fs 경과, 빌드 %.1fs)",
                       state.get("stage") or "?",
                       f" [{state.get('source')}]" if state.get("source") else "",
                       float(state.get("stage_elapsed") or 0),
                       float(state.get("elapsed") or 0))
        err = f"TimeoutError({_TIMEOUT_SEC}s)"
        build_log.record_failure(name, args, "timeout", time.time() - t0, err, state)
        _emit_build_failure(name, args, "timeout", time.time() - t0, err, state)
        _reset_pool(shutdown=True)
        build_log.drop_states(doomed_pids)   # terminate 된 워커의 sidecar 잔해 정리
        raise
    except Exception as exc:
        STATS["error"] += 1
        state = _dead_worker_state(args[0] if args else "")
        build_log.record_failure(name, args, "error", time.time() - t0, repr(exc), state)
        _emit_build_failure(name, args, "error", time.time() - t0, repr(exc), state)
        raise


def _emit_build_failure(name, args, result, elapsed, error_text, state):
    """콜드 빌드 실패를 진단 사건으로 — 빌드 로그와 달리 사용자 요청·오류와 한 타임라인에
    묶인다(같은 session_id 로 이어짐). 여기가 아니면 빌드 실패는 관리자 이력 탭에서만
    보이고, "그때 그 사용자가 못 연 세션"과 연결되지 않는다."""
    try:
        import diagnostics
        session_id = args[0] if args and isinstance(args[0], str) else ""
        st = state or {}
        diagnostics.emit("critical" if result != "error" else "warning", "build",
                         f"build_{result}",
                         session_id=session_id, build_id=st.get("build_id") or None,
                         elapsed_ms=int(max(0.0, elapsed) * 1000),
                         error_type=result, message=f"{name}: {error_text}",
                         source=st.get("source") or None,
                         last_stage=st.get("stage") or "(워커 미시작 — 큐 대기)",
                         **diagnostics.current_ids())
    except Exception:
        pass


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
    with _distpack_lock:
        distpack_pending = len(_distpack_pending)
    return {"workers": _WORKERS, "pool_alive": pool is not None, "processes": alive,
            "timeout_sec": _TIMEOUT_SEC, "tasks_per_child": _TASKS_PER_CHILD,
            "prewarm_pending": pending, "prewarm_max": _PREWARM_MAX_PENDING,
            "ondemand_pending": ondemand_pending, "ondemand_workers": _ONDEMAND_WORKERS,
            "distpack_pending": distpack_pending,
            "stats": dict(STATS)}


# ── 워커 잡 (모듈 최상위 — spawn pickling 요건). service 를 재사용하므로 값이
#    인라인 계산과 동일하고, 워커 안에서는 should_offload=False 라 재귀하지 않는다. ──
#
# report/dist/map 잡은 **(결과, timing|None)** 튜플을 돌려준다. 단계별 소요는 실제로
# 계산이 일어난 워커 프로세스 안에서만 잴 수 있어, 그 dict 를 결과에 실어 부모로 보낸다
# (부모는 record_offloaded 로 풀 대기·IPC 를 얹어 기록). 워커가 디스크 캐시로 즉답하는
# 등 콜드 빌드가 없었으면 timing 은 None 이다.

def _stamp(t_start: float) -> dict | None:
    """이번 호출에서 일어난 콜드 빌드의 단계 기록에 프로세스 간 비교용 시각을 붙인다."""
    timing = build_log.pop_stash()
    if timing is not None:
        timing["t_start"] = t_start
        timing["t_end"] = time.time()
    return timing


@_job("report")
def report_job(session_id: str, upload_root_str: str):
    from database import report_db
    from . import service
    t_start = time.time()
    _, report = service.load_webreport(
        session_id, report_db=report_db, upload_root=Path(upload_root_str))
    return report, _stamp(t_start)


@_job("dist")
def dist_job(session_id: str, upload_root_str: str, bin1: bool = False,
             bin1_scope: str = ""):
    from database import report_db
    from . import service
    t_start = time.time()
    blob = service.get_distribution_gzip(
        session_id, report_db=report_db, upload_root=Path(upload_root_str),
        bin1=bool(bin1), bin1_scope=str(bin1_scope or ""))
    return blob, _stamp(t_start)


@_job("temp_map")
def temp_map_job(session_id: str, upload_root_str: str):
    """Temperature 항목별 fail die 인덱스 gzip — map_job 과 같은 규약(워커 오프로드)."""
    from database import report_db
    from . import service
    t_start = time.time()
    blob = service._temp_map_blob(
        service.get_temp_map(session_id, report_db=report_db,
                             upload_root=Path(upload_root_str)))
    return blob, _stamp(t_start)


@_job("map")
def map_job(session_id: str, upload_root_str: str):
    from database import report_db
    from . import service
    t_start = time.time()
    blob = service.get_map_gzip(
        session_id, report_db=report_db, upload_root=Path(upload_root_str))
    return blob, _stamp(t_start)


@_job("trim")
def trim_job(session_id: str, upload_root_str: str, source: str) -> bytes:
    from database import report_db
    from . import service
    blob, _ = service.get_trim_analysis_gzip(
        session_id, report_db=report_db, upload_root=Path(upload_root_str), source=source)
    return blob


@_job("trim")
def trim_chart_batch_job(session_id: str, upload_root_str: str, source: str,
                         group_ids: list) -> bytes:
    """Trim 산포 1페이지(≤3그룹) 차트 배치 — 콜드일 때만 여기로 온다.

    그룹 1개짜리 차트는 웹 프로세스 인라인이라 waitress 스레드가 GIL 을 잡고 있었다.
    배치는 tables 디코드까지 워커에서 끝내고 gzip bytes 만 돌려준다.
    """
    from database import report_db
    from . import service
    return service.get_trim_charts_batch(
        session_id, report_db=report_db, upload_root=Path(upload_root_str),
        source=source, group_ids=list(group_ids))


@_job("dist_pack")
def dist_pack_job(session_id: str, upload_root_str: str, base: bool = False) -> None:
    """Distribution pack 생성 잡 — 결과는 dist_pack_store 에 영구 저장되므로 반환값 없음."""
    from database import report_db
    from . import service
    service.materialize_dist_pack(
        session_id, report_db=report_db, upload_root=Path(upload_root_str), base=bool(base))
    return None


def prewarm_job(session_id: str, upload_root_str: str, dist_seeded: bool = False) -> dict:
    """프리웜 전용 잡 — report(+시딩된 dist)를 빌드하고 **timing 만 반환**한다.

    report_job 결과(payload dict, 수 MB)는 부모 RAM 캐시 적재 외엔 쓸모가 없어 IPC
    반송(pickle) 비용만 크다. 워커가 disk_cache 를 채우므로 부모의 첫 조회는 디스크
    캐시로 열린다. dist 는 시딩된 blob 이 disk_cache 에 이미 있어 값싼 작업이다.
    payload 는 여기서 버리고 단계 기록(수 KB 미만)만 실어 보낸다.
    """
    _, report_timing = report_job(session_id, upload_root_str)
    dist_timing = None
    if dist_seeded:
        _, dist_timing = dist_job(session_id, upload_root_str)
    return {"report": report_timing, "dist": dist_timing}


# ── 프리웜 큐 ────────────────────────────────────────────────────────────────
# 업로드당 스레드를 띄우면 폭주 시 스레드가 무한히 쌓인다(세마포어는 동시 실행만 제한할 뿐
# 대기 스레드를 막지 못한다). 단일 소비자 스레드 + 상한 있는 큐로 바꾸고, 넘치면 가장
# 오래된 대기분을 버린다 — 프리웜은 실패해도 첫 조회가 다시 계산하므로 손실이 무해하다.
_PREWARM_MAX_PENDING = max(2, int(os.getenv("WEB_REPORT_PREWARM_QUEUE", "8") or 8))
_prewarm_queue = collections.deque()
_prewarm_lock = threading.Lock()
_prewarm_wake = threading.Event()
_prewarm_thread = None


def _prewarm_one(session_id: str, upload_root_str: str, dist_seeded: bool,
                 t_enq: float = 0.0) -> None:
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
        queue_wait = round(max(0.0, time.time() - t_enq), 3) if t_enq else 0.0
        with build_log.context(trigger="prewarm", queue_wait=queue_wait):
            t_sub = time.time()
            timings = run(prewarm_job, session_id, upload_root_str, bool(dist_seeded))
            t_recv = time.time()
            for kind, timing in (timings or {}).items():
                if timing:      # 워커에서 실제 콜드 빌드가 일어난 경우만
                    build_log.record_offloaded(kind, session_id, "", t_sub, t_recv, timing)
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
        # 같은 세션이 이미 대기 중이면 접는다 — 편집 blur 자동저장이 연달아 와도
        # 리빌드는 1회면 충분하다(실행 시점의 최신 edits_rev 로 빌드되므로 정확).
        if any(q[0] == session_id and q[2] == bool(dist_seeded) for q in _prewarm_queue):
            return
        if len(_prewarm_queue) >= _PREWARM_MAX_PENDING:
            dropped = _prewarm_queue.popleft()
            STATS["prewarm_dropped"] += 1
            _log.warning("[prewarm] 큐 포화(%d) — 가장 오래된 요청 폐기: session=%s",
                         _PREWARM_MAX_PENDING, dropped[0])
        _prewarm_queue.append((session_id, upload_root_str, bool(dist_seeded), time.time()))
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
        session_id, upload_root_str, kind, t_enq = item
        try:
            # 큐 대기(= 앞선 콜드 빌드에 밀린 시간)는 여기서만 잴 수 있다.
            with build_log.context(trigger="ondemand",
                                   queue_wait=round(max(0.0, time.time() - t_enq), 3)):
                _ONDEMAND_JOBS[kind](session_id, upload_root_str)
            STATS["ondemand_done"] += 1
            build_status.clear_failure(session_id, kind)
        except Exception as exc:
            # 실패하면 pending 이 풀려 다음 폴링이 다시 큐에 넣는다 — 워커 타임아웃을
            # 넘기는 세션은 그 재등록이 15분간 반복됐다. 연속 실패를 세어 일정 횟수
            # 넘으면 재등록을 막고(request_build) 프런트에도 실패를 알린다.
            STATS["ondemand_error"] += 1
            build_status.mark_failure(session_id, kind, f"{type(exc).__name__}: {exc}")
            _log.warning("[ondemand] %s build failed session=%s", kind, session_id,
                         exc_info=True)
        finally:
            with _ondemand_lock:
                _ondemand_pending.discard((session_id, kind))


def request_build(session_id: str, upload_root_str: str, kind: str = "report") -> bool:
    """콜드 빌드를 백그라운드에 요청한다 (이미 대기/실행 중이면 무시).

    라우트가 202 를 반환하기 직전에 부른다. 같은 세션을 여러 명이 동시에 열어도 등록은
    1건이고, 실제 빌드 안에서 keyed_lock single-flight 가 한 번 더 중복을 막는다.
    연속 실패로 차단된 (세션, kind) 는 등록하지 않는다 — 쿨다운이 지나면 다시 열린다.

    반환값은 "이번 호출로 새로 등록했는가" 다. 기존 호출부는 반환을 쓰지 않는다.
    """
    if kind not in _ONDEMAND_JOBS:
        raise ValueError(f"unknown build kind: {kind}")
    if build_status.failure_blocked(session_id, kind):
        return False
    with _ondemand_lock:
        key = (session_id, kind)
        if key in _ondemand_pending:
            return False
        _ondemand_pending.add(key)
        _ondemand_queue.append((session_id, upload_root_str, kind, time.time()))
        STATS["ondemand_queued"] += 1
        while len(_ondemand_threads) < _ONDEMAND_WORKERS:
            th = threading.Thread(target=_ondemand_loop,
                                  name=f"webreport-ondemand-{len(_ondemand_threads)}",
                                  daemon=True)
            _ondemand_threads.append(th)
            th.start()
    _ondemand_wake.set()
    return True


# ── Distribution pack 생성 큐 (2026-07-23) ───────────────────────────────────
# 전처리(preprocess)를 켠 세션은 업로드 시점 pack 이 안 맞아 조회마다 서버가 다시
# 정렬했다. 그 세션용 pack 을 한 번만 만들어 영구 저장하는 잡을 여기서 돌린다.
#
# 프리웜/온디맨드 큐와 분리한 이유: 프리웜은 포화 시 오래된 요청을 버리고 중복도 막지
# 않아, 폴백 조회가 반복 요청하면 남의 업로드 프리웜을 밀어낸다. 온디맨드는 사용자가
# 화면에서 202 를 기다리는 경로라 최대 300s 걸리는 pack 빌드가 슬롯(2개)을 점유하면
# 그 사용자들이 멈춘 화면을 본다. 여기는 아무도 기다리지 않고(그 사이 조회는 폴백으로
# 정상 응답) 중복도 무의미하므로, 단일 소비자 + pending 집합으로 동시 1건만 돈다.
_distpack_queue = collections.deque()
_distpack_pending: set = set()      # (session_id, base) — 큐 대기 + 실행 중
_distpack_lock = threading.Lock()
_distpack_wake = threading.Event()
_distpack_thread = None


def _distpack_loop() -> None:
    while True:
        with _distpack_lock:
            item = _distpack_queue.popleft() if _distpack_queue else None
            if item is None:
                _distpack_wake.clear()
        if item is None:
            _distpack_wake.wait()
            continue
        session_id, upload_root_str, base, t_enq = item
        try:
            with build_log.context(trigger="distpack",
                                   queue_wait=round(max(0.0, time.time() - t_enq), 3)):
                run(dist_pack_job, session_id, upload_root_str, base)
            STATS["distpack_done"] += 1
        except Exception:
            # 실패해도 조회는 기존 계산 폴백으로 정상 동작한다. pending 해제 후 다음
            # 폴백 조회가 다시 요청하므로 재시도는 자연히 일어난다.
            STATS["distpack_error"] += 1
            _log.warning("[distpack] build failed session=%s base=%s", session_id, base,
                         exc_info=True)
        finally:
            with _distpack_lock:
                _distpack_pending.discard((session_id, bool(base)))


def request_dist_pack(session_id: str, upload_root_str: str, base: bool = False) -> None:
    """Distribution pack 생성을 백그라운드에 요청한다 (이미 대기/실행 중이면 무시).

    base=True 면 전처리 미적용 원본 pack (raw 셀 편집 후 재생성용),
    False 면 세션의 현 전처리 spec 을 적용한 variant.

    워커 프로세스 안에서는 무시한다 — dist_job 경유로 워커에서도 pack 조회 경로가
    돌기 때문에, 워커가 또 잡을 예약해 되먹임하는 것을 여기서 한 번에 막는다.
    """
    global _distpack_thread
    if _IN_WORKER:
        return
    key = (session_id, bool(base))
    with _distpack_lock:
        if key in _distpack_pending:
            return
        _distpack_pending.add(key)
        _distpack_queue.append((session_id, upload_root_str, bool(base), time.time()))
        STATS["distpack_queued"] += 1
        if _distpack_thread is None:
            _distpack_thread = threading.Thread(target=_distpack_loop,
                                                name="webreport-distpack", daemon=True)
            _distpack_thread.start()
    _distpack_wake.set()


# ── 기동 후 재웜 스윕 (2026-08-06) ───────────────────────────────────────────
# 서버 재기동이나 캐시 스키마 버전 상승(cache_policy.REPORT_SCHEMA_VERSION) 배포 직후에는
# 모든 세션이 콜드라, 그날 처음 세션을 여는 사용자마다 콜드 빌드를 정면으로 맞는다.
# 여기서는 기동 후 잠깐 뒤부터 최근 세션을 하나씩 훑어 **콜드인 것만** 프리웜 큐에 넣어,
# 그 폭풍을 유휴 워커가 대신 흡수하게 한다.
#
# 사용자 요청이 항상 우선이다 — 온디맨드(202 대기) 잡이 하나라도 있거나 프리웜 큐가
# 비어 있지 않으면 스윕은 멈춰 기다린다. 실패·중단은 전부 무해하다(조회가 재계산).
_REWARM_ON_START = (os.getenv("WEB_REPORT_REWARM_ON_START", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")
_REWARM_LIMIT = max(0, int(os.getenv("WEB_REPORT_REWARM_LIMIT", "30") or 30))
_REWARM_DELAY_SEC = max(0.0, float(os.getenv("WEB_REPORT_REWARM_DELAY_SEC", "60") or 60))
_REWARM_POLL_SEC = 3.0
# 사용자 트래픽에 계속 양보하다 스윕이 영원히 남아 있지 않도록 전체 예산을 둔다.
_REWARM_BUDGET_SEC = 3600.0
_rewarm_thread = None


def _rewarm_idle() -> bool:
    """사용자가 기다리는 작업이 없고 프리웜 큐도 비었는가 (스윕 투입 조건)."""
    with _ondemand_lock:
        if _ondemand_pending:
            return False
    with _prewarm_lock:
        return not _prewarm_queue


def _rewarm_sweep(upload_root_str: str) -> None:
    time.sleep(_REWARM_DELAY_SEC)
    from database import report_db
    from . import service

    upload_root = Path(upload_root_str)
    deadline = time.time() + _REWARM_BUDGET_SEC
    try:
        rows = report_db.get_history(source="web_report", limit=_REWARM_LIMIT)
    except Exception:
        _log.warning("[rewarm] 세션 목록 조회 실패 — 스윕 생략", exc_info=True)
        return
    queued = 0
    for row in rows:
        session_id = row.get("session_id")
        if not session_id:
            continue
        while not _rewarm_idle():
            if time.time() > deadline:
                _log.info("[rewarm] 예산(%.0fs) 소진 — %d건 예약 후 중단",
                          _REWARM_BUDGET_SEC, queued)
                return
            time.sleep(_REWARM_POLL_SEC)
        try:
            session = report_db.get_session(session_id)
            if not session or not session.get("analysis_key"):
                continue
            if not service.report_is_cold(session_id, report_db=report_db,
                                          upload_root=upload_root, session=session):
                continue
        except Exception:
            continue        # 개별 세션 실패는 건너뛴다 (조회가 재계산)
        prewarm(session_id, upload_root_str)
        STATS["rewarm_queued"] += 1
        queued += 1
    _log.info("[rewarm] 기동 후 재웜 스윕 완료 — 최근 %d건 중 콜드 %d건 예약",
              len(rows), queued)


def sweep_interrupted_builds() -> None:
    """기동 시 남아 있는 워커 sidecar 를 정리하고 사건으로 남긴다.

    풀이 아직 없는 기동 시점이므로 남아 있는 파일은 전부 **지난 프로세스의 잔해**다 —
    즉 그 빌드는 서버가 죽거나 재기동되면서 끊긴 것이다. 지금까지는 그 사실이 어디에도
    남지 않아, watchdog 재기동과 "리포트가 안 열린다" 신고가 연결되지 않았다."""
    if _IN_WORKER:
        return
    try:
        stale = build_log.clear_states()
    except Exception:
        return
    for st in stale:
        try:
            _log.warning("[build] 중단된 콜드 빌드 발견 — session=%s stage=%s elapsed=%.1fs",
                         st.get("session"), st.get("stage"), float(st.get("elapsed") or 0))
            build_log.record({"kind": st.get("kind") or "report",
                              "session": st.get("session") or "",
                              "offloaded": True, "result": "interrupted",
                              "total": st.get("elapsed"),
                              "build_id": st.get("build_id") or "",
                              "last_stage": st.get("stage") or "",
                              "last_source": st.get("source") or "",
                              "error": "서버 재기동으로 중단"})
            import diagnostics
            diagnostics.emit("warning", "build", "build_interrupted",
                             session_id=st.get("session") or None,
                             build_id=st.get("build_id") or None,
                             source=st.get("source") or None,
                             last_stage=st.get("stage") or None,
                             message=f"서버 재기동으로 중단됨 (stage={st.get('stage')})")
        except Exception:
            pass


def start_rewarm_sweep(upload_root_str: str) -> None:
    """기동 후 1회 재웜 스윕을 띄운다 (report_extension.init_app 이 호출).

    워커 프로세스(_IN_WORKER)·워커 0(인라인 모드)·env 로 끈 경우에는 아무것도 하지 않는다.
    """
    global _rewarm_thread
    if _IN_WORKER or _WORKERS <= 0 or not _REWARM_ON_START or _REWARM_LIMIT <= 0:
        return
    if _rewarm_thread is not None:
        return
    _rewarm_thread = threading.Thread(target=_rewarm_sweep, args=(upload_root_str,),
                                      name="webreport-rewarm", daemon=True)
    _rewarm_thread.start()

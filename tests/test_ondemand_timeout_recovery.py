"""온디맨드(202) 콜드 빌드의 시간 상한·자동 재시도·유령 회수 검증.

배경: Issue Table 편집 직후 세션이 열리지 않고 무한 로딩이 났다(2026-08-13). 실제
계산이 오래 걸린 게 아니라 ① 온디맨드 빌드가 워커를 안 타 300초 상한이 안 걸렸고
② pending 등록만 남은 "유령"이 재빌드를 영원히 막았기 때문이다. 여기서 그 두 가지와,
새로 넣은 자동 재시도(총 2회)가 기존 실패 카운터를 부풀리지 않는지 확인한다.

실행:
    python tests/test_ondemand_timeout_recovery.py

시나리오:
  (1) 일시 장애는 자동으로 한 번 더 돌린다 — 1차 실패 → 재시도 → 2차 성공,
      그리고 성공했으므로 실패 기록이 남지 않는다
  (2) 2회 모두 실패하면 mark_failure 는 **1회만** (FAIL_LIMIT 의 의미 보존)
  (3) 순수 TimeoutError(워커 hang)는 재시도하지 않는다 — 재시도 1회당 300초를 더 태우고
      풀 전체 terminate 로 무고한 동시 빌드까지 죽이기 때문(_is_retryable 계약)
  (4) 재시도 대기 중인 pending 은 TTL 유령으로 오인되지 않고, 진짜 유령만 만료 후 재등록
  (5) build_status.snapshot 이 STALE 초과 등록을 걷어낸다 ("N초 경과" 무한 증가 차단)
  (6) 온디맨드 소비자 컨텍스트에서는 tables 가 웜이어도 워커로 보낸다
      (force_offload_for_consumer — 이 빌드의 유일한 시간 상한)

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# compute import 전에 확정돼야 하는 값들 (모듈 전역으로 굳는다).
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "2"
os.environ["WEB_REPORT_ONDEMAND_RETRY_DELAY_SEC"] = "0.05"   # 테스트는 즉시 재시도
os.environ.setdefault("REPORT_DIAG_DIR", os.path.join(tempfile.gettempdir(),
                                                      "honey_test_log"))

from concurrent.futures import CancelledError  # noqa: E402
from concurrent.futures.process import BrokenProcessPool  # noqa: E402

from web_report import build_status, compute  # noqa: E402

_ROOT_STR = "uploads"


def _drain(session_id, kind="report", timeout=10.0):
    """소비자가 이 (세션, kind) 를 다 처리할 때까지 기다린다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with compute._ondemand_lock:
            busy = (session_id, kind) in compute._ondemand_pending
        if not busy:
            return True
        time.sleep(0.02)
    raise AssertionError(f"온디맨드 잡이 끝나지 않음: {session_id}/{kind}")


def _reset(session_id, kind="report"):
    build_status.clear_failure(session_id, kind)
    compute.drop_pending(session_id, kind)


def _install_job(kind, fn):
    """_ONDEMAND_JOBS 에 더미 잡을 심는다 — 원래 값을 돌려준다(복원용)."""
    prev = compute._ONDEMAND_JOBS.get(kind)
    compute._ONDEMAND_JOBS[kind] = fn
    return prev


def _restore_job(kind, prev):
    if prev is None:
        compute._ONDEMAND_JOBS.pop(kind, None)
    else:
        compute._ONDEMAND_JOBS[kind] = prev


def test_transient_failure_is_retried_once():
    """(1) 일시 장애는 한 번 더 돌린다 — 2차 성공이면 실패 기록이 남지 않는다."""
    sid = "retry-ok"
    calls = []

    def job(session_id, root):
        calls.append(time.time())
        if len(calls) == 1:
            raise compute.QueueWaitTimeout("queue wait 60s")
        return None

    prev = _install_job("report", job)
    _reset(sid)
    try:
        assert compute.request_build(sid, _ROOT_STR, "report") is True
        _drain(sid)
        assert len(calls) == 2, f"재시도가 일어나지 않았다: {len(calls)}회 실행"
        assert build_status.failure_blocked(sid, "report") is None
        assert build_status._FAILED.get((sid, "report")) is None, \
            "2차에 성공했는데 실패 기록이 남았다"
    finally:
        _restore_job("report", prev)
        _reset(sid)
    print("  (1) 일시 장애 → 재시도 1회 → 성공, 실패 기록 없음 OK")


def test_exhausted_retry_marks_failure_once():
    """(2) 2회 모두 실패해도 mark_failure 는 1회 — FAIL_LIMIT 의미(논리 빌드 실패)를 보존."""
    sid = "retry-exhausted"
    calls = []

    def job(session_id, root):
        calls.append(time.time())
        raise BrokenProcessPool("worker died")

    prev = _install_job("report", job)
    _reset(sid)
    try:
        compute.request_build(sid, _ROOT_STR, "report")
        _drain(sid)
        assert len(calls) == compute._ONDEMAND_MAX_ATTEMPTS, \
            f"실행 횟수가 상한과 다르다: {len(calls)} != {compute._ONDEMAND_MAX_ATTEMPTS}"
        entry = build_status._FAILED.get((sid, "report"))
        assert entry and entry["count"] == 1, \
            f"재시도마다 실패를 세면 FAIL_LIMIT 이 절반으로 줄어든다: {entry}"
    finally:
        _restore_job("report", prev)
        _reset(sid)
    print("  (2) 재시도 소진 → 총 실행 2회, mark_failure 1회 OK")


def test_worker_hang_is_not_retried():
    """(3) 순수 TimeoutError(워커 hang)는 재시도 금지 — 도미노 증폭 방지 계약."""
    assert compute._is_retryable(compute.QueueWaitTimeout("q")) is True
    assert compute._is_retryable(CancelledError()) is True
    assert compute._is_retryable(BrokenProcessPool("x")) is True
    assert compute._is_retryable(TimeoutError("300.0s")) is False, \
        "워커 hang 을 재시도하면 300초를 더 태우고 전 워커 terminate 가 반복된다"
    assert compute._is_retryable(ValueError("bad input")) is False

    sid = "hang-no-retry"
    calls = []

    def job(session_id, root):
        calls.append(1)
        raise TimeoutError("300.0s")

    prev = _install_job("report", job)
    _reset(sid)
    try:
        compute.request_build(sid, _ROOT_STR, "report")
        _drain(sid)
        assert len(calls) == 1, f"hang 을 재시도했다: {len(calls)}회"
        entry = build_status._FAILED.get((sid, "report"))
        assert entry and entry["count"] == 1
    finally:
        _restore_job("report", prev)
        _reset(sid)
    print("  (3) 워커 hang 은 재시도 없이 즉시 실패 기록 OK")


def test_ghost_pending_expires_but_live_retry_does_not():
    """(4) 진짜 유령만 TTL 후 재등록되고, 큐에 남아 있는 항목은 만료되지 않는다."""
    sid = "ghost"
    key = (sid, "report")
    _reset(sid)
    # 소비자가 사라진 것처럼 등록만 남기고 시각을 TTL 이전으로 조작한다.
    with compute._ondemand_lock:
        compute._ondemand_pending[key] = time.time() - compute._ONDEMAND_PENDING_TTL_SEC - 1
        expired = compute._expire_ghost_pending(key)
    assert expired is True, "TTL 을 넘긴 등록이 유령으로 판정되지 않았다"
    assert key not in compute._ondemand_pending

    # 큐에 아직 남아 있으면(= 순번 대기) 만료 대상이 아니다.
    with compute._ondemand_lock:
        compute._ondemand_pending[key] = time.time() - compute._ONDEMAND_PENDING_TTL_SEC - 1
        compute._ondemand_queue.append((sid, _ROOT_STR, "report", time.time(), 0, 0.0))
        expired2 = compute._expire_ghost_pending(key)
        compute._ondemand_queue.clear()
        compute._ondemand_pending.pop(key, None)
    assert expired2 is False, "큐 대기 중인 항목을 유령으로 지우면 중복 실행이 된다"
    print("  (4) 유령만 TTL 만료, 큐 잔류분은 보존 OK")


def test_build_status_stale_entry_is_dropped():
    """(5) begin 만 남은 유령은 snapshot 에서 걷어낸다 — 'N초 경과' 무한 증가 차단."""
    sid = "stale-status"
    build_status.end(sid, "report")
    build_status.end(sid, "map")
    build_status.begin(sid, "report")
    with build_status._LOCK:      # 오래 전에 시작된 것처럼 조작
        build_status._ACTIVE[(sid, "report")]["t0"] -= build_status.STALE_SEC + 10
    build_status.begin(sid, "map")     # 방금 시작한 정상 등록
    try:
        snap = build_status.snapshot(sid)
        assert snap["state"] == "building" and snap["stage"] == "map", \
            f"유령이 정상 stage 를 가렸다: {snap}"
        assert (sid, "report") not in build_status._ACTIVE, "유령이 정리되지 않았다"
        rows = [r for r in build_status.snapshot_all() if r["session_id"] == sid]
        assert rows and all("stale" in r for r in rows), \
            "관리자 화면은 stale 플래그로 유령을 볼 수 있어야 한다"
    finally:
        build_status.end(sid, "report")
        build_status.end(sid, "map")
    print("  (5) build_status 유령 정리 + 관리자 stale 플래그 OK")


def test_consumer_context_forces_offload():
    """(6) 온디맨드 소비자 컨텍스트면 tables 웜이어도 워커로 — 유일한 시간 상한."""
    from web_report import build_log

    assert compute.force_offload_for_consumer() is False, \
        "큐 컨텍스트 밖에서는 강제 오프로드가 아니다"
    with build_log.context(trigger="ondemand", kind="report"):
        assert compute.force_offload_for_consumer() is True
    with build_log.context(trigger="prewarm", kind="report"):
        assert compute.force_offload_for_consumer() is False, \
            "프리웜은 이미 run() 경유라 이 강제가 필요 없다"
    # dist/temp_map/trim 은 컨텍스트와 무관하게 항상 워커로 보낸다(202 규약이 없어
    # 인라인이면 요청 스레드가 상한 없이 계산한다).
    assert compute.should_offload_heavy(("any", "key")) is True
    print("  (6) 온디맨드·중량 산출물 강제 오프로드 OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    test_transient_failure_is_retried_once()
    test_exhausted_retry_marks_failure_once()
    test_worker_hang_is_not_retried()
    test_ghost_pending_expires_but_live_retry_does_not()
    test_build_status_stale_entry_is_dropped()
    test_consumer_context_forces_offload()
    print("전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

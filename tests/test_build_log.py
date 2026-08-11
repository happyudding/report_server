"""콜드 빌드 계측(web_report/build_log.py) 검증.

배경: 콜드 빌드가 한번씩 300초 가까이 걸리는데 원인을 볼 기록이 없었다. build_log 가
단계별 소요와 대기 시간(큐/풀/IPC)을 남기고, compute.run 의 타임아웃·워커 붕괴도
레코드로 남기는지 확인한다.

실행:
    python tests/test_build_log.py

시나리오:
  (1) stage() 는 수집기 없으면 no-op, collecting() 안에서는 누적된다
  (2) record() → history() 왕복 (JSON line 파일)
  (3) context(trigger/queue_wait) 가 레코드에 병합된다
  (4) record_offloaded 가 pool_wait/build/ipc 를 자식 시각에서 계산한다
  (5) compute.run 타임아웃 → result="timeout" 레코드 (총 소요 = 큐 대기 포함)
  (6) compute.run 잡 예외 → result="error" 레코드
  (7) report_job/dist_job/map_job 이 (결과, timing) 튜플을 반환한다 (호출부 계약)
  (8) 워커가 타임아웃으로 죽어도 **마지막 단계·source 파일**이 실패 레코드에 남는다
      (실행 중 체크포인트 sidecar — 300초 타임아웃 진단의 핵심)

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# compute import 전에 확정돼야 하는 값들 (모듈 전역으로 굳는다).
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "2"
os.environ["WEB_REPORT_COMPUTE_TIMEOUT_SEC"] = "2"
# 로그는 임시 폴더로 격리한다 — 테스트 레코드가 운영 server/log 에 섞이면 관리자
# 화면의 빌드 이력을 못 믿게 된다. 워커(spawn)도 환경을 물려받아 같은 폴더를 쓴다.
os.environ.setdefault("REPORT_DIAG_DIR", os.path.join(tempfile.gettempdir(),
                                                      "honey_test_log"))

from web_report import build_log  # noqa: E402


def _slow_job(session_id, seconds):
    time.sleep(seconds)
    return None


def _boom_job(session_id):
    raise ValueError("boom")


def _stage_hang_job(session_id, seconds):
    """단계에 들어간 뒤 멎는 잡 — 운영의 hang 워커를 흉내낸다 (compute._job 과 같은 배선)."""
    build_log.begin_job("report", session_id)
    try:
        with build_log.stage("decode"):
            build_log.checkpoint("decode", "2/3 lot_b.csv")
            time.sleep(seconds)
    finally:
        build_log.end_job()


def main():
    from web_report import compute

    # ── (1) stage 수집 ────────────────────────────────────────────────────────
    with build_log.stage("nope"):
        pass          # 수집기 없음 — 예외 없이 no-op
    with build_log.collecting() as stages:
        with build_log.stage("decode"):
            time.sleep(0.02)
        with build_log.stage("decode"):
            time.sleep(0.02)
    assert set(stages) == {"decode"}, stages
    assert stages["decode"] >= 0.03, stages   # 같은 이름은 누적
    assert getattr(build_log._tls, "stages", None) is None, "collecting 종료 후 해제"
    print("(1) stage/collecting 누적·해제 ok")

    # ── (2) record → history 왕복 ─────────────────────────────────────────────
    mark = f"selftest-{os.getpid()}"
    build_log.record({"kind": "report", "session": mark, "result": "ok",
                      "total": 1.5, "stages": {"decode": 1.0}})
    rows = [r for r in build_log.history(1, 500) if r.get("session") == mark]
    assert len(rows) == 1, rows
    assert rows[0]["total"] == 1.5 and rows[0]["ts"], rows[0]
    print(f"(2) record→history 왕복 ok (server/log/{build_log.LOG_PREFIX}_*.log)")

    # ── (3) context 병합 ──────────────────────────────────────────────────────
    mark3 = mark + "-ctx"
    with build_log.context(trigger="ondemand", queue_wait=12.3):
        build_log.record({"kind": "report", "session": mark3, "result": "ok"})
    row = [r for r in build_log.history(1, 500) if r.get("session") == mark3][0]
    assert row["trigger"] == "ondemand" and row["queue_wait"] == 12.3, row
    assert getattr(build_log._tls, "ctx", None) is None, "context 종료 후 해제"
    print("(3) context(trigger/queue_wait) 병합·해제 ok")

    # ── (4) 오프로드 시간 분해 ────────────────────────────────────────────────
    mark4 = mark + "-off"
    t_sub = 1000.0
    build_log.record_offloaded("report", mark4, "akey1234567890", t_sub, 1010.0,
                               {"t_start": 1003.0, "t_end": 1009.0,
                                "stages": {"decode": 5.0}, "sources": 3})
    row = [r for r in build_log.history(1, 500) if r.get("session") == mark4][0]
    assert row["total"] == 10.0 and row["pool_wait"] == 3.0, row
    assert row["build"] == 6.0 and row["ipc"] == 1.0, row
    assert row["akey"] == "akey12345678" and row["sources"] == 3, row
    print("(4) record_offloaded 분해(총10 = 풀대기3 + 빌드6 + IPC1) ok")

    # ── (5) 타임아웃 실패 기록 ────────────────────────────────────────────────
    assert compute._TIMEOUT_SEC == 2.0, compute._TIMEOUT_SEC
    mark5 = mark + "-timeout"
    t0 = time.time()
    try:
        compute.run(_slow_job, mark5, 30)
        raise AssertionError("TimeoutError 가 나야 한다")
    except TimeoutError:
        pass
    row = [r for r in build_log.history(1, 500) if r.get("session") == mark5][0]
    assert row["result"] == "timeout" and row["kind"] == "_slow_job", row
    assert 2.0 <= row["total"] < time.time() - t0 + 1, row
    assert compute.STATS["timeout"] >= 1
    print(f"(5) 타임아웃 레코드 ok (total={row['total']}s, 상한 {compute._TIMEOUT_SEC}s)")

    # ── (6) 잡 예외 기록 ──────────────────────────────────────────────────────
    mark6 = mark + "-error"
    try:
        compute.run(_boom_job, mark6)
        raise AssertionError("ValueError 가 나야 한다")
    except ValueError:
        pass
    row = [r for r in build_log.history(1, 500) if r.get("session") == mark6][0]
    assert row["result"] == "error" and "boom" in row["error"], row
    print("(6) 잡 예외 레코드 ok")

    # ── (8) 타임아웃 시 마지막 단계·파일 보존 ─────────────────────────────────
    # 워커가 terminate 되면 자식이 잰 단계 기록은 IPC 로 돌아오지 못한다. 실행 중
    # sidecar 가 없으면 "300초 걸렸다"만 남고 어디서 멎었는지는 영영 알 수 없다.
    mark8 = mark + "-hang"
    try:
        compute.run(_stage_hang_job, mark8, 30)
        raise AssertionError("TimeoutError 가 나야 한다")
    except TimeoutError:
        pass
    row = [r for r in build_log.history(1, 500) if r.get("session") == mark8][0]
    assert row["result"] == "timeout", row
    assert row["last_stage"] == "decode", row
    assert row["last_source"] == "2/3 lot_b.csv", row
    assert row.get("build_id"), row
    assert row.get("last_stage_elapsed") is not None, row
    print(f"(8) 타임아웃 실패 레코드에 마지막 단계 보존 ok "
          f"(stage={row['last_stage']} source={row['last_source']})")

    # 큐 대기만 하다 죽은 잡은 sidecar 가 없어야 한다 — 그 부재가 '대기 타임아웃'의 증거다.
    assert [r for r in build_log.history(1, 500)
            if r.get("session") == mark5][0]["last_stage"] == "", "큐/미계측 잡은 빈 값"
    print("(9) 계측 없는 잡은 last_stage 가 빈 값 (대기 타임아웃과 구분 가능) ok")

    compute._reset_pool(shutdown=True)     # 테스트 워커 정리
    assert not build_log.read_states(), "종료 후 sidecar 잔해가 없어야 한다"

    # ── (7) 잡 반환 계약 ──────────────────────────────────────────────────────
    # 실제 세션 없이 돌릴 수 없으므로 소스에서 계약만 확인한다 — service.py 3곳이
    # 튜플 언팩으로 받으므로 잡이 단일 값을 돌려주면 조회가 통째로 깨진다.
    for name in ("report_job", "dist_job", "map_job"):
        src = inspect.getsource(getattr(compute, name))
        assert "_stamp(t_start)" in src and "return " in src, name
    svc = inspect.getsource(sys.modules["web_report.service"]) \
        if "web_report.service" in sys.modules else None
    if svc is None:
        from web_report import service as _svc
        svc = inspect.getsource(_svc)
    for pat in ("report, child_t = compute.run(compute.report_job",
                "blob, child_t = compute.run(compute.dist_job",
                "blob, child_t = compute.run(compute.map_job"):
        assert pat in svc, pat
    print("(7) 잡 (결과, timing) 튜플 계약 + service 언팩 ok")

    print("\n전체 통과")


# Windows spawn: 워커가 이 모듈을 __mp_main__ 으로 재실행하므로 워커를 띄우는 코드는
# 반드시 __main__ 가드 안에서만 실행해야 한다(재귀 spawn 방지).
if __name__ == "__main__":
    main()

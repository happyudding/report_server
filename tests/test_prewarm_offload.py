"""업로드 직후 prewarm 이 waitress 프로세스가 아니라 컴퓨트 워커에서 계산되는지 검증.

배경: _prewarm_one 이 report_job/dist_job 을 직접 호출하면 수 초 CPU 가 waitress 와 같은
프로세스에서 돌며 GIL 을 잡아 세션 밖 값싼 요청(홈·VOC)까지 지연시켰다. 지금은
run(prewarm_job) 을 경유해 WEB_REPORT_COMPUTE_WORKERS>0 이면 별도 프로세스가 계산한다
(=0 이면 인라인 폴백).

실행:
    python tests/test_prewarm_offload.py

시나리오:
  (1) _prewarm_one 이 report_job 을 직접 부르지 않고 run(prewarm_job, ...) 을 경유한다
  (2) WEB_REPORT_COMPUTE_WORKERS=2 에서 run() 이 반환하는 PID 가 부모와 다르다 (프로세스 분리)

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# compute import 전에 워커 수를 확정해야 _WORKERS 가 2 로 잡힌다.
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "2"

from web_report import compute  # noqa: E402


def main():
    # ── (1) _prewarm_one → run(prewarm_job, ...) 경유 ─────────────────────────
    # run 을 가로채 실제 워커 실행 없이 호출 인자만 기록한다.
    calls = []
    _orig_run = compute.run
    compute.run = lambda job, *a: calls.append((job, a)) or None
    try:
        compute._prewarm_one("sid-x", "/tmp/root", True)
    finally:
        compute.run = _orig_run

    assert len(calls) == 1, calls
    job, args = calls[0]
    assert job is compute.prewarm_job, job
    assert args == ("sid-x", "/tmp/root", True), args   # dist_seeded=True 전달
    assert compute.STATS["prewarm_done"] >= 1
    print("(1) _prewarm_one 이 run(prewarm_job, sid, root, dist_seeded) 경유 ok")

    # ── (2) 운영 설정(WORKERS=2)에서 계산 프로세스가 부모와 분리 ────────────────
    assert compute._WORKERS == 2, compute._WORKERS
    parent_pid = os.getpid()
    try:
        worker_pid = compute.run(os.getpid)   # 워커 프로세스에서 os.getpid() 실행
        assert isinstance(worker_pid, int) and worker_pid != parent_pid, \
            (worker_pid, parent_pid)
    finally:
        compute._reset_pool(shutdown=True)    # 테스트 워커 정리
    print(f"(2) 워커 PID {worker_pid} != 부모 PID {parent_pid} — 프로세스 분리 ok")

    print("\n전체 통과")


# Windows spawn: 워커가 이 모듈을 __mp_main__ 으로 재실행하므로, 워커를 띄우는
# 코드는 반드시 __main__ 가드 안에서만 실행해야 한다(재귀 spawn 방지).
if __name__ == "__main__":
    main()

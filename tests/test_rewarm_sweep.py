"""기동 후 재웜 스윕(web_report/compute.py) 검증.

배경: 재기동·배포(REPORT_SCHEMA_VERSION 상승) 직후에는 전 세션이 콜드라 그날 처음
세션을 여는 사용자마다 콜드 빌드를 정면으로 맞는다. 스윕이 그 폭풍을 유휴 워커로
대신 흡수하는지, 그리고 **사용자 요청을 밀어내지 않는지**를 본다.

실행:
    python tests/test_rewarm_sweep.py

시나리오:
  (1) 콜드 세션만 프리웜 큐에 예약한다 (웜 세션·analysis_key 없는 세션은 건너뜀)
  (2) 온디맨드(사용자 202 대기) 잡이 있으면 투입하지 않고 기다린다
  (3) 기다리다 예산을 넘기면 스윕을 중단한다 (영구 잔존 방지)
  (4) 워커 프로세스·env off 에서는 스레드를 띄우지 않는다

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import contextlib
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "2"

from web_report import compute  # noqa: E402


class _FakeDB:
    """report_db 대역 — 세션 목록/단건만."""

    def __init__(self, sessions):
        self._sessions = sessions

    def get_history(self, **kwargs):
        return [{"session_id": sid} for sid in self._sessions]

    def get_session(self, session_id):
        return self._sessions.get(session_id)


@contextlib.contextmanager
def _patched(sessions, cold_ids, queued):
    """compute._rewarm_sweep 이 지연 import 하는 두 모듈을 대역으로 바꾼다.

    `from . import service` 는 sys.modules 보다 **패키지 속성**을 먼저 본다 — 다른
    테스트가 이미 web_report.service 를 import 한 뒤라면 속성을 바꿔야 stub 이 먹는다.
    """
    import web_report

    stub_service = types.SimpleNamespace(
        report_is_cold=lambda sid, **kw: sid in cold_ids)
    stub_db = types.SimpleNamespace(report_db=_FakeDB(sessions))
    saved = (sys.modules.get("web_report.service"), getattr(web_report, "service", None),
             sys.modules.get("database"), compute.prewarm)
    sys.modules["web_report.service"] = stub_service
    web_report.service = stub_service
    sys.modules["database"] = stub_db
    compute.prewarm = lambda sid, root, dist_seeded=False: queued.append(sid)
    try:
        yield
    finally:
        real_service, real_attr, real_db, real_prewarm = saved
        sys.modules["web_report.service"] = real_service
        web_report.service = real_attr
        sys.modules["database"] = real_db
        compute.prewarm = real_prewarm
        for key, value in (("web_report.service", real_service), ("database", real_db)):
            if value is None:
                sys.modules.pop(key, None)


def test_only_cold_sessions_queued():
    sessions = {
        "cold-1": {"analysis_key": "a1"},
        "warm-1": {"analysis_key": "a2"},
        "cold-2": {"analysis_key": "a3"},
        "no-key": {"analysis_key": ""},      # 산출물 없는 세션 — 건너뛴다
    }
    queued: list = []
    compute._REWARM_DELAY_SEC = 0.0
    with _patched(sessions, {"cold-1", "cold-2", "no-key"}, queued):
        compute._rewarm_sweep("uploads")
    assert queued == ["cold-1", "cold-2"], queued
    assert compute.STATS["rewarm_queued"] >= 2


def test_yields_to_ondemand_and_gives_up():
    """사용자 요청이 안 끝나면 투입하지 않고, 예산이 지나면 스윕을 접는다."""
    queued: list = []
    compute._REWARM_DELAY_SEC = 0.0
    compute._REWARM_POLL_SEC = 0.01
    compute._REWARM_BUDGET_SEC = 0.05
    with compute._ondemand_lock:
        compute._ondemand_pending.add(("someone-else", "report"))
    try:
        assert compute._rewarm_idle() is False
        with _patched({"cold-1": {"analysis_key": "a1"}}, {"cold-1"}, queued):
            compute._rewarm_sweep("uploads")
        assert queued == [], queued          # 사용자 잡이 끝날 때까지 투입 없음
    finally:
        with compute._ondemand_lock:
            compute._ondemand_pending.discard(("someone-else", "report"))


def test_start_guards():
    compute._rewarm_thread = None
    compute._IN_WORKER = True
    compute.start_rewarm_sweep("uploads")
    assert compute._rewarm_thread is None, "워커 프로세스에서는 스윕을 띄우지 않는다"
    compute._IN_WORKER = False
    on_start = compute._REWARM_ON_START
    compute._REWARM_ON_START = False
    try:
        compute.start_rewarm_sweep("uploads")
        assert compute._rewarm_thread is None, "env 로 끄면 스윕을 띄우지 않는다"
    finally:
        compute._REWARM_ON_START = on_start


def main():
    test_only_cold_sessions_queued()
    test_yields_to_ondemand_and_gives_up()
    test_start_guards()
    print("OK - rewarm sweep")


if __name__ == "__main__":
    main()

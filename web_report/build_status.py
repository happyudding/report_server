"""콜드 빌드 진행 상태 레지스트리 (프런트 로드 오버레이용).

세션 상세 첫 조회는 콜드일 때 수 초~1분 이상 걸린다(콜드 ~10s, 대용량은 그 이상).
그동안 /full 응답이 없어 프런트가 실제 진척을 알 수 없었고, boot.js 는 시간 기반
creep 으로 "계산 중" 을 추정 표시했다 — 서버가 실제로 빌드 중인지, 몇 초째인지는
알 수 없었다. 여기서 **부모 프로세스가 실제 콜드 빌드에 들어간 구간**만 기록해
프런트가 GET .../web_report/build_status 로 사실을 확인하게 한다.

범위 한정(의도):
- 부모(웹) 프로세스의 in-memory dict — 단일 프로세스 waitress 전제(docs/12)에서
  /full 을 기다리는 그 요청 스레드가 곧 여기 등록자다. 워커 오프로드(compute.run)
  중에도 부모 스레드는 결과를 기다리며 등록 상태를 유지하므로 정확하다.
  (워커 프로세스 안에서 begin 이 다시 불려도 그쪽은 별개 dict 라 무해.)
- 진척률(%)이 아니라 **상태(building/idle) + 경과초**만 준다 — 남은 시간을 알 수 없는
  작업에 가짜 %를 만들지 않는다.
- 관측 전용. 실패해도 빌드에 영향 없어야 하므로 호출부는 try/finally 로만 쓴다.
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
# session_id -> {"stage": str, "t0": float}
_ACTIVE: dict[str, dict] = {}


def begin(session_id: str, stage: str = "report") -> None:
    """콜드 빌드 시작 기록. 같은 세션 중복 진입은 최초 t0 를 유지한다."""
    with _LOCK:
        if session_id not in _ACTIVE:
            _ACTIVE[session_id] = {"stage": stage, "t0": time.monotonic()}


def end(session_id: str) -> None:
    """콜드 빌드 종료 기록 (성공/실패 무관 — 호출부 finally)."""
    with _LOCK:
        _ACTIVE.pop(session_id, None)


def snapshot(session_id: str) -> dict:
    """현재 상태 — {"state":"building","stage","elapsed"} 또는 {"state":"idle"}."""
    with _LOCK:
        entry = _ACTIVE.get(session_id)
        if entry is None:
            return {"state": "idle"}
        return {"state": "building", "stage": entry["stage"],
                "elapsed": round(time.monotonic() - entry["t0"], 1)}

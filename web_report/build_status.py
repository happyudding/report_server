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
# (session_id, stage) -> t0.  stage 별로 나눠 담는 이유: report 와 map 콜드 빌드가
# 겹칠 수 있는데(세션 열자마자 Map 탭), 세션당 1칸이면 나중에 끝난 쪽의 end() 가
# 아직 진행 중인 다른 stage 의 기록까지 지워 프런트가 "끝났다"고 오판한다.
_ACTIVE: dict[tuple, float] = {}


def begin(session_id: str, stage: str = "report") -> None:
    """콜드 빌드 시작 기록. 같은 (세션, stage) 중복 진입은 최초 t0 를 유지한다."""
    with _LOCK:
        _ACTIVE.setdefault((session_id, stage), time.monotonic())


def end(session_id: str, stage: str = "report") -> None:
    """콜드 빌드 종료 기록 (성공/실패 무관 — 호출부 finally)."""
    with _LOCK:
        _ACTIVE.pop((session_id, stage), None)


def snapshot(session_id: str) -> dict:
    """현재 상태 — {"state":"building","stage","elapsed"} 또는 {"state":"idle"}.

    여러 stage 가 동시에 도는 경우 **가장 오래 돌고 있는 것**을 보고한다(사용자가 실제로
    기다리는 시간에 가깝다).
    """
    with _LOCK:
        entries = [(t0, stage) for (sid, stage), t0 in _ACTIVE.items() if sid == session_id]
        if not entries:
            return {"state": "idle"}
        t0, stage = min(entries)
    return {"state": "building", "stage": stage,
            "elapsed": round(time.monotonic() - t0, 1)}


def snapshot_all() -> list[dict]:
    """진행 중인 콜드 빌드 전부 — 오래 걸리는 순. 관리자 부하 모니터링 전용.

    snapshot() 은 프런트가 자기 세션만 보는 용도라 세션 단위인데, 관리자 화면은
    "지금 서버가 몇 개 세션을 동시에 빌드 중인가"(= 컴퓨트 워커 포화 원인)를 봐야 한다.
    """
    with _LOCK:
        entries = list(_ACTIVE.items())
    now = time.monotonic()
    out = [{"session_id": sid, "stage": stage, "elapsed": round(now - t0, 1)}
           for (sid, stage), t0 in entries]
    out.sort(key=lambda d: d["elapsed"], reverse=True)
    return out

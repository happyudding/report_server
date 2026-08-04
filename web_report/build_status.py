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

import os
import threading
import time

_LOCK = threading.Lock()
# (session_id, stage) -> t0.  stage 별로 나눠 담는 이유: report 와 map 콜드 빌드가
# 겹칠 수 있는데(세션 열자마자 Map 탭), 세션당 1칸이면 나중에 끝난 쪽의 end() 가
# 아직 진행 중인 다른 stage 의 기록까지 지워 프런트가 "끝났다"고 오판한다.
_ACTIVE: dict[tuple, float] = {}

# (session_id, stage) -> {"count", "t_last", "error"}.  연속 실패 기록.
# 워커 타임아웃(_TIMEOUT_SEC=300)을 넘기는 세션은 온디맨드 소비자가 예외를 삼키고
# pending 을 풀기 때문에, 다음 폴링이 곧바로 재등록해 **같은 빌드를 15분간 반복**했다
# (프런트 타임아웃 15분 > 워커 300s). 몇 번 연속 실패하면 일정 시간 재등록을 막고
# 프런트에 사실대로 실패를 알려, 워커 잠식과 헛폴링을 함께 끊는다.
_FAILED: dict[tuple, dict] = {}
FAIL_LIMIT = max(1, int(os.getenv("WEB_REPORT_BUILD_FAIL_LIMIT", "2") or 2))
FAIL_COOLDOWN_SEC = float(os.getenv("WEB_REPORT_BUILD_FAIL_COOLDOWN_SEC", "600") or 600)


def begin(session_id: str, stage: str = "report") -> None:
    """콜드 빌드 시작 기록. 같은 (세션, stage) 중복 진입은 최초 t0 를 유지한다."""
    with _LOCK:
        _ACTIVE.setdefault((session_id, stage), time.monotonic())


def end(session_id: str, stage: str = "report") -> None:
    """콜드 빌드 종료 기록 (성공/실패 무관 — 호출부 finally)."""
    with _LOCK:
        _ACTIVE.pop((session_id, stage), None)


def mark_failure(session_id: str, stage: str, error: str = "") -> None:
    """콜드 빌드 실패 1건 기록 (연속 카운트 증가)."""
    with _LOCK:
        entry = _FAILED.get((session_id, stage))
        if entry is None:
            entry = {"count": 0, "t_last": 0.0, "error": ""}
            _FAILED[(session_id, stage)] = entry
        entry["count"] += 1
        entry["t_last"] = time.monotonic()
        entry["error"] = str(error)[:300]


def clear_failure(session_id: str, stage: str) -> None:
    """빌드가 성공했으면 연속 실패 기록을 지운다."""
    with _LOCK:
        _FAILED.pop((session_id, stage), None)


def failure_blocked(session_id: str, stage: str = "report") -> dict | None:
    """연속 실패로 재빌드를 막아야 하면 실패 정보, 아니면 None.

    쿨다운이 지나면 다시 None 을 돌려줘 자동으로 재시도가 열린다 — 디스크 풀 같은
    일시 장애가 복구되면 사람이 손대지 않아도 회복된다.
    """
    with _LOCK:
        entry = _FAILED.get((session_id, stage))
        if entry is None or entry["count"] < FAIL_LIMIT:
            return None
        if time.monotonic() - entry["t_last"] >= FAIL_COOLDOWN_SEC:
            _FAILED.pop((session_id, stage), None)
            return None
        return dict(entry)


def snapshot(session_id: str) -> dict:
    """현재 상태 — building / failed / idle.

    - 빌드 중: ``{"state":"building","stage","elapsed"}``. 여러 stage 가 동시에 도는
      경우 **가장 오래 돌고 있는 것**을 보고한다(사용자가 실제로 기다리는 시간에 가깝다).
    - 연속 실패로 차단된 상태: ``{"state":"failed","stage","fail_count"}``.
    - 그 외: ``{"state":"idle"}``.

    구 프런트는 ``state !== "building"`` 을 전부 무시하므로 failed 추가는 하위호환이다.
    """
    with _LOCK:
        entries = [(t0, stage) for (sid, stage), t0 in _ACTIVE.items() if sid == session_id]
        if entries:
            t0, stage = min(entries)
            return {"state": "building", "stage": stage,
                    "elapsed": round(time.monotonic() - t0, 1)}
        failed = [(stage, dict(e)) for (sid, stage), e in _FAILED.items()
                  if sid == session_id and e["count"] >= FAIL_LIMIT
                  and time.monotonic() - e["t_last"] < FAIL_COOLDOWN_SEC]
    if failed:
        stage, entry = failed[0]
        return {"state": "failed", "stage": stage, "fail_count": entry["count"]}
    return {"state": "idle"}


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

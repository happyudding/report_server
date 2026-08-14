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
# (session_id, stage) -> {"t0", "started", "trigger"}.  stage 별로 나눠 담는 이유:
# report 와 map 콜드 빌드가 겹칠 수 있는데(세션 열자마자 Map 탭), 세션당 1칸이면 나중에
# 끝난 쪽의 end() 가 아직 진행 중인 다른 stage 의 기록까지 지워 프런트가 "끝났다"고 오판한다.
#   t0       monotonic — 경과 계산(시계 튐 무관)
#   started  time.time() — 관리자 화면에 "언제 시작됐나"를 보이기 위한 벽시계
#   trigger  이 빌드를 시작시킨 경로(ondemand:report / ondemand:ai / prewarm …) —
#            "무엇이 콜드 빌드를 유발했나"에 답하는 유일한 단서다
_ACTIVE: dict[tuple, dict] = {}

# (session_id, stage) -> {"count", "t_last", "error"}.  연속 실패 기록.
# 워커 타임아웃(_TIMEOUT_SEC=300)을 넘기는 세션은 온디맨드 소비자가 예외를 삼키고
# pending 을 풀기 때문에, 다음 폴링이 곧바로 재등록해 **같은 빌드를 15분간 반복**했다
# (프런트 타임아웃 15분 > 워커 300s). 몇 번 연속 실패하면 일정 시간 재등록을 막고
# 프런트에 사실대로 실패를 알려, 워커 잠식과 헛폴링을 함께 끊는다.
_FAILED: dict[tuple, dict] = {}
FAIL_LIMIT = max(1, int(os.getenv("WEB_REPORT_BUILD_FAIL_LIMIT", "2") or 2))
FAIL_COOLDOWN_SEC = float(os.getenv("WEB_REPORT_BUILD_FAIL_COOLDOWN_SEC", "600") or 600)

# begin() 만 남고 end() 가 오지 않은 등록을 "유령"으로 보는 상한. 등록자는 try/finally
# 라 예외로는 새지 않지만, 스레드가 통째로 사라지면 등록이 남아 경과초가 무한히 커진다
# (사용자가 본 "10,000초 경과"의 정체). 워커 타임아웃 300s + 큐 대기 + 여유보다 크게 잡아
# 정상 빌드를 조기에 지우지 않는다.
STALE_SEC = float(os.getenv("WEB_REPORT_BUILD_STATUS_STALE_SEC", "900") or 900)


def _ambient_trigger() -> str:
    """이 빌드를 시작시킨 큐 컨텍스트 — 호출부를 고치지 않고 알아내는 방법.

    begin() 을 부르는 service.load_webreport 는 자기가 어느 경로로 불렸는지 모른다.
    반면 큐 소비자 스레드(compute._ondemand_loop 등)는 같은 스레드에 이미
    build_log.context(trigger=..., kind=...) 를 심어두므로 그것을 그대로 읽는다.
    부모가 사용자 요청 스레드에서 직접 빌드하면 컨텍스트가 없어 빈 문자열이다.
    """
    try:
        from . import build_log
        ctx = build_log.current_context()
        trig = str(ctx.get("trigger") or "")
        kind = str(ctx.get("kind") or "")
        return f"{trig}:{kind}" if (trig and kind) else trig
    except Exception:
        return ""


def begin(session_id: str, stage: str = "report", trigger: str = "") -> None:
    """콜드 빌드 시작 기록. 같은 (세션, stage) 중복 진입은 최초 t0 를 유지한다."""
    if not trigger:
        trigger = _ambient_trigger()
    with _LOCK:
        if (session_id, stage) in _ACTIVE:
            return
        _ACTIVE[(session_id, stage)] = {"t0": time.monotonic(), "started": time.time(),
                                        "trigger": trigger}


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
    - 연속 실패로 차단된 상태: ``{"state":"failed","stage","fail_count","error"}``.
      error 는 mark_failure 가 받아 둔 예외 요약이다 — 여태 보관만 하고 아무데도
      내보내지 않아, 사용자도 관리자도 실패 사유를 볼 수 없었다.
    - 그 외: ``{"state":"idle"}``.

    구 프런트는 ``state !== "building"`` 을 전부 무시하므로 failed 추가는 하위호환이다.
    """
    now = time.monotonic()
    with _LOCK:
        # 유령(STALE_SEC 초과)은 여기서 걷어낸다 — 주기 스레드 없이, 문제가 되는 바로 그
        # 순간(프런트 폴링)에 정리한다. 안 지우면 min() 이 유령을 고르므로 실제로 도는
        # 다른 stage 대신 "N초 경과"가 무한히 자란다.
        for key in [k for k, m in _ACTIVE.items()
                    if k[0] == session_id and now - m["t0"] > STALE_SEC]:
            _ACTIVE.pop(key, None)
        entries = [(m["t0"], stage) for (sid, stage), m in _ACTIVE.items() if sid == session_id]
        if entries:
            t0, stage = min(entries)
            return {"state": "building", "stage": stage,
                    "elapsed": round(now - t0, 1)}
        failed = [(stage, dict(e)) for (sid, stage), e in _FAILED.items()
                  if sid == session_id and e["count"] >= FAIL_LIMIT
                  and time.monotonic() - e["t_last"] < FAIL_COOLDOWN_SEC]
    if failed:
        stage, entry = failed[0]
        return {"state": "failed", "stage": stage, "fail_count": entry["count"],
                "error": entry.get("error") or ""}
    return {"state": "idle"}


def snapshot_all() -> list[dict]:
    """진행 중인 콜드 빌드 전부 — 오래 걸리는 순. 관리자 부하 모니터링 전용.

    snapshot() 은 프런트가 자기 세션만 보는 용도라 세션 단위인데, 관리자 화면은
    "지금 서버가 몇 개 세션을 동시에 빌드 중인가"(= 컴퓨트 워커 포화 원인)를 봐야 한다.
    """
    with _LOCK:
        entries = list(_ACTIVE.items())
    now = time.monotonic()
    # 관리자 화면은 유령도 **보여야** 한다 — 보이지 않으면 clear_stuck 으로 지울 수도
    # 없다. 그래서 여기서는 지우지 않고 표시만 한다(프런트용 snapshot 과 정반대).
    out = [{"session_id": sid, "stage": stage, "elapsed": round(now - m["t0"], 1),
            "trigger": m.get("trigger") or "", "started": m.get("started"),
            "stale": (now - m["t0"]) > STALE_SEC}
           for (sid, stage), m in entries]
    out.sort(key=lambda d: d["elapsed"], reverse=True)
    return out


def failures() -> list[dict]:
    """연속 실패 기록 전부 — 차단 중인 것 먼저. 관리자 화면(해제 버튼) 전용.

    snapshot() 은 세션 1건의 차단 여부만 답한다. 관리자는 "지금 어떤 세션들이 재빌드
    차단에 걸려 있고 쿨다운이 얼마 남았나"를 봐야 손을 쓸 수 있다.
    """
    now = time.monotonic()
    with _LOCK:
        items = [(sid, stage, dict(e)) for (sid, stage), e in _FAILED.items()]
    out = []
    for sid, stage, e in items:
        age = now - e["t_last"]
        blocked = e["count"] >= FAIL_LIMIT and age < FAIL_COOLDOWN_SEC
        out.append({"session_id": sid, "stage": stage, "count": e["count"],
                    "error": e.get("error") or "", "age": round(age, 1),
                    "blocked": blocked,
                    "cooldown_left": round(FAIL_COOLDOWN_SEC - age, 1) if blocked else 0.0})
    out.sort(key=lambda d: (not d["blocked"], d["age"]))
    return out

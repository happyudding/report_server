"""트레이스 결과 보관 — 요약은 즉시 응답하고 케이스 상세는 토큰으로 나중에 꺼낸다.

케이스 수백 건 × features/조건분해를 한 응답에 담으면 수 MB 가 되므로 프로세스
메모리에 최근 몇 건만 두고 상세는 1건씩 내려준다. 관리자 전용 저빈도 기능이라
LRU + TTL 로 충분하다(재시작 시 소실 = 의도).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

MAX_RUNS = 4
TTL_SECONDS = 30 * 60

_lock = threading.Lock()
_runs: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()


def _purge(now: float) -> None:
    for token in [t for t, (ts, _) in _runs.items() if now - ts > TTL_SECONDS]:
        _runs.pop(token, None)
    while len(_runs) > MAX_RUNS:
        _runs.popitem(last=False)


def put(token: str, result: dict) -> None:
    with _lock:
        now = time.time()
        _runs[token] = (now, result)
        _runs.move_to_end(token)
        _purge(now)


def latest_for_session(session_id: str):
    """같은 세션의 가장 최근 run → (token, result). 없으면 None (전후 비교용).

    토큰 문자열을 파싱하지 않고 result 안의 session_id 로 찾는다 — 토큰은
    "<session_id>-<ts>" 인데 session_id 자체에 '-' 가 들어갈 수 있어 모호하다.
    보관은 LRU 4런/TTL 이라 직전 run 이 이미 밀려났으면 None 이 정상(best-effort).
    """
    with _lock:
        now = time.time()
        for token in reversed(_runs):                 # move_to_end 이므로 뒤가 최신
            ts, result = _runs[token]
            if now - ts > TTL_SECONDS:
                continue
            if str(result.get("session_id") or "") == session_id:
                return token, result
        return None


def get(token: str) -> dict | None:
    with _lock:
        entry = _runs.get(token)
        if entry is None:
            return None
        ts, result = entry
        if time.time() - ts > TTL_SECONDS:
            _runs.pop(token, None)
            return None
        _runs.move_to_end(token)
        return result

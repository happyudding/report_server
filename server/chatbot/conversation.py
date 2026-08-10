"""대화 상태 — "1번 세션", "그중", "이 항목" 같은 후속 질문을 받기 위한 최소 기억.

**저장하는 것은 사실뿐이다**: 직전에 보여준 세션 목록, 지금 보고 있는 세션, 마지막으로 다룬
항목·제품. 질문 원문이나 모델의 추론은 담지 않는다(그건 report_chatbot_log 의 몫이고,
여기 쌓이면 오래된 문맥이 새 질문을 오염시킨다).

프로세스 메모리 LRU 다 — 재시작하면 사라지고 그게 맞다. 대화는 몇 분짜리이고,
웹 위젯이 sessionStorage 에 conversation_id 만 들고 있다가 다시 물으면 그때 새로 쌓인다.
컴퓨트 워커가 아니라 웹 프로세스에서만 쓰이므로 프로세스 간 공유도 필요 없다.
"""
from __future__ import annotations

import threading
import time
import uuid

_TTL_SEC = 30 * 60          # 이 시간 지난 대화는 없는 것으로 친다
_MAX_CONVERSATIONS = 200    # 동시 대화 상한(관리자 전용이라 넉넉하다)
_MAX_SESSIONS = 10          # "N번" 으로 지목 가능한 목록 길이 = 답변에 보여준 만큼

_LOCK = threading.Lock()
_STORE: dict[str, dict] = {}      # conversation_id → {"at": epoch, "state": {...}}


def new_id() -> str:
    return uuid.uuid4().hex


def recall(conversation_id) -> dict:
    """대화 상태 (없거나 만료면 빈 dict)."""
    if not conversation_id:
        return {}
    now = time.time()
    with _LOCK:
        entry = _STORE.get(conversation_id)
        if not entry or now - entry["at"] > _TTL_SEC:
            _STORE.pop(conversation_id, None)
            return {}
        entry["at"] = now                  # 쓰는 동안은 살려 둔다
        return dict(entry["state"])


def remember(conversation_id, **changes) -> None:
    """상태 갱신 — 값이 None 인 키는 무시한다(덮어쓰지 않는다).

    "지난번에 고른 세션"을 새 질문이 그 세션을 안 말했다고 지우면 후속 질문이 끊긴다.
    """
    if not conversation_id:
        return
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        return
    now = time.time()
    with _LOCK:
        entry = _STORE.get(conversation_id)
        if entry is None or now - entry["at"] > _TTL_SEC:
            entry = {"at": now, "state": {}}
            _STORE[conversation_id] = entry
        entry["at"] = now
        if "sessions" in changes:
            changes["sessions"] = list(changes["sessions"])[:_MAX_SESSIONS]
        entry["state"].update(changes)
        _prune_locked(now)


def _prune_locked(now):
    """만료 정리 + 상한 초과 시 오래된 대화부터 버린다 (_LOCK 을 잡은 상태에서 호출)."""
    for key in [k for k, v in _STORE.items() if now - v["at"] > _TTL_SEC]:
        _STORE.pop(key, None)
    if len(_STORE) > _MAX_CONVERSATIONS:
        for key, _ in sorted(_STORE.items(), key=lambda kv: kv[1]["at"])[
                :len(_STORE) - _MAX_CONVERSATIONS]:
            _STORE.pop(key, None)


def stats() -> dict:
    """관리자 탭용 — 살아 있는 대화 수."""
    now = time.time()
    with _LOCK:
        alive = sum(1 for v in _STORE.values() if now - v["at"] <= _TTL_SEC)
    return {"conversations": alive, "ttl_sec": _TTL_SEC}


def reset() -> None:
    """테스트용 — 프로세스 상태를 비운다."""
    with _LOCK:
        _STORE.clear()

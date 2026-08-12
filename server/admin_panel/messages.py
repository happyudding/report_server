"""관리자 → 사용자 팝업 메시지 저장소 (프로세스 메모리, DB 미사용).

DB 를 쓰지 않기로 한 결정(2026-08-12)이라 **서버를 재시작하면 미전달 메시지가
사라진다**. 공지 성격상 재발송이 쉬워 이 한계를 그대로 받아들인다. Flask 앱은
단일 프로세스(waitress 멀티스레드)이므로 스레드 간 공유는 lock 하나로 충분하다
— admin_panel/metrics.py 의 in-flight 카운터와 같은 전제다.

수신자 키는 사용량 집계(report/routes_misc._record_page_visit)와 같은 규칙:
신원(HoneyUser UA / SSO / 웹 로그인)이 있으면 소문자 계정, 없으면 'ip:<addr>'.
'1회만 표시'라 읽음은 그 키 단위로 기록한다 — 같은 사람이 Honey 와 일반 브라우저를
같이 쓰면 키가 갈려 두 번 볼 수 있다(팝업 1회 더 보는 것이라 무해).

폴링(GET pending_for)이 사용자당 30초마다 들어오므로 임계구역은 활성 메시지
순회뿐이다. 보관 상한(_MAX_KEEP)과 만료(_TTL_SEC)는 조회·생성 시점에만 정리한다.
"""
import threading
import time

MAX_TITLE = 200
MAX_BODY = 2000
LEVELS = ("info", "warn")

_MAX_KEEP = 200                 # 보관 상한 (넘으면 오래된 것부터 버림)
_TTL_SEC = 7 * 24 * 3600        # 이 시간이 지난 메시지는 목록에서도 제거

_lock = threading.Lock()
_messages = []      # 최신이 뒤 — [{id, targets, title, body, level, created_at,
                    #               created_by, active, reads: {user_key: ts}}]
_next_id = 1


def _prune_locked(now):
    global _messages
    if len(_messages) > _MAX_KEEP:
        _messages = _messages[-_MAX_KEEP:]
    cut = now - _TTL_SEC
    _messages = [m for m in _messages if m["created_at"] >= cut]


def _public(m):
    """관리자 목록용 표현 — 읽음 시각 맵은 명단·건수로 접어서 내보낸다."""
    readers = sorted(m["reads"].items(), key=lambda kv: kv[1], reverse=True)
    return {
        "id": m["id"],
        "targets": list(m["targets"]),
        "title": m["title"],
        "body": m["body"],
        "level": m["level"],
        "created_at": m["created_at"],
        "created_by": m["created_by"],
        "active": bool(m["active"]),
        "read_count": len(readers),
        "readers": [{"user": u, "at": ts} for u, ts in readers[:50]],
    }


def normalize_targets(raw):
    """대상 입력 → 소문자 계정 리스트. 빈 리스트면 '전체 공지'.

    raw 는 리스트 또는 콤마/공백/줄바꿈 구분 문자열을 받는다.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").replace(" ", ",").split(",")
    out = []
    for item in raw:
        uid = (item or "").strip().lower()
        if uid and uid not in out:
            out.append(uid)
    return out


def create(body, title="", targets=None, level="info", created_by=""):
    """메시지 1건 등록 → 관리자 표현 dict. body 가 비면 ValueError."""
    global _next_id
    body = (body or "").strip()
    if not body:
        raise ValueError("body required")
    title = (title or "").strip()[:MAX_TITLE]
    body = body[:MAX_BODY]
    if level not in LEVELS:
        level = "info"
    now = int(time.time())
    with _lock:
        msg = {
            "id": _next_id, "targets": normalize_targets(targets),
            "title": title, "body": body, "level": level,
            "created_at": now, "created_by": created_by or "",
            "active": True, "reads": {},
        }
        _next_id += 1
        _messages.append(msg)
        _prune_locked(now)
        return _public(msg)


def list_all():
    """관리자 목록 (최신 먼저)."""
    now = int(time.time())
    with _lock:
        _prune_locked(now)
        return [_public(m) for m in reversed(_messages)]


def pending_for(user_key):
    """이 사용자가 아직 안 본 활성 메시지 (오래된 것부터). 키가 비면 빈 리스트."""
    if not user_key:
        return []
    with _lock:
        return [
            {"id": m["id"], "title": m["title"], "body": m["body"],
             "level": m["level"], "created_at": m["created_at"]}
            for m in _messages
            if m["active"]
            and (not m["targets"] or user_key in m["targets"])
            and user_key not in m["reads"]
        ]


def mark_read(message_id, user_key):
    """읽음 기록. 이미 읽었거나 없는 id 여도 True(멱등) — 재시도가 실패로 보이지 않게."""
    if not user_key:
        return False
    with _lock:
        for m in _messages:
            if m["id"] == message_id:
                m["reads"].setdefault(user_key, int(time.time()))
                return True
    return True


def revoke(message_id):
    """회수 — 아직 안 본 사람에게 더는 안 뜬다. 읽음 기록은 남긴다."""
    with _lock:
        for m in _messages:
            if m["id"] == message_id:
                m["active"] = False
                return True
    return False


def delete(message_id):
    """목록에서 완전히 제거."""
    global _messages
    with _lock:
        before = len(_messages)
        _messages = [m for m in _messages if m["id"] != message_id]
        return len(_messages) < before

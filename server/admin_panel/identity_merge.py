"""관리자 화면 공통 신원 병합 — "IP 가 같으면 같은 사용자" 규칙.

신원 토큰이 없는 접속(일반 브라우저·구버전 Honey)은 계정 대신 `ip:<addr>` 로 집계된다
(metrics.active_users / stats.usage_ranking / stats.user_ranking). 같은 PC 에서 Honey 로도
접속하면 **한 사람이 계정 행 + IP 행 둘로 갈라져** 보인다. 여기서 IP→계정 매핑을 만들어
관리자 화면 전체가 같은 기준으로 합치게 한다.

매핑 근거:
  1. report_audit_log 의 (client_user, client_ip) 짝 — 재시작과 무관한 영구 근거
  2. 지금 접속 중인 사용자(metrics._active_users)의 (uid, ip) — 감사 기록이 아직 없는 신규 PC

**한 IP 에 계정이 2개 이상이면 활동이 가장 많은 계정으로 합친다** (2026-08-12 완화 —
그전에는 병합하지 않아 IP 행이 익명으로 남았고, 실제로는 한 사람의 행이 갈라져 목록이
길어졌다). 공용 PC·NAT 에서는 남의 활동이 주 사용자에게 붙을 수 있다는 뜻이므로, 합쳐진
행에는 원래 이름을 <span>IP 병합</span> 배지로 함께 보여준다(화면 쪽 mergeBadge).
동률이면 계정명 사전순으로 고정한다 — 새로고침마다 대표가 바뀌면 표가 흔들린다.

`admin-panel`(관리자 패널 자체 감사 기록)과 `system`(정리 스케줄러)은 사람이 아니라
매핑에서 제외한다 — 안 그러면 관리자 PC 의 IP 가 통째로 'admin-panel' 로 붙는다.
"""
import re
import threading
import time

from database import report_db

# 사람이 아닌 예약 계정 — 매핑 근거에서 제외
_NON_HUMAN = frozenset(("admin-panel", "system"))

# 매핑 산출 기간(일)과 캐시 수명(초). 실시간 표가 10초마다 부르므로 매번 집계하지 않는다.
MAP_DAYS = 90
_TTL_SEC = 60.0

_lock = threading.Lock()
_cache = {"ts": 0.0, "map": {}}

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def ip_of_name(name):
    """표시 이름이 IP 이면 그 IP 를, 계정이면 None.

    두 표기를 모두 받는다: `ip:1.2.3.4`(usage/실시간 집계) · `1.2.3.4`(감사로그 폴백).
    IPv6·호스트명은 대상이 아니다 — 병합은 IPv4 표기에만 적용한다(오탐 방지).
    """
    if not name:
        return None
    text = str(name).strip()
    if text.startswith("ip:"):
        text = text[3:].strip()
        return text or None
    return text if _IPV4_RE.match(text) else None


def _db_pairs(days):
    """report_audit_log 의 (ip, 계정) 짝 → {ip: {계정: 활동건수}}.

    건수를 함께 세는 이유는 한 IP 에 계정이 여럿일 때 주 사용자를 고르기 위해서다."""
    cutoff = int(time.time()) - days * 86400
    out = {}
    try:
        with report_db.get_conn() as conn:
            rows = conn.execute(
                "SELECT client_ip AS ip, LOWER(TRIM(client_user)) AS uid, COUNT(*) AS n "
                "FROM report_audit_log "
                "WHERE created_at >= ? AND client_ip IS NOT NULL AND client_ip <> '' "
                "      AND client_user IS NOT NULL AND TRIM(client_user) <> '' "
                "GROUP BY ip, uid", (cutoff,)).fetchall()
    except Exception:
        return out
    for r in rows:
        uid = (r["uid"] or "").strip()
        if not uid or uid in _NON_HUMAN:
            continue
        by_uid = out.setdefault(r["ip"], {})
        by_uid[uid] = by_uid.get(uid, 0) + int(r["n"] or 0)
    return out


def _live_pairs(into):
    """지금 접속 중인 사용자의 (ip, 계정)도 근거에 더한다 (감사 기록 전 신규 PC 대비).

    건수는 0 으로 둔다 — 감사 기록이라는 실적 근거보다 앞세우지 않되, 그 IP 에 계정이
    이것뿐이면 여전히 대표로 뽑힌다."""
    try:
        from admin_panel import metrics
        for uid, ip in metrics.live_identity_pairs():
            if uid and ip and uid not in _NON_HUMAN:
                into.setdefault(ip, {}).setdefault(uid, 0)
    except Exception:
        pass


def _primary(by_uid):
    """{계정: 활동건수} → 대표 계정. 건수 최다 → 동률이면 계정명 사전순(결정적)."""
    return min(by_uid.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def ip_to_user(force=False):
    """{ip: 계정} — 그 IP 의 주 사용자(활동 최다 계정).

    계정이 하나면 그 계정, 여럿이면 활동이 가장 많은 계정이다(모듈 docstring 참조).
    TTL 캐시(60초). force=True 면 즉시 재산출(테스트/수동 새로고침용)."""
    now = time.time()
    with _lock:
        if not force and (now - _cache["ts"]) < _TTL_SEC:
            return _cache["map"]
    pairs = _db_pairs(MAP_DAYS)
    _live_pairs(pairs)
    mapping = {ip: _primary(by_uid) for ip, by_uid in pairs.items() if by_uid}
    with _lock:
        _cache["ts"] = time.time()
        _cache["map"] = mapping
    return mapping


def resolve(name, ip=None, mapping=None):
    """표시 이름 → (계정, 병합됨?).

    이름이 IP 표기이고 그 IP 가 계정 하나로 확정되면 그 계정으로 바꾼다. 아니면 원래 이름
    그대로. ip 를 따로 주면(감사로그처럼 이름과 IP 가 별도 컬럼) 그 값을 우선 본다."""
    mapping = ip_to_user() if mapping is None else mapping
    addr = ip_of_name(name) or (ip or None)
    if addr and (not name or ip_of_name(name)):
        uid = mapping.get(addr)
        if uid:
            return uid, True
    return name, False


def invalidate():
    """캐시 무효화 — 테스트에서 매핑 근거를 바꾼 직후 사용."""
    with _lock:
        _cache["ts"] = 0.0
        _cache["map"] = {}

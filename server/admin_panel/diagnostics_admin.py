"""진단 사건 조회 — 관리자 '진단 사건' 탭 구현 (routes.py 는 얇은 핸들러만).

사건 저장소(server/diagnostics.py)에 더해 두 가지를 여기서 합친다:
- **콜드 빌드 기록**(web_report/build_log) — 같은 세션·build_id 의 빌드 레코드
- **watchdog 재기동 기록**(server/log/watchdog_events.log) — 사건 직전에 서버가
  재기동됐는지가 원인 판정을 가르므로 타임라인에 함께 세운다 (watchdog.ps1 은 읽기만)

원인 안내(explain)는 **증거가 있을 때만** 말한다. 근거가 없으면 그럴듯한 추정을 지어내지
않고 "확인 불가"를 돌려준다 — 틀린 단서 하나가 진짜 원인 탐색을 몇 시간 돌려세운다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import config
import diagnostics

WATCHDOG_LOG = "watchdog_events.log"


def _log_dir() -> Path:
    return config.ROOT_DIR / "server" / "log"


# ── 목록 / 요약 ──────────────────────────────────────────────────────────────

def events(args) -> dict:
    """사건 목록 + 요약 카운트 (관리자 탭 첫 화면)."""
    hours = args.get("hours", 24)
    out = diagnostics.history(
        hours=hours, severity=(args.get("severity") or "").strip(),
        component=(args.get("component") or "").strip(),
        q=args.get("q") or "", limit=args.get("limit", 200),
        unacked_only=str(args.get("unacked") or "") == "1")
    return {"events": out, "summary": diagnostics.summary(hours),
            "builds": _slow_builds(hours)}


def _slow_builds(hours) -> list[dict]:
    """오래 걸린/실패한 콜드 빌드 — 60초 이상이면 경고, 300초 이상이면 심각.

    성공했어도 60초가 걸렸다면 다음 번엔 타임아웃이 될 수 있다는 신호라 함께 세운다."""
    try:
        from web_report import build_log
        rows = build_log.history(int(hours or 24), 500)
    except Exception:
        return []
    out = []
    for r in rows:
        total = r.get("total") or 0
        if r.get("result") == "ok" and total < 60:
            continue
        out.append(r)
    return out[:200]


# ── 상세 / 타임라인 ──────────────────────────────────────────────────────────

def event_detail(event_id: str, hours: int = 24 * 7) -> dict:
    """사건 1건 + 상관 ID 로 이어진 타임라인 + 빌드 기록 + watchdog + 원인 안내."""
    data = diagnostics.related(event_id, hours=hours)
    if data.get("event") is None:
        return {"found": False}
    keys = data.get("keys") or {}
    builds = _related_builds(keys, hours)
    wd = _watchdog_near(data["timeline"])
    return {"found": True, "event": data["event"], "timeline": data["timeline"],
            "keys": keys, "builds": builds, "watchdog": wd,
            "detail": diagnostics.read_detail(event_id),
            "explain": explain(data["event"], data["timeline"], builds, wd)}


def _related_builds(keys: dict, hours: int) -> list[dict]:
    try:
        from web_report import build_log
        rows = build_log.history(int(hours), 500)
    except Exception:
        return []
    sid, bid = keys.get("session_id"), keys.get("build_id")
    out = [r for r in rows
           if (sid and r.get("session") == sid) or (bid and r.get("build_id") == bid)]
    return out[:50]


def _watchdog_near(timeline: list[dict], window_sec: int = 3600) -> list[dict]:
    """사건 앞뒤 1시간의 watchdog 이벤트 — 재기동이 원인/결과인지 판단용."""
    if not timeline:
        return []
    try:
        anchor = time.mktime(time.strptime(timeline[0].get("ts") or "",
                                           "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError, OverflowError):
        return []
    out = []
    try:
        p = _log_dir() / WATCHDOG_LOG
        if not p.exists():
            return []
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                ts = rec.get("ts") or rec.get("time") or ""
                try:
                    epoch = time.mktime(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, TypeError, OverflowError):
                    continue
                if abs(epoch - anchor) <= window_sec:
                    out.append(rec)
    except Exception:
        return []
    return out[-20:]


# ── 증거 기반 원인 안내 ──────────────────────────────────────────────────────

def explain(event: dict, timeline: list[dict], builds: list[dict],
            watchdog: list[dict]) -> dict:
    """무엇이 원인인지 — **기록으로 뒷받침되는 것만**. 근거가 없으면 확인 불가.

    반환: {"cause": 한 줄 결론, "evidence": [근거 문자열], "confident": bool}
    """
    ev = []
    build = builds[0] if builds else None
    if build:
        qw = float(build.get("queue_wait") or 0)
        pw = float(build.get("pool_wait") or 0)
        total = float(build.get("total") or 0)
        last_stage = build.get("last_stage")
        if build.get("result") in ("timeout", "broken", "interrupted"):
            ev.append(f"빌드 기록: {build.get('result')} / {total:.0f}초")
        if last_stage:
            src = f" [{build.get('last_source')}]" if build.get("last_source") else ""
            ev.append(f"마지막 단계: {last_stage}{src}")
            return {"cause": f"'{last_stage}' 단계에서 멎었습니다{src}.",
                    "evidence": ev, "confident": True}
        if last_stage == "" and build.get("result") == "timeout":
            ev.append("워커 체크포인트 없음 = 계산을 시작조차 못 함")
            return {"cause": "컴퓨트 큐에서 대기만 하다 타임아웃했습니다 "
                             "(다른 빌드가 워커를 점유 — 계산이 느린 것이 아님).",
                    "evidence": ev, "confident": True}
        if total > 0 and qw > total * 0.5:
            ev.append(f"큐 대기 {qw:.0f}초 / 전체 {total:.0f}초")
            return {"cause": "온디맨드 큐 포화 — 앞선 빌드에 밀렸습니다.",
                    "evidence": ev, "confident": True}
        if total > 0 and pw > total * 0.5:
            ev.append(f"풀 대기 {pw:.0f}초 / 전체 {total:.0f}초")
            return {"cause": "워커 슬롯 대기·프로세스 spawn 에 시간이 갔습니다.",
                    "evidence": ev, "confident": True}
        stages = build.get("stages") or {}
        if stages:
            top = max(stages.items(), key=lambda kv: kv[1])
            ev.append(f"최대 단계: {top[0]} {top[1]}초")
            return {"cause": f"'{top[0]}' 단계가 시간을 지배했습니다.",
                    "evidence": ev, "confident": True}

    if watchdog and any(str(w.get("action") or w.get("event") or "").lower()
                        .find("restart") >= 0 for w in watchdog):
        return {"cause": "이 시각 전후로 watchdog 이 서버를 재기동했습니다.",
                "evidence": [f"watchdog 이벤트 {len(watchdog)}건"], "confident": True}

    comp = event.get("component")
    if comp in ("honey", "browser"):
        # ⚠️ 상관 ID 가 **있는데도** 서버 기록이 없을 때만 "서버에 안 닿았다"고 말할 수
        # 있다. ID 자체가 없으면 찾을 방법이 없었던 것이지 안 닿았다는 증거가 아니다
        # (그 구분을 놓치면 멀쩡한 서버 오류를 네트워크 탓으로 몰게 된다).
        linked = event.get("request_id") or event.get("operation_id")
        if linked and not any(e.get("component") == "server" for e in timeline):
            return {"cause": "클라이언트만 실패를 기록했고 대응하는 서버 요청 기록이 "
                             "없습니다 — 네트워크 단절이나 서버 도달 실패 쪽입니다.",
                    "evidence": [f"상관 ID {linked} 의 서버 사건 0건"], "confident": True}

    if event.get("stack"):
        return {"cause": "서버 예외 — 스택의 최상단 프레임을 보세요.",
                "evidence": ["서버 스택 있음"], "confident": True}

    return {"cause": "확인 불가 — 원인을 특정할 근거가 기록에 없습니다.",
            "evidence": ev, "confident": False}

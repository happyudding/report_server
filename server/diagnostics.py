"""진단 사건 수집 — 오류 추적의 단일 저장소 (2026-08-11).

서버 500·컴퓨트 타임아웃·업로드 실패·브라우저 오류·Honey 오류가 각각 다른 곳에
(콘솔 로그 / 감사 로그 / 빌드 로그 / 아무데도) 흩어져 있어, "사용자가 신고한 그 실패"를
서버 기록에서 찾아낼 방법이 없었다. 여기서 **사건 1건 = JSON 1줄**로
`server/log/diagnostic_YYYYMMDD.log` 에 모은다.

사건들은 상관 ID 로 이어진다:

  request_id    요청 1건 (모든 응답의 X-Request-ID 헤더로 나간다 = 사용자가 신고할 번호)
  operation_id  Honey 작업 1건 (분석→업로드처럼 여러 요청에 걸친 단위)
  build_id      콜드 빌드 1건 (build_log 레코드와 같은 값)
  session_id    세션
  event_id      사건 자신 (Honey 가 오프라인 큐에서 재전송할 때 중복 제거 키)

ID 하나만 있으면 related() 가 나머지를 전부 끌어온다 — 그게 이 모듈의 존재 이유다.

설계 규약:
- **DB 를 쓰지 않는다.** 에러가 나는 순간은 DB 잠금·디스크 압박이 겹치기 쉬운 때라,
  기록하려다 또 터지면 안 된다. build_log 와 같은 open-append-close JSONL.
- 전 함수 best-effort — 기록 실패가 요청을 죽이면 안 되므로 예외를 모두 삼키고
  메모리 링버퍼로 폴백한다.
- 큰 본문(사용자 제출 상세 로그)은 JSONL 에 넣지 않고 별도 파일로 뺀다 — history()
  가 매 줄을 파싱하므로 한 줄이 커지면 조회가 통째로 느려진다.
- 개인정보·원본 데이터는 담지 않는다. 파일은 basename 만(scrub_paths 참조).
"""
from __future__ import annotations

import collections
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

LOG_PREFIX = "diagnostic"
DETAIL_PREFIX = "diagnostic_detail"
ACK_FILE = "diagnostic_ack.json"

_KEEP_DAYS = float(os.getenv("LOG_KEEP_DAYS", "14") or 14)
_HISTORY_MAX_HOURS = 24 * 14
_DETAIL_MAX_BYTES = 512 * 1024

# 심각도 / 발생원 어휘 (관리자 필터가 이 값을 그대로 쓴다)
SEVERITIES = ("critical", "warning", "info")
COMPONENTS = ("server", "build", "browser", "honey", "watchdog")

_MSG_MAX = 2000
_STACK_MAX = 20000

_RECENT = collections.deque(maxlen=300)
_RECENT_LOCK = threading.Lock()
_last_prune_date = ""
_ack_lock = threading.Lock()

# 전체 경로 → basename (Windows 드라이브 경로 · UNC · POSIX 절대경로)
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/)[^\s'\"|,;)]{3,}")


def new_id(n: int = 4) -> str:
    """상관 ID 1개 (기본 8자 hex)."""
    return secrets.token_hex(n)


def scrub_paths(text) -> str:
    """전체 경로를 basename 으로 줄인다 — 클라이언트가 보낸 내용에 적용한다.

    사용자 PC 의 폴더 구조는 진단에 쓸모가 없고 남기면 개인정보다. 파일명은 남긴다
    (어느 source 에서 터졌는지가 핵심 단서라서)."""
    if not text:
        return ""
    def _base(m):
        raw = m.group(0).replace("\\", "/").rstrip("/")
        return raw.rsplit("/", 1)[-1] or raw
    try:
        return _PATH_RE.sub(_base, str(text))
    except Exception:
        return str(text)


# ── 파일 위치 ────────────────────────────────────────────────────────────────

def log_dir() -> Path:
    """진단 로그 폴더. REPORT_DIAG_DIR 로 덮어쓸 수 있다(테스트가 운영 로그를 오염시키지
    않도록 — 실제 server/log 에 테스트 사건이 섞이면 조회가 못 믿을 것이 된다)."""
    override = os.getenv("REPORT_DIAG_DIR", "").strip()
    if override:
        d = Path(override)
    else:
        import config
        d = Path(config.ROOT_DIR) / "server" / "log"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune(d: Path, date: str) -> None:
    """날짜가 바뀔 때만 보관기간 경과 파일 정리 (build_log._prune 패턴)."""
    global _last_prune_date
    if date == _last_prune_date:
        return
    _last_prune_date = date
    try:
        cutoff = time.time() - _KEEP_DAYS * 86400
        for pat in (f"{LOG_PREFIX}_2*.log", f"{DETAIL_PREFIX}_*.txt"):
            for p in d.glob(pat):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except Exception:
                    pass
    except Exception:
        pass


# ── 기록 ─────────────────────────────────────────────────────────────────────

def emit(severity: str, component: str, event: str, **fields) -> str:
    """사건 1건 기록 → event_id 반환 (호출부가 사용자에게 보여줄 수 있다).

    fields 로 받는 것: event_id(재전송 시 클라 생성값), request_id, operation_id,
    build_id, session_id, endpoint, method, http_status, elapsed_ms, user, host,
    honey_version, error_type, message, stack, source, detail(별도 파일로 분리).
    None/빈 값은 저장하지 않는다 — 레코드를 얇게 유지해야 조회가 빠르다.
    """
    detail = fields.pop("detail", None)
    event_id = str(fields.pop("event_id", "") or "").strip()[:32] or new_id()
    try:
        ts = time.time()
        lt = time.localtime(ts)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", lt),
               "event_id": event_id,
               "severity": severity if severity in SEVERITIES else "info",
               "component": component if component in COMPONENTS else "server",
               "event": str(event)[:60]}
        for k, v in fields.items():
            if v is None or v == "":
                continue
            if k == "message":
                v = str(v)[:_MSG_MAX]
            elif k == "stack":
                v = str(v)[:_STACK_MAX]
            elif isinstance(v, str):
                v = v[:500]
            rec[k] = v
        with _RECENT_LOCK:
            _RECENT.append(rec)
        if _KEEP_DAYS <= 0:
            return event_id
        d = log_dir()
        date = time.strftime("%Y%m%d", lt)
        _prune(d, date)
        if detail:
            rec["has_detail"] = _write_detail(d, event_id, detail)
        with (d / f"{LOG_PREFIX}_{date}.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return event_id


def _write_detail(d: Path, event_id: str, detail: str) -> bool:
    """사용자 제출 상세(정제 traceback + 실행 로그 꼬리)를 별도 파일로."""
    try:
        text = str(detail)[:_DETAIL_MAX_BYTES]
        (d / f"{DETAIL_PREFIX}_{event_id}.txt").write_text(text, encoding="utf-8",
                                                           errors="replace")
        return True
    except Exception:
        return False


def read_detail(event_id: str) -> str:
    try:
        safe = re.sub(r"[^0-9a-zA-Z_-]", "", str(event_id))[:32]
        if not safe:
            return ""
        p = log_dir() / f"{DETAIL_PREFIX}_{safe}.txt"
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return ""


# ── 조회 ─────────────────────────────────────────────────────────────────────

def _epoch(text) -> float:
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError, TypeError):
        return 0.0


def _iter_records(hours: int):
    """최근 hours 시간의 레코드 (파일 → 없으면 메모리 폴백)."""
    now = time.time()
    cutoff = now - hours * 3600
    out: list[dict] = []
    try:
        d = log_dir()
        for i in range(int(hours // 24) + 2):
            name = time.strftime(f"{LOG_PREFIX}_%Y%m%d.log", time.localtime(now - i * 86400))
            p = d / name
            if not p.exists():
                continue
            try:
                with p.open("r", encoding="utf-8", errors="replace") as f:
                    for ln in f:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            rec = json.loads(ln)
                        except ValueError:
                            continue
                        if _epoch(rec.get("ts")) >= cutoff:
                            out.append(rec)
            except OSError:
                pass
    except Exception:
        pass
    if not out:
        with _RECENT_LOCK:
            out = [r for r in _RECENT if _epoch(r.get("ts")) >= cutoff]
    return out


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return default


def history(hours: int = 24, severity: str = "", component: str = "",
            q: str = "", limit: int = 200, unacked_only: bool = False) -> list[dict]:
    """사건 목록 — 최신순. q 는 message/user/session/endpoint/event_id 부분일치."""
    hours = _clamp(hours, 1, _HISTORY_MAX_HOURS, 24)
    limit = _clamp(limit, 1, 1000, 200)
    acks = load_acks()
    needle = (q or "").strip().lower()
    out = []
    for rec in _iter_records(hours):
        if severity and rec.get("severity") != severity:
            continue
        if component and rec.get("component") != component:
            continue
        ack = acks.get(rec.get("event_id") or "")
        if unacked_only and ack:
            continue
        if needle:
            hay = " ".join(str(rec.get(k) or "") for k in
                           ("message", "user", "session_id", "endpoint", "event_id",
                            "error_type", "build_id", "request_id", "operation_id",
                            "source", "event")).lower()
            if needle not in hay:
                continue
        if ack:
            rec = dict(rec, ack=ack)
        out.append(rec)
    out.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return out[:limit]


_LINK_KEYS = ("request_id", "operation_id", "build_id", "session_id", "event_id")


def related(event_id: str, hours: int = 24 * 7) -> dict:
    """한 사건과 상관 ID 로 이어진 모든 사건 — 관리자 타임라인의 데이터원.

    끌어오는 규칙: 기준 사건이 가진 ID 중 **하나라도** 같으면 같은 줄기로 본다
    (요청 → 빌드 → 오류가 서로 다른 ID 로만 연결돼 있어서, 하나씩 따라가면 끊긴다).
    """
    hours = _clamp(hours, 1, _HISTORY_MAX_HOURS, 24 * 7)
    records = _iter_records(hours)
    base = None
    for rec in records:
        if rec.get("event_id") == event_id:
            base = rec
            break
    if base is None:
        return {"event": None, "timeline": [], "keys": {}}
    keys = {k: base[k] for k in _LINK_KEYS if base.get(k)}
    seen_ids = set()
    timeline = []
    for rec in records:
        if any(rec.get(k) == v for k, v in keys.items()):
            eid = rec.get("event_id")
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            timeline.append(rec)
    timeline.sort(key=lambda r: r.get("ts") or "")
    acks = load_acks()
    for rec in timeline:
        a = acks.get(rec.get("event_id") or "")
        if a:
            rec["ack"] = a
    return {"event": base, "timeline": timeline, "keys": keys}


def summary(hours: int = 24) -> dict:
    """미확인 심각도별 건수 — 현황 탭 경고 칩용."""
    acks = load_acks()
    counts = {"critical": 0, "warning": 0, "info": 0}
    unacked = {"critical": 0, "warning": 0}
    for rec in _iter_records(_clamp(hours, 1, _HISTORY_MAX_HOURS, 24)):
        sev = rec.get("severity") or "info"
        if sev in counts:
            counts[sev] += 1
        if sev in unacked and not acks.get(rec.get("event_id") or ""):
            unacked[sev] += 1
    return {"hours": hours, "counts": counts, "unacked": unacked}


# ── 확인 처리(ack) — 작은 JSON 파일 (DB 스키마 무변경 원칙) ───────────────────

def _ack_path() -> Path:
    return log_dir() / ACK_FILE


def load_acks() -> dict:
    try:
        p = _ack_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def ack(event_id: str, by: str = "") -> bool:
    """사건 확인 처리. 오래된 항목은 함께 정리한다(파일 무한 증가 방지)."""
    eid = str(event_id or "").strip()[:32]
    if not eid:
        return False
    try:
        with _ack_lock:
            data = load_acks()
            now = int(time.time())
            cutoff = now - int(_KEEP_DAYS * 86400)
            data = {k: v for k, v in data.items()
                    if isinstance(v, dict) and int(v.get("ts") or 0) >= cutoff}
            data[eid] = {"ts": now, "by": str(by or "")[:60]}
            tmp = _ack_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, _ack_path())
        return True
    except Exception:
        return False


# ── Flask 요청 훅 (상관 ID 발급) ─────────────────────────────────────────────

def current_ids() -> dict:
    """지금 요청의 상관 ID — 요청 컨텍스트 밖이면 빈 dict."""
    try:
        from flask import g, has_request_context
        if not has_request_context():
            return {}
        out = {}
        for k in ("request_id", "operation_id"):
            v = getattr(g, k, None)
            if v:
                out[k] = v
        return out
    except Exception:
        return {}


def init_app(app) -> None:
    """모든 요청에 request_id 를 붙이고 응답 헤더로 돌려준다.

    사용자가 신고할 때 화면에서 읽어줄 번호가 이것이고, 서버 콘솔 로그(`[rid=...]`)와
    진단 사건이 같은 값을 쓴다 — 셋을 잇는 유일한 끈이다."""
    from flask import g, request

    @app.before_request
    def _diag_assign_ids():
        try:
            g.request_id = new_id()
            op = request.headers.get("X-Honey-Operation-ID")
            if op:
                g.operation_id = str(op)[:32]
        except Exception:
            pass

    @app.after_request
    def _diag_response_header(resp):
        try:
            rid = getattr(g, "request_id", None)
            if rid:
                resp.headers["X-Request-ID"] = rid
        except Exception:
            pass
        return resp

"""Honey 오류 자동 보고 (2026-08-11).

브라우저에는 error_beacon.js 가 있어 웹 화면 오류가 서버 감사·진단 사건에 남는데,
정작 Honey 데스크톱 앱은 실패해도 **서버에 아무 흔적이 없었다** — 사용자가 "업로드가
안 된다"고 말해도 관리자가 확인할 기록이 사용자 PC 의 log\\*.txt 뿐이었다.

여기서 실패를 서버 `POST /pe/report/api/client_diagnostic` 로 best-effort 전송한다.

규약:
- **전부 무음** — 보고가 실패해도, 서버가 죽어 있어도 UI 에 영향이 0 이어야 한다.
- **최소 수집**: 파일은 basename, 단계·버전·오류 종류까지. 전체 경로·데이터 내용은
  보내지 않는다. 상세(traceback + 실행 로그 꼬리)는 사용자가 오류 창에서 직접
  "진단 정보 보내기"를 눌렀을 때만(mode="detail").
- **사용자 취소·정상적인 입력 검증 오류는 보고 대상이 아니다** — 그건 고장이 아니다.
- **오프라인 큐**: 서버 연결 실패 시 로컬 파일에 쌓아 두고 다음 실행/복구 때 재전송한다.
  event_id 를 클라가 만들어 보내므로 재전송이 중복 사건이 되지 않는다.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .config import CURRENT_VERSION, SERVER_BASE_URL

_TIMEOUT = (3, 5)                 # 오류 보고가 UI 를 붙잡으면 안 된다 — 짧게
_QUEUE_MAX_ITEMS = 50
_QUEUE_MAX_BYTES = 20 * 1024 * 1024
_QUEUE_MAX_AGE_SEC = 14 * 86400
_DETAIL_MAX = 256 * 1024
_sent_keys = set()                # 프로세스당 같은 오류 1회 (루프 폭주 방지)

# 현재 작업 단위 — honey_main 이 작업 시작 시 지정하면 그 이후 오류·업로드 요청이
# 같은 operation_id 를 공유해 서버에서 한 타임라인으로 묶인다.
_operation = {"id": "", "name": ""}


def begin_operation(name: str) -> str:
    """작업 1건 시작 (분석·업로드·Excel 왕복 등). 반환값은 operation_id."""
    _operation["id"] = uuid.uuid4().hex[:12]
    _operation["name"] = str(name or "")[:40]
    return _operation["id"]


def current_operation() -> dict:
    return dict(_operation)


def operation_headers() -> dict:
    """서버 요청에 실을 상관 ID 헤더 (없으면 빈 dict)."""
    return {"X-Honey-Operation-ID": _operation["id"]} if _operation["id"] else {}


def _queue_path() -> Path:
    try:
        import config as honey_config
        base = Path(honey_config.CONFIG_DIR)
    except Exception:
        base = Path.home() / ".honey"
    base.mkdir(parents=True, exist_ok=True)
    return base / "diag_queue.jsonl"


def _identity() -> str:
    try:
        import client_identity
        return client_identity.collect().get("user", "") or ""
    except Exception:
        return ""


def _headers() -> dict:
    """신원 토큰 UA — 서버가 누구의 오류인지 귀속하려면 이게 있어야 한다
    (uploader._upload_headers 와 같은 규칙)."""
    h = {"Content-Type": "application/json"}
    user = _identity()
    if user:
        h["User-Agent"] = f"python-requests HoneyUser/{quote(user, safe='')}"
    h.update(operation_headers())
    return h


def report_error(kind: str, message: str, *, stack: str = "", context: dict | None = None,
                 detail: str = "", session_id: str = "", dedupe: bool = True) -> str:
    """오류 1건 보고 → event_id (오류 창에 보여줄 번호). 실패해도 예외를 내지 않는다."""
    event_id = uuid.uuid4().hex[:12]
    try:
        key = f"{kind}|{str(message)[:200]}"
        if dedupe:
            if key in _sent_keys:
                return event_id
            _sent_keys.add(key)
        payload = {
            "event_id": event_id,
            "kind": str(kind)[:40],
            "message": str(message)[:500],
            "version": CURRENT_VERSION,
            "operation_id": _operation["id"],
            "operation": _operation["name"],
            "session_id": str(session_id or "")[:64],
            "mode": "detail" if detail else "minimal",
        }
        if stack:
            payload["stack"] = str(stack)[-2000:]
        if context:
            payload["context"] = {k: str(v)[:200] for k, v in context.items()}
        if detail:
            payload["detail"] = str(detail)[-_DETAIL_MAX:]
        if not _post(payload):
            _enqueue(payload)
    except Exception:
        pass
    return event_id


def _post(payload: dict) -> bool:
    try:
        import requests
        base = SERVER_BASE_URL.rstrip("/")
        resp = requests.post(f"{base}/pe/report/api/client_diagnostic",
                             data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             headers=_headers(), timeout=_TIMEOUT)
        return resp.status_code < 500
    except Exception:
        return False


# ── 오프라인 큐 ──────────────────────────────────────────────────────────────

def _enqueue(payload: dict) -> None:
    """서버에 못 보낸 보고를 로컬에 쌓는다 — 서버가 죽어 있던 사고일수록 기록이 중요하다."""
    try:
        payload = dict(payload, queued_at=int(time.time()))
        p = _queue_path()
        if p.exists() and p.stat().st_size > _QUEUE_MAX_BYTES:
            p.unlink()
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def flush_queue() -> int:
    """쌓인 보고를 재전송한다 (앱 시작 시·업로드 성공 직후 호출). 보낸 건수 반환.

    한 건이라도 실패하면 남은 것은 그대로 둔다 — 서버가 아직 안 돌아온 것이므로
    다음 기회를 기다린다."""
    p = _queue_path()
    if not p.exists():
        return 0
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    cutoff = time.time() - _QUEUE_MAX_AGE_SEC
    items = []
    for ln in lines[-_QUEUE_MAX_ITEMS:]:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if int(rec.get("queued_at") or 0) >= cutoff:
            items.append(rec)
    sent = 0
    rest = []
    for i, rec in enumerate(items):
        if rest or not _post(rec):
            rest = items[i:]
            break
        sent += 1
    try:
        if rest:
            p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rest) + "\n",
                         encoding="utf-8")
        else:
            p.unlink(missing_ok=True)
    except OSError:
        pass
    return sent

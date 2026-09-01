# -*- coding: utf-8 -*-
"""AI Comment 클라 대행 현황 (관리자 패널 'AI Comment' 탭의 데이터원).

**왜 필요한가.** `[제안]` 문장 생성은 업로더 PC 의 로컬 Claude CLI 가 대행하는데
(docs/23), 실패해도 화면에는 에러가 아니라 **룰 폴백 문장**이 나온다 — 관리자가
"이 기능이 실제로 도는가"를 판단할 근거가 어디에도 없었다. 현장(Enterprise gateway)
검증이 남아 있는 동안에는 더더욱, 안 되면 왜 안 되는지가 화면에 남아야 한다.

세 데이터원을 조회 시점에 합친다 — **새 테이블을 만들지 않는다**(stats.py·voc_admin
관례: report_db 를 수정하지 않고 자체 SELECT/기존 모듈 재사용만).

  (a) 커버리지 — `report_session.webreport_options` 에서 대상 세션(ai_comment optin +
      ai_model=claude)을 고르고, `report_webreport_edit` 의 push marker(kind=ai_suggest,
      item_key=push) 유무로 반영 여부를 본다. marker 는 세션당 1행이라 가장 싼 신호다.
  (b) push 현황 — 감사 로그 `action='ai_suggest'`(2026-08-28 'edit' 에서 분리)의
      `changed_fields` 를 파싱해 수용/스킵 합계.
  (c) 클라 실패 — 진단 사건 중 component=honey + event 가 'ai_suggest' 로 시작하는 것.
      클라 워커가 실패 사유 5종을 보낸다(transport/ai_suggest._report_failure).

구성요소 하나가 죽어도 탭은 뜬다 — 각각 try/except (chatbot_admin·diagnostics_admin 관례).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import config
from database import report_db

_log = logging.getLogger(__name__)

# service.apply_ai_suggestions 가 남기는 형식 — 바뀌면 여기 파싱도 함께 고쳐야 한다.
_AUDIT_ACTION = "ai_suggest"
_AUDIT_RE = re.compile(r"ai_suggest\(accepted=(\d+),skipped=(\d+)\)")

# 클라가 보내는 실패 kind (transport/ai_suggest._report_failure) → 화면 라벨·대응 안내.
FAILURE_KINDS = {
    "ai_suggest_no_cli": "claude CLI 없음",
    "ai_suggest_no_prompts": "프롬프트 수신 실패",
    "ai_suggest_empty": "생성 결과 0건",
    "ai_suggest_push_failed": "서버 저장 실패",
    "ai_suggest_worker_error": "워커 오류",
}

_SESSION_SCAN_MAX = 2000    # 세션 스캔 상한 (최신순) — 운영 규모에서 충분하고 상한이 있어야 안전
_LIST_MAX = 50              # 화면 표에 싣는 행 수


def _upload_root() -> Path:
    return Path(config.REPORT_UPLOAD_DIR)


def _wanted(opts_raw) -> bool:
    """이 세션이 클라 대행 대상인가 — service._ai_suggest_wanted 와 같은 판정.

    판정 자체를 복제하지 않고 web_report.validation 의 정본 함수를 쓴다(둘이 갈리면
    커버리지 분모가 조용히 틀어진다).
    """
    from web_report.validation import webreport_ai_comment, webreport_ai_model
    raw = opts_raw or ""
    return bool(webreport_ai_comment(raw)) and webreport_ai_model(raw) == "claude"


def _target_sessions(limit=_SESSION_SCAN_MAX) -> list:
    """대상 세션 + push marker 를 한 번에 — 최신순.

    `sessions_admin.list_sessions` 를 쓰지 않는 이유: 그 함수는 webreport_options 를
    반환하지 않아 대상 판정을 할 수 없고, limit 이 500 이라 페이징 루프가 필요하다.
    """
    sql = """
        SELECT s.session_id, s.file_name, s.product, s.product_type, s.lot_id,
               s.uploaded_by, s.created_at, s.webreport_options,
               e.value AS push_marker, e.updated_at AS push_at
        FROM report_session s
        LEFT JOIN report_webreport_edit e
               ON e.session_id = s.session_id
              AND e.kind = 'ai_suggest' AND e.item_key = 'push'
        WHERE s.source = 'web_report' AND s.deleted_at IS NULL
        ORDER BY s.created_at DESC, s.session_id
        LIMIT ?
    """
    with report_db.get_conn() as conn:
        rows = conn.execute(sql, (int(limit),)).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        if not _wanted(row.get("webreport_options")):
            continue
        count = None
        if row.get("push_marker"):
            try:
                count = int(json.loads(row["push_marker"]).get("count") or 0)
            except Exception:  # noqa: BLE001 — marker 손상은 "반영됨" 판정만 남기고 넘어간다
                count = None
        out.append({"session_id": row["session_id"], "file_name": row.get("file_name") or "",
                    "product": row.get("product") or "", "lot_id": row.get("lot_id") or "",
                    "product_type": row.get("product_type") or "",
                    "uploaded_by": row.get("uploaded_by") or "",
                    "created_at": row.get("created_at") or 0,
                    "covered": bool(row.get("push_marker")),
                    "push_at": row.get("push_at") or 0, "push_count": count})
    return out


def _coverage() -> dict:
    """대상 세션 수 / 반영 완료 / 미반영 목록 + **비어 있는 사유**.

    "0" 이 여러 의미를 갖는다 — 대상 세션이 없는 것과 전부 반영된 것은 다음에 할 일이
    다르다(review.queue 규약). note 로 구분해 내려준다.
    """
    sessions = _target_sessions()
    covered = [s for s in sessions if s["covered"]]
    pending = [s for s in sessions if not s["covered"]]
    if not sessions:
        note = "AI Model=claude 로 업로드된 세션이 아직 없습니다."
    elif not pending:
        note = "대상 세션이 모두 반영됐습니다."
    else:
        note = f"{len(pending)}개 세션이 아직 반영되지 않았습니다."
    return {"total": len(sessions), "covered": len(covered), "pending": len(pending),
            "note": note, "rows": pending[:_LIST_MAX], "covered_rows": covered[:_LIST_MAX]}


def _push(days: int) -> dict:
    """감사 로그 기반 push 현황 — 건수·수용/스킵 합계 + 최근 목록.

    `changed_fields` 형식이 바뀌면 파싱이 실패하는데, 그걸 조용히 0 으로 만들지 않고
    `unparsed` 로 세어 화면에 드러낸다(형식 드리프트 감지).
    """
    since = int(time.time()) - max(1, int(days)) * 86400
    logs = report_db.get_audit_logs(action=_AUDIT_ACTION, limit=500)
    recent = [r for r in logs if int(r.get("created_at") or 0) >= since]
    accepted = skipped = unparsed = 0
    rows = []
    for r in recent:
        m = _AUDIT_RE.search(str(r.get("changed_fields") or ""))
        if m:
            accepted += int(m.group(1))
            skipped += int(m.group(2))
        else:
            unparsed += 1
        if len(rows) < _LIST_MAX:
            rows.append({"created_at": r.get("created_at") or 0,
                         "session_id": r.get("session_id") or "",
                         "user": r.get("client_user") or "",
                         "product": r.get("product") or "",
                         "lot_id": r.get("lot_id") or "",
                         "accepted": int(m.group(1)) if m else None,
                         "skipped": int(m.group(2)) if m else None,
                         "changed_fields": r.get("changed_fields") or ""})
    return {"pushes": len(recent), "accepted": accepted, "skipped": skipped,
            "unparsed": unparsed, "all_time": len(logs), "rows": rows}


def _failures(days: int) -> dict:
    """클라 실패 사건 — kind 별 건수 + 최근 목록. 진단 저장소는 시간 단위라 days→hours."""
    import diagnostics
    hours = max(1, int(days)) * 24
    events = [e for e in diagnostics.history(hours=hours, component="honey", limit=1000)
              if str(e.get("event") or "").startswith("ai_suggest")]
    by_kind = {}
    for e in events:
        kind = str(e.get("event") or "")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    # ts 는 ISO 문자열("YYYY-MM-DDTHH:MM:SS") — 진단 저장소 원형 그대로 넘긴다.
    rows = [{"ts": e.get("ts") or "", "event": e.get("event") or "",
             "label": FAILURE_KINDS.get(str(e.get("event") or ""), ""),
             "user": e.get("user") or "", "session_id": e.get("session_id") or "",
             "message": e.get("message") or "", "event_id": e.get("event_id") or "",
             "honey_version": e.get("honey_version") or ""}
            for e in events[:_LIST_MAX]]
    return {"total": len(events),
            "by_kind": [{"kind": k, "label": FAILURE_KINDS.get(k, ""), "cnt": v}
                        for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])],
            "rows": rows}


def overview(days=14) -> dict:
    """탭 진입 데이터 — 커버리지 + push + 클라 실패. 구성요소별로 실패를 격리한다."""
    try:
        days = max(1, min(int(days), 90))
    except (TypeError, ValueError):
        days = 14
    out = {"days": days}
    for key, fn in (("coverage", _coverage), ("push", lambda: _push(days)),
                    ("failures", lambda: _failures(days))):
        try:
            out[key] = fn()
        except Exception as exc:  # noqa: BLE001 — 하나가 죽어도 나머지는 보여준다
            _log.warning("ai_comment %s 집계 실패", key, exc_info=True)
            out[key] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def session_suggestions(session_id: str) -> dict:
    """한 세션에 저장된 [제안] 문장 목록 — 품질 검수용.

    본문은 DB 가 아니라 영구 저장 파일(ai_suggest_store)에 있다. **파일 1개만** 읽는다.
    `stale` 은 "지금 프롬프트 sha 와 달라 폴백 중"인지 — 룰이 바뀌면 저장된 문장은
    자동으로 안 붙는다(docs/23 sha 게이트). 판정에 캐시를 **새로 만들지 않는다**
    (allow_build=False) — 관리자 조회가 콜드 빌드를 유발하면 안 된다.
    """
    from web_report import ai_suggest_store, service as web_report_service

    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    if not _wanted(session.get("webreport_options")):
        return {"items": [], "note": "AI Model=claude 대상 세션이 아닙니다."}
    upload_root = _upload_root()
    akey, chash, mode, prep = web_report_service._ai_suggest_coords(
        session, session_id, report_db=report_db)
    stored = ai_suggest_store.load(upload_root, akey, chash, mode, prep)
    if not stored:
        return {"items": [], "note": "저장된 문장이 없습니다 (아직 push 되지 않음)."}
    prompts = {}
    note = ""
    try:
        result, _how = web_report_service._ai_comment_cached(
            session, session_id, None, None, report_db=report_db,
            upload_root=upload_root, allow_build=False)
        if result is None:
            note = "현재 평가 캐시가 없어 최신 여부(sha)는 확인하지 못했습니다."
        else:
            prompts = result.get("prompts") or {}
    except Exception:  # noqa: BLE001 — 검수 목록 자체는 보여준다
        _log.warning("ai_comment sha 대조 실패 (session=%s)", session_id, exc_info=True)
        note = "최신 여부(sha) 확인 중 오류가 발생했습니다."
    items = []
    for item, row in sorted(stored.items()):
        sha = str(row.get("sha") or "")
        cur = (prompts.get(item) or {}).get("sha") if prompts else None
        items.append({"item": item, "sha": sha,
                      "suggestion": str(row.get("suggestion") or ""),
                      "by": str(row.get("by") or ""), "ts": int(row.get("ts") or 0),
                      "stale": (None if not prompts else (cur != sha))})
    return {"items": items, "note": note,
            "session": {"session_id": session_id,
                        "file_name": session.get("file_name") or "",
                        "product": session.get("product") or "",
                        "lot_id": session.get("lot_id") or ""}}

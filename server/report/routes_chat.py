"""웹 챗봇 라우트 — `POST /pe/report/api/chat` (관리자 전용).

`server/chatbot/` 는 CLI 로만 쓰이던 조회 엔진이다. 이 모듈이 그것을 웹에 노출하는
**유일한** 지점이며, 조회 로직은 여기 없다(전부 chatbot.agent.answer_web).

세 가지가 이 얇은 라우트의 존재 이유다:
1. **master 전용 게이트** — 아직 테스트 단계라 admin 로그인 PC 에만 연다. UI 에서
   버튼을 숨기는 것은 편의일 뿐이고, 실효 경계는 여기 `_is_master()` 404 다
   (routes_misc.debug_threads 와 같은 태도 — 존재 자체를 숨긴다).
2. **동시실행 상한** — waitress 스레드가 13개뿐인데 LLM 플래너는 최대 30초를 기다린다.
   챗 요청이 스레드를 다 물면 검색결과·세션 조회까지 굶는다. 세마포어 3개로 막고,
   못 잡으면 429 로 즉시 돌려보낸다(대기열에 쌓아 두지 않는다).
3. **계측** — 총/대기/LLM 소요를 3분해해 기록한다. 총 소요만 남기면 "느린 게 LLM 탓인지
   동시성 제한 탓인지" 를 관리자 탭에서 가릴 수 없다(web_report/build_log.py 와 같은 이유).
"""
import logging
import threading
import time

from flask import abort, jsonify, request

from auth_identity import current_user as _current_user
from database import report_db
from report.report_extension import report_bp
from report.security import _client_meta, _is_master, _require_csrf, _validate_session_id

_log = logging.getLogger(__name__)

_MAX_QUESTION = 500
_CONCURRENCY = 3        # 동시 처리 상한 (waitress 13스레드 보호)
_ACQUIRE_TIMEOUT = 10   # 초 — 이보다 밀리면 기다리게 두지 않고 429

_CHAT_SEM = threading.BoundedSemaphore(_CONCURRENCY)
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT = 0


def chat_runtime():
    """관리자 탭용 실시간 현황 — 진행 중 건수와 남은 슬롯."""
    with _INFLIGHT_LOCK:
        inflight = _INFLIGHT
    return {"inflight": inflight, "concurrency": _CONCURRENCY,
            "free_slots": max(0, _CONCURRENCY - inflight)}


@report_bp.post("/api/chat")
def api_chat():
    if not _is_master():
        abort(404)
    _require_csrf()

    body = request.get_json(force=True, silent=True) or {}
    question = str(body.get("question") or "").strip()
    if not question:
        abort(400, "question is required")
    if len(question) > _MAX_QUESTION:
        abort(400, f"question too long (max {_MAX_QUESTION})")
    context = body.get("context") or {}
    ctx_sid = str(context.get("session_id") or "").strip() or None
    if ctx_sid:
        _validate_session_id(ctx_sid)

    viewer = _current_user()
    client_ip, _ = _client_meta()
    started = time.perf_counter()
    acquired_at = time.perf_counter()
    if not _CHAT_SEM.acquire(timeout=_ACQUIRE_TIMEOUT):
        wait_ms = int((time.perf_counter() - acquired_at) * 1000)
        report_db.log_chat(question=question, user=viewer, client_ip=client_ip,
                           context_session_id=ctx_sid, wait_ms=wait_ms,
                           total_ms=wait_ms, result="busy")
        return jsonify({"error": "챗봇이 혼잡합니다 — 잠시 후 다시 시도해 주세요."}), 429
    wait_ms = int((time.perf_counter() - acquired_at) * 1000)

    global _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT += 1
    try:
        from chatbot import agent as chatbot_agent
        result = chatbot_agent.answer_web(
            question, viewer=viewer,
            see_all_private=True,          # master 확정 후라 전 세션 조회 가능
            context_session_id=ctx_sid)
    except Exception as exc:
        _log.exception("chatbot 응답 실패: %s", question[:120])
        report_db.log_chat(question=question, user=viewer, client_ip=client_ip,
                           context_session_id=ctx_sid, wait_ms=wait_ms,
                           total_ms=int((time.perf_counter() - started) * 1000),
                           result=f"error:{type(exc).__name__}")
        return jsonify({"error": "답변 생성 중 오류가 발생했습니다."}), 500
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT -= 1
        _CHAT_SEM.release()

    plan = result.get("plan") or {}
    web = result.get("web") or {}
    report_db.log_chat(question=question, user=viewer, client_ip=client_ip,
                       context_session_id=ctx_sid, answer=result.get("text"),
                       intent=plan.get("intent"), planner=plan.get("planner"),
                       plan=plan, steps=result.get("steps"),
                       total_ms=int((time.perf_counter() - started) * 1000),
                       wait_ms=wait_ms, llm_ms=plan.get("llm_ms"), result="ok")
    return jsonify({"text": result.get("text") or "",
                    "links": web.get("links") or [],
                    "choices": web.get("choices") or [],
                    "building": bool(web.get("building")),
                    "plan": plan})

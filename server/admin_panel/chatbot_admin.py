"""웹 챗봇 사용 현황·부하 조회 — 관리자 패널 Chatbot 탭.

기록은 report_chatbot_log 한 곳뿐이다(database/chatbot_log.py). 여기서는 그 집계와
실시간 상태(진행 중 건수·남은 슬롯·LLM 설정 여부)를 합쳐 타일용 dict 를 만든다.
"""
import logging

from database import report_db

_log = logging.getLogger(__name__)


def overview(hours=24):
    """타일 — 최근 N시간 집계 + 실시간 동시성 + LLM 플래너 설정 여부."""
    data = {"stats": report_db.chat_stats(hours)}

    # 라우트 모듈이 세마포어를 들고 있다. 조회 실패가 탭 전체를 깨지 않게 개별 try —
    # 관리자 화면은 구성요소 하나가 죽어도 나머지를 보여줘야 한다.
    try:
        from report.routes_chat import chat_runtime
        data["runtime"] = chat_runtime()
    except Exception:
        _log.debug("chat_runtime 조회 실패", exc_info=True)
        data["runtime"] = {}

    try:
        from chatbot import planner
        data["llm_enabled"] = bool(planner.llm_enabled())
    except Exception:
        _log.debug("planner.llm_enabled 조회 실패", exc_info=True)
        data["llm_enabled"] = False
    return data


def list_logs(q=None, limit=50, offset=0, errors_only=False):
    """질문/답변 이력 (최신순). limit/offset 클램프는 chatbot_log 가 한다.

    errors_only 는 실패만 추린다 — 성공 기록 사이에서 오류를 찾아 헤매지 않도록.
    """
    return report_db.list_chats(q=q, limit=limit, offset=offset,
                                errors_only=bool(errors_only))

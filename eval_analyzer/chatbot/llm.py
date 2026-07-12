"""LLM 어댑터 — eval_engine.config.EVAL_LLM_* (OpenAI 호환) 를 LangChain 으로 매핑.

모델/endpoint 하드코딩 금지: 값은 전부 config 에서만 읽는다.
값이 비어(LLM 미설정) 있으면 None 을 반환 → agent 가 규칙기반 router 로 fallback.
langchain_openai 는 지연 import (미설치 시 명확한 안내).
"""
from eval_engine import config
from eval_engine.llm_client import is_enabled


def build_llm():
    """설정돼 있으면 ChatOpenAI, 아니면 None.

    EVAL_LLM_ENDPOINT 는 OpenAI 호환 base URL(예: http://host:port/v1).
    """
    if not is_enabled():
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:
        raise ImportError(
            "langchain-openai 미설치. `pip install -r chatbot/requirements.txt`"
        ) from e
    return ChatOpenAI(
        base_url=config.EVAL_LLM_ENDPOINT,
        api_key=config.EVAL_LLM_API_KEY or "not-needed",
        model=config.EVAL_LLM_MODEL,
        timeout=config.EVAL_LLM_TIMEOUT,
        temperature=0,
    )

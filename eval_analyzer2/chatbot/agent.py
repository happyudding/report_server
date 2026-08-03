"""챗봇 최상위 진입점 — ask(question) -> str.

파이프라인 조립:
  LLM 설정 O → tool-calling agent (LLM 이 Tool 선택·실행·요약)
  LLM 설정 X → 규칙기반 router (실제 DB 결과 반환)
LLM 경로에서 예외가 나면 router 로 안전 fallback.
"""
from . import router
from .llm import build_llm

_SYSTEM = (
    "너는 반도체 fail-item 평가 결과 DB(eval.db) 조회 도우미다. "
    "제공된 Tool 로만 데이터를 조회하고, 임의로 값을 지어내지 마라. "
    "조회 결과를 근거로 한국어로 간결하게 답하라. "
    "정보가 부족하면 어떤 Tool 인자가 더 필요한지 되물어라."
)


def build_agent(llm):
    """tool-calling AgentExecutor. 지연 import(langchain 미설치 시 안내)."""
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    from .tools import build_tools

    tools = build_tools()
    prompt = ChatPromptTemplate.from_messages([
        ("system", _SYSTEM),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=False)


def ask(question: str) -> str:
    """자연어 질문 → 답변 텍스트. LLM off/실패 시 규칙기반 router."""
    llm = build_llm()
    if llm is None:
        return router.route(question)
    try:
        executor = build_agent(llm)
        result = executor.invoke({"input": question})
        return result.get("output", "").strip() or router.route(question)
    except Exception as e:  # LLM/네트워크/파싱 실패 → 안전 fallback
        return f"[LLM 경로 실패 → 규칙기반 fallback: {type(e).__name__}]\n" + router.route(question)

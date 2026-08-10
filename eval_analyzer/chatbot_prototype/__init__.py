"""eval.db 자연어 조회 챗봇 (프로토타입 뼈대 — **보류 중**).

독립 top-level 패키지 — eval_engine(config/store) 만 참조, report_server 무관.
진입점: chatbot_prototype.ask(question) / `python -m chatbot_prototype.cli`.
자세한 구조는 README.md.

⚠ 운영 챗봇은 여기가 아니라 **server/chatbot/** 이다. 이 패키지는 참조용으로 남긴
LangChain 실험이며 어디에서도 import 되지 않는다(langchain 미설치라 LLM 경로는 실행 불가).
2026-08-10 에 `chatbot` → `chatbot_prototype` 으로 개명했다: 같은 top-level 이름이
server/chatbot 과 충돌해 운영에서
`AttributeError: module 'chatbot.agent' has no attribute 'answer_web'` 를 냈기 때문이다.
"""
from .agent import ask

__all__ = ["ask"]

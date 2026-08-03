"""eval.db 자연어 조회 챗봇 (프로토타입 뼈대).

독립 top-level 패키지 — eval_engine(config/store) 만 참조, report_server 무관.
진입점: chatbot.ask(question) / `python -m chatbot.cli`.
자세한 구조는 README.md.
"""
from .agent import ask

__all__ = ["ask"]

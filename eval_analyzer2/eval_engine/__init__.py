"""eval_engine — 반도체 fail-item 평가 분석 엔진 (독립 라이브러리).

공개 진입점: evaluate(). report_server 를 import 하지 않는다.
설계 문서: ../docs/ (DB_SCHEMA / 5STAGE_COLUMNS / INTEGRATION_CONTRACT / CODE_TO_PORT 등).
"""
from .api import evaluate

__all__ = ["evaluate"]
__version__ = "0.0.1"  # scaffold

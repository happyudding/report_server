"""queries.py 조회 함수 → LangChain StructuredTool 래핑.

LLM 이 자연어에서 Tool + 인자를 고르도록 스키마(pydantic)를 붙인다.
임의 SQL 없음 — 노출되는 조회는 queries 의 4종뿐(read-only). langchain 의존 계층.
"""
from . import queries


def build_tools():
    """StructuredTool 리스트. 지연 import(langchain 미설치 시 안내)."""
    try:
        from langchain_core.tools import StructuredTool
        from pydantic import BaseModel, Field
    except ImportError as e:
        raise ImportError(
            "langchain-core/pydantic 미설치. `pip install -r chatbot/requirements.txt`"
        ) from e

    class SearchCasesArgs(BaseModel):
        product: str | None = Field(None, description="제품명(부분일치)")
        item: str | None = Field(None, description="item 이름(item_canonical 부분일치)")
        status: str | None = Field(None, description="CRITICAL|MAJOR|MINOR|MONITOR")
        item_class: str | None = Field(None, description="category_major|value_type|bin")
        limit: int = Field(20, description="최대 건수")

    class CaseDetailArgs(BaseModel):
        case_id: str = Field(..., description="fail_case.case_id (sha256 hex)")

    class PrecedentArgs(BaseModel):
        item_name: str = Field(..., description="선례를 찾을 item 이름")
        value_type: str | None = Field(None, description="V|A|Hz|CODE|P_F 등(미지정 시 추정)")
        family_product: str | None = Field(None, description="보조 필터(제품군)")

    class StatsArgs(BaseModel):
        group_by: str = Field("status", description="status|product|product_type|item_class")

    return [
        StructuredTool.from_function(
            func=queries.search_cases, name="search_cases",
            description="제품/item/status 조건으로 fail case 를 검색해 최신 평가와 함께 반환.",
            args_schema=SearchCasesArgs),
        StructuredTool.from_function(
            func=queries.get_case_detail, name="get_case_detail",
            description="case_id 로 단일 case 의 전체 맥락(평가·metrics·signature·label·outcome).",
            args_schema=CaseDetailArgs),
        StructuredTool.from_function(
            func=queries.find_precedents, name="find_precedents",
            description="item 이름으로 과거 유사 사례(선례)를 검색. 조치/결과/코멘트 포함.",
            args_schema=PrecedentArgs),
        StructuredTool.from_function(
            func=queries.stats_summary, name="stats_summary",
            description="status/product/product_type/item_class 축으로 case 수 집계.",
            args_schema=StatsArgs),
    ]

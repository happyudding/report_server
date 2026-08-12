"""HONEY 공개 기능 카탈로그 조회 툴.

보고서·평가 데이터와 완전히 분리된 정적 카탈로그만 읽는다. 기능 질문은 이 툴 하나로
답해 LLM이나 세션·이력 DB에 의존하지 않는다.
"""
from __future__ import annotations

import help_catalog

_OVERVIEW_IDS = (
    "landing", "report-search", "normal-mode", "temperature-mode", "rawdata-hub",
    "summary", "issue-table", "map-analysis", "engr-chatbot",
)


def search_help_features(query, limit=5):
    """자연어 기능 검색. 일반 도움 요청이면 대표 기능 목록을 돌려준다."""
    limit = max(1, min(int(limit or 5), 10))
    if help_catalog.is_generic_feature_question(query):
        rows = [help_catalog.get_feature(feature_id) for feature_id in _OVERVIEW_IDS[:limit]]
        return {"features": [row for row in rows if row], "generic": True,
                "catalog_version": help_catalog.CATALOG_VERSION}
    rows = help_catalog.search_features(query, limit=limit)
    return {"features": rows, "generic": False,
            "catalog_version": help_catalog.CATALOG_VERSION}


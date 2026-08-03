"""provider-agnostic 선례검색 어댑터. SQL(기본) / RAG 교체형.

find_precedents 뒤의 유일한 백엔드 경계. 백엔드 선택은 config.EVAL_PRECEDENT_BACKEND.
RAG 붙이는 법은 docs/PRECEDENT_RAG_HANDOFF.md 참조. 반환 dict 계약(엄수):
  list[dict], 관련도 내림차순, 각 dict 에 action / result / human_comment key.
"""
from . import config, store


def backend() -> str:
    return config.EVAL_PRECEDENT_BACKEND


def search(case_ctx: dict, sig_result: dict) -> list:
    """선례 list 반환. 백엔드에 위임(기본 sql, 'rag' 면 _rag_search)."""
    if backend() == "rag":
        return _rag_search(case_ctx, sig_result)
    return _sql_search(case_ctx, sig_result)


def _sql_search(case_ctx: dict, sig_result: dict) -> list:
    """기존 SQL 선례검색. DB_SCHEMA §9. bin 은 매칭 조건에서 제외."""
    return store.search_precedents(
        case_ctx["value_type"], case_ctx["item_canonical"],
        family_product=case_ctx.get("family_product"),
        exclude_case_id=case_ctx.get("case_id"))


def _rag_search(case_ctx: dict, sig_result: dict) -> list:
    """RAG 선례검색 — report_server 담당자가 구현. (현재 스텁)

    입력:
      case_ctx: bin(int) / value_type(str) / item_canonical(str, 쿼리 핵심) /
                family_product(str|None) / case_id(str, 자기 자신 제외용)
      sig_result: 발화 signature 들(쿼리 보강용, 선택)
    반환(엄수): list[dict], 관련도 내림차순. 각 dict 최소 key:
      action / result / human_comment. 결과 없으면 []. (precedents[0]=최상위 선례)
    endpoint/모델 하드코딩 금지 — config.EVAL_PRECEDENT_RAG_* 사용.
    """
    raise NotImplementedError(
        "RAG 선례검색 미구현 — docs/PRECEDENT_RAG_HANDOFF.md 참조")

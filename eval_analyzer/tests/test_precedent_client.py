"""L5 선례검색 어댑터(precedent_client) — store 로 넘기는 **인자 배선**을 고정한다.

store.search_precedents 쪽 동작은 test_store.py 가 본다. 여기서 보는 것은 그 반대편 —
case_ctx / sig_result 에 들어 있는 값이 실제로 store 호출까지 전달되는가다. 자기 세션 제외
(시간 누출 차단)와 top-k 상한은 인자를 하나 빠뜨리는 것만으로 조용히 무력화되는데, store
단독 테스트로는 그걸 잡을 수 없다.
"""
import pytest

from eval_engine import config, precedent_client, store
from test_store import _seed_precedent


def _case_ctx(**kw):
    """선례검색이 읽는 축만 담은 case_ctx."""
    c = {"value_type": "V", "item_canonical": "vref_trim", "family_product": "SOC",
         "case_id": "CASE_SELF", "session_id": "S123", "analysis_key": "AK9"}
    c.update(kw)
    return c


def test_sql_search_forwards_every_exclusion_arg(monkeypatch):
    """case_ctx 의 6개 축 + 발화 signature + top-k 상한이 전부 store 로 넘어간다."""
    seen = {}

    def _capture(value_type, item_canonical, **kw):
        seen.update(kw, value_type=value_type, item_canonical=item_canonical)
        return []

    monkeypatch.setattr(store, "search_precedents", _capture)
    sig_result = {"signatures": [{"id": "SUBPOP_GAP"}, {"id": "OUTLIER_WARN"}]}
    precedent_client.search(_case_ctx(), sig_result)

    assert seen["value_type"] == "V"
    assert seen["item_canonical"] == "vref_trim"
    assert seen["family_product"] == "SOC"
    assert seen["exclude_case_id"] == "CASE_SELF"
    assert seen["exclude_session_id"] == "S123"      # 시간 누출 차단
    assert seen["exclude_analysis_key"] == "AK9"     # 〃
    assert seen["fired_signatures"] == ["SUBPOP_GAP", "OUTLIER_WARN"]
    assert seen["limit"] == config.EVAL_PRECEDENT_TOPK


def test_sql_search_without_session_meta_passes_none(monkeypatch):
    """session_id/analysis_key 가 없는 구 호출부도 KeyError 없이 None 으로 넘어간다."""
    seen = {}
    monkeypatch.setattr(store, "search_precedents",
                        lambda *a, **kw: seen.update(kw) or [])
    ctx = _case_ctx()
    del ctx["session_id"], ctx["analysis_key"]
    precedent_client.search(ctx, {"signatures": []})
    assert seen["exclude_session_id"] is None
    assert seen["exclude_analysis_key"] is None
    assert seen["fired_signatures"] == []


def test_backend_default_is_sql_and_rag_is_stub(monkeypatch):
    assert precedent_client.backend() == "sql"
    monkeypatch.setattr(config, "EVAL_PRECEDENT_BACKEND", "rag")
    assert precedent_client.backend() == "rag"
    with pytest.raises(NotImplementedError):
        precedent_client.search(_case_ctx(), {"signatures": []})


def test_search_applies_topk_cap(fresh_db, monkeypatch):
    """선례가 상한보다 많아도 top-k 만 돌려준다 — 코멘트 프롬프트 비대화 방지."""
    monkeypatch.setattr(config, "EVAL_PRECEDENT_TOPK", 5)
    with store.get_conn() as conn:
        for i in range(7):
            _seed_precedent(conn, product=f"P{i}", item_canon="vref_trim",
                            comment=f"사례 {i}")
    res = precedent_client.search(_case_ctx(case_id=None, session_id=None,
                                            analysis_key=None), {"signatures": []})
    assert len(res) == 5
    # 계약: 각 dict 에 action/result/human_comment (docs/PRECEDENT_RAG_HANDOFF.md)
    assert {"action", "result", "human_comment"} <= set(res[0])

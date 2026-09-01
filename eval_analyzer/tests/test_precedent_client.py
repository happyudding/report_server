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


def test_evaluate_queries_precedents_once_per_param_set(fresh_db, monkeypatch):
    """evaluate 1회에서 선례 **SQL 은 파라미터 조합당 1번**만 돈다 (2026-09-02).

    그 쿼리의 파라미터는 value_type·family·exclude 세션뿐이라 item 이 달라도 결과가
    같은데, 종전에는 case 마다 DB 를 새로 열고 같은 쿼리를 되풀이했다(실측 L5 가 L2 의
    5배 — 선례가 0건인데도). api.evaluate 가 case 에 실어 주는 `_precedent_cache` 가
    그 반복을 없앤다. **case 별 결과(유사도·정렬)는 종전과 같아야 하므로** 반환값도 함께 본다.
    """
    from eval_engine import api

    with store.get_conn() as conn:
        _seed_precedent(conn, item_canon="vref_trim", comment="과거 vref 사례")

    opened = []
    real_get_conn = store.get_conn
    monkeypatch.setattr(store, "get_conn",
                        lambda *a, **kw: (opened.append(1), real_get_conn(*a, **kw))[1])

    # item 3종(전부 value_type=V) × fail 이 있는 raw_table → case 3건.
    rows = []
    items = ["VREF_TRIM", "VREF_BIAS", "VREF_TRIM_OFFSET"]
    for i in range(20):
        rows.append({"DUT": i + 1, "XCoord": i % 5, "YCoord": i // 5, "Bin": 1,
                     "Serial": f"S{i}", **{it: 1.20 for it in items}})
    for i in range(4):
        rows.append({"DUT": 100 + i, "XCoord": 50 + i, "YCoord": 50 + i, "Bin": 18,
                     "Serial": f"F{i}", **{it: 1.55 + 0.02 * i for it in items}})
    run_input = {
        "meta": {"product_name": "PX", "family_product": "SOC", "product_type": "PMIC",
                 "revision": 0.0, "lot_id": "L9", "wafer_number": 1},
        "raw_table": {"meta_columns": ["DUT", "XCoord", "YCoord", "Bin", "Serial"],
                      "item_columns": items,
                      "units": {it: "V" for it in items},
                      "lower_limit": {it: 1.0 for it in items},
                      "upper_limit": {it: 1.4 for it in items},
                      "rows": rows},
    }
    result = api.evaluate(run_input, persist=False)

    assert len(result["cases"]) >= 2, "case 가 여러 건이어야 반복 호출을 잴 수 있다"
    assert len(opened) == 1, f"선례 쿼리가 {len(opened)}회 — case 마다 DB 를 다시 열었다"
    # 캐시를 써도 선례는 정상적으로 붙는다(유사도 필터는 case 별로 계속 돈다).
    assert any(c["precedents"] for c in result["cases"])


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

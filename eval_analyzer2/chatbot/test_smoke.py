"""챗봇 뼈대 스모크 — 임시 eval.db 로 queries 4종 + ask() 규칙기반 fallback.

langchain 미설치여도 통과(코어/router 경로만 탄다). LLM 경로는 여기서 검증 안 함.
DB 격리: config.DB_PATH 를 tmp 로 monkeypatch(호출 시점 조회라 충분 — tests/conftest 와 동일 규칙).
"""
import pytest

from eval_engine import config, store

from chatbot import queries, router
from chatbot.agent import ask

_PROD = "PRODUCT0001234"


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "eval.db")
    monkeypatch.setattr(config, "EVAL_LLM_ENABLED", False)  # 규칙기반 경로 강제
    store.init_db()
    case_id = _seed()
    return case_id


def _seed():
    store.upsert_product_master(
        {"product_name": _PROD, "product_type": "PMIC", "family_product": "SOC"})
    item_id = store.upsert_item_master(
        "vreftrim", "VREF_TRIM", "vref", "trim", "TRIM", "", "V", "V")
    run_id = store.create_ingest_run(
        {"product_name": _PROD, "lot_id": "LOT1", "wafer_number": 1})
    case_id = store.make_case_id(_PROD, "LOT1", 1, item_id, 5, 1.0)
    store.upsert_fail_case(case_id, _PROD, "LOT1", 1, item_id, 5, 1.0, "TRIM|V|5")
    store.link_run_case(run_id, case_id)
    store.save_raw_metrics(case_id, run_id, {
        "cpk": 0.8, "yield": 0.95, "mean": 1.0, "stdev": 0.1,
        "fail_count": 5, "total_count": 100})
    eval_id = store.save_evaluation(
        case_id, run_id, config.ENGINE_VERSION, "", "MAJOR", 0.7, "full", "재검토 필요")
    store.save_case_signature(eval_id, [{"id": "LOW_CPK", "role": "primary", "score": 0.9}])
    label_id = store.insert_label(
        case_id, eval_id, "MAJOR", "equipment", "", 1, 0, "retest 후 정상",
        "tester", "rev", "good")
    store.insert_case_outcome(
        case_id, label_id, "retest", "", "recovered_normal", "me", None, "ok")
    return case_id


def test_search_cases(seeded_db):
    rows = queries.search_cases(status="MAJOR")
    assert any(r["case_id"] == seeded_db for r in rows)
    assert rows[0]["status"] == "MAJOR"


def test_get_case_detail(seeded_db):
    d = queries.get_case_detail(seeded_db)
    assert d["evaluation"]["status"] == "MAJOR"
    assert d["metrics"] and d["metrics"][0]["cpk"] == 0.8
    assert any(s["signature"] == "LOW_CPK" for s in d["signatures"])
    assert d["labels"] and d["outcomes"]


def test_find_precedents(seeded_db):
    rows = queries.find_precedents("vreftrim", value_type="V")
    assert any(r["case_id"] == seeded_db for r in rows)


def test_stats_summary(seeded_db):
    rows = queries.stats_summary("status")
    assert {r["key"]: r["count"] for r in rows}.get("MAJOR") == 1


def test_stats_summary_bad_group_by(seeded_db):
    with pytest.raises(ValueError):
        queries.stats_summary("nope")


def test_ask_fallback_stats(seeded_db):
    out = ask("MAJOR 케이스 통계")
    assert "MAJOR" in out


def test_router_precedent(seeded_db):
    out = router.route("vreftrim 선례")
    assert "vreftrim" in out


def test_queries_no_db(monkeypatch, tmp_path):
    """DB 파일 없으면 조회는 빈 결과(크래시 없음)."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "absent.db")
    assert queries.search_cases() == []
    assert queries.get_case_detail("x") == {}
    assert queries.stats_summary("status") == []

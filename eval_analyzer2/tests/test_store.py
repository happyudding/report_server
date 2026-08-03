"""store CRUD + search_precedents 단독 테스트 (모두 tmp DB)."""
import pytest

from eval_engine import store
from eval_engine.pipeline._rules import outcome_label, validate_outcome


def _seed_precedent(conn, *, product="P1", item_raw="VREF_TRIM", item_canon="vref_trim",
                    value_type="V", bin_=18, family="SOC",
                    action="retest", result="recovered_normal", comment="과거 정상복귀"):
    store.upsert_product_master(
        {"product_name": product, "family_product": family, "product_type": "PMIC"}, conn=conn)
    item_id = store.upsert_item_master(item_canon, item_raw, None, None, "TRIM", None,
                                       value_type, None, conn=conn)
    case_id = store.make_case_id(product, "L1", 1, item_id, bin_, 0.0)
    store.upsert_fail_case(case_id, product, "L1", 1, item_id, bin_, 0.0,
                           f"TRIM|{value_type}|{bin_}", conn=conn)
    label_id = store.insert_label(case_id, None, "MAJOR", "equipment", None, 0, 0,
                                  comment, "seed", None, "seed", conn=conn)
    store.insert_case_outcome(case_id, label_id, action, None, result, None, None, None, conn=conn)
    return case_id


def test_product_item_roundtrip(fresh_db):
    with store.get_conn() as conn:
        store.upsert_product_master(
            {"product_name": "PX", "family_product": "F", "product_type": "PMIC"}, conn=conn)
        item_id = store.upsert_item_master("vref", "VREF", "vref", None, "NON_TRIM", None,
                                           "V", "V", conn=conn)
        store.upsert_item_alias("VREF", item_id, conn=conn)
        assert store.resolve_item_id("VREF", conn=conn) == item_id
        # upsert 멱등 — 같은 canonical 재삽입 시 동일 id
        item_id2 = store.upsert_item_master("vref", "VREF", "vref", None, "NON_TRIM", None,
                                            "V", "V", conn=conn)
        assert item_id2 == item_id


def test_make_case_id_deterministic():
    a = store.make_case_id("P", "L", "W", 1, 18, "EVT0")
    b = store.make_case_id("P", "L", "W", 1, 18, "EVT0")
    c = store.make_case_id("P", "L", "W", 1, 19, "EVT0")
    assert a == b
    assert a != c


def test_search_precedents_matches_similar_name(fresh_db):
    with store.get_conn() as conn:
        _seed_precedent(conn, item_raw="VREF_TRIM", item_canon="vref_trim")
    # 동일 value_type + 유사 이름(vref_trim vs vref_trim_p2)
    res = store.search_precedents("V", "vref_trim_p2")
    assert len(res) >= 1
    assert res[0]["action"] == "retest"
    assert res[0]["result"] == "recovered_normal"
    assert res[0]["similarity"] >= 0.70


def test_search_precedents_excludes_dissimilar_name(fresh_db):
    with store.get_conn() as conn:
        _seed_precedent(conn, item_raw="VREF_TRIM", item_canon="vref_trim")
    # 전혀 다른 이름 → 유사도 < 0.70 → 제외
    res = store.search_precedents("V", "iddq_leakage_current_xyz")
    assert res == []


def test_search_precedents_excludes_self(fresh_db):
    with store.get_conn() as conn:
        case_id = _seed_precedent(conn, item_canon="vref_trim")
    res_all = store.search_precedents("V", "vref_trim")
    assert len(res_all) == 1
    res_excl = store.search_precedents("V", "vref_trim", exclude_case_id=case_id)
    assert res_excl == []


def test_search_precedents_dedup_latest_label(fresh_db):
    with store.get_conn() as conn:
        case_id = _seed_precedent(conn, comment="첫 라벨")
        # 같은 case 에 최신 label + outcome 추가 → label×outcome 곱 대신 case 당 1행(최신 기준)
        lbl2 = store.insert_label(case_id, None, "MINOR", "spec", None, 0, 0,
                                  "최신 라벨", "seed", None, "seed", conn=conn)
        store.insert_case_outcome(case_id, lbl2, "spec_release", None, "improved",
                                  None, None, None, conn=conn)
    res = store.search_precedents("V", "vref_trim")
    assert len(res) == 1
    assert res[0]["human_comment"] == "최신 라벨"
    assert res[0]["action"] == "spec_release"


def test_search_precedents_value_type_filter(fresh_db):
    with store.get_conn() as conn:
        _seed_precedent(conn, item_canon="vref_trim", value_type="V")
    # 같은 이름이지만 value_type 다름 → 후보에서 제외
    assert store.search_precedents("A", "vref_trim") == []


def test_search_precedents_returns_product_name(fresh_db):
    with store.get_conn() as conn:
        _seed_precedent(conn, product="P1", item_canon="vref_trim", family="SOC")
    res = store.search_precedents("V", "vref_trim")
    assert res[0]["product_name"] == "P1"
    assert res[0]["family_product"] == "SOC"


def test_search_precedents_ignores_bin(fresh_db):
    with store.get_conn() as conn:
        _seed_precedent(conn, item_canon="vref_trim", bin_=3)
    # 검색 시 bin 인자 자체가 없음 — 다른 bin 으로 seed 돼도 매칭됨
    res = store.search_precedents("V", "vref_trim")
    assert len(res) == 1


def test_search_precedents_excludes_other_family(fresh_db):
    with store.get_conn() as conn:
        _seed_precedent(conn, item_canon="vref_trim", family="SOC")
    assert store.search_precedents("V", "vref_trim", family_product="MEMORY") == []


def test_search_precedents_returns_all_matches_no_cap(fresh_db):
    with store.get_conn() as conn:
        for i in range(7):
            store.upsert_product_master(
                {"product_name": f"P{i}", "family_product": "SOC",
                 "product_type": "PMIC"}, conn=conn)
            item_id = store.upsert_item_master("vref_trim", "VREF_TRIM", None, None,
                                                "TRIM", None, "V", None, conn=conn)
            case_id = store.make_case_id(f"P{i}", "L1", 1, item_id, 18, 0.0)
            store.upsert_fail_case(case_id, f"P{i}", "L1", 1, item_id, 18, 0.0,
                                   "TRIM|V|18", conn=conn)
            label_id = store.insert_label(case_id, None, "MAJOR", "equipment", None, 0, 0,
                                          f"comment {i}", "seed", None, "seed", conn=conn)
            store.insert_case_outcome(case_id, label_id, "retest", None,
                                      "recovered_normal", None, None, None, conn=conn)
    res = store.search_precedents("V", "vref_trim")
    assert len(res) == 7  # limit 기본이 None → 전체 반환(과거의 5건 cap 없음)


def test_outcome_label_ko_and_group():
    assert outcome_label("action", "retest") == {"ko": "재측정", "group": "재검증"}
    assert outcome_label("result", "false_fail")["ko"] == "실불량아님"
    assert outcome_label("action", None) == {}
    assert outcome_label("action", "no_such_code") == {}


def test_validate_outcome_accepts_vocab_and_none():
    validate_outcome("false_fail", "inconclusive")  # 신규 어휘 통과
    validate_outcome(None, None)                     # None 통과
    validate_outcome("other", "other")               # 이스케이프값 통과


def test_validate_outcome_rejects_unknown():
    with pytest.raises(ValueError):
        validate_outcome("bogus_action", "recovered_normal")
    with pytest.raises(ValueError):
        validate_outcome("retest", "bogus_result")


def test_insert_case_outcome_rejects_unknown_vocab(fresh_db):
    with store.get_conn() as conn:
        with pytest.raises(ValueError):
            _seed_precedent(conn, action="bogus_action")


# ── 스키마 v4 ────────────────────────────────────────────────────────────────
def test_schema_v4_user_version_and_objects(fresh_db):
    with store.get_conn() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "eval_precedent" in tables
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_item_master_value_type", "idx_product_master_family",
                "idx_fail_case_item"} <= indexes
        assert "updated_at" in {r[1] for r in conn.execute("PRAGMA table_info(evaluation)")}
        assert "created_at" in {r[1] for r in conn.execute("PRAGMA table_info(case_outcome)")}


def test_migrate_v3_to_v4_idempotent(fresh_db):
    with store.get_conn() as conn:
        store._migrate_v3_to_v4(conn)  # 이미 v4 인 DB 에 재적용 — no-op 이어야 함
        store._migrate_v3_to_v4(conn)


def test_case_outcome_created_at_populated(fresh_db):
    with store.get_conn() as conn:
        case_id = _seed_precedent(conn)
        row = conn.execute("SELECT created_at FROM case_outcome WHERE case_id=?",
                           (case_id,)).fetchone()
        assert row["created_at"] is not None


def test_evaluation_updated_at_set_on_resave(fresh_db):
    with store.get_conn() as conn:
        eval_id = store.save_evaluation("C1", 1, "ev1", None, "MAJOR", 0.9, "full",
                                        "첫 판정", conn=conn)
        row = conn.execute("SELECT updated_at FROM evaluation WHERE eval_id=?",
                           (eval_id,)).fetchone()
        assert row["updated_at"] is None          # 최초 insert
        eval_id2 = store.save_evaluation("C1", 1, "ev1", None, "MINOR", 0.9, "full",
                                         "재판정", conn=conn)
        assert eval_id2 == eval_id                # 같은 키 → upsert
        row = conn.execute("SELECT status, updated_at FROM evaluation WHERE eval_id=?",
                           (eval_id,)).fetchone()
        assert row["status"] == "MINOR"
        assert row["updated_at"] is not None      # 갱신 시각 기록


def test_save_eval_precedents_roundtrip(fresh_db):
    precedents = [{"case_id": "PC1", "similarity": 0.95},
                  {"case_id": "PC2", "similarity": 0.80},
                  {"similarity": 0.99}]  # case_id 없는 행(RAG 등)은 skip
    with store.get_conn() as conn:
        store.save_eval_precedents(7, precedents, conn=conn)
        rows = conn.execute("""SELECT precedent_case_id, rank, similarity
                               FROM eval_precedent WHERE eval_id=7 ORDER BY rank""").fetchall()
        assert [(r["precedent_case_id"], r["rank"]) for r in rows] == [("PC1", 1), ("PC2", 2)]
        store.save_eval_precedents(7, precedents, conn=conn)  # 재저장 idempotent
        n = conn.execute("SELECT COUNT(*) FROM eval_precedent WHERE eval_id=7").fetchone()[0]
        assert n == 2

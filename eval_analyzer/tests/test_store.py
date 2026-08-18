"""store CRUD + search_precedents 단독 테스트 (모두 tmp DB)."""
import pytest

from eval_engine import store
from eval_engine.pipeline import features as features_module
from eval_engine.pipeline._rules import outcome_label, validate_outcome


def _seed_precedent(conn, *, product="P1", item_raw="VREF_TRIM", item_canon="vref_trim",
                    value_type="V", bin_=18, family="SOC",
                    action="retest", result="recovered_normal", comment="과거 정상복귀"):
    """선례 1건(product+item+case+label+outcome)을 심고 case_id 반환.

    search_precedents 가 인용할 수 있으려면 label 의 human_comment 까지 있어야 하므로
    다섯 테이블을 한 번에 엮는다. 키워드 인자로 값을 바꿔 여러 변형을 만든다.
    """
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


def test_make_case_id_condition_backward_compatible():
    """빈 condition 은 해시 재료에서 빠진다 — 기존 case_id 가 그대로여야 한다.

    이게 깨지면 운영 eval.db 의 모든 case 가 고아가 되고 선례·라벨 매칭이 통째로 끊긴다.
    """
    import hashlib
    base = store.make_case_id("P", "L", None, 7, None, 0.0)
    assert base == store.make_case_id("P", "L", None, 7, None, 0.0, "")
    assert base == hashlib.sha256(b"P|L|None|7|None|0.0").hexdigest()
    # 조건 축이 붙으면 별개 case (TEMP 코멘트가 ETC 코멘트를 덮어쓰던 원인)
    assert base != store.make_case_id("P", "L", None, 7, None, 0.0, "TEMP")


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


def test_search_precedents_excludes_own_session_and_analysis_key(fresh_db):
    """같은 세션/analysis_key 로 적재된 사례는 선례에서 제외 — 시간 누출 차단."""
    with store.get_conn() as conn:
        case_id = _seed_precedent(conn, item_canon="vref_trim")
        run_id = store.create_ingest_run(
            {"product_name": "P1", "lot_id": "L1", "session_id": "S123",
             "analysis_key": "AK9"}, conn=conn)
        store.link_run_case(run_id, case_id, conn=conn)
    assert len(store.search_precedents("V", "vref_trim")) == 1
    assert store.search_precedents("V", "vref_trim", exclude_session_id="S123") == []
    assert store.search_precedents("V", "vref_trim", exclude_analysis_key="AK9") == []
    # 다른 세션/키는 영향 없음
    assert len(store.search_precedents("V", "vref_trim",
                                       exclude_session_id="OTHER")) == 1


def test_search_precedents_signature_boost(fresh_db):
    """발화 signature 가 겹치는 선례가 (comment 동급이면) 먼저 온다 — 하드필터 아님."""
    with store.get_conn() as conn:
        # 선례 2건: A(EDGE_FAIL), B(SUBPOP_GAP) — 이름/유사도/comment 동급
        for prod, sig in (("PA", "EDGE_FAIL"), ("PB", "SUBPOP_GAP")):
            case_id = _seed_precedent(conn, product=prod, item_canon="vref_trim",
                                      comment=f"{prod} 사례")
            eval_id = store.save_evaluation(case_id, 1, "ev1", None, "MAJOR", 0.9,
                                            "full", "c", conn=conn)
            store.save_case_signature(eval_id, [{"id": sig, "role": "primary",
                                                 "score": 1.0}], conn=conn)
    res = store.search_precedents("V", "vref_trim", fired_signatures=["SUBPOP_GAP"])
    assert res[0]["signature"] == "SUBPOP_GAP"
    res2 = store.search_precedents("V", "vref_trim", fired_signatures=["EDGE_FAIL"])
    assert res2[0]["signature"] == "EDGE_FAIL"
    # 부스트 없이도 둘 다 회수된다(하드필터 아님)
    assert len(store.search_precedents("V", "vref_trim")) == 2


def test_search_precedents_limit_cap(fresh_db):
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
            store.insert_label(case_id, None, None, None, None, 0, 0,
                               f"c{i}", "seed", None, "seed", conn=conn)
    assert len(store.search_precedents("V", "vref_trim", limit=5)) == 5


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


# ── 스키마 ───────────────────────────────────────────────────────────────────
def test_schema_user_version_and_objects(fresh_db):
    with store.get_conn() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "eval_precedent" in tables
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_item_master_value_type", "idx_product_master_family",
                "idx_fail_case_item"} <= indexes
        assert "updated_at" in {r[1] for r in conn.execute("PRAGMA table_info(evaluation)")}
        assert "created_at" in {r[1] for r in conn.execute("PRAGMA table_info(case_outcome)")}
        # v5/v6 에서 추가된 features 컬럼
        feat_cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
        assert {"shot_fail_ratio", "ring_fail_ratio", "radial_gradient_norm",
                "x_gradient_norm", "y_gradient_norm", "n_modes", "modality_v2"} <= feat_cols


def test_save_features_ignores_derived_keys(fresh_db):
    """DB 컬럼 없는 파생 feature 가 섞여 들어와도 저장은 성공하고, 컬럼은 늘지 않는다.

    features.compute 는 value_gap_ratio/value_gap_minor_mass 를 반환하지만 이는 판정·트레이스
    용 파생값이라 일부러 저장하지 않는다(store.save_features 의 cols 화이트리스트).
    화이트리스트를 걷어내고 dict 를 그대로 쓰면 여기서 OperationalError 로 터진다.
    """
    f = {k: None for k in features_module._FEATURE_KEYS}
    f.update(n_dut=60, value_gap_ratio=0.9, value_gap_minor_mass=0.25)
    with store.get_conn() as conn:
        store.save_features("C_DERIVED", 1, "ev1", f, conn=conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
        assert {"value_gap_ratio", "value_gap_minor_mass"} & cols == set()
        row = conn.execute("SELECT n_dut FROM features WHERE case_id=?",
                           ("C_DERIVED",)).fetchone()
        assert row["n_dut"] == 60


def test_migrate_v3_to_v4_idempotent(fresh_db):
    with store.get_conn() as conn:
        store._migrate_v3_to_v4(conn)  # 이미 v4 인 DB 에 재적용 — no-op 이어야 함
        store._migrate_v3_to_v4(conn)


_V7_FAIL_CASE_DDL = """
CREATE TABLE fail_case (
    case_id TEXT PRIMARY KEY, product_name TEXT NOT NULL, lot_id TEXT, wafer_number INTEGER,
    item_id INTEGER NOT NULL, bin INTEGER, revision REAL, item_class TEXT,
    created_at INTEGER NOT NULL, updated_at INTEGER,
    UNIQUE(product_name, lot_id, wafer_number, item_id, bin, revision)
);
CREATE INDEX idx_fail_case_item_class ON fail_case(item_class);
CREATE INDEX idx_fail_case_product ON fail_case(product_name);
CREATE INDEX idx_fail_case_item ON fail_case(item_id);
CREATE TABLE eval_precedent (
    eval_id INTEGER NOT NULL, precedent_case_id TEXT NOT NULL, rank INTEGER, similarity REAL,
    PRIMARY KEY (eval_id, precedent_case_id),
    FOREIGN KEY (precedent_case_id) REFERENCES fail_case(case_id)
);
"""


def test_migrate_v7_to_v8_rebuilds_fail_case(tmp_path):
    """v8: test_condition 추가 + UNIQUE 편입. 재구축이라 데이터·인덱스·FK 를 다 확인한다.

    구 테이블을 먼저 RENAME 하면 eval_precedent 의 FK 가 새 이름으로 재작성돼 dangling
    되므로, 그 순서 실수를 잡는 것이 이 테스트의 핵심이다.
    """
    import sqlite3
    conn = sqlite3.connect(tmp_path / "v7.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(_V7_FAIL_CASE_DDL)
    conn.executemany(
        "INSERT INTO fail_case (case_id,product_name,lot_id,wafer_number,item_id,bin,"
        "revision,item_class,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,1,1)",
        [("c1", "P", "L", 1, 10, 18, 0.0, "TRIM|V|18"),
         ("c2", "P", "L", None, 11, None, 0.0, "NON_TRIM|V|")])
    conn.execute("INSERT INTO eval_precedent VALUES (1,'c1',1,0.9)")
    conn.commit()

    store._migrate_v7_to_v8(conn)
    store._migrate_v7_to_v8(conn)   # idempotent

    assert "test_condition" in {r[1] for r in conn.execute("PRAGMA table_info(fail_case)")}
    assert conn.execute("SELECT COUNT(*) FROM fail_case WHERE test_condition=''").fetchone()[0] == 2
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(fail_case)")}
    assert {"idx_fail_case_item_class", "idx_fail_case_product", "idx_fail_case_item"} <= idx
    ucols = [[c["name"] for c in conn.execute(f"PRAGMA index_info('{u['name']}')")]
             for u in conn.execute("PRAGMA index_list(fail_case)")
             if u["unique"] and u["origin"] == "u"]
    assert any(len(u) == 7 and "test_condition" in u for u in ucols)
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='eval_precedent'").fetchone()[0]
    assert "REFERENCES fail_case(case_id)" in ddl and "fail_case_new" not in ddl
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()

    # 같은 자연키라도 조건이 다르면 별개 행 (UNIQUE 가 condition 을 포함해야 통과)
    conn.execute("INSERT INTO fail_case (case_id,product_name,lot_id,wafer_number,item_id,"
                 "bin,revision,item_class,test_condition,created_at,updated_at) "
                 "VALUES ('c3','P','L',NULL,11,NULL,0.0,'NON_TRIM|V|','TEMP',1,1)")
    assert conn.execute("SELECT COUNT(*) FROM fail_case WHERE item_id=11").fetchone()[0] == 2
    conn.close()


def test_upsert_fail_case_stores_condition(fresh_db):
    with store.get_conn() as conn:
        store.upsert_fail_case("x1", "P", "L", None, 1, None, 0.0, "NON_TRIM|V|", conn=conn)
        store.upsert_fail_case("x2", "P", "L", None, 1, None, 0.0, "NON_TRIM|V|", "TEMP",
                               conn=conn)
        got = dict(conn.execute(
            "SELECT case_id, test_condition FROM fail_case ORDER BY case_id").fetchall())
        assert got == {"x1": "", "x2": "TEMP"}


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

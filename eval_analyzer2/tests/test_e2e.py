"""api.evaluate E2E 단독 테스트 (degrade / raw+선례 / idempotency)."""
from eval_engine import api, store

_REQUIRED_KEYS = {"case_id", "item_canonical", "item_class", "bin", "status",
                  "primary_signature", "secondary_signatures", "confidence",
                  "data_completeness", "comment", "evidence", "precedents"}

_VALID_STATUS = {"CRITICAL", "MAJOR", "MINOR", "MONITOR", "OK"}


def _raw_run_input(n_pass=20, n_fail=4):
    rows = []
    dut = 1
    for i in range(n_pass):  # pass: bin1, 중앙부, spec 내
        rows.append({"DUT": dut, "XCoord": i % 5, "YCoord": i // 5, "Bin": 1,
                     "Serial": f"S{dut}", "VREF_TRIM": 1.20 + 0.01 * (i % 3)})
        dut += 1
    for i in range(n_fail):  # fail: bin18, edge(큰 좌표), usl 초과 outlier
        rows.append({"DUT": dut, "XCoord": 50 + i, "YCoord": 50 + i, "Bin": 18,
                     "Serial": f"S{dut}", "VREF_TRIM": 1.55 + 0.02 * i})
        dut += 1
    return {
        "meta": {"product_name": "S5E_TEST_0000001", "family_product": "SOC",
                 "product_type": "PMIC", "revision": 0.0, "lot_id": "LOT001",
                 "wafer_number": 3},
        "raw_table": {"meta_columns": ["DUT", "XCoord", "YCoord", "Bin", "Serial"],
                      "item_columns": ["VREF_TRIM"], "units": {"VREF_TRIM": "V"},
                      "lower_limit": {"VREF_TRIM": 1.0}, "upper_limit": {"VREF_TRIM": 1.4},
                      "rows": rows},
    }


def _assert_case_shape(case):
    assert _REQUIRED_KEYS <= set(case)
    assert case["status"] in _VALID_STATUS
    assert len(case["item_class"].split("|")) == 3


def test_degrade_gross_fail():
    ri = {"meta": {"product_name": "P1", "product_type": "PMIC", "revision": 0.0,
                   "lot_id": "L1", "wafer_number": 1, "family_product": "SOC"},
          "items": [{"item_name": "BUCK_SCAN", "bin": 40, "unit": "P_F",
                     "yield": 0.3, "fail_count": 196, "total_count": 280,
                     "lsl": None, "usl": None}]}
    result = api.evaluate(ri, persist=False)
    assert result["run_id"] is None
    assert len(result["cases"]) == 1
    case = result["cases"][0]
    _assert_case_shape(case)
    assert case["status"] == "CRITICAL"
    assert case["primary_signature"] == "GROSS_FAIL"
    assert case["data_completeness"] == "low"


def test_raw_mode_fires_signature(fresh_db):
    result = api.evaluate(_raw_run_input(), persist=True)
    assert result["run_id"] is not None
    cases = [c for c in result["cases"] if c["bin"] == 18]
    assert len(cases) == 1
    case = cases[0]
    _assert_case_shape(case)
    assert case["item_canonical"] == "vref_trim"
    assert case["status"] in {"MAJOR", "CRITICAL"}
    assert case["primary_signature"] is not None
    # persist 확인
    with store.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM evaluation").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM features").fetchone()[0] >= 1


def test_raw_mode_attaches_precedent(fresh_db):
    # 선례 시드: 다른 product(cross-product), 같은 family/bin/value_type/유사 이름
    with store.get_conn() as conn:
        store.upsert_product_master(
            {"product_name": "OLD_PROD", "family_product": "SOC",
             "product_type": "PMIC"}, conn=conn)
        item_id = store.upsert_item_master("vref_trim", "VREF_TRIM", None, None, "TRIM",
                                           None, "V", None, conn=conn)
        old_case = store.make_case_id("OLD_PROD", "L0", 0, item_id, 18, 0.0)
        store.upsert_fail_case(old_case, "OLD_PROD", "L0", 0, item_id, 18, 0.0,
                               "TRIM|V|18", conn=conn)
        lbl = store.insert_label(old_case, None, "MAJOR", "equipment", None, 0, 0,
                                 "golden unit 재측정", "seed", None, "seed", conn=conn)
        store.insert_case_outcome(old_case, lbl, "retest", None, "recovered_normal",
                                  None, None, None, conn=conn)

    result = api.evaluate(_raw_run_input(), persist=True)
    case = [c for c in result["cases"] if c["bin"] == 18][0]
    assert len(case["precedents"]) >= 1
    top = case["precedents"][0]
    assert top["action"] == "retest"
    assert top["product_name"] == "OLD_PROD"  # 선례 제품명 통과
    assert top["family_product"] == "SOC"
    assert "golden unit 재측정" in case["comment"]  # 템플릿 코멘트에 human_comment 결합
    # 선례 사용 이력(eval_precedent) 저장 확인
    with store.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM eval_precedent").fetchone()[0]
        assert n >= 1


def test_ingest_rejects_missing_input_keys():
    meta = {"product_name": "P1", "product_type": "PMIC", "family_product": "SOC",
            "revision": 0.0, "lot_id": "L1", "wafer_number": 1}
    import pytest
    from eval_engine.pipeline import ingest
    with pytest.raises(ValueError, match="raw_df / raw_table / items"):
        ingest.ingest({"meta": meta}, persist=False)
    with pytest.raises(ValueError, match="raw_table 필수 키"):
        ingest.ingest({"meta": meta, "raw_table": {"rows": []}}, persist=False)


def test_idempotency_case_id_stable(fresh_db):
    ri = _raw_run_input()
    api.evaluate(ri, persist=True)
    with store.get_conn() as conn:
        fc1 = conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0]
        rm1 = conn.execute("SELECT COUNT(*) FROM raw_metrics").fetchone()[0]
    api.evaluate(ri, persist=True)  # 동일 입력 재실행
    with store.get_conn() as conn:
        fc2 = conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0]
        rm2 = conn.execute("SELECT COUNT(*) FROM raw_metrics").fetchone()[0]
    assert fc2 == fc1  # case_id 안정 → fail_case 불변
    assert rm2 > rm1   # raw_metrics 는 run_id 별 누적(스키마 의도)

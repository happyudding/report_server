"""db_input/import_csv — label(human_status/root_cause) + case_outcome 적재, 재실행 idempotent."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # eval_analyzer/ (db_input 탐색)

from db_input import import_csv  # noqa: E402
from eval_engine import store  # noqa: E402


def _row(**kw):
    r = {"product_name": "S5E_TEST_0000001", "product_type": "PMIC",
         "family_product": "SOC", "lot_id": "LOT001", "wafer_number": "3",
         "revision": "0.0", "item_name": "VREF_TRIM", "value_type": "V", "bin": "18",
         "USL": "1.4", "LSL": "1.0", "average": "1.21", "stdev": "0.09",
         "human_comment": "site 3 튐, golden 재측정", "session_id": "",
         "human_status": "MAJOR", "root_cause_category": "equipment",
         "outcome_action": "retest", "outcome_condition": "",
         "outcome_result": "recovered_normal"}
    r.update(kw)
    return r


def test_import_group_loads_label_and_outcome(fresh_db):
    import_csv._import_group("PMIC", "SOC", None, [_row()], "test.csv", fresh_db)
    with store.get_conn() as conn:
        lab = conn.execute("SELECT * FROM label").fetchone()
        assert lab["human_status"] == "MAJOR"
        assert lab["root_cause_category"] == "equipment"
        out = conn.execute("SELECT * FROM case_outcome").fetchone()
        assert out["action"] == "retest"
        assert out["result"] == "recovered_normal"
        assert out["label_id"] == lab["label_id"]  # outcome ↔ label 연결
        assert out["created_at"] is not None

    # 재실행 idempotent — label/outcome 중복 삽입 없음
    import_csv._import_group("PMIC", "SOC", None, [_row()], "test.csv", fresh_db)
    with store.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM case_outcome").fetchone()[0] == 1

    # 선례검색 결과에 action/result 가 채워져 나옴 (기존 갭: NULL)
    res = store.search_precedents("V", "vref_trim")
    assert res and res[0]["action"] == "retest"
    assert res[0]["result"] == "recovered_normal"
    assert res[0]["root_cause_category"] == "equipment"


def test_import_group_outcome_without_label(fresh_db):
    row = _row(human_comment="", human_status="", root_cause_category="")
    import_csv._import_group("PMIC", "SOC", None, [row], "t.csv", fresh_db)
    with store.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 0
        out = conn.execute("SELECT * FROM case_outcome").fetchone()
        assert out["action"] == "retest"
        assert out["label_id"] is None


def test_import_group_backfills_outcome_label_link(fresh_db):
    """1차 임포트에 outcome 만(라벨 없음) → 이후 comment 추가 재임포트 시
    기존 outcome 의 label_id 가 백필된다."""
    first = _row(human_comment="", human_status="", root_cause_category="")
    import_csv._import_group("PMIC", "SOC", None, [first], "t.csv", fresh_db)
    with store.get_conn() as conn:
        assert conn.execute("SELECT label_id FROM case_outcome").fetchone()["label_id"] is None

    import_csv._import_group("PMIC", "SOC", None, [_row()], "t.csv", fresh_db)
    with store.get_conn() as conn:
        lab = conn.execute("SELECT * FROM label").fetchone()
        out = conn.execute("SELECT * FROM case_outcome").fetchone()
        assert lab is not None
        assert out["label_id"] == lab["label_id"]  # 백필 확인
        assert conn.execute("SELECT COUNT(*) FROM case_outcome").fetchone()[0] == 1


def test_import_group_rejects_unknown_outcome_vocab(fresh_db):
    with pytest.raises(ValueError):
        import_csv._import_group("PMIC", "SOC", None,
                                 [_row(outcome_action="bogus_action")], "t.csv", fresh_db)


def test_import_group_legacy_columns_still_work(fresh_db):
    """기존 15컬럼 CSV(신규 컬럼 없음) — human_comment 만으로 label 적재, outcome 없음."""
    row = _row()
    for k in ("human_status", "root_cause_category", "outcome_action",
              "outcome_condition", "outcome_result"):
        row.pop(k)
    import_csv._import_group("PMIC", "SOC", None, [row], "t.csv", fresh_db)
    with store.get_conn() as conn:
        lab = conn.execute("SELECT * FROM label").fetchone()
        assert lab["human_comment"].startswith("site 3")
        assert lab["human_status"] is None
        assert conn.execute("SELECT COUNT(*) FROM case_outcome").fetchone()[0] == 0

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_input import ai_extract, import_text  # noqa: E402
from eval_engine import store  # noqa: E402


def _row(**kw):
    row = {
        "product_name": "S5E_TEST_0000001",
        "product_type": "PMIC",
        "family_product": "SOC",
        "lot_id": "LOT001",
        "wafer_number": "3",
        "revision": "0.0",
        "item_name": "VREF_TRIM",
        "value_type": "V",
        "bin": "18",
        "USL": "1.4",
        "LSL": "1.0",
        "average": "1.21",
        "stdev": "0.09",
        "human_comment": "site 3 튐, golden 재측정",
        "session_id": "",
        "human_status": "MAJOR",
        "root_cause_category": "equipment",
        "outcome_action": "retest",
        "outcome_condition": "",
        "outcome_result": "recovered_normal",
    }
    row.update(kw)
    return row


def test_validate_rows_accepts_valid_row():
    result = ai_extract.validate_rows([_row()])
    assert result[0]["status"] == "READY"
    assert result[0]["errors"] == []


def test_validate_rows_blocks_missing_required():
    row = _row(item_name="")
    result = ai_extract.validate_rows([row])
    assert result[0]["status"] == "BLOCKED"
    assert "필수값 누락: item_name" in result[0]["errors"]


def test_validate_rows_blocks_unknown_outcome_vocab():
    row = _row(outcome_action="bogus_action")
    result = ai_extract.validate_rows([row])
    assert result[0]["status"] == "BLOCKED"
    assert any("outcome.action" in err for err in result[0]["errors"])


def test_validate_rows_blocks_bad_numeric():
    row = _row(bin="not-a-number")
    result = ai_extract.validate_rows([row])
    assert result[0]["status"] == "BLOCKED"
    assert "숫자 변환 불가: bin='not-a-number'" in result[0]["errors"]


def test_rows_to_csv_is_import_csv_compatible(tmp_path):
    out = ai_extract.rows_to_csv(tmp_path / "out.csv", [_row(extra="ignored")])
    with open(out, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["product_name"] == "S5E_TEST_0000001"
    assert rows[0]["outcome_action"] == "retest"
    assert "extra" not in rows[0]
    assert list(rows[0].keys()) == ai_extract.CSV_COLUMNS


def test_import_text_json_save_uses_import_group(fresh_db, tmp_path, monkeypatch):
    json_path = tmp_path / "rows.json"
    json_path.write_text(json.dumps([_row()], ensure_ascii=False), encoding="utf-8")

    rc = import_text.main(["--json", str(json_path), "--save", "--to-eval-db"])
    assert rc == 0

    with store.get_conn() as conn:
        lab = conn.execute("SELECT * FROM label").fetchone()
        out = conn.execute("SELECT * FROM case_outcome").fetchone()
    assert lab["human_status"] == "MAJOR"
    assert lab["human_comment"] == "site 3 튐, golden 재측정"
    assert out["action"] == "retest"
    assert out["result"] == "recovered_normal"


def test_import_text_json_numeric_values_are_normalized(fresh_db, tmp_path):
    """JSON 숫자값(bin/wafer 등)도 CSV 문자열 행처럼 정규화되어 저장된다."""
    row = _row(bin=18, wafer_number=3, USL=1.4, LSL=1.0, average=1.21,
               stdev=0.09, revision=0.0)
    json_path = tmp_path / "rows.json"
    json_path.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")

    rc = import_text.main(["--json", str(json_path), "--save", "--to-eval-db"])
    assert rc == 0
    with store.get_conn() as conn:
        fc = conn.execute("SELECT * FROM fail_case").fetchone()
    assert fc["bin"] == 18
    assert fc["wafer_number"] == 3


def test_import_text_input_mode_reports_llm_not_implemented(tmp_path, capsys):
    input_path = tmp_path / "note.txt"
    input_path.write_text("raw pasted note", encoding="utf-8")
    rc = import_text.main(["--input", str(input_path), "--preview"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "LLM extractor not implemented yet" in captured.err

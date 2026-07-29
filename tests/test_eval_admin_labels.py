"""admin_panel/eval_admin — family_product 노출 + db_input 단순 포맷 CSV export 검증.

실행:
    python tests/test_eval_admin_labels.py

검증:
  (a) list_labels() 행에 product_type / family_product 가 실려 나온다 (기존 갭)
  (b) 검색어가 family_product 로도 매칭된다
  (c) labels_csv_iter() = BOM + "Product type,Family Product,unit,Item,comment" 5컬럼,
      unit 은 im.unit(원문) 이 아니라 im.value_type(엔진 어휘), 빈 코멘트 행은 제외
  (d) 내려받은 CSV 를 db_input/import_csv 가 그대로 다시 읽는다 (왕복)

pytest 미사용(그건 eval_analyzer 전용) — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))       # import config
sys.path.insert(0, os.path.join(_ROOT, "eval_analyzer"))  # db_input 왕복 검증용

_TMP = Path(tempfile.mkdtemp(prefix="eval_admin_test_"))
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")

from admin_panel import eval_admin  # noqa: E402
from web_report import eval_export  # noqa: E402


def seed():
    """product_master(family 포함) + item + case + label 3건 (1건은 빈 코멘트)."""
    store, _ = eval_export._engine()
    conn = eval_export.open_conn(create=True)
    try:
        run_id = store.create_ingest_run(
            {"source_file": "seed", "session_id": "1700000000_seed",
             "ingested_by": "test"}, conn=conn)
        store.upsert_product_master(
            {"product_name": "PMIC_SOC", "product_type": "PMIC",
             "family_product": "SOC"}, conn=conn)
        seeds = [("vref_trim", "VREF_TRIM", "V", "mV", "전압 마진 부족"),
                 ("osc_freq", "OSC_FREQ", "Hz", "MHz", "주파수 산포 큼"),
                 ("no_comment", "NO_COMMENT", "V", None, "")]
        for canonical, raw, value_type, unit, comment in seeds:
            item_id = store.upsert_item_master(canonical, raw, None, None, "NON_TRIM",
                                               None, value_type, unit, conn=conn)
            case_id = store.make_case_id("PMIC_SOC", None, None, item_id, 0, 0.0)
            store.upsert_fail_case(case_id, "PMIC_SOC", None, None, item_id, 0, 0.0,
                                   f"NON_TRIM|{value_type}|0", conn=conn)
            store.link_run_case(run_id, case_id, conn=conn)
            store.insert_label(case_id, None, None, None, None, 0, 0, comment,
                               "db_input", "tester", "manual", conn=conn)
        conn.commit()
    finally:
        conn.close()


def read_csv():
    text = "".join(eval_admin.labels_csv_iter())
    assert text.startswith("﻿"), "Excel 한글용 UTF-8 BOM 없음"
    return text, list(csv.DictReader(io.StringIO(text[1:])))


def main():
    seed()

    # (a) family_product 노출 ────────────────────────────────────────────────
    data = eval_admin.list_labels()
    assert data["exists"] and data["total"] == 3, data
    by_item = {r["item"]: r for r in data["rows"]}
    assert by_item["VREF_TRIM"]["family_product"] == "SOC", by_item["VREF_TRIM"]
    assert by_item["VREF_TRIM"]["product_type"] == "PMIC", by_item["VREF_TRIM"]

    # (b) family_product 검색 ────────────────────────────────────────────────
    assert eval_admin.list_labels(q="SOC")["total"] == 3
    assert eval_admin.list_labels(q="NOSUCHFAMILY")["total"] == 0

    # (c) CSV 포맷 ───────────────────────────────────────────────────────────
    text, rows = read_csv()
    assert text.splitlines()[0].lstrip("﻿") == \
        "Product type,Family Product,unit,Item,comment", text.splitlines()[0]
    assert len(rows) == 2, f"빈 코멘트 행이 제외되지 않음: {rows}"
    got = {r["Item"]: r for r in rows}
    assert got["VREF_TRIM"]["unit"] == "V", got["VREF_TRIM"]      # im.unit('mV') 아님
    assert got["OSC_FREQ"]["unit"] == "Hz", got["OSC_FREQ"]
    assert got["VREF_TRIM"]["Product type"] == "PMIC"
    assert got["VREF_TRIM"]["Family Product"] == "SOC"
    assert got["VREF_TRIM"]["comment"] == "전압 마진 부족"

    # (d) 왕복 — 내려받은 CSV 를 db_input 이 그대로 읽는다 ────────────────────
    from db_input import import_csv
    csv_path = _TMP / "roundtrip.csv"
    csv_path.write_text(text, encoding="utf-8")
    parsed = import_csv._read_rows(csv_path)
    assert len(parsed) == 2, parsed
    assert {r["item_name"] for r in parsed} == {"VREF_TRIM", "OSC_FREQ"}
    assert {r["value_type"] for r in parsed} == {"V", "Hz"}
    assert all(r["product_name"] == "PMIC_SOC" for r in parsed), parsed

    print("PASS: test_eval_admin_labels (a/b/c/d)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

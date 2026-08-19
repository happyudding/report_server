"""Unit 그룹(value_type) 별칭 규칙 + 관리자 수정 API 검증.

실행:
    python tests/test_eval_unit_group.py

검증:
  (a) unit_group() 부분문자열 규칙 — VOLT→V / AMP→A / HERTZ→Hz, 그 외 None
  (b) export 경로가 엔진 정확매칭 표보다 이 규칙을 먼저 쓴다 (HERTZ 가 PF 로 안 떨어짐)
  (c) list_labels() 행에 item_id 가 실려 나온다 (수정 UI 대상 지정용)
  (d) set_item_value_type() 이 item_master.value_type + fail_case.item_class 를 함께 갱신
  (e) remap_unit_aliases(dry_run=True) 는 DB 를 안 바꾸고, 실행하면 오분류만 교정

pytest 미사용(그건 eval_analyzer 전용) — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))       # import config

_TMP = Path(tempfile.mkdtemp(prefix="eval_unit_test_"))
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")

from admin_panel import eval_admin  # noqa: E402
from web_report import eval_export  # noqa: E402

# (item_canonical, raw, 저장된 value_type, unit 원문) — 앞 2건은 일부러 오분류 상태
SEEDS = [
    ("vref_trim", "VREF_TRIM", "PF", "VOLTS"),      # → V 로 교정돼야 함
    ("osc_freq", "OSC_FREQ", "PF", "MEGAHERTZ"),    # → Hz
    ("idd_leak", "IDD_LEAK", "A", "uAMP"),           # 이미 맞음 → 변화 없음
    ("bg_code", "BG_CODE", "CODE", "code"),          # 규칙 밖 → 손대지 않음
]


def seed():
    store, _ = eval_export._engine()
    conn = eval_export.open_conn(create=True)
    try:
        run_id = store.create_ingest_run(
            {"source_file": "seed", "session_id": "1700000000_seed",
             "ingested_by": "test"}, conn=conn)
        store.upsert_product_master(
            {"product_name": "PMIC_SOC", "product_type": "PMIC",
             "family_product": "SOC"}, conn=conn)
        for canonical, raw, value_type, unit in SEEDS:
            item_id = store.upsert_item_master(canonical, raw, None, None, "NON_TRIM",
                                               None, value_type, unit, conn=conn)
            case_id = store.make_case_id("PMIC_SOC", None, None, item_id, 0, 0.0)
            store.upsert_fail_case(case_id, "PMIC_SOC", None, None, item_id, 0, 0.0,
                                   f"NON_TRIM|{value_type}|0", conn=conn)
            store.link_run_case(run_id, case_id, conn=conn)
            store.insert_label(case_id, None, None, None, None, 0, 0, f"{raw} 코멘트",
                               "db_input", "tester", "manual", conn=conn)
        conn.commit()
    finally:
        conn.close()


def item_state(raw):
    conn = eval_export.open_conn(create=False)
    try:
        im = conn.execute("SELECT item_id, value_type FROM item_master "
                          "WHERE item_name_raw=?", (raw,)).fetchone()
        fc = conn.execute("SELECT item_class FROM fail_case WHERE item_id=?",
                          (im["item_id"],)).fetchone()
        return im["item_id"], im["value_type"], fc["item_class"]
    finally:
        conn.close()


def main():
    # (a) 별칭 규칙 자체 ─────────────────────────────────────────────────────
    for unit, want in [("VOLTS", "V"), ("mVOLT", "V"), ("volt", "V"),
                       ("AMPS", "A"), ("uAMP", "A"), ("HERTZ", "Hz"),
                       ("MEGAHERTZ", "Hz"), ("Hertz", "Hz")]:
        assert eval_export.unit_group(unit) == want, (unit, eval_export.unit_group(unit))
    for unit in ["mV", "V", "hz", "code", "ohm", "", None]:
        assert eval_export.unit_group(unit) is None, unit

    # (b) export 경로 우선순위 — 엔진 표엔 HERTZ 가 없어 원래 PF 였다 ────────
    _, engine_ingest = eval_export._engine()
    assert engine_ingest._classify_value_type("HERTZ", "OSC_FREQ") == "PF"
    assert (eval_export.unit_group("HERTZ")
            or engine_ingest._classify_value_type("HERTZ", "OSC_FREQ")) == "Hz"

    seed()

    # (c) 목록에 item_id ─────────────────────────────────────────────────────
    rows = {r["item"]: r for r in eval_admin.list_labels()["rows"]}
    assert len(rows) == len(SEEDS), rows
    assert all(isinstance(r["item_id"], int) for r in rows.values()), rows

    # (d) 수동 지정 — value_type 과 item_class 가 함께 바뀐다 ─────────────────
    iid, _, _ = item_state("BG_CODE")
    res = eval_admin.set_item_value_type([iid], "Ohm")
    assert res == {"updated": 1, "cases": 1, "exists": True}, res
    assert item_state("BG_CODE")[1:] == ("Ohm", "NON_TRIM|Ohm"), item_state("BG_CODE")
    eval_admin.set_item_value_type([iid], "CODE")   # 원복 (e 의 기대값 유지)

    try:
        eval_admin.set_item_value_type([iid], "볼트")
    except ValueError:
        pass
    else:
        raise AssertionError("어휘 밖 value_type 이 통과함")

    # (e) 일괄 재적용 — dry_run 은 무변경, 실행은 오분류 2건만 교정 ───────────
    preview = eval_admin.remap_unit_aliases(dry_run=True)
    assert preview["changed"] == 2, preview
    assert {i["item"] for i in preview["items"]} == {"VREF_TRIM", "OSC_FREQ"}, preview
    assert item_state("VREF_TRIM")[1] == "PF", "dry_run 이 DB 를 바꿈"

    applied = eval_admin.remap_unit_aliases()
    assert applied["changed"] == 2 and applied["cases"] == 2, applied
    # 교정된 item 은 **2단** item_class 로 재작성된다(2026-08-19 — bin 조각 없음).
    assert item_state("VREF_TRIM")[1:] == ("V", "NON_TRIM|V"), item_state("VREF_TRIM")
    assert item_state("OSC_FREQ")[1:] == ("Hz", "NON_TRIM|Hz"), item_state("OSC_FREQ")
    # 손대지 않은 item 은 픽스처가 심어 둔 **구 3단 값 그대로** 남는다 — 하위호환 확인
    # (소비자는 split 길이를 고정하지 않는다). 재수집·재-export 하면 2단이 된다.
    assert item_state("IDD_LEAK")[1:] == ("A", "NON_TRIM|A|0"), item_state("IDD_LEAK")
    assert item_state("BG_CODE")[1:] == ("CODE", "NON_TRIM|CODE"), item_state("BG_CODE")
    assert eval_admin.remap_unit_aliases(dry_run=True)["changed"] == 0, "멱등 아님"

    print("PASS: test_eval_unit_group (a/b/c/d/e)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

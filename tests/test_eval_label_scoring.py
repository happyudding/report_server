"""eval 패널 정답 라벨(save_human_label) + 채점(scoring) 검증.

실행:
    python tests/test_eval_label_scoring.py

검증:
  (a) save_human_label 이 evaluation(엔진 판정) + label(eval_id 연결, labeler=eval-panel)
      쌍을 같은 case_id 로 저장한다 — 학습/채점의 원재료
  (b) 같은 케이스 재검수 시 이전 패널 라벨은 교체된다 (case 당 정답 1건)
  (c) 기존 item_master 의 value_type 을 덮어쓰지 않는다 (선례검색 하드필터 보호)
  (d) scoring() — 혼동행렬/일치율/high-severity precision·recall/수용률 산식
  (e) get_panel_label — 저장한 라벨을 같은 case_id 산식으로 되찾는다 (폼 프리필)

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="eval_label_test_"))
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")

from admin_panel import eval_admin  # noqa: E402
from web_report import eval_export  # noqa: E402

_SESSION = {
    "session_id": "S_TEST1", "analysis_key": "AK_TEST",
    "product_type": "PMIC", "family_product": "", "product": "PRODX",
    "lot_id": "LOT1", "revision": "1.0", "file_name": "t.csv",
}
_ENGINE_MAJOR = {
    "engine_version": "ev1", "status": "MAJOR", "confidence": 0.9,
    "data_completeness": "full", "comment": "[현상] outlier",
    "primary_signature": "SEVERE_OUTLIER", "secondary_signatures": ["OUTLIER_WARN"],
}


def main():
    # (a) 정정 라벨 — 쌍 저장 ────────────────────────────────────────────────
    r1 = eval_export.save_human_label(
        _SESSION, item="VOUT_TRIM", bin_=18, item_class="TRIM|V|18",
        engine=_ENGINE_MAJOR,
        human={"accepted": False, "human_status": "MINOR",
               "human_comment": "실제로는 경미", "root_cause_category": "spec"})
    assert r1["human_status"] == "MINOR" and r1["eval_id"], r1

    # (b) 재검수(수용) — 교체 ────────────────────────────────────────────────
    r2 = eval_export.save_human_label(
        _SESSION, item="VOUT_TRIM", bin_=18, item_class="TRIM|V|18",
        engine=_ENGINE_MAJOR, human={"accepted": True})
    assert r2["case_id"] == r1["case_id"] and r2["human_status"] == "MAJOR", r2

    # 엔진 MONITOR / 사람 CRITICAL — high-severity 미탐 케이스
    eng_mon = dict(_ENGINE_MAJOR, status="MONITOR", primary_signature=None,
                   secondary_signatures=[])
    eval_export.save_human_label(
        _SESSION, item="IDD_LEAK", bin_=31, item_class="NON_TRIM|A|31",
        engine=eng_mon,
        human={"accepted": False, "human_status": "CRITICAL",
               "human_comment": "누설 실불량"})

    conn = eval_export.open_conn(create=False)
    try:
        n_label = conn.execute(
            "SELECT COUNT(*) FROM label WHERE labeler='eval-panel'").fetchone()[0]
        assert n_label == 2, f"재검수 교체 실패 — label {n_label}건"
        sigs = {(r[0], r[1]) for r in conn.execute(
            "SELECT signature, role FROM case_signature")}
        assert ("SEVERE_OUTLIER", "primary") in sigs, sigs

        # (c) 기존 item value_type 보호 — 같은 item 을 다른 item_class 로 재라벨해도 유지
        eval_export.save_human_label(
            _SESSION, item="VOUT_TRIM", bin_=18, item_class="TRIM|PF|18",
            engine=_ENGINE_MAJOR, human={"accepted": True})
        vt = conn.execute(
            "SELECT value_type FROM item_master WHERE item_name_raw='VOUT_TRIM'"
        ).fetchone()[0]
        assert vt == "V", f"기존 item value_type 이 덮였음: {vt}"
    finally:
        conn.close()

    # (d) scoring 산식 ───────────────────────────────────────────────────────
    sc = eval_admin.scoring()
    assert sc["pairs"] == 2, sc
    assert sc["agree_rate"] == 0.5 and sc["accepted_rate"] == 0.5, sc
    # 엔진 高판정 1건(MAJOR, 사람도 MAJOR) / 사람 高판정 2건(MAJOR+CRITICAL) 중 엔진이 1건
    assert sc["high"] == {"engine": 1, "human": 2, "both": 1,
                          "precision": 1.0, "recall": 0.5}, sc["high"]
    assert sc["confusion"]["MAJOR"]["MAJOR"] == 1
    assert sc["confusion"]["MONITOR"]["CRITICAL"] == 1
    by_sig = {r["signature"]: r for r in sc["per_signature"]}
    assert by_sig["SEVERE_OUTLIER"]["agree_rate"] == 1.0

    # (e) 기존 라벨 조회 — 저장한 최신 1건을 되찾는다 ────────────────────────
    got = eval_export.get_panel_label(_SESSION, item="VOUT_TRIM", bin_=18)
    assert got is not None, "저장한 라벨을 못 찾음 (case_id 산식 불일치)"
    assert got["human_status"] == "MAJOR", got          # (c) 의 수용 라벨이 최신
    assert got["engine_comment_accepted"] == 1, got
    got2 = eval_export.get_panel_label(_SESSION, item="IDD_LEAK", bin_=31)
    assert got2["human_status"] == "CRITICAL" and got2["human_comment"] == "누설 실불량", got2
    assert eval_export.get_panel_label(_SESSION, item="NO_SUCH_ITEM", bin_=1) is None
    # bin 은 case 를 가르지 않는다(2026-08-19) — 같은 item 이면 어떤 bin 으로 물어도
    # 같은 라벨이 나온다. 이게 곧 "화면 어느 섹션에서 눌러도 같은 학습 데이터" 라는 뜻이다.
    assert eval_export.get_panel_label(_SESSION, item="VOUT_TRIM", bin_=99) == got

    print("PASS: test_eval_label_scoring (a/b/c/d/e)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

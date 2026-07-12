"""연계 테스트 — 정본 raw_df 포맷(samples/*.csv) → evaluate E2E.

samples/ 의 CSV 는 report_server 가 앞으로 넘길 **기대 입력 포맷**(FAILTNO/TNO 포함,
6-메타행). 이 경로가 evaluate() 전 구간(preview + persist)에서 도는지 실데이터로 고정한다.
어댑터(adapter.sample_csv_to_run_input)는 eval_engine 을 import 하지 않는다(report_server 측 모사).
"""
import glob
import os

import pytest

pytestmark = pytest.mark.integration

_SAMPLE_GLOB = os.path.join(
    os.path.dirname(__file__), "..", "..", "samples",
    "sample_semiconductor_1000chips_15items_stepP2_*.csv")

_REQUIRED_KEYS = {"case_id", "item_canonical", "item_raw", "item_class", "bin", "status",
                  "issue_category", "primary_signature", "secondary_signatures", "confidence",
                  "data_completeness", "comment", "evidence", "precedents"}
_VALID_STATUS = {"CRITICAL", "MAJOR", "MINOR", "MONITOR", "OK"}
_VALID_CATEGORY = {"YIELD", "CPK", "ETC"}


def _sample_paths():
    paths = sorted(glob.glob(_SAMPLE_GLOB))
    if not paths:
        pytest.skip(f"sample CSV 없음: {_SAMPLE_GLOB}")
    return paths


def test_sample_raw_df_preview():
    from adapter import sample_csv_to_run_input
    from eval_engine import api
    for path in _sample_paths():
        result = api.evaluate(sample_csv_to_run_input(path), persist=False)
        assert result["run_id"] is None
        assert len(result["cases"]) > 0, f"fail case 0: {path}"
        for case in result["cases"]:
            assert _REQUIRED_KEYS <= set(case)
            assert case["status"] in _VALID_STATUS
            assert case["issue_category"] in _VALID_CATEGORY
            assert case["item_raw"]           # 원본 item명 존재 (join 키)
            assert len(case["item_class"].split("|")) == 3
            # bin: yield fail 이면 fail bin, cpk<cpk_warn 트리거면 PASS_BIN(1) 일 수 있음


def test_sample_raw_df_persist(fresh_db):
    from adapter import sample_csv_to_run_input
    from eval_engine import api, store
    path = _sample_paths()[0]
    result = api.evaluate(sample_csv_to_run_input(path), persist=True)
    assert result["run_id"] is not None
    n_cases = len(result["cases"])
    with store.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0] == n_cases
        assert conn.execute("SELECT COUNT(*) FROM evaluation").fetchone()[0] == n_cases
        assert conn.execute("SELECT COUNT(*) FROM features").fetchone()[0] == n_cases

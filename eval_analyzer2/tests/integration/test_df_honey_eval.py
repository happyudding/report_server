"""연계 테스트 — report_generator.df_honey 로 실제 raw CSV 로드 → run_input → evaluate.

report_generator(df_honey) 또는 mass_huge CSV 가 없는 환경에서는 자동 skip.
eval_engine 은 여기(드라이버)에서만 간접 사용 — 본체는 report_server 를 import 안 함.
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

_RG_CLIENT = r"f:/COINAPI/report_server/client"
_CSV = r"f:/COINAPI/mass_huge_W01_W07/mass_huge_W01.csv"

_REQUIRED_KEYS = {"case_id", "item_canonical", "item_class", "bin", "status",
                  "primary_signature", "secondary_signatures", "confidence",
                  "data_completeness", "comment", "evidence", "precedents"}
_VALID_STATUS = {"CRITICAL", "MAJOR", "MINOR", "MONITOR", "OK"}


def _load_honey(nrows=105, ncols=35):
    if _RG_CLIENT not in sys.path:
        sys.path.insert(0, _RG_CLIENT)
    if not os.path.exists(_CSV):
        pytest.skip(f"raw CSV 없음: {_CSV}")
    df_honey_mod = pytest.importorskip("report_generator.df_honey")
    import pandas as pd
    raw = pd.read_csv(_CSV, header=None, dtype=str, nrows=nrows, usecols=range(ncols))
    return df_honey_mod.df_honey.from_dataframe(raw, name="W01")


def _run_input():
    from adapter import df_honey_to_run_input
    honey = _load_honey()
    return df_honey_to_run_input(honey)


def test_df_honey_to_evaluate_preview():
    from eval_engine import api
    run_input = _run_input()
    result = api.evaluate(run_input, persist=False)

    assert result["run_id"] is None
    assert isinstance(result["cases"], list)
    for case in result["cases"]:
        assert _REQUIRED_KEYS <= set(case)
        assert case["status"] in _VALID_STATUS
        assert len(case["item_class"].split("|")) == 3
        assert isinstance(case["evidence"], list)
        assert isinstance(case["precedents"], list)


def test_df_honey_to_evaluate_persist(fresh_db):
    from eval_engine import api, store
    run_input = _run_input()
    result = api.evaluate(run_input, persist=True)
    assert result["run_id"] is not None
    with store.get_conn() as conn:
        # fail item 이 하나라도 있으면 적재되어야 함
        n_cases = len(result["cases"])
        n_fail_case = conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0]
        assert n_fail_case == n_cases
        if n_cases:
            assert conn.execute("SELECT COUNT(*) FROM evaluation").fetchone()[0] == n_cases
            assert conn.execute("SELECT COUNT(*) FROM features").fetchone()[0] == n_cases

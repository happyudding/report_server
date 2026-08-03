"""신규 raw df 포맷(REPORT_GENERATOR_DATA_REQUEST) ingest 테스트.

레이아웃: columns[:7]=meta(SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO), [7:]=item.
row0 TSEQ / row1 TNO / row2 STEP / row3 UNIT / row4 HILIM / row5 LOLIM / row6+ 측정.
fail 식별 = FAILTNO(serial이 fail한 test의 TNO) == item의 TNO.
"""
import logging
import math

import pandas as pd
import pytest

from eval_engine import api, store
from eval_engine.pipeline import ingest

_COLS = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO", "VREF_TRIM", "IDDQ"]
_VREF_TNO, _IDDQ_TNO = 101, 202


def _new_df(n_pass=20, n_fail=4):
    """VREF_TRIM 이 n_fail 개 serial 에서 fail(FAILTNO=101, bin18, edge). IDDQ 는 무fail."""
    nan = float("nan")
    meta_rows = [
        ["TSEQ", None, None, None, None, None, None, 1, 2],
        ["TNO", None, None, None, None, None, None, _VREF_TNO, _IDDQ_TNO],
        ["STEP", None, None, None, None, None, None, "P2", "P1"],
        ["UNIT", None, None, None, None, None, None, "V", "A"],
        ["HILIM", None, None, None, None, None, None, 1.4, 15.0],
        ["LOLIM", None, None, None, None, None, None, 1.0, nan],
    ]
    data_rows = []
    s = 1
    for i in range(n_pass):  # pass: bin1, 중앙부, spec 내, FAILTNO 없음
        data_rows.append([s, 1, s, i % 5, i // 5, 1, nan,
                          1.20 + 0.01 * (i % 3), 12.0 + 0.01 * (i % 4)])
        s += 1
    for i in range(n_fail):  # fail: VREF_TRIM(FAILTNO=101), bin18, edge, usl 초과
        data_rows.append([s, 1, s, 50 + i, 50 + i, 18, _VREF_TNO,
                          1.55 + 0.02 * i, 12.1])
        s += 1
    return pd.DataFrame(meta_rows + data_rows, columns=_COLS)


def _meta():
    return {"product_name": "S5E_TEST_0000001", "family_product": "SOC",
            "product_type": "PMIC", "revision": 0.0, "lot_id": "LOT001",
            "wafer_number": 3}


def test_raw_df_fail_mapping_no_persist():
    run = {"meta": _meta(), "raw_df": _new_df()}
    ctx = ingest.ingest(run, persist=False)
    cases = ctx["cases"]
    # 모든 item 이 candidate 로 방출: VREF_TRIM fail(bin18) + IDDQ 무fail(PASS_BIN candidate)
    assert len(cases) == 2
    c = next(x for x in cases if x["bin"] == 18)
    assert c["item_canonical"] == "vref_trim"
    assert c["item_raw"] == "VREF_TRIM"      # 원본 item명 보존 (Issue Table join 키)
    assert c["value_type"] == "V"
    assert c["lsl"] == 1.0 and c["usl"] == 1.4
    # 분포는 전체 serial(측정된 값) 기준, fail_mask 는 4개만 True
    assert len(c["values"]) == 24
    assert sum(1 for f in c["fail_mask"] if f) == 4
    # 공간: fail serial 은 edge 좌표
    assert all(x is not None for x in c["x_pos"])
    # IDDQ 는 fail 없음 → PASS_BIN(1) candidate, fail_mask 전부 False (저장 판단은 이후 should_store)
    iddq = next(x for x in cases if x["bin"] == 1)
    assert iddq["item_canonical"] == "iddq"
    assert not any(iddq["fail_mask"])


def test_raw_df_e2e_fires_signature(fresh_db):
    result = api.evaluate({"meta": _meta(), "raw_df": _new_df()}, persist=True)
    assert result["run_id"] is not None
    cases = [c for c in result["cases"] if c["bin"] == 18]
    assert len(cases) == 1
    case = cases[0]
    assert case["item_canonical"] == "vref_trim"
    assert case["item_raw"] == "VREF_TRIM"
    assert case["issue_category"] in {"YIELD", "CPK", "ETC"}
    assert case["status"] in {"MAJOR", "CRITICAL"}
    assert case["primary_signature"] is not None
    with store.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM features").fetchone()[0] >= 1


def test_raw_df_failtno_blank_is_pass():
    """FAILTNO 공란/NaN serial 은 fail 로 잡히지 않는다. 이제 모든 item 은 PASS_BIN candidate
    로 방출되고(저장 여부는 이후 api.evaluate 의 should_store 판단), fail_mask 는 전부 False."""
    df = _new_df(n_pass=24, n_fail=0)
    ctx = ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)
    cases = ctx["cases"]
    assert len(cases) == 2                          # VREF_TRIM, IDDQ — 둘 다 무fail
    assert all(c["bin"] == 1 for c in cases)        # PASS_BIN candidate
    assert all(not any(c["fail_mask"]) for c in cases)


_LOWCPK_COLS = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO", "VOUT", "VREF_OK"]


def _lowcpk_df(n=30):
    """무fail(FAILTNO 공란) 두 item: VOUT 은 산포 넓어 cpk<1.33, VREF_OK 는 tight 해 cpk 높음."""
    nan = float("nan")
    meta_rows = [
        ["TSEQ", None, None, None, None, None, None, 1, 2],
        ["TNO", None, None, None, None, None, None, 303, 404],
        ["STEP", None, None, None, None, None, None, "P2", "P2"],
        ["UNIT", None, None, None, None, None, None, "V", "V"],
        ["HILIM", None, None, None, None, None, None, 1.4, 2.0],
        ["LOLIM", None, None, None, None, None, None, 1.0, 0.0],
    ]
    data_rows = []
    for i in range(n):  # 전부 bin1, FAILTNO 공란(무fail)
        vout = 1.20 + 0.06 * ((i % 5) - 2)   # 1.08~1.32, 넓은 산포 → cpk 낮음
        vref = 1.00 + 0.02 * ((i % 3) - 1)   # 0.98~1.02, tight → cpk 높음
        data_rows.append([i + 1, 1, i + 1, i % 5, i // 5, 1, nan, vout, vref])
    return pd.DataFrame(meta_rows + data_rows, columns=_LOWCPK_COLS)


def test_lowcpk_nofail_is_stored(fresh_db):
    """yield fail 없어도 cpk<cpk_warn 이면 저장. cpk 높은 무fail item 은 저장 안 됨."""
    result = api.evaluate({"meta": _meta(), "raw_df": _lowcpk_df()}, persist=True)
    # VOUT 만 저장 대상(cpk 낮음), VREF_OK 는 제외(cpk 높고 무fail)
    assert len(result["cases"]) == 1
    case = result["cases"][0]
    assert case["item_canonical"] == "vout"
    assert case["bin"] == 1                       # PASS_BIN — yield fail 아닌 cpk 트리거
    with store.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0] == 1
        cpk = conn.execute("SELECT cpk FROM raw_metrics").fetchone()[0]
    assert cpk is not None and cpk < 1.33         # cpk<cpk_warn 이라 저장된 것


# --- 레이아웃 구조 선검증(_validate_raw_df) — 계약 위반 시 명확한 ValueError -------------

def test_raw_df_rejects_wrong_meta_columns():
    df = _new_df()
    df.columns = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "WRONG", "VREF_TRIM", "IDDQ"]
    with pytest.raises(ValueError, match="meta 컬럼"):
        ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)


def test_raw_df_rejects_reordered_meta_rows():
    df = _new_df()
    df.iloc[2, 0], df.iloc[3, 0] = "UNIT", "STEP"   # STEP↔UNIT 라벨 교환
    with pytest.raises(ValueError, match="메타행"):
        ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)


def test_raw_df_rejects_too_few_rows():
    df = pd.DataFrame([[lab, None, None, None, None, None, None, 1, 2]
                       for lab in ["TSEQ", "TNO", "STEP", "UNIT", "HILIM"]],  # 메타행 5개뿐
                      columns=_COLS)
    with pytest.raises(ValueError, match="메타행 6개"):
        ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)


def test_raw_df_rejects_duplicate_items():
    df = _new_df()
    df.columns = _COLS[:7] + ["IDDQ", "IDDQ"]        # item 컬럼 중복
    with pytest.raises(ValueError, match="중복"):
        ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)


def test_raw_df_warns_on_nonnumeric_items(caplog):
    """item 데이터셀이 전부 문자열 → 파서가 무시해 case 0. 하드에러 아님, warning 으로 legible."""
    df = _new_df(n_pass=4, n_fail=0)
    for col in ("VREF_TRIM", "IDDQ"):
        df.loc[6:, col] = df.loc[6:, col].astype(str)   # 데이터행만 문자열화
    with caplog.at_level(logging.WARNING):
        ctx = ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)
    assert ctx["cases"] == []
    assert "case 0" in caplog.text

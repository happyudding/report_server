"""df_honey → run_input 어댑터 (eval_engine 밖).

불변 규칙 1: eval_engine 은 report_server 를 import 하지 않는다. 이 어댑터는 호출자
(report_server) 쪽 코드 — df_honey 객체의 속성만 읽어 중립 dict(run_input)으로 변환한다.
실제 결합 시 이 함수를 report_server 의 report_generator 파이프라인 끝에 둔다
(HANDOFF_TO_REPORT_SERVER 의 build_run_input 역할). eval_engine 은 import 하지 않는다.
"""
import math


def _nan_to_none(x):
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return x


def df_honey_to_run_input(honey, meta_override=None):
    subjects = list(honey.subjects)
    units = dict(zip(subjects, honey.units))
    lower = {s: _nan_to_none(v) for s, v in zip(subjects, honey.lower_limits)}
    upper = {s: _nan_to_none(v) for s, v in zip(subjects, honey.upper_limits)}
    meta_df = honey.meta            # DUT/XCoord/YCoord/Bin/Serial
    scores = honey.numeric_scores   # 행=DUT, 열=subject idx

    rows = []
    for i in range(len(scores)):
        bin_raw = meta_df.iat[i, 3]
        try:
            bin_ = int(float(bin_raw))
        except (TypeError, ValueError):
            bin_ = 1                 # PASS_BIN fallback
        row = {"DUT": meta_df.iat[i, 0],
               "XCoord": _nan_to_none(meta_df.iat[i, 1]),
               "YCoord": _nan_to_none(meta_df.iat[i, 2]),
               "Bin": bin_,
               "Serial": meta_df.iat[i, 4]}
        for j, s in enumerate(subjects):
            val = scores.iat[i, j]
            row[s] = _nan_to_none(float(val)) if val == val else None  # NaN!=NaN
        rows.append(row)

    raw_table = {"meta_columns": ["DUT", "XCoord", "YCoord", "Bin", "Serial"],
                 "item_columns": subjects, "units": units,
                 "lower_limit": lower, "upper_limit": upper, "rows": rows}

    meta = {"product_name": "MASS_HUGE_TEST", "product_type": "PMIC", "revision": 0.0,
            "lot_id": "MASS_LOT", "wafer_number": 1, "family_product": "SOC",
            "source_file": "mass_huge_W01.csv", "ingested_by": "integration_test"}
    if meta_override:
        meta.update(meta_override)
    return {"meta": meta, "raw_table": raw_table}


def sample_csv_to_run_input(path, meta_override=None):
    """정본 raw_df 레이아웃 CSV → run_input(raw_df). report_server 인계용 레퍼런스.

    report_server 는 honey_parse 정규화 df 를 아래 레이아웃 그대로 in-memory 로 넘긴다
    (INTEGRATION_CONTRACT §3). 이 함수는 그 df 를 CSV 에서 재구성하는 어댑터로,
    dtype 계약을 명시한다: 데이터행 item 셀은 **실제 숫자 객체**여야 하고
    (ingest._is_num 이 문자열을 거부), 메타행(TNO/UNIT/HILIM/LOLIM)·좌표·BIN·FAILTNO 는
    문자열이어도 파서가 변환한다. eval_engine 은 import 하지 않는다.

    레이아웃: columns SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO,<item...>
      iloc row0 TSEQ / row1 TNO / row2 STEP / row3 UNIT / row4 HILIM / row5 LOLIM / row6+ 측정.
    """
    import pandas as pd
    df = pd.read_csv(path, header=0, dtype=str).astype(object)
    for col in list(df.columns[7:]):            # item 컬럼의 데이터행만 숫자화(메타행 0..5 은 유지)
        df.loc[6:, col] = pd.to_numeric(df.loc[6:, col], errors="coerce")

    meta = {"product_name": "S5E_SAMPLE_0000001", "product_type": "PMIC",
            "family_product": "SOC", "revision": 0.0, "lot_id": "LOT_SAMPLE",
            "wafer_number": 1, "source_file": str(path), "ingested_by": "integration_test"}
    if meta_override:
        meta.update(meta_override)
    return {"meta": meta, "raw_df": df}

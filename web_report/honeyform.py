"""7-meta honeyform DataFrame helpers.

Canonical layout:
columns: SERIAL, SHOT, DUT, XPOS, YPOS, BIN, FAILTNO, item...
row0..5: TSEQ, TNO, STEP, UNIT, HILIM, LOLIM
row6+  : measurement data
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pandas as pd

META_COLUMNS = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO"]
META_ROW_LABELS = ["TSEQ", "TNO", "STEP", "UNIT", "HILIM", "LOLIM"]
DATA_START_ROW = len(META_ROW_LABELS)
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"


@dataclass
class HoneyformTable:
    source: str
    file_name: str
    df: pd.DataFrame
    item_columns: list[str]
    tseq: dict[str, object]
    tno: dict[str, object]
    step: dict[str, object]
    units: dict[str, object]
    hilim: dict[str, object]
    lolim: dict[str, object]
    data: pd.DataFrame


def _norm(value) -> str:
    return str(value).strip().lstrip("\ufeff").upper()


def validate_honeyform_df(df: pd.DataFrame) -> list[str]:
    issues: list[str] = []
    if df is None:
        return ["df is None"]
    if not isinstance(df, pd.DataFrame):
        return [f"df must be pandas.DataFrame, got {type(df)!r}"]

    n_rows, n_cols = df.shape
    if n_cols < len(META_COLUMNS) + 1:
        issues.append("meta 7 columns + at least 1 item column are required")
    else:
        got_cols = [_norm(c) for c in list(df.columns[:len(META_COLUMNS)])]
        if got_cols != META_COLUMNS:
            issues.append(f"first 7 columns must be {META_COLUMNS}, got {got_cols}")

    if n_rows < DATA_START_ROW:
        issues.append(f"{DATA_START_ROW} metadata rows are required")
    elif n_cols:
        got_labels = [_norm(df.iloc[i, 0]) for i in range(DATA_START_ROW)]
        if got_labels != META_ROW_LABELS:
            issues.append(f"metadata row labels must be {META_ROW_LABELS}, got {got_labels}")

    item_cols = [str(c) for c in list(df.columns[len(META_COLUMNS):])]
    duplicates = sorted({c for c in item_cols if item_cols.count(c) > 1})
    if duplicates:
        issues.append(f"duplicate item columns: {duplicates}")
    if n_rows <= DATA_START_ROW:
        issues.append("at least 1 data row is required")
    return issues


def _require_parquet_engine() -> None:
    try:
        import pyarrow  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("pyarrow is required for web report parquet payloads") from exc


def _string_frame_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    for col in out.columns:
        out[col] = out[col].astype("string")
    return out


def encode_honeyform_parquet(df: pd.DataFrame, *, compression: str = "zstd") -> bytes:
    """Encode a validated honeyform DataFrame to parquet bytes."""
    issues = validate_honeyform_df(df)
    if issues:
        raise ValueError("; ".join(issues))
    _require_parquet_engine()
    buf = BytesIO()
    _string_frame_for_parquet(df).to_parquet(
        buf, index=False, engine="pyarrow", compression=compression)
    return buf.getvalue()


def decode_honeyform_parquet(data: bytes) -> pd.DataFrame:
    """Decode parquet bytes and restore item data rows to numeric values."""
    _require_parquet_engine()
    df = pd.read_parquet(BytesIO(data), engine="pyarrow")
    df = df.astype(object).where(pd.notna(df), None)
    issues = validate_honeyform_df(df)
    if issues:
        raise ValueError("; ".join(issues))
    for col in list(df.columns[len(META_COLUMNS):]):
        numeric = pd.to_numeric(df.loc[DATA_START_ROW:, col], errors="coerce")
        df.loc[DATA_START_ROW:, col] = numeric.astype(object)
    return df


def read_honeyform_file(path) -> pd.DataFrame:
    """Read a 7-meta honeyform CSV/xlsx file without legacy df_honey normalization."""
    p = Path(path)
    if p.suffix.lower() == ".xlsx":
        df = pd.read_excel(p, dtype=object)
    else:
        df = pd.read_csv(p, dtype=object, keep_default_na=False)
    issues = validate_honeyform_df(df)
    if issues:
        raise ValueError("; ".join(issues))
    return df


def _fmt_dut(value) -> str:
    """DUT 셀 값을 라벨 문자열로 정규화 (float '1.0' → '1', 공백/None → '(blank)')."""
    if value is None:
        return "(blank)"
    try:
        if pd.isna(value):
            return "(blank)"
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s:
        return "(blank)"
    # '1.0' 처럼 소수부가 0 인 실수 표기는 정수로 다듬는다 (DUT/site 는 정수 코드).
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return s


def split_table_by_dut(table: "HoneyformTable") -> list["HoneyformTable"]:
    """단일 HoneyformTable 을 DUT 컬럼 값별로 분할 — 각 DUT 가 새 source 가 된다 (DUT 모드).

    meta(tno/step/units/hilim/lolim/item_columns)는 공유하고 data/df 만 필터한다.
    DUT 종류가 1개 이하면 분할이 무의미하므로 원본을 그대로 담아 반환한다.
    다운샘플 없음 — 모든 die 를 해당 DUT source 로 보존한다 (규칙 #6).
    """
    data = table.data
    labels = data["DUT"].map(_fmt_dut)
    uniq = list(dict.fromkeys(labels.tolist()))   # 등장 순서 유지
    if len(uniq) <= 1:
        return [table]

    meta_rows = table.df.iloc[:DATA_START_ROW]
    data_rows = table.df.iloc[DATA_START_ROW:].reset_index(drop=True)
    out: list[HoneyformTable] = []
    for label in uniq:
        mask = (labels == label).to_numpy()
        sub_data = data[mask].reset_index(drop=True)
        sub_df = pd.concat([meta_rows, data_rows[mask]], ignore_index=True)
        out.append(HoneyformTable(
            source=f"DUT {label}",
            file_name=f"{table.file_name} · DUT {label}",
            df=sub_df,
            item_columns=list(table.item_columns),
            tseq=dict(table.tseq),
            tno=dict(table.tno), step=dict(table.step), units=dict(table.units),
            hilim=dict(table.hilim), lolim=dict(table.lolim),
            data=sub_data,
        ))
    return out


def split_honeyform(df: pd.DataFrame, source: str, file_name: str = "") -> HoneyformTable:
    issues = validate_honeyform_df(df)
    if issues:
        raise ValueError("; ".join(issues))
    item_cols = [str(c) for c in list(df.columns[len(META_COLUMNS):])]
    data = df.iloc[DATA_START_ROW:].reset_index(drop=True).copy()
    for col in item_cols:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    return HoneyformTable(
        source=source,
        file_name=file_name or source,
        df=df,
        item_columns=item_cols,
        tseq={c: df.at[0, c] for c in item_cols},
        tno={c: df.at[1, c] for c in item_cols},
        step={c: df.at[2, c] for c in item_cols},
        units={c: df.at[3, c] for c in item_cols},
        hilim={c: df.at[4, c] for c in item_cols},
        lolim={c: df.at[5, c] for c in item_cols},
        data=data,
    )

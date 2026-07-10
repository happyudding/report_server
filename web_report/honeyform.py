"""7-meta honeyform DataFrame helpers.

Canonical layout:
columns: SERIAL, SHOT, DUT, XPOS, YPOS, BIN, FAILTNO, item...
row0..5: TSEQ, TNO, STEP, UNIT, HILIM, LOLIM
row6+  : measurement data
"""
from __future__ import annotations

from collections import Counter
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
    counts = Counter(item_cols)
    duplicates = sorted(c for c, n in counts.items() if n > 1)
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
    # per-column astype 루프(2000+ 컬럼 × 프레임 재배치) 대신 일괄 변환 — 값 동일, 속도만 개선
    out = df.astype("string")
    out.columns = [str(c).strip() for c in out.columns]
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


def _numeric_item_block(frame: pd.DataFrame, item_labels: list) -> pd.DataFrame:
    """item 컬럼 블록을 per-column pd.to_numeric 으로 변환해 DataFrame 으로 반환.

    per-column 변환을 유지하는 이유: 정수 전용 컬럼은 int64, 그 외는 float64 로
    dtype 이 갈리는 기존 동작(payload 의 1 vs 1.0 표기)을 보존해야 한다.
    다만 2000+ 컬럼 각각을 .loc 로 조회/대입하면 프레임 재배치로 수 초가 걸리므로
    numpy 블록에서 변환해 한 번에 조립한다 (값·dtype 동일, 속도만 개선).
    """
    vals = frame[item_labels].to_numpy(dtype=object)
    conv = {c: pd.to_numeric(vals[:, j], errors="coerce")
            for j, c in enumerate(item_labels)}
    return pd.DataFrame(conv, index=frame.index, columns=item_labels)


def _object_with_none(frame: pd.DataFrame) -> pd.DataFrame:
    # astype(object).where(...) 는 컬럼(2000+) 단위 오버헤드가 커서 numpy 로 일괄 처리
    vals = frame.to_numpy(dtype=object)
    vals[pd.isna(vals)] = None
    return pd.DataFrame(vals, index=frame.index, columns=frame.columns)


def _decode_parts(data: bytes):
    """parquet bytes → 검증 후 (head, tail_meta, num_df, item_labels) 조립 부품.

    item 데이터 셀(전체의 99%)은 어차피 numeric 으로 변환하므로 object+None 변환은
    메타 행/컬럼에만 적용한다 (전체 프레임 astype(object).where 왕복 제거).
    """
    _require_parquet_engine()
    raw = pd.read_parquet(BytesIO(data), engine="pyarrow")
    issues = validate_honeyform_df(raw)
    if issues:
        raise ValueError("; ".join(issues))
    item_labels = list(raw.columns[len(META_COLUMNS):])
    meta_labels = list(raw.columns[:len(META_COLUMNS)])
    head = _object_with_none(raw.iloc[:DATA_START_ROW])
    tail = raw.iloc[DATA_START_ROW:]
    tail_meta = _object_with_none(tail[meta_labels])
    num_df = _numeric_item_block(tail, item_labels) if item_labels else None
    return head, tail_meta, num_df, item_labels


def _assemble_df(head, tail_meta, num_df) -> pd.DataFrame:
    if num_df is None:
        df = pd.concat([head, tail_meta], axis=0)
    else:
        df = pd.concat(
            [head, pd.concat([tail_meta, num_df.astype(object)], axis=1)], axis=0)
    df.index = pd.RangeIndex(len(df))
    return df


def decode_honeyform_parquet(data: bytes) -> pd.DataFrame:
    """Decode parquet bytes and restore item data rows to numeric values."""
    head, tail_meta, num_df, _ = _decode_parts(data)
    return _assemble_df(head, tail_meta, num_df)


def decode_split_honeyform_parquet(data: bytes, *, source: str,
                                   file_name: str = "") -> HoneyformTable:
    """decode + split 을 한 번에 수행 (loader 전용 빠른 경로).

    ``split_honeyform(decode_honeyform_parquet(data), ...)`` 과 결과가 동일하지만,
    item 블록 to_numeric 1회분과 재검증을 생략한다 (콜드 로드 시간 절반).
    """
    head, tail_meta, num_df, item_labels = _decode_parts(data)
    df = _assemble_df(head, tail_meta, num_df)
    item_cols = [str(c) for c in item_labels]
    if num_df is None:
        data_frame = tail_meta.reset_index(drop=True)
    else:
        data_frame = pd.concat([tail_meta.reset_index(drop=True),
                                num_df.reset_index(drop=True)], axis=1)
    # 메타 6행 dict 를 df.at 라벨 조회(항목수×6회) 대신 head 블록에서 일괄 추출
    meta_rows = head[item_labels].to_numpy(dtype=object)
    def _row(i):
        return dict(zip(item_cols, meta_rows[i]))
    return HoneyformTable(
        source=source,
        file_name=file_name or source,
        df=df,
        item_columns=item_cols,
        tseq=_row(0),
        tno=_row(1),
        step=_row(2),
        units=_row(3),
        hilim=_row(4),
        lolim=_row(5),
        data=data_frame,
    )


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
    meta_labels = list(df.columns[:len(META_COLUMNS)])
    data = df.iloc[DATA_START_ROW:].reset_index(drop=True)
    if item_cols:
        # per-column to_numeric 의 dtype 동작을 유지하며 블록 단위로 변환 (decode 참조)
        data = pd.concat([data[meta_labels].copy(),
                          _numeric_item_block(data, item_cols)], axis=1)
    else:
        data = data.copy()
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

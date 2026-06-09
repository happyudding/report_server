"""CSV / xlsx sheet 로딩 → 정규화 → df_honey 포맷 DataFrame / 구성 요소.

_reference/analysis/preprocessor_fromhoney.py + data_loader.py 의 순수 로직만 이식.
config / analysis 패키지 의존 없음. 정규화 표준 = 5-meta (constants 참조).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .constants import (
    DATA_START_ROW, LOWER_LIMIT_ROW, META_COLUMNS, N_META_COLUMNS,
    UNITS_ROW, UPPER_LIMIT_ROW,
)
from .file_to_df import DF_YIELD_COLUMNS
from .file_to_df import file_to_df as _file_to_df_impl


def file_to_df(path, product_type=None, progress_cb=None) -> tuple:
    """외부 file_to_df 래퍼. progress_cb 는 현재 미전달.

    **불변 보증 지점**: 반환 df 는 항상 canonical 구조(헤더는 df.columns 로만, row0=Units).
    실제 honey_parse 가 계약대로 row0=헤더(중복)를 주더라도 여기서 1행 드롭해 정합시킨다.

    브랜치 교체 시 이 한 줄만 수정:
        df, df_yield = _file_to_df_impl(path, progress_cb=progress_cb)
    """
    df, df_yield = _file_to_df_impl(path, product_type=product_type)
    return _ensure_canonical(df), df_yield


def _ensure_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """row0 이 헤더(==df.columns) 중복이면 1행 드롭 → row0=Units 보장. 아니면 그대로."""
    if df is None or getattr(df, "empty", True):
        return df
    if list(df.iloc[0]) == list(df.columns):
        return df.iloc[1:].reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# raw 읽기

def _read_raw(path: Path) -> pd.DataFrame:
    """CSV/xlsx 파일을 header=None raw DataFrame 으로 읽음."""
    if path.suffix.lower() == ".xlsx":
        return pd.read_excel(path, header=None, dtype=object)
    try:
        return pd.read_csv(path, header=None, dtype=object, keep_default_na=False)
    except Exception:
        return pd.read_csv(
            path, header=None, dtype=object, keep_default_na=False,
            engine="python", on_bad_lines="skip",
        )


# ---------------------------------------------------------------------------
# 포맷 정규화 (preprocessor_fromhoney 이식)

def normalize_raw(raw: pd.DataFrame) -> pd.DataFrame:
    """raw(header=None) → 표준 5-meta DataFrame (columns = row0 값)."""
    if raw is None or raw.empty or raw.shape[1] < 5:
        return pd.DataFrame()
    fmt = _detect_format(raw)
    if fmt == "test_rp":
        return _normalize_test_rp(raw)
    return _normalize_standard(raw)


def _detect_format(raw: pd.DataFrame) -> str:
    """raw 첫 100행 첫 열을 보고 'standard' 또는 'test_rp' 포맷 판별."""
    n = min(100, raw.shape[0])
    for i in range(n):
        v = raw.iat[i, 0]
        if v is None:
            continue
        first = str(v).strip().lower()
        if i == 0 and first == "dut":
            return "standard"
        if first == "site #":
            return "test_rp"
    return "standard"


def _normalize_standard(raw: pd.DataFrame) -> pd.DataFrame:
    """standard 포맷 raw → 5-meta 표준 DataFrame (4-meta면 Serial 열 삽입)."""
    df = raw.copy()
    # 4-meta → 5-meta (Serial 컬럼 삽입)
    if str(df.iat[0, 4]).strip().lower() != "serial":
        left = df.iloc[:, :4].reset_index(drop=True)
        right = df.iloc[:, 4:].reset_index(drop=True)
        serial = pd.DataFrame({"_s": [None] * len(df)})
        df = pd.concat([left, serial, right], axis=1, ignore_index=True)
        df.iat[0, 4] = "Serial"
    _canonicalize_row_labels(df)
    if df.shape[0] >= 6:
        _fill_duplicate_limit_rows(df)
    df.columns = df.iloc[0].tolist()
    # 헤더는 df.columns 로만 보존하고 중복 row0(원본 헤더)은 제거 → row0=Units (불변 구조)
    return df.iloc[1:].reset_index(drop=True)


def _normalize_test_rp(raw: pd.DataFrame) -> pd.DataFrame:
    """test_rp 포맷(Test Name/Site # 행 기반) raw → 5-meta 표준 DataFrame."""
    def _find(token_lower, require_data=False):
        for i in range(raw.shape[0]):
            v = str(raw.iat[i, 0]).strip().lower()
            if v != token_lower:
                continue
            if require_data:
                tail = [str(x).strip() for x in raw.iloc[i, 5:]]
                if not any(tail):
                    continue
            return i
        return None

    r_subject = _find("test name", require_data=True)
    r_lower = _find("lower limit", require_data=True)
    r_upper = _find("upper limit", require_data=True)
    r_units = _find("units", require_data=True)
    r_metahdr = _find("site #", require_data=False)
    if None in (r_subject, r_lower, r_upper, r_units, r_metahdr):
        return pd.DataFrame()

    metahdr = [str(x).strip().lower() for x in raw.iloc[r_metahdr, :5]]
    name_to_col = {name: i for i, name in enumerate(metahdr)}
    try:
        src_order = [name_to_col[k] for k in ("site #", "xcoord", "ycoord", "bin", "shot")]
    except KeyError:
        return pd.DataFrame()

    subjects = raw.iloc[r_subject, 5:].tolist()
    units = raw.iloc[r_units, 5:].tolist()
    lo = raw.iloc[r_lower, 5:].tolist()
    hi = raw.iloc[r_upper, 5:].tolist()
    header_rows = [
        ["DUT", "XCoord", "YCoord", "Bin", "Serial", *subjects],
        ["Units", None, None, None, None, *units],
        ["Lower Limit", None, None, None, None, *lo],
        ["Upper Limit", None, None, None, None, *hi],
        ["Lower Limit", None, None, None, None, *lo],
        ["Upper Limit", None, None, None, None, *hi],
    ]
    data_block = raw.iloc[r_metahdr + 1:].reset_index(drop=True)
    meta_part = data_block.iloc[:, :5].iloc[:, src_order].reset_index(drop=True)
    score_part = data_block.iloc[:, 5:].reset_index(drop=True)
    data_part = pd.concat([meta_part, score_part], axis=1)
    data_part.columns = range(data_part.shape[1])

    head_df = pd.DataFrame(header_rows, dtype=object)
    out = pd.concat([head_df, data_part], ignore_index=True)
    out.columns = out.iloc[0].tolist()
    # 헤더 중복 row0 제거 → row0=Units (불변 구조)
    return out.iloc[1:].reset_index(drop=True)


def _canonicalize_row_labels(df: pd.DataFrame) -> None:
    """row 1~5 의 첫 열 레이블을 표준(Units/Lower Limit/Upper Limit…)으로 고정, meta 열 None."""
    label_map = {1: "Units", 2: "Lower Limit", 3: "Upper Limit",
                 4: "Lower Limit", 5: "Upper Limit"}
    for row_idx, label in label_map.items():
        if row_idx >= df.shape[0]:
            continue
        df.iat[row_idx, 0] = label
        for col in range(1, 5):
            df.iat[row_idx, col] = None


def _fill_duplicate_limit_rows(df: pd.DataFrame) -> None:
    """row 4·5 가 비어 있으면 row 2·3 값을 복사해 표준 6행 헤더 구조를 완성."""
    for src, dst in ((2, 4), (3, 5)):
        tail_dst = df.iloc[dst, 5:]
        if all((v is None) or (isinstance(v, float) and pd.isna(v)) or (str(v).strip() == "")
               for v in tail_dst):
            df.iloc[dst, :] = df.iloc[src, :].values


# ---------------------------------------------------------------------------
# 정규화 df → 구성 요소 분리 (data_loader.load_table 이식)

def split_components(norm: pd.DataFrame) -> dict:
    """정규화 DataFrame → subjects/units/limits/scores/meta dict."""
    row = lambda r: norm.iloc[r, N_META_COLUMNS:]
    subjects = [str(s) for s in norm.columns[N_META_COLUMNS:].tolist()]
    units = [str(u) if pd.notna(u) else "" for u in row(UNITS_ROW).tolist()]
    lo = pd.to_numeric(row(LOWER_LIMIT_ROW), errors="coerce").tolist()
    hi = pd.to_numeric(row(UPPER_LIMIT_ROW), errors="coerce").tolist()
    block = norm.iloc[DATA_START_ROW:].reset_index(drop=True)
    meta = block.iloc[:, :N_META_COLUMNS].copy()
    meta.columns = META_COLUMNS
    scores = block.iloc[:, N_META_COLUMNS:].copy()
    scores.columns = range(len(subjects))
    return {
        "subjects": subjects,
        "units": units,
        "lower_limits": lo,
        "upper_limits": hi,
        "scores": scores,
        "meta": meta,
    }


def load_components(path) -> dict:
    """파일 경로 → 구성 요소 dict (정규화 + 분리). 실패 시 ValueError."""
    df, _ = file_to_df(path)
    return split_components(df)

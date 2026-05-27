from dataclasses import dataclass

import pandas as pd

from config import (
    UPPER_LIMIT_ROW, LOWER_LIMIT_ROW, META_COLUMNS, N_META_COLUMNS,
    DATA_START_ROW, SUBJECT_NAME_ROW, UNITS_ROW,
)
from analysis.file_handling import csvfile_to_df


@dataclass
class ExcelData:
    subjects: list
    units: list
    lower_limits: list
    upper_limits: list
    scores: pd.DataFrame
    meta: pd.DataFrame


def load_table(path):
    raw = csvfile_to_df(path)
    row = lambda r: raw.iloc[r, N_META_COLUMNS:]
    subjects = [str(s) for s in row(SUBJECT_NAME_ROW).tolist()]
    units = [str(u) if pd.notna(u) else "" for u in row(UNITS_ROW).tolist()]
    lo = pd.to_numeric(row(LOWER_LIMIT_ROW), errors="coerce").tolist()
    hi = pd.to_numeric(row(UPPER_LIMIT_ROW), errors="coerce").tolist()
    block = raw.iloc[DATA_START_ROW:].reset_index(drop=True)
    meta = block.iloc[:, :N_META_COLUMNS].copy()
    meta.columns = META_COLUMNS
    scores = block.iloc[:, N_META_COLUMNS:].copy()
    scores.columns = range(len(subjects))
    return ExcelData(subjects, units, lo, hi, scores, meta)

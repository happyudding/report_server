from dataclasses import dataclass

import pandas as pd

from config import (
    HI_LIMIT_ROW, LO_LIMIT_ROW, META_COLUMNS, N_META_COLUMNS,
    STUDENT_DATA_START_ROW, SUBJECT_NAME_ROW, UNIT_ROW,
)


@dataclass
class ExcelData:
    subjects: list
    units: list
    lo_limits: list
    hi_limits: list
    scores: pd.DataFrame
    meta: pd.DataFrame


def load_table(path):
    reader = pd.read_csv if path.suffix.lower() == ".csv" else pd.read_excel
    raw = reader(path, header=None)
    row = lambda r: raw.iloc[r, N_META_COLUMNS:]
    subjects = [str(s) for s in row(SUBJECT_NAME_ROW).tolist()]
    units = [str(u) if pd.notna(u) else "" for u in row(UNIT_ROW).tolist()]
    lo = pd.to_numeric(row(LO_LIMIT_ROW), errors="coerce").tolist()
    hi = pd.to_numeric(row(HI_LIMIT_ROW), errors="coerce").tolist()
    block = raw.iloc[STUDENT_DATA_START_ROW:].reset_index(drop=True)
    meta = block.iloc[:, :N_META_COLUMNS].copy()
    meta.columns = META_COLUMNS
    scores = block.iloc[:, N_META_COLUMNS:].copy()
    scores.columns = range(len(subjects))
    return ExcelData(subjects, units, lo, hi, scores, meta)

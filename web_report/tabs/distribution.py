"""Distribution tab payload builder."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import json_safe, round_num


def to_numeric_clean(series):
    """Series → float64 배열 (유한값만, NaN·inf 제거)."""
    arr = pd.to_numeric(series, errors="coerce")
    return arr[np.isfinite(arr)].to_numpy()


def cumulative_distribution_full(values):
    """고유값별 누적 분포(ECDF) 계산. 반환: (unique_vals, cumulative_percent)."""
    if values.size == 0:
        return np.empty(0), np.empty(0)
    unique_vals, counts = np.unique(np.sort(values), return_counts=True)
    cum = np.cumsum(counts) / values.size * 100.0
    return unique_vals, cum


def build_distribution_rows(tables, all_items):
    rows = []
    for item in all_items:
        for table in tables:
            if item not in table.item_columns:
                continue
            values = to_numeric_clean(table.data[item])
            unique_vals, cum = cumulative_distribution_full(values)
            units = json_safe(table.units.get(item)) or ""
            lower_limit = round_num(table.lolim.get(item))
            upper_limit = round_num(table.hilim.get(item))
            for x, pct in zip(unique_vals, cum):
                rows.append({
                    "subject": item,
                    "source": table.source,
                    "units": units,
                    "lower_limit": lower_limit,
                    "upper_limit": upper_limit,
                    "value": round_num(x),
                    "cum_pct": round_num(pct, 3),
                })
    return rows

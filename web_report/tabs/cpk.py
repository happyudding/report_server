"""CPK tab payload builder."""
from __future__ import annotations

import pandas as pd

from .common import json_safe, num, round_num


def _stats(series, lo, hi):
    s = pd.to_numeric(series, errors="coerce").dropna()
    n = int(len(s))
    avg = s.mean() if n else None
    stdev = s.std(ddof=1) if n > 1 else None
    lo_n = num(lo)
    hi_n = num(hi)
    can = (
        n > 1
        and stdev not in (None, 0)
        and num(stdev) is not None
        and lo_n is not None
        and hi_n is not None
    )
    cp = cpl = cpu = cpk = None
    if can:
        cp = (hi_n - lo_n) / (6.0 * stdev)
        cpl = (avg - lo_n) / (3.0 * stdev)
        cpu = (hi_n - avg) / (3.0 * stdev)
        cpk = min(cpl, cpu)
    return {
        "n": n,
        "min": round_num(s.min() if n else None),
        "median": round_num(s.median() if n else None),
        "max": round_num(s.max() if n else None),
        "average": round_num(avg),
        "stdev": round_num(stdev, 3),
        "cp": round_num(cp, 3),
        "cpl": round_num(cpl, 3),
        "cpu": round_num(cpu, 3),
        "cpk": round_num(cpk, 3),
    }


def build_cpk_rows(tables, all_items):
    rows = []
    for item in all_items:
        for table in tables:
            if item not in table.item_columns:
                continue
            rows.append({
                "subject": item,
                "source": table.source,
                "units": json_safe(table.units.get(item)) or "",
                "lower_limit": round_num(table.lolim.get(item)),
                "upper_limit": round_num(table.hilim.get(item)),
                **_stats(table.data[item], table.lolim.get(item), table.hilim.get(item)),
            })
    return rows


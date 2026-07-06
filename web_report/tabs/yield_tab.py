"""Yield tab payload builder."""
from __future__ import annotations

from collections import Counter, defaultdict

import pandas as pd

from .common import PASS_BIN, fmt_type


def _tno_norm(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        f = float(value)
        if f == 0:
            return None
        if f.is_integer():
            return int(f)
        return f
    except (TypeError, ValueError):
        text = str(value).strip()
        return text or None


def _bin_sort_key(value):
    text = str(value)
    try:
        return (0, float(text))
    except ValueError:
        return (1, text)


def fail_counts_by_source(table) -> Counter:
    tno_to_item = defaultdict(list)
    for item, tno in table.tno.items():
        norm = _tno_norm(tno)
        if norm is not None:
            tno_to_item[norm].append(item)

    counts = Counter()
    for _, row in table.data.iterrows():
        fail_tno = _tno_norm(row.get("FAILTNO"))
        if fail_tno is None:
            continue
        bin_value = fmt_type(row.get("BIN"))
        for item in tno_to_item.get(fail_tno, []):
            counts[(bin_value, item)] += 1
    return counts


def _item_meta(tables):
    out = {}
    for table in tables:
        for item in table.item_columns:
            out.setdefault(item, {
                "step": fmt_type(table.step.get(item)),
                "tno": fmt_type(table.tno.get(item)),
            })
    return out


def build_yield_rows(tables, fail_counts):
    rows = []
    totals = {t.source: len(t.data) for t in tables}
    item_meta = _item_meta(tables)

    pass_row = {"step": "", "bin": PASS_BIN, "TNO": "", "Item": "Pass"}
    pass_portions = []
    for table in tables:
        bins = table.data["BIN"].map(fmt_type)
        count = int((bins == PASS_BIN).sum())
        portion = round(count / totals[table.source] * 100.0, 2) if totals[table.source] else 0.0
        pass_row[f"{table.source}_yield"] = portion
        pass_row[f"{table.source}_count"] = count
        pass_portions.append(portion)
    pass_row["avg"] = round(sum(pass_portions) / len(pass_portions), 2) if pass_portions else 0.0
    pass_row["comment"] = ""
    rows.append(pass_row)

    keys = sorted(
        {key for counts in fail_counts.values() for key in counts.keys() if key[0] != PASS_BIN},
        key=lambda key: (_bin_sort_key(key[0]), str(key[1])),
    )
    for bin_value, item in keys:
        meta = item_meta.get(item, {})
        row = {
            "step": meta.get("step", ""),
            "bin": bin_value,
            "TNO": meta.get("tno", ""),
            "Item": item,
        }
        portions = []
        total_count = 0
        for table in tables:
            count = int(fail_counts[table.source].get((bin_value, item), 0))
            portion = round(count / totals[table.source] * 100.0, 2) if totals[table.source] else 0.0
            row[f"{table.source}_yield"] = portion
            row[f"{table.source}_count"] = count
            portions.append(portion)
            total_count += count
        row["count"] = total_count
        row["avg"] = round(sum(portions) / len(portions), 2) if portions else 0.0
        row["comment"] = ""
        rows.append(row)
    return rows

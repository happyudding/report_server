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


def build_yield_rows(tables, fail_counts):
    rows = []
    all_bins = sorted(
        {b for t in tables for b in t.data["BIN"].map(fmt_type).tolist()},
        key=_bin_sort_key,
    )
    totals = {t.source: len(t.data) for t in tables}

    for bin_value in all_bins:
        row = {"bin": bin_value}
        counts = []
        portions = []
        top_counter = Counter()

        for t in tables:
            bins = t.data["BIN"].map(fmt_type)
            count = int((bins == bin_value).sum())
            portion = round(count / totals[t.source] * 100.0, 2) if totals[t.source] else 0.0
            row[f"{t.source}_count"] = count
            row[f"{t.source}_yield"] = portion
            counts.append(count)
            portions.append(portion)

            for (b, item), fail_count in fail_counts[t.source].items():
                if b == bin_value:
                    top_counter[item] += fail_count

        row["count"] = sum(counts)
        row["avg"] = round(sum(portions) / len(portions), 2) if portions else 0.0
        row["Main Fail subject"] = "Pass" if bin_value == PASS_BIN else (
            top_counter.most_common(1)[0][0] if top_counter else "N/A")
        row["comment"] = ""
        rows.append(row)
    return rows


"""Web report metrics from 7-meta honeyform tables."""
from __future__ import annotations

import math
from collections import Counter, defaultdict

import pandas as pd

from .honeyform import HoneyformTable, META_COLUMNS

PASS_BIN = "1"


def _json_safe(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _fmt_type(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    try:
        f = float(value)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _num(value):
    try:
        if value is None:
            return None
        f = float(value)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _round(value, digits=6):
    value = _num(value)
    return None if value is None else round(value, digits)


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


def _stats(series, lo, hi):
    s = pd.to_numeric(series, errors="coerce").dropna()
    n = int(len(s))
    avg = s.mean() if n else None
    stdev = s.std(ddof=1) if n > 1 else None
    lo_n = _num(lo)
    hi_n = _num(hi)
    can = n > 1 and stdev not in (None, 0) and _num(stdev) is not None and lo_n is not None and hi_n is not None
    cp = cpl = cpu = cpk = None
    if can:
        cp = (hi_n - lo_n) / (6.0 * stdev)
        cpl = (avg - lo_n) / (3.0 * stdev)
        cpu = (hi_n - avg) / (3.0 * stdev)
        cpk = min(cpl, cpu)
    return {
        "n": n,
        "min": _round(s.min() if n else None),
        "median": _round(s.median() if n else None),
        "max": _round(s.max() if n else None),
        "average": _round(avg),
        "stdev": _round(stdev, 3),
        "cp": _round(cp, 3),
        "cpl": _round(cpl, 3),
        "cpu": _round(cpu, 3),
        "cpk": _round(cpk, 3),
    }


def _fail_counts_by_source(table: HoneyformTable) -> Counter:
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
        bin_value = _fmt_type(row.get("BIN"))
        for item in tno_to_item.get(fail_tno, []):
            counts[(bin_value, item)] += 1
    return counts


def build_report_payload(tables: list[HoneyformTable], selected_items=None, sheets=None) -> dict:
    selected_set = {str(v) for v in (selected_items or []) if str(v)}
    if selected_set:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected_set]

    sources = [{"name": t.source, "file_name": t.file_name} for t in tables]
    all_items = sorted({item for t in tables for item in t.item_columns})
    total_rows = sum(len(t.data) for t in tables)
    pass_rows = sum((t.data["BIN"].map(_fmt_type) == PASS_BIN).sum() for t in tables)

    summary = [
        {"metric": "Sources", "value": len(tables)},
        {"metric": "Total DUT", "value": int(total_rows)},
        {"metric": "Pass Bin1", "value": int(pass_rows)},
        {"metric": "Pass Yield (%)", "value": round(pass_rows / total_rows * 100.0, 2) if total_rows else 0.0},
        {"metric": "Item Count", "value": len(all_items)},
    ]

    fail_counts = {t.source: _fail_counts_by_source(t) for t in tables}
    yield_rows = _build_yield(tables, fail_counts)
    cpk_rows = _build_cpk(tables, all_items)
    fail_item = _build_fail_item(yield_rows, fail_counts)
    issue = _build_issue_table(yield_rows, cpk_rows)
    distribution = _build_distribution(tables, all_items)
    raw = _build_raw_preview(tables)

    return {
        "sources": sources,
        "sheets": {
            "Summary": summary,
            "Yield": yield_rows,
            "CPK": cpk_rows,
            "Fail Item": fail_item,
            "Issue Table": issue,
            "Raw": raw,
        },
        "distribution": distribution,
        "selected_items": sorted(selected_set),
        "requested_sheets": list(sheets or []),
    }


def _build_yield(tables, fail_counts):
    rows = []
    all_bins = sorted(
        {b for t in tables for b in t.data["BIN"].map(_fmt_type).tolist()},
        key=_bin_sort_key,
    )
    totals = {t.source: len(t.data) for t in tables}
    for bin_value in all_bins:
        row = {"bin": bin_value}
        counts = []
        portions = []
        top_counter = Counter()
        for t in tables:
            bins = t.data["BIN"].map(_fmt_type)
            count = int((bins == bin_value).sum())
            portion = round(count / totals[t.source] * 100.0, 2) if totals[t.source] else 0.0
            row[f"{t.source}_count"] = count
            row[f"{t.source}_yield"] = portion
            counts.append(count)
            portions.append(portion)
            for (b, item), fail_count in fail_counts[t.source].items():
                if b == bin_value:
                    top_counter[item] += fail_count
        total_count = sum(counts)
        row["count"] = total_count
        row["avg"] = round(sum(portions) / len(portions), 2) if portions else 0.0
        row["Main Fail subject"] = "Pass" if bin_value == PASS_BIN else (
            top_counter.most_common(1)[0][0] if top_counter else "N/A")
        row["comment"] = ""
        rows.append(row)
    return rows


def _build_cpk(tables, all_items):
    rows = []
    for item in all_items:
        for t in tables:
            if item not in t.item_columns:
                continue
            stats = _stats(t.data[item], t.lolim.get(item), t.hilim.get(item))
            rows.append({
                "subject": item,
                "source": t.source,
                "units": _json_safe(t.units.get(item)) or "",
                "lower_limit": _round(t.lolim.get(item)),
                "upper_limit": _round(t.hilim.get(item)),
                **stats,
                "comment": "",
            })
    return rows


def _build_fail_item(yield_rows, fail_counts):
    rows = []
    for y in yield_rows:
        bin_value = y["bin"]
        subjects = Counter()
        for counts in fail_counts.values():
            for (b, item), count in counts.items():
                if b == bin_value:
                    subjects[item] += count
        ranked = [
            {"subject": item, "count": count}
            for item, count in subjects.most_common()
        ]
        rows.append({
            **y,
            "Fail Subjects": "Pass" if bin_value == PASS_BIN else (
                "N/A" if not ranked else f"{len(ranked)} subjects"),
            "fail_subjects": ranked,
        })
    return rows


def _build_issue_table(yield_rows, cpk_rows):
    rows = []
    for y in yield_rows:
        if y.get("bin") == PASS_BIN:
            continue
        item = y.get("Main Fail subject")
        if not item or item == "N/A":
            continue
        rows.append({
            "Category": "Yield",
            "Bin": y.get("bin"),
            "Item": item,
            "avg": y.get("avg"),
            "comment": "",
            "개발 1차 comment": "",
            "PTE 1차 comment": "",
        })
    for row in cpk_rows:
        cpk = _num(row.get("cpk"))
        if cpk is not None and cpk < 1.33:
            rows.append({
                "Category": "CPK",
                "Bin": PASS_BIN,
                "Item": row.get("subject"),
                "avg": cpk,
                "comment": "",
                "개발 1차 comment": "",
                "PTE 1차 comment": "",
            })
    return rows


def _build_distribution(tables, all_items):
    subjects = []
    for idx, item in enumerate(all_items):
        traces = []
        lo = hi = unit = None
        for t in tables:
            if item not in t.item_columns:
                continue
            vals = pd.to_numeric(t.data[item], errors="coerce").dropna().sort_values().tolist()
            n = len(vals)
            if not n:
                continue
            lo = lo if lo is not None else _round(t.lolim.get(item))
            hi = hi if hi is not None else _round(t.hilim.get(item))
            unit = unit if unit is not None else (_json_safe(t.units.get(item)) or "")
            traces.append({
                "source": t.source,
                "x": [float(v) for v in vals],
                "y": [round((i + 1) / n * 100.0, 6) for i in range(n)],
            })
        if traces:
            subjects.append({
                "id": idx,
                "name": item,
                "unit": unit or "",
                "lo": lo,
                "hi": hi,
                "traces": traces,
            })
    return {"subjects": subjects, "sources": [t.source for t in tables]}


def _build_raw_preview(tables):
    rows = []
    for t in tables:
        preview = t.data[[c for c in META_COLUMNS if c in t.data.columns] + t.item_columns].head(200)
        for row in preview.to_dict("records"):
            rows.append({"source": t.source, **{k: _json_safe(v) for k, v in row.items()}})
    return rows


def summary_rows_for_db(yield_rows):
    out = []
    for row in yield_rows:
        bin_value = row.get("bin")
        try:
            bin_number = int(float(bin_value))
        except (TypeError, ValueError):
            bin_number = None
        out.append({
            "item_name": str(bin_value),
            "bin_number": bin_number,
            "yield_percent": _num(row.get("avg")),
            "fail_count": int(row.get("count") or 0),
            "cpk_val": None,
            "mean_val": _num(row.get("avg")),
            "stdev_val": None,
            "lsl": None,
            "usl": None,
            "unit": None,
        })
    return out


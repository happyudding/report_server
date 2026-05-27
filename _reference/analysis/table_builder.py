import json
import math
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd

from config import DATASETS_DIR, META_COLUMNS

JSON_KWARGS = {"ensure_ascii": False, "separators": (",", ":")}
PASS_BIN = "1"


def _json_safe(value):
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if pd.isna(value):
        return None
    return value


def _fmt_type(value):
    if pd.isna(value):
        return ""
    try:
        f = float(value)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return str(value)


def _fmt_num(value, digits=6):
    value = _json_safe(value)
    if value is None:
        return "N/A"
    return round(float(value), digits)


def _fmt_metric(value):
    return _fmt_num(value, digits=3)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, **JSON_KWARGS), encoding="utf-8")


def _subject_columns(table):
    return [str(s) for s in table.subjects]


def _combined_frames(schools):
    frames = []
    for source_name, table in schools.items():
        meta = table.meta.reset_index(drop=True).copy()
        scores = table.scores.reset_index(drop=True).copy()
        scores.columns = _subject_columns(table)
        frame = pd.concat([meta, scores], axis=1)
        frame.insert(0, "source_file", source_name)
        frame["Bin"] = frame["Bin"].map(_fmt_type)
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["source_file", *META_COLUMNS])
    return pd.concat(frames, ignore_index=True)


def _fail_mask_for_table(table):
    numeric = table.scores.apply(pd.to_numeric, errors="coerce")
    arr = numeric.to_numpy(dtype="float64", copy=False)
    n_sub = arr.shape[1]

    def _lim(seq, i):
        if i >= len(seq):
            return np.nan
        v = seq[i]
        if v is None:
            return np.nan
        try:
            f = float(v)
        except (TypeError, ValueError):
            return np.nan
        return f

    lo = np.array([_lim(table.lower_limits, i) for i in range(n_sub)], dtype="float64")
    hi = np.array([_lim(table.upper_limits, i) for i in range(n_sub)], dtype="float64")

    with np.errstate(invalid="ignore"):
        fail = (arr < lo) | (arr > hi)
    return pd.DataFrame(fail, index=numeric.index, columns=numeric.columns, copy=False)


def _type_sort_key(value):
    text = str(value)
    try:
        return (0, float(text))
    except ValueError:
        return (1, text)


def _subject_rankings_by_type(schools):
    rankings = {}
    type_totals = {}
    first = next(iter(schools.values()))
    subject_names = _subject_columns(first)
    for _source_name, table in schools.items():
        fail_mask = _fail_mask_for_table(table)
        bin_types = table.meta["Bin"].map(_fmt_type)
        for bin_type in sorted(bin_types.unique(), key=_type_sort_key):
            rows = bin_types == bin_type
            row_count = int(rows.sum())
            type_totals[bin_type] = type_totals.get(bin_type, 0) + row_count
            if bin_type == PASS_BIN or row_count == 0:
                continue
            counts = fail_mask.loc[rows].sum(axis=0)
            bucket = rankings.setdefault(bin_type, {})
            for sid, count in counts.items():
                if int(count) <= 0:
                    continue
                subject_id = int(sid)
                item = bucket.setdefault(subject_id, {
                    "subject_id": subject_id,
                    "subject": subject_names[subject_id],
                    "count": 0,
                    "portion (%)": 0.0,
                })
                item["count"] += int(count)
    for bin_type, bucket in rankings.items():
        total = type_totals.get(bin_type, 0)
        subjects = []
        for item in bucket.values():
            item["portion (%)"] = round(item["count"] / total * 100.0, 3) if total else 0.0
            subjects.append(item)
        subjects.sort(key=lambda x: (-x["portion (%)"], -x["count"], x["subject"]))
        rankings[bin_type] = subjects
    return rankings


def _build_yield(schools):
    combined = _combined_frames(schools)
    total = len(combined)
    subject_rankings = _subject_rankings_by_type(schools)
    rows = []
    if total == 0:
        return rows
    sources = list(schools.keys())
    per_file_total = {}
    per_file_type_count = {}
    for source_name, table in schools.items():
        types = table.meta["Bin"].map(_fmt_type)
        per_file_total[source_name] = len(types)
        per_file_type_count[source_name] = types.value_counts(dropna=False).to_dict()
    counts = combined["Bin"].map(_fmt_type).value_counts(dropna=False)
    for bin_type, count in counts.sort_index(key=lambda s: s.map(_type_sort_key)).items():
        fail_subjects = subject_rankings.get(bin_type, [])
        portion_fields = {}
        portions = []
        for src in sources:
            file_total = per_file_total.get(src, 0)
            file_count = int(per_file_type_count.get(src, {}).get(bin_type, 0))
            portion = round(file_count / file_total * 100.0, 2) if file_total else 0.0
            portion_fields[f"portion_{src}"] = portion
            portions.append(portion)
        avg_portion = round(sum(portions) / len(portions), 2) if portions else 0.0
        rows.append({
            "bin": bin_type,
            "count": int(count),
            "portion (%)": round(int(count) / total * 100.0, 2),
            **portion_fields,
            "avg": avg_portion,
            "Main Fail subject": "Pass" if bin_type == PASS_BIN else (fail_subjects[0]["subject"] if fail_subjects else "N/A"),
            "comment": "",
        })
    return rows


def _calc_stats(series, lo, hi):
    series = series.dropna() if series is not None else pd.Series(dtype=float)
    n = len(series)
    stdev = series.std(ddof=1) if n > 1 else float("nan")
    avg = series.mean() if n else float("nan")
    can_calc = (
        n > 1 and stdev and not pd.isna(stdev) and stdev != 0
        and lo is not None and hi is not None and not pd.isna(lo) and not pd.isna(hi)
    )
    if can_calc:
        cp = (float(hi) - float(lo)) / (6.0 * stdev)
        cpl = (avg - float(lo)) / (3.0 * stdev)
        cpu = (float(hi) - avg) / (3.0 * stdev)
        cpk = min(cpl, cpu)
    else:
        cp = cpl = cpu = cpk = None
    return {
        "n": n,
        "min": _fmt_num(series.min() if n else None),
        "median": _fmt_num(series.median() if n else None),
        "max": _fmt_num(series.max() if n else None),
        "average": _fmt_num(avg),
        "stdev": _fmt_metric(stdev),
        "cp": _fmt_metric(cp),
        "cpl": _fmt_metric(cpl),
        "cpu": _fmt_metric(cpu),
        "cpk": _fmt_metric(cpk),
    }


def _build_cpk(schools):
    rows = []
    first = next(iter(schools.values()))
    for idx, subject in enumerate(first.subjects):
        lo = first.lower_limits[idx] if idx < len(first.lower_limits) else None
        hi = first.upper_limits[idx] if idx < len(first.upper_limits) else None
        unit = first.units[idx] if idx < len(first.units) else ""
        per_source = []
        for source_name, table in schools.items():
            series = pd.to_numeric(table.scores.iloc[:, idx], errors="coerce")
            per_source.append(series)
            rows.append({
                "subject": subject,
                "source": source_name,
                "units": unit,
                "lower_limit": _fmt_num(lo),
                "upper_limit": _fmt_num(hi),
                **_calc_stats(series, lo, hi),
            })
        total_series = pd.concat(per_source, ignore_index=True) if per_source else pd.Series(dtype=float)
        rows.append({
            "subject": subject,
            "source": "total",
            "units": unit,
            "lower_limit": _fmt_num(lo),
            "upper_limit": _fmt_num(hi),
            **_calc_stats(total_series, lo, hi),
        })
    return rows


def _build_fail_items(schools):
    yield_rows = _build_yield(schools)
    subject_rankings = _subject_rankings_by_type(schools)
    rows = []
    for row in yield_rows:
        bin_type = row["bin"]
        fail_subjects = [] if bin_type == PASS_BIN else subject_rankings.get(bin_type, [])
        rows.append({
            **row,
            "Fail Subjects": "Pass" if bin_type == PASS_BIN else ("N/A" if not fail_subjects else f"{len(fail_subjects)} subjects"),
            "fail_subjects": fail_subjects,
        })
    return {"rows": rows}


def build_table_artifacts(dataset_id, schools):
    out_dir = DATASETS_DIR / dataset_id
    tables_dir = out_dir / "tables"
    combined = _combined_frames(schools)
    first = next(iter(schools.values()))
    meta = {
        "dataset_id": dataset_id,
        "sources": list(schools.keys()),
        "row_count": int(len(combined)),
        "subjects": [
            {
                "subject_id": idx,
                "subject": subject,
                "units": first.units[idx] if idx < len(first.units) else "",
                "lower_limit": _json_safe(first.lower_limits[idx] if idx < len(first.lower_limits) else None),
                "upper_limit": _json_safe(first.upper_limits[idx] if idx < len(first.upper_limits) else None),
            }
            for idx, subject in enumerate(first.subjects)
        ],
        "raw_columns": combined.columns.tolist(),
    }
    _write_json(tables_dir / "meta.json", meta)
    _write_json(tables_dir / "yield.json", _build_yield(schools))
    _write_json(tables_dir / "cpk.json", _build_cpk(schools))
    _write_json(tables_dir / "fail_items.json", _build_fail_items(schools))
    return {"tables_dir": str(tables_dir), "row_count": meta["row_count"]}


def load_raw_page(dataset_id, page_current=0, page_size=25, sort_by=None, filter_query="", source_file=None):
    input_dir = DATASETS_DIR / dataset_id / "input"
    from analysis.data_loader import load_table

    schools = {p.stem: load_table(p) for p in sorted(input_dir.glob("*.csv"))}
    df = _combined_frames(schools)
    if source_file and source_file != "__all__":
        df = df[df["source_file"].astype(str) == str(source_file)]
    df = _apply_filter(df, filter_query or "")
    if sort_by:
        for sort in reversed(sort_by):
            col = sort.get("column_id")
            if col in df.columns:
                df = df.sort_values(col, ascending=sort.get("direction") != "desc", kind="mergesort")
    total = len(df)
    start = int(page_current or 0) * int(page_size or 25)
    end = start + int(page_size or 25)
    page = df.iloc[start:end].where(pd.notna(df), None)
    return page.to_dict("records"), total, df.columns.tolist()


def _apply_filter(df, filter_query):
    if not filter_query:
        return df
    filtered = df
    for expr in filter_query.split(" && "):
        if " contains " in expr:
            left, right = expr.split(" contains ", 1)
            col = left.strip().strip("{}")
            val = right.strip().strip("'\"")
            if col in filtered.columns:
                filtered = filtered[filtered[col].astype(str).str.contains(val, case=False, na=False)]
        elif " eq " in expr:
            left, right = expr.split(" eq ", 1)
            col = left.strip().strip("{}")
            val = right.strip().strip("'\"")
            if col in filtered.columns:
                filtered = filtered[filtered[col].astype(str) == val]
    return filtered


def read_table_json(dataset_id, name):
    path = DATASETS_DIR / dataset_id / "tables" / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_fail_values(dataset_id: str) -> list:
    """
    For every non-pass DUT (Bin != '1') in every input CSV,
    find each subject whose measured value is outside [lower_limit, upper_limit].

    Returns a flat list of dicts — one entry per (DUT, failing subject):
      source, dut, x_coord, y_coord, bin,
      subject, value, lower_limit, upper_limit, fail ("< lo" | "> hi")
    """
    from analysis.data_loader import load_table

    input_dir = DATASETS_DIR / dataset_id / "input"
    if not input_dir.exists():
        return []

    schools = {p.stem: load_table(p) for p in sorted(input_dir.glob("*.csv"))}
    all_rows = []

    for source_name, table in schools.items():
        subjects_list = _subject_columns(table)
        n_sub = len(subjects_list)

        meta = table.meta.reset_index(drop=True).copy()
        meta["Bin"] = meta["Bin"].map(_fmt_type)

        non_pass_mask = meta["Bin"] != PASS_BIN
        if not non_pass_mask.any():
            continue

        meta_np   = meta[non_pass_mask].reset_index(drop=True)
        scores_np = table.scores[non_pass_mask].reset_index(drop=True)
        numeric   = scores_np.apply(pd.to_numeric, errors="coerce")

        lo_arr = [table.lower_limits[i] if i < len(table.lower_limits) else None for i in range(n_sub)]
        hi_arr = [table.upper_limits[i] if i < len(table.upper_limits) else None for i in range(n_sub)]

        fail_lo = pd.DataFrame(False, index=numeric.index, columns=numeric.columns)
        fail_hi = pd.DataFrame(False, index=numeric.index, columns=numeric.columns)

        for idx in range(n_sub):
            lo, hi = lo_arr[idx], hi_arr[idx]
            col_s  = numeric.iloc[:, idx]
            if lo is not None and pd.notna(lo):
                fail_lo.iloc[:, idx] = (col_s < float(lo)).fillna(False)
            if hi is not None and pd.notna(hi):
                fail_hi.iloc[:, idx] = (col_s > float(hi)).fillna(False)

        fail_any = fail_lo | fail_hi

        failing = fail_any.stack()
        failing = failing[failing]
        if len(failing) == 0:
            continue

        for row_i, col_i in failing.index:
            subj_name = subjects_list[col_i]
            val       = numeric.at[row_i, col_i]
            lo        = lo_arr[col_i]
            hi        = hi_arr[col_i]
            is_lo     = bool(fail_lo.at[row_i, col_i])
            meta_row  = meta_np.iloc[row_i]

            all_rows.append({
                "source":       source_name,
                "dut":          _fmt_type(meta_row["DUT"]),
                "x_coord":      _fmt_type(meta_row["XCoord"]),
                "y_coord":      _fmt_type(meta_row["YCoord"]),
                "bin":          _fmt_type(meta_row["Bin"]),
                "subject":      subj_name,
                "value":        _fmt_num(val),
                "lower_limit":  _fmt_num(lo) if (lo is not None and pd.notna(lo)) else "N/A",
                "upper_limit":  _fmt_num(hi) if (hi is not None and pd.notna(hi)) else "N/A",
                "fail":         "< lo" if is_lo else "> hi",
            })

    return all_rows


def get_fail_values(dataset_id: str) -> list:
    """Return fail_values rows, computing and caching to disk on first call."""
    cache_path = DATASETS_DIR / dataset_id / "tables" / "fail_values.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    rows = build_fail_values(dataset_id)
    _write_json(cache_path, rows)
    return rows


def build_raw_xlsx(dataset_id):
    from analysis.data_loader import load_table

    input_dir = DATASETS_DIR / dataset_id / "input"
    if not input_dir.exists():
        raise FileNotFoundError(f"input directory missing for dataset {dataset_id}")

    schools = {p.stem: load_table(p) for p in sorted(input_dir.glob("*.csv"))}
    if not schools:
        raise FileNotFoundError(f"no input csv files for dataset {dataset_id}")

    buf = BytesIO()
    used_names = set()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for source_name, table in schools.items():
            meta = table.meta.reset_index(drop=True).copy()
            scores = table.scores.reset_index(drop=True).copy()
            scores.columns = _subject_columns(table)
            frame = pd.concat([meta, scores], axis=1)
            frame["Bin"] = frame["Bin"].map(_fmt_type)

            sheet = source_name[:31] or "sheet"
            base = sheet
            i = 1
            while sheet in used_names:
                suffix = f"_{i}"
                sheet = (base[: 31 - len(suffix)] + suffix)
                i += 1
            used_names.add(sheet)
            frame.to_excel(writer, sheet_name=sheet, index=False)
    buf.seek(0)
    return buf.getvalue()

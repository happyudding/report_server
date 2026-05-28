"""순수 분석 함수 (pandas/numpy 만).

반도체 mass_data(웨이퍼/로트 단위 측정 데이터) 분석 로직. flask / db / s3 /
plotly / config 의존 없음.

`mass_data_map` = {source_name: mass_data} dict. 각 value(mass_data)는 DfHoney
인스턴스로, 하나의 입력 sheet/CSV(= 한 mass_data 단위)에 대응하며 다음 속성을 갖는다:
    subjects, units, lower_limits, upper_limits, scores(DataFrame), meta(DataFrame[...,"Bin"])
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .constants import PASS_BIN, META_COLUMNS


# ---------------------------------------------------------------------------
# 포맷 헬퍼

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


def _subject_columns(mass_data):
    return [str(s) for s in mass_data.subjects]


# ---------------------------------------------------------------------------
# 결합 / 마스크

def _combined_frames(mass_data_map):
    frames = []
    for source_name, mass_data in mass_data_map.items():
        meta = mass_data.meta.reset_index(drop=True).copy()
        scores = mass_data.scores.reset_index(drop=True).copy()
        scores.columns = _subject_columns(mass_data)
        frame = pd.concat([meta, scores], axis=1)
        frame.insert(0, "source_file", source_name)
        frame["Bin"] = frame["Bin"].map(_fmt_type)
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["source_file", *META_COLUMNS])
    return pd.concat(frames, ignore_index=True)


def _fail_mask(mass_data):
    """mass_data 의 각 측정값이 lo/up 한계를 벗어났는지 bool DataFrame."""
    numeric = mass_data.scores.apply(pd.to_numeric, errors="coerce")
    arr = numeric.to_numpy(dtype="float64", copy=False)
    n_sub = arr.shape[1]

    def _lim(seq, i):
        if i >= len(seq):
            return np.nan
        v = seq[i]
        if v is None:
            return np.nan
        try:
            return float(v)
        except (TypeError, ValueError):
            return np.nan

    lo = np.array([_lim(mass_data.lower_limits, i) for i in range(n_sub)], dtype="float64")
    hi = np.array([_lim(mass_data.upper_limits, i) for i in range(n_sub)], dtype="float64")
    with np.errstate(invalid="ignore"):
        fail = (arr < lo) | (arr > hi)
    return pd.DataFrame(fail, index=numeric.index, columns=numeric.columns, copy=False)


def _type_sort_key(value):
    text = str(value)
    try:
        return (0, float(text))
    except ValueError:
        return (1, text)


def _subject_rankings_by_type(mass_data_map):
    rankings = {}
    type_totals = {}
    first = next(iter(mass_data_map.values()))
    subject_names = _subject_columns(first)
    for _source_name, mass_data in mass_data_map.items():
        fail_mask = _fail_mask(mass_data)
        bin_types = mass_data.meta["Bin"].map(_fmt_type)
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


# ---------------------------------------------------------------------------
# yield

def build_yield(mass_data_map):
    combined = _combined_frames(mass_data_map)
    total = len(combined)
    subject_rankings = _subject_rankings_by_type(mass_data_map)
    rows = []
    if total == 0:
        return rows
    sources = list(mass_data_map.keys())
    per_file_total = {}
    per_file_type_count = {}
    for source_name, mass_data in mass_data_map.items():
        types = mass_data.meta["Bin"].map(_fmt_type)
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
            "Main Fail subject": "Pass" if bin_type == PASS_BIN else (
                fail_subjects[0]["subject"] if fail_subjects else "N/A"),
            "comment": "",
        })
    return rows


# ---------------------------------------------------------------------------
# cpk

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


def build_cpk(mass_data_map):
    rows = []
    first = next(iter(mass_data_map.values()))
    for idx, subject in enumerate(first.subjects):
        lo = first.lower_limits[idx] if idx < len(first.lower_limits) else None
        hi = first.upper_limits[idx] if idx < len(first.upper_limits) else None
        unit = first.units[idx] if idx < len(first.units) else ""
        per_source = []
        for source_name, mass_data in mass_data_map.items():
            series = pd.to_numeric(mass_data.scores.iloc[:, idx], errors="coerce")
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


# ---------------------------------------------------------------------------
# fail items

def build_fail_items(mass_data_map):
    yield_rows = build_yield(mass_data_map)
    subject_rankings = _subject_rankings_by_type(mass_data_map)
    rows = []
    for row in yield_rows:
        bin_type = row["bin"]
        fail_subjects = [] if bin_type == PASS_BIN else subject_rankings.get(bin_type, [])
        rows.append({
            **row,
            "Fail Subjects": "Pass" if bin_type == PASS_BIN else (
                "N/A" if not fail_subjects else f"{len(fail_subjects)} subjects"),
            "fail_subjects": fail_subjects,
        })
    return {"rows": rows}


# ---------------------------------------------------------------------------
# issue_table (legacy) — yield/fail_items 기반 bin별 "most fail item" 요약

def build_issue_summary(mass_data_map):
    """fail bin 별 1순위 fail subject + avg + source 별 portion.

    _reference/server_legacy/xlsx_export._build_issue_rows 와 동일 개념.
    pass(bin 1) 는 제외하고 avg 내림차순 정렬.
    """
    fi = build_fail_items(mass_data_map)["rows"]
    sources = list(mass_data_map.keys())
    rows = []
    for r in fi:
        st = str(r.get("bin", "")).strip()
        if st == PASS_BIN:
            continue
        fail_subjects = r.get("fail_subjects") or []
        subject = fail_subjects[0].get("subject", "N/A") if fail_subjects else "N/A"
        row = {
            "bin": st,
            "subject": subject,
            "avg": r.get("avg"),
            "portion (%)": r.get("portion (%)"),
        }
        for src in sources:
            row[f"portion_{src}"] = r.get(f"portion_{src}")
        rows.append(row)
    rows.sort(key=lambda x: -(x.get("avg") or 0.0))
    return rows


# ---------------------------------------------------------------------------
# fail_values — 비합격 DUT별 한계 이탈 레코드 (DfHoney.fail_values 용)

def build_issue_table(mass_data_map):
    rows = []
    for source_name, mass_data in mass_data_map.items():
        subjects_list = _subject_columns(mass_data)
        n_sub = len(subjects_list)
        meta = mass_data.meta.reset_index(drop=True).copy()
        meta["Bin"] = meta["Bin"].map(_fmt_type)
        non_pass = meta["Bin"] != PASS_BIN
        if not non_pass.any():
            continue
        meta_np = meta[non_pass].reset_index(drop=True)
        scores_np = mass_data.scores[non_pass].reset_index(drop=True)
        numeric = scores_np.apply(pd.to_numeric, errors="coerce")
        lo_arr = [mass_data.lower_limits[i] if i < len(mass_data.lower_limits) else None for i in range(n_sub)]
        hi_arr = [mass_data.upper_limits[i] if i < len(mass_data.upper_limits) else None for i in range(n_sub)]
        fail_lo = pd.DataFrame(False, index=numeric.index, columns=numeric.columns)
        fail_hi = pd.DataFrame(False, index=numeric.index, columns=numeric.columns)
        for idx in range(n_sub):
            col_s = numeric.iloc[:, idx]
            if lo_arr[idx] is not None and pd.notna(lo_arr[idx]):
                fail_lo.iloc[:, idx] = (col_s < float(lo_arr[idx])).fillna(False)
            if hi_arr[idx] is not None and pd.notna(hi_arr[idx]):
                fail_hi.iloc[:, idx] = (col_s > float(hi_arr[idx])).fillna(False)
        fail_any = (fail_lo | fail_hi)
        failing = fail_any.stack()
        failing = failing[failing]
        for row_i, col_i in failing.index:
            is_lo = bool(fail_lo.at[row_i, col_i])
            meta_row = meta_np.iloc[row_i]
            rows.append({
                "source": source_name,
                "dut": _fmt_type(meta_row["DUT"]),
                "x_coord": _fmt_type(meta_row["XCoord"]),
                "y_coord": _fmt_type(meta_row["YCoord"]),
                "bin": _fmt_type(meta_row["Bin"]),
                "subject": subjects_list[col_i],
                "value": _fmt_num(numeric.at[row_i, col_i]),
                "lower_limit": _fmt_num(lo_arr[col_i]) if (lo_arr[col_i] is not None and pd.notna(lo_arr[col_i])) else "N/A",
                "upper_limit": _fmt_num(hi_arr[col_i]) if (hi_arr[col_i] is not None and pd.notna(hi_arr[col_i])) else "N/A",
                "fail": "< lo" if is_lo else "> hi",
            })
    return rows


# ---------------------------------------------------------------------------
# summary rows

def _to_float(value):
    if value is None or value == "N/A":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _try_int(value):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or not f.is_integer():
        return None
    return int(f)


def build_summary_rows(mass_data_map):
    cpk_rows = build_cpk(mass_data_map)
    yield_rows = build_yield(mass_data_map)
    fail_items = build_fail_items(mass_data_map)["rows"]

    _fail_sums = []
    _fail_rows = []
    for mass_data in mass_data_map.values():
        mask = _fail_mask(mass_data)
        _fail_sums.append(mask.sum(axis=0).to_numpy(dtype=int, copy=False))
        _fail_rows.append(int(len(mask)))

    def _combined_fail_count_cached(subject_idx):
        total_fail = 0
        total_rows = 0
        for sums, n_rows in zip(_fail_sums, _fail_rows):
            if subject_idx >= sums.shape[0]:
                continue
            total_fail += int(sums[subject_idx])
            total_rows += n_rows
        return total_fail, total_rows

    rows = []

    # 1) per-subject overall (bin_number=None) — cpk 행은 source+total 이 섞여있어 total 행만 사용
    subj_idx = -1
    for cpk in cpk_rows:
        if cpk["source"] != "total":
            continue
        subj_idx += 1
        fail_count, total_rows = _combined_fail_count_cached(subj_idx)
        yield_pct = ((total_rows - fail_count) / total_rows * 100.0) if total_rows else None
        rows.append({
            "item_name": str(cpk["subject"]),
            "bin_number": None,
            "yield_percent": yield_pct,
            "fail_count": fail_count,
            "cpk_val": _to_float(cpk.get("cpk")),
            "mean_val": _to_float(cpk.get("average")),
            "stdev_val": _to_float(cpk.get("stdev")),
            "lsl": _to_float(cpk.get("lower_limit")),
            "usl": _to_float(cpk.get("upper_limit")),
            "unit": cpk.get("units") or "",
        })

    # 2) per-bin × item
    seen = set()
    for fail_row in fail_items:
        bin_n = _try_int(fail_row.get("bin"))
        if bin_n is None:
            continue
        for fs in fail_row.get("fail_subjects") or []:
            item_name = str(fs.get("subject"))
            key = (item_name, bin_n)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "item_name": item_name,
                "bin_number": bin_n,
                "yield_percent": _to_float(fs.get("portion (%)")),
                "fail_count": int(fs.get("count") or 0),
                "cpk_val": None, "mean_val": None, "stdev_val": None,
                "lsl": None, "usl": None, "unit": None,
            })

    # 3) bin 전체
    for yrow in yield_rows:
        bin_n = _try_int(yrow.get("bin"))
        if bin_n is None:
            continue
        rows.append({
            "item_name": "__bin_total__",
            "bin_number": bin_n,
            "yield_percent": _to_float(yrow.get("portion (%)")),
            "fail_count": int(yrow.get("count") or 0),
            "cpk_val": None, "mean_val": None, "stdev_val": None,
            "lsl": None, "usl": None, "unit": None,
        })

    return rows


# ---------------------------------------------------------------------------
# distribution (CDF)

def to_numeric_clean(series):
    return pd.to_numeric(series, errors="coerce").dropna().to_numpy()


def cumulative_distribution_full(values):
    if values.size == 0:
        return np.empty(0), np.empty(0)
    unique_vals, counts = np.unique(np.sort(values), return_counts=True)
    return unique_vals, np.cumsum(counts) / values.size * 100.0

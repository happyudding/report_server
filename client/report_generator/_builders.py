"""순수 분석 함수 (pandas/numpy 만).

반도체 mass_data(웨이퍼/로트 단위 측정 데이터) 분석 로직. flask / db / s3 /
plotly / config 의존 없음.

`mass_data_map` = {source_name: mass_data} dict. 각 value(mass_data)는 df_honey
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
        fail_mask = mass_data.fail_mask
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

def build_yield(mass_data_map, subject_rankings=None):
    combined = _combined_frames(mass_data_map)
    total = len(combined)
    if subject_rankings is None:
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
            # 템플릿 yield/issue_table 의 source별 컬럼: {src}_count / {src}_yield
            portion_fields[f"{src}_count"] = file_count
            portion_fields[f"{src}_yield"] = portion
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

def get_df_cpk_summary(numeric_df, lo_arr, hi_arr):
    """source 측정행렬(행=DUT, 열=subject) → subject별 **raw 통계 DataFrame** (벡터 컬럼연산).

    lo_arr/hi_arr: 열 정렬 limit(canonical, float/NaN). 결측 조건은 _calc_stats 와 동일:
    can = n>1 & stdev 유효(NaN·0 아님) & lo/hi 유효. 반환 idx=subject, 열 = n/min/median/max/
    average/stdev/cp/cpl/cpu/cpk (raw, 결측=NaN). 포맷은 호출부에서 _fmt_num/_fmt_metric.
    """
    cols = list(numeric_df.columns)
    n = numeric_df.notna().sum()
    mn = numeric_df.min()
    med = numeric_df.median()
    mx = numeric_df.max()
    avg = numeric_df.mean()
    std = numeric_df.std(ddof=1)
    lo = pd.Series(lo_arr, index=cols, dtype="float64")
    hi = pd.Series(hi_arr, index=cols, dtype="float64")
    can = (n > 1) & std.notna() & (std != 0) & lo.notna() & hi.notna()
    with np.errstate(invalid="ignore", divide="ignore"):
        cp = (hi - lo) / (6.0 * std)
        cpl = (avg - lo) / (3.0 * std)
        cpu = (hi - avg) / (3.0 * std)
    cpk = pd.Series(np.minimum(cpl.to_numpy(), cpu.to_numpy()), index=cols)
    nan = float("nan")
    cp = cp.where(can, nan)
    cpl = cpl.where(can, nan)
    cpu = cpu.where(can, nan)
    cpk = cpk.where(can, nan)
    return pd.DataFrame({
        "n": n, "min": mn, "median": med, "max": mx, "average": avg,
        "stdev": std, "cp": cp, "cpl": cpl, "cpu": cpu, "cpk": cpk,
    })


def _cpk_stat_dict(r):
    """get_df_cpk_summary 한 행(raw) → 포맷된 stats dict (_calc_stats 출력과 동일)."""
    return {
        "n": int(r["n"]),
        "min": _fmt_num(r["min"]),
        "median": _fmt_num(r["median"]),
        "max": _fmt_num(r["max"]),
        "average": _fmt_num(r["average"]),
        "stdev": _fmt_metric(r["stdev"]),
        "cp": _fmt_metric(r["cp"]),
        "cpl": _fmt_metric(r["cpl"]),
        "cpu": _fmt_metric(r["cpu"]),
        "cpk": _fmt_metric(r["cpk"]),
    }


def classify_subjects(mass_data_map):
    """2개 파일의 subject 를 이름 기준으로 common/a_only/b_only 분류.

    파일이 2개가 아니거나 두 파일의 subject 집합이 동일하면 None 반환
    (diff compare 불필요 → 기존 단일 모드 유지).
    반환 dict: {common, a_only, b_only, name_a, name_b}. 각 목록은 해당 파일의
    원본 subject 순서를 유지한다.
    """
    if len(mass_data_map) != 2:
        return None
    name_a, name_b = list(mass_data_map.keys())
    subs_a = [str(s) for s in mass_data_map[name_a].subjects]
    subs_b = [str(s) for s in mass_data_map[name_b].subjects]
    set_a, set_b = set(subs_a), set(subs_b)
    if set_a == set_b:
        return None
    return {
        "common": [s for s in subs_a if s in set_b],
        "a_only": [s for s in subs_a if s not in set_b],
        "b_only": [s for s in subs_b if s not in set_a],
        "name_a": name_a,
        "name_b": name_b,
    }


def build_cpk_for_subjects(mass_data_map, subject_names):
    """지정 subject 이름 목록에 대해서만 CPK 계산 (이름 기반 인덱스 조회).

    build_cpk 는 첫 파일 기준 위치(idx)로 모든 파일을 슬라이싱하므로 파일별 subject
    구성이 다르면 어긋난다. 이 함수는 각 subject 를 보유한 파일에서만 해당 이름의
    열을 찾아 통계를 낸다 (diff compare 의 common/a_only/b_only 시트용).
    """
    if not mass_data_map:
        return []
    # source별 측정행렬(열=subject 이름) — 캐시된 numeric_frame 재사용
    src_frames = {name: md.numeric_frame() for name, md in mass_data_map.items()}
    # canonical lo/hi/unit: 각 subject 를 처음 보유한 source 기준 (현재 first_md 동작과 동일)
    canon = {}
    for name, md in mass_data_map.items():
        subs = [str(s) for s in md.subjects]
        for i, s in enumerate(subs):
            if s not in canon:
                lo = md.lower_limits[i] if i < len(md.lower_limits) else None
                hi = md.upper_limits[i] if i < len(md.upper_limits) else None
                unit = md.units[i] if i < len(md.units) else ""
                canon[s] = (lo, hi, unit)

    def _lohi(cols):
        lo_arr = [_to_float(canon.get(c, (None, None, ""))[0]) for c in cols]
        hi_arr = [_to_float(canon.get(c, (None, None, ""))[1]) for c in cols]
        return lo_arr, hi_arr

    # source별 + total(세로 concat, subject 이름 정렬) 통계 1벌씩 벡터 산출
    src_summ = {}
    for name, frame in src_frames.items():
        cols = list(frame.columns)
        lo_arr, hi_arr = _lohi(cols)
        src_summ[name] = get_df_cpk_summary(frame, lo_arr, hi_arr)
    total_frame = pd.concat(list(src_frames.values()), ignore_index=True)
    t_lo, t_hi = _lohi(list(total_frame.columns))
    total_summ = get_df_cpk_summary(total_frame, t_lo, t_hi)

    rows = []
    for subject_name in subject_names:
        relevant = [(name, fr) for name, fr in src_frames.items()
                    if subject_name in fr.columns]
        if not relevant:
            continue
        lo, hi, unit = canon[subject_name]
        meta = {"subject": subject_name, "units": unit,
                "lower_limit": _fmt_num(lo), "upper_limit": _fmt_num(hi)}
        for name, _fr in relevant:
            rows.append({**meta, "source": name,
                         **_cpk_stat_dict(src_summ[name].loc[subject_name])})
        rows.append({**meta, "source": "total",
                     **_cpk_stat_dict(total_summ.loc[subject_name])})
    return rows


def build_cpk(mass_data_map):
    """첫 파일 기준 subject 전체의 CPK.

    위치(iloc) 기반으로 모든 파일을 슬라이싱하면 파일별 subject 구성이 다를 때(diff)
    첫 파일 idx 가 다른 파일에서 범위를 벗어난다. 이름 기반 매칭(build_cpk_for_subjects)
    에 위임해 subject 를 보유한 파일에서만 해당 이름 열을 찾는다 — 구성이 동일하면
    기존 위치 기반과 결과·순서가 같고, 달라도 안전하다.
    """
    if not mass_data_map:
        return []
    first = next(iter(mass_data_map.values()))
    return build_cpk_for_subjects(mass_data_map, [str(s) for s in first.subjects])


# ---------------------------------------------------------------------------
# fail items

def build_fail_items(mass_data_map, yield_rows=None, subject_rankings=None):
    if subject_rankings is None:
        subject_rankings = _subject_rankings_by_type(mass_data_map)
    if yield_rows is None:
        yield_rows = build_yield(mass_data_map, subject_rankings=subject_rankings)
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

def build_issue_summary(mass_data_map, fail_items=None):
    """fail bin 별 1순위 fail subject + avg + source 별 portion.

    pass(bin 1) 는 제외하고 avg 내림차순 정렬.
    """
    fi = fail_items if fail_items is not None else build_fail_items(mass_data_map)["rows"]
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
# fail_values — 비합격 DUT별 한계 이탈 레코드 (df_honey.fail_values 용)

def build_issue_table(mass_data_map):
    rows = []
    for source_name, mass_data in mass_data_map.items():
        subjects_list = _subject_columns(mass_data)
        n_sub = len(subjects_list)
        meta = mass_data.meta.reset_index(drop=True)
        non_pass = (meta["Bin"].map(_fmt_type) != PASS_BIN).to_numpy()
        if not non_pass.any():
            continue
        # 작업 A 의 방향별 캐시 마스크 재사용 (numeric 재변환·column loop 제거)
        lo_mask = mass_data.fail_mask_lo.to_numpy()
        hi_mask = mass_data.fail_mask_hi.to_numpy()
        brk_mask = mass_data.fail_mask_break.to_numpy()
        fail_any = (lo_mask | hi_mask | brk_mask) & non_pass[:, None]
        # np.where 는 행→열 C-order → 기존 DataFrame.stack() 순회 순서와 동일
        row_idx, col_idx = np.where(fail_any)
        if row_idx.size == 0:
            continue
        numeric_arr = mass_data.numeric_scores.to_numpy()
        lo_arr = [mass_data.lower_limits[i] if i < len(mass_data.lower_limits) else None for i in range(n_sub)]
        hi_arr = [mass_data.upper_limits[i] if i < len(mass_data.upper_limits) else None for i in range(n_sub)]
        dut, xc, yc, bn = meta["DUT"], meta["XCoord"], meta["YCoord"], meta["Bin"]
        for r, c in zip(row_idx.tolist(), col_idx.tolist()):
            fail_dir = "< lo" if lo_mask[r, c] else ("> hi" if hi_mask[r, c] else "break")
            rows.append({
                "source": source_name,
                "dut": _fmt_type(dut.iat[r]),
                "x_coord": _fmt_type(xc.iat[r]),
                "y_coord": _fmt_type(yc.iat[r]),
                "bin": _fmt_type(bn.iat[r]),
                "subject": subjects_list[c],
                "value": _fmt_num(numeric_arr[r, c]),
                "lower_limit": _fmt_num(lo_arr[c]) if (lo_arr[c] is not None and pd.notna(lo_arr[c])) else "N/A",
                "upper_limit": _fmt_num(hi_arr[c]) if (hi_arr[c] is not None and pd.notna(hi_arr[c])) else "N/A",
                "fail": fail_dir,
            })
    return rows


# ---------------------------------------------------------------------------
# major fail subjects — summary 시트 "Major Fail Bins" (subject별 총 fail 랭킹)

def build_major_fail_subjects(mass_data_map, top: int = 5):
    """subject별 총 fail 수를 bin 무관하게 합산한 상위 top 랭킹.

    반환: [{"subject": str, "fail_count": int, "ratio": float}, ...]
      ratio = subject 총 fail 수 / 전체 DUT 수 (소수, 예: 0.0102).
    summary 시트의 1st~5th Fail 표시에 쓰인다.
    """
    total_dut = 0
    fail_counts = {}            # subject_id -> 누적 fail 수
    subject_names = None
    for mass_data in mass_data_map.values():
        if subject_names is None:
            subject_names = _subject_columns(mass_data)
        total_dut += len(mass_data.scores)
        sums = mass_data.fail_mask.sum(axis=0)
        for sid, count in sums.items():
            count = int(count)
            if count <= 0:
                continue
            sid = int(sid)
            fail_counts[sid] = fail_counts.get(sid, 0) + count
    subject_names = subject_names or []
    rows = [
        {
            "subject": subject_names[sid] if sid < len(subject_names) else str(sid),
            "fail_count": count,
            "ratio": round(count / total_dut, 4) if total_dut else 0.0,
        }
        for sid, count in fail_counts.items()
    ]
    rows.sort(key=lambda x: (-x["fail_count"], x["subject"]))
    return rows[:top]


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


def build_summary_rows(mass_data_map, cpk_rows=None, yield_rows=None,
                       fail_items=None, subject_rankings=None):
    if cpk_rows is None:
        cpk_rows = build_cpk(mass_data_map)
    if yield_rows is None:
        yield_rows = build_yield(mass_data_map, subject_rankings=subject_rankings)
    if fail_items is None:
        fail_items = build_fail_items(mass_data_map, yield_rows=yield_rows,
                                      subject_rankings=subject_rankings)["rows"]

    _fail_sums = []
    _fail_rows = []
    for mass_data in mass_data_map.values():
        mask = mass_data.fail_mask
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
    arr = pd.to_numeric(series, errors="coerce")
    return arr[np.isfinite(arr)].to_numpy()


def cumulative_distribution_full(values):
    if values.size == 0:
        return np.empty(0), np.empty(0)
    unique_vals, counts = np.unique(np.sort(values), return_counts=True)
    cum = np.cumsum(counts) / values.size * 100.0
    return unique_vals, cum

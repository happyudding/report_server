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


def _break_mask(mass_data):
    """stop-on-fail 로 data 흐름이 뚝 끊긴 시점(말미 연속 NaN 런의 시작 열) bool DataFrame.

    각 DUT(행)에서 측정값이 끝까지 이어지지 않고 어느 item 부터 끝까지 모두 비어버린
    (NaN) 경우, 그 끊긴 첫 item 한 곳만 True. 전부 NaN 인 행과 PASS_BIN DUT 는 제외
    (옵션 미측정 오탐 방지). limit 위반과 별개의 fail 원인 — fail item/fail value 정의에
    함께 합산된다.
    """
    numeric = mass_data.scores.apply(pd.to_numeric, errors="coerce")
    isnan = numeric.isna().to_numpy()
    n_rows, n_sub = isnan.shape
    out = np.zeros((n_rows, n_sub), dtype=bool)
    if n_rows == 0 or n_sub == 0:
        return pd.DataFrame(out, index=numeric.index, columns=numeric.columns, copy=False)
    # 말미 연속 NaN 런: col..끝 이 모두 NaN 인 위치 (오른쪽부터 cumprod)
    trailing = np.cumprod(isnan[:, ::-1], axis=1)[:, ::-1].astype(bool)
    # onset = 런의 시작(False→True 전환) 한 곳만
    prev = np.zeros_like(trailing)
    prev[:, 1:] = trailing[:, :-1]
    onset = trailing & ~prev
    onset[:, 0] = False   # col 0 onset = 전부 NaN 행 → 제외 (앞에 valid 값 없음)
    # PASS_BIN DUT 제외
    bins = mass_data.meta["Bin"].map(_fmt_type).to_numpy()
    onset &= (bins != PASS_BIN)[:, None]
    return pd.DataFrame(onset, index=numeric.index, columns=numeric.columns, copy=False)


def _fail_mask(mass_data):
    """각 측정값의 fail 여부 bool DataFrame — limit 위반 ∪ data 흐름 끊김(onset).

    fail item 과 fail value 가 동일 정의를 공유하도록 통합한다.
      limit: value < lower 또는 > upper.
      break: stop-on-fail 로 말미부터 값이 끊긴 첫 item (_break_mask).
    """
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
    limit = pd.DataFrame(fail, index=numeric.index, columns=numeric.columns, copy=False)
    return limit | _break_mask(mass_data)


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
    rows = []
    for subject_name in subject_names:
        relevant = {n: md for n, md in mass_data_map.items()
                    if subject_name in [str(s) for s in md.subjects]}
        if not relevant:
            continue
        first_md = next(iter(relevant.values()))
        names0 = [str(s) for s in first_md.subjects]
        idx0 = names0.index(subject_name)
        lo = first_md.lower_limits[idx0] if idx0 < len(first_md.lower_limits) else None
        hi = first_md.upper_limits[idx0] if idx0 < len(first_md.upper_limits) else None
        unit = first_md.units[idx0] if idx0 < len(first_md.units) else ""
        per_source = []
        for source_name, md in relevant.items():
            idx = [str(s) for s in md.subjects].index(subject_name)
            series = pd.to_numeric(md.scores.iloc[:, idx], errors="coerce")
            per_source.append(series)
            rows.append({
                "subject": subject_name,
                "source": source_name,
                "units": unit,
                "lower_limit": _fmt_num(lo),
                "upper_limit": _fmt_num(hi),
                **_calc_stats(series, lo, hi),
            })
        total_series = pd.concat(per_source, ignore_index=True) if per_source else pd.Series(dtype=float)
        rows.append({
            "subject": subject_name,
            "source": "total",
            "units": unit,
            "lower_limit": _fmt_num(lo),
            "upper_limit": _fmt_num(hi),
            **_calc_stats(total_series, lo, hi),
        })
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
# fail_values — 비합격 DUT별 한계 이탈 레코드 (df_honey.fail_values 용)

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
        # data 흐름 끊김(stop-on-fail) onset 도 fail 로 합산 — non_pass 행만 정렬해 정렬
        brk_np = _break_mask(mass_data)[non_pass].reset_index(drop=True)
        brk_np.columns = numeric.columns
        fail_any = (fail_lo | fail_hi | brk_np)
        failing = fail_any.stack()
        failing = failing[failing]
        for row_i, col_i in failing.index:
            is_lo = bool(fail_lo.at[row_i, col_i])
            is_hi = bool(fail_hi.at[row_i, col_i])
            fail_dir = "< lo" if is_lo else ("> hi" if is_hi else "break")
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
        sums = _fail_mask(mass_data).sum(axis=0)
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
    arr = pd.to_numeric(series, errors="coerce")
    return arr[np.isfinite(arr)].to_numpy()


# ── ECDF 선분 표현 기능 플래그 ────────────────────────────────────────────────
# True : 정수형 중복 data → NaN gap 계단형 선분 (수직선만, 수평 연결선 없음)
# False: 기존 방식 — 모든 data unique값 1포인트, 점(마커)으로 표시
# 복구하려면 아래 값을 True 로 변경
_ECDF_STEP_LINES = False


def cumulative_distribution_full(values):
    if values.size == 0:
        return np.empty(0), np.empty(0)
    unique_vals, counts = np.unique(np.sort(values), return_counts=True)
    cum = np.cumsum(counts) / values.size * 100.0
    # 정수형이고 중복이 있는 경우에만 step-function (2포인트/값, 선분 표현).
    # 연속형(all-unique) 또는 실수형은 기존 방식(1포인트/unique값, 점 표현).
    has_duplicates = len(unique_vals) < values.size
    is_integer_data = has_duplicates and np.all(values == np.floor(values))
    if is_integer_data and _ECDF_STEP_LINES:
        y_starts = np.concatenate(([0.0], cum[:-1]))
        n = len(unique_vals)
        # NaN gap 패턴: [v, v, NaN] × n — NaN 위치에서 Excel 선이 끊겨 수직 선분만 표시
        all_xs = np.full(3 * n, np.nan)
        all_ys = np.full(3 * n, np.nan)
        all_xs[0::3] = unique_vals   # 구간 시작 x
        all_xs[1::3] = unique_vals   # 구간 끝 x (같은 값)
        all_ys[0::3] = y_starts      # 구간 시작 %
        all_ys[1::3] = cum           # 구간 끝 %
        xs, ys = all_xs, all_ys
    else:
        xs = unique_vals
        ys = cum
    return xs, ys

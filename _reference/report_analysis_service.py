import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from database import report_db
from s3_storage import report_s3
from analysis.chart_payload import build_payload
from config import (
    REPORT_LOCK_MAX_WAIT_SEC,
    REPORT_LOCK_POLL_SEC,
    REPORT_S3_BUCKET,
    REPORT_THUMB_WORKERS,
)
from analysis.data_loader import ExcelData, load_table
from analysis.preprocess import cumulative_distribution_full, to_numeric_clean
from analysis.svg_builder import build_subject_svg
from analysis.table_builder import (
    _build_cpk,
    _build_fail_items,
    _build_yield,
    _fail_mask_for_table,
)

# fail_items / distribution 과 동일한 색상 팔레트 (시각 일관성)
COLOR_PALETTE = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]

_CHUNK = 64 * 1024


class AnalysisError(RuntimeError):
    pass


class AnalysisLockTimeout(RuntimeError):
    pass


# ---------- hash / key helpers --------------------------------------------

def hash_files_streaming(file_paths):
    h = hashlib.sha256()
    for path in sorted(file_paths, key=lambda p: p.name):
        h.update(path.name.encode("utf-8"))
        h.update(b"\x00")
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        h.update(b"\x00")
    return h.hexdigest()


def normalize_options(options):
    if options is None:
        options = {}
    return json.dumps(options, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_analysis_key(content_hash, options_json, session_id=None):
    """analysis_key = sha256(content_hash : options_json [: session_id]).

    session_id 가 주어지면 항상 unique 한 키를 생성한다 (캐시 재사용 없음).
    동일 CSV/옵션이라도 세션마다 독립적인 분석 결과를 보장하기 위함.
    """
    digest = hashlib.sha256()
    digest.update(content_hash.encode("utf-8"))
    digest.update(b":")
    digest.update(options_json.encode("utf-8"))
    if session_id:
        digest.update(b":")
        digest.update(session_id.encode("utf-8"))
    return digest.hexdigest()


# ---------- options helpers ----------------------------------------------

def _extract_selected_items(options):
    """options 에서 selected_items 리스트 추출. dict/JSON 문자열 둘 다 허용."""
    if not options:
        return None
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except (TypeError, ValueError):
            return None
    if not isinstance(options, dict):
        return None
    items = options.get("selected_items")
    if not items or not isinstance(items, (list, tuple)):
        return None
    items = [str(s) for s in items if s is not None and str(s) != ""]
    return items or None


def _filter_schools_by_items(schools, selected_items):
    """selected_items 에 들어있는 subject 만 남긴 새 schools dict 반환.

    selected_items 가 None/빈 리스트면 schools 를 그대로 반환.
    각 ExcelData 의 subjects/units/lower_limits/upper_limits/scores 컬럼을 필터링.
    meta(DUT/XCoord/YCoord/Bin) 는 그대로 유지 (행 수 동일).
    """
    if not selected_items:
        return schools
    sel_set = set(selected_items)
    filtered = {}
    for name, table in schools.items():
        keep_indices = [i for i, s in enumerate(table.subjects) if s in sel_set]
        new_subjects = [table.subjects[i] for i in keep_indices]
        new_units = [
            table.units[i] if i < len(table.units) else "" for i in keep_indices
        ]
        new_lower = [
            table.lower_limits[i] if i < len(table.lower_limits) else None
            for i in keep_indices
        ]
        new_upper = [
            table.upper_limits[i] if i < len(table.upper_limits) else None
            for i in keep_indices
        ]
        if keep_indices:
            new_scores = table.scores.iloc[:, keep_indices].copy()
            new_scores.columns = list(range(len(keep_indices)))
        else:
            new_scores = table.scores.iloc[:, 0:0].copy()
        filtered[name] = ExcelData(
            subjects=new_subjects,
            units=new_units,
            lower_limits=new_lower,
            upper_limits=new_upper,
            scores=new_scores,
            meta=table.meta,
        )
    return filtered


# ---------- numeric helpers ------------------------------------------------

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


# ---------- summary mapping ------------------------------------------------

def _combined_fail_count(schools, subject_idx):
    """subject 별, 전체 school 합산 fail count (lsl/usl 초과).

    NOTE: 호출자가 매 subject 마다 mask 를 재계산하지 않도록 build_summary_rows
    내부에서는 사전 캐싱된 합산을 사용한다. 본 함수는 외부 호환을 위해 유지.
    """
    total_fail = 0
    total_rows = 0
    for table in schools.values():
        mask = _fail_mask_for_table(table)
        if subject_idx >= mask.shape[1]:
            continue
        col = mask.iloc[:, subject_idx]
        total_fail += int(col.sum())
        total_rows += int(len(col))
    return total_fail, total_rows


def build_summary_rows(schools):
    """(item, bin) summary row list 생성. 분석 결과 자체는 변경 없음."""
    cpk_rows = _build_cpk(schools)
    yield_rows = _build_yield(schools)
    fail_items = _build_fail_items(schools)["rows"]

    # subject 루프에서 _fail_mask_for_table 가 school 마다 매번 재계산되는 비용
    # (이전: cpk_rows 길이 × school 수, 작은 입력에서도 600+회) 제거를 위한 사전 캐시.
    _fail_sums = []
    _fail_rows = []
    for table in schools.values():
        mask = _fail_mask_for_table(table)
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

    # 1) per-subject overall (bin_number=NULL)
    for idx, cpk in enumerate(cpk_rows):
        fail_count, total_rows = _combined_fail_count_cached(idx)
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

    # 2) per-bin × item (fail_subjects 펼침)
    seen = set()  # (item_name, bin_number)
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
                "cpk_val": None,
                "mean_val": None,
                "stdev_val": None,
                "lsl": None,
                "usl": None,
                "unit": None,
            })

    # 3) bin 전체 (item_name = "__bin_total__") — yield row 자체 정보 보존
    for yrow in yield_rows:
        bin_n = _try_int(yrow.get("bin"))
        if bin_n is None:
            continue
        rows.append({
            "item_name": "__bin_total__",
            "bin_number": bin_n,
            "yield_percent": _to_float(yrow.get("portion (%)")),
            "fail_count": int(yrow.get("count") or 0),
            "cpk_val": None,
            "mean_val": None,
            "stdev_val": None,
            "lsl": None,
            "usl": None,
            "unit": None,
        })

    return rows


# ---------- issue_table (fail_values) builder --------------------------------

def _build_issue_table(schools):
    """비합격 DUT별 측정값 초과 레코드 (fail_values). 전 source 통합."""
    from analysis.table_builder import PASS_BIN, _fmt_type, _fmt_num, _subject_columns
    import pandas as pd

    rows = []
    for source_name, table in schools.items():
        subjects_list = _subject_columns(table)
        n_sub = len(subjects_list)
        meta = table.meta.reset_index(drop=True).copy()
        meta["Bin"] = meta["Bin"].map(_fmt_type)
        non_pass = meta["Bin"] != PASS_BIN
        if not non_pass.any():
            continue
        meta_np = meta[non_pass].reset_index(drop=True)
        scores_np = table.scores[non_pass].reset_index(drop=True)
        numeric = scores_np.apply(pd.to_numeric, errors="coerce")
        lo_arr = [table.lower_limits[i] if i < len(table.lower_limits) else None for i in range(n_sub)]
        hi_arr = [table.upper_limits[i] if i < len(table.upper_limits) else None for i in range(n_sub)]
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


def _idx_or(seq, i, default=None):
    return seq[i] if i < len(seq) else default


def _extract_fail_subject_ids(fail_data):
    """fail_items JSON에서 실제 차트가 필요한 subject_id 집합만 추출."""
    ids = set()
    for row in (fail_data.get("rows") or []):
        for fs in (row.get("fail_subjects") or []):
            sid = fs.get("subject_id")
            if sid is not None:
                ids.add(int(sid))
    return ids


def _upload_svgs_for_subjects(analysis_key, schools, subject_ids):
    """지정된 subject_id 목록에 대해서만 SVG를 생성하고 S3에 업로드."""
    names = list(schools.keys())
    color_map = {n: COLOR_PALETTE[i % len(COLOR_PALETTE)] for i, n in enumerate(names)}
    first = schools[names[0]]

    def gen_and_upload(idx):
        traces = []
        for name in names:
            xs, ys = cumulative_distribution_full(
                to_numeric_clean(schools[name].scores.iloc[:, idx])
            )
            traces.append({"school": name, "color": color_map[name], "xs": xs, "ys": ys})
        unit = _idx_or(first.units, idx, "")
        lo = _idx_or(first.lower_limits, idx)
        hi = _idx_or(first.upper_limits, idx)
        payload = build_payload(idx, first.subjects[idx], unit, lo, hi, traces)
        svg = build_subject_svg(
            idx, first.subjects[idx], unit, lo, hi, traces, payload["layout"]
        )
        s3_key = report_s3.make_thumb_s3_key(analysis_key, idx)
        report_s3.upload_bytes_to_s3(s3_key, svg.encode("utf-8"), "image/svg+xml; charset=utf-8")

    if not subject_ids:
        return

    workers = max(1, int(REPORT_THUMB_WORKERS))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(gen_and_upload, sid) for sid in subject_ids]
        for f in as_completed(futures):
            f.result()


def upload_derived_if_absent(analysis_key, content_hash, options_json, file_paths):
    """fail_items + issue_table + 필요한 SVG 썸네일만 → S3 업로드 (이미 있으면 스킵).

    file_paths: list of Path — 분석에 사용된 CSV 파일들 (로컬에 있어야 함).

    SVG는 2000개 전체가 아니라 fail_subjects에 등장하는 subject_id만 생성.
    """
    need_fail  = not report_db.get_object_info(analysis_key, "fail_items")
    need_issue = not report_db.get_object_info(analysis_key, "issue_table")
    need_thumbs = not report_db.get_object_info(analysis_key, "thumbs_fail_set")

    if not need_fail and not need_issue and not need_thumbs:
        return

    file_paths = [Path(p) for p in file_paths]
    schools = {p.stem: load_table(p) for p in sorted(file_paths, key=lambda x: x.name)}
    schools = _filter_schools_by_items(schools, _extract_selected_items(options_json))

    # fail_items (JSON) — thumbs 생성에도 필요하므로 먼저 빌드
    fail_data = None
    if need_fail or need_thumbs:
        fail_data = _build_fail_items(schools)

    if need_fail:
        s3_key = report_s3.make_fail_items_s3_key(analysis_key)
        uri = report_s3.upload_json_to_s3(s3_key, fail_data)
        report_db.upsert_object_info(
            analysis_key, content_hash, options_json,
            "fail_items", REPORT_S3_BUCKET, s3_key, uri,
        )

    if need_issue:
        issue_data = _build_issue_table(schools)
        s3_key = report_s3.make_issue_table_s3_key(analysis_key)
        uri = report_s3.upload_json_to_s3(s3_key, issue_data)
        report_db.upsert_object_info(
            analysis_key, content_hash, options_json,
            "issue_table", REPORT_S3_BUCKET, s3_key, uri,
        )

    # SVG 썸네일: fail_subjects에 등장하는 subject_id만 (수십 개)
    if need_thumbs:
        fail_subject_ids = _extract_fail_subject_ids(fail_data or {})
        _upload_svgs_for_subjects(analysis_key, schools, fail_subject_ids)
        prefix_key = report_s3.make_thumb_prefix_key(analysis_key)
        report_db.upsert_object_info(
            analysis_key, content_hash, options_json,
            "thumbs_fail_set", REPORT_S3_BUCKET,
            prefix_key, report_s3.make_s3_uri(prefix_key),
        )


# ---------- top-level flow -------------------------------------------------

def _wait_for_summary(analysis_key, lock_owner):
    deadline = time.time() + REPORT_LOCK_MAX_WAIT_SEC
    while time.time() < deadline:
        if report_db.has_summary(analysis_key):
            return True
        if report_db.try_acquire_analysis_lock(analysis_key, lock_owner):
            return False
        time.sleep(REPORT_LOCK_POLL_SEC)
    raise AnalysisLockTimeout(f"analysis_key {analysis_key} busy > {REPORT_LOCK_MAX_WAIT_SEC}s")


def get_or_compute_analysis(session_id, file_paths, options):
    """analyze 흐름의 핵심.

    Returns: {"reused": bool, "analysis_key": str, "content_hash": str,
              "options_json": str, "summary": list[dict]}
    """
    def _log(msg):
        print(f"[analysis:{session_id}] {msg}", flush=True)

    _log(f"START files={[str(p.name) for p in file_paths]}")
    file_paths = [Path(p) for p in file_paths]
    for p in file_paths:
        if not p.exists():
            raise AnalysisError(f"missing file: {p}")

    _log("hashing files...")
    t0 = time.time()
    content_hash = hash_files_streaming(file_paths)
    _log(f"hashed in {time.time()-t0:.2f}s")
    options_json = normalize_options(options)
    # session_id 를 키에 포함 → 같은 CSV+옵션이라도 세션마다 독립 (캐시 재사용 없음).
    analysis_key = compute_analysis_key(content_hash, options_json, session_id)
    _log(f"analysis_key={analysis_key[:12]}...")

    report_db.update_session(
        session_id,
        analysis_key=analysis_key,
        content_hash=content_hash,
        status="running",
    )

    # cache hit?
    if report_db.has_summary(analysis_key):
        _log("cache hit (has_summary) → reused")
        report_db.update_session(session_id, status="reused")
        return {
            "reused": True,
            "analysis_key": analysis_key,
            "content_hash": content_hash,
            "options_json": options_json,
            "summary": report_db.get_summary_by_analysis_key(analysis_key),
        }

    # lock 획득. 다른 워커가 계산중이면 대기 후 캐시 재조회.
    lock_owner = f"analyze:{session_id}"
    if not report_db.try_acquire_analysis_lock(analysis_key, lock_owner):
        _log("lock busy → wait_for_summary")
        already_done = _wait_for_summary(analysis_key, lock_owner)
        if already_done:
            _log("summary became available during wait → reused")
            report_db.update_session(session_id, status="reused")
            return {
                "reused": True,
                "analysis_key": analysis_key,
                "content_hash": content_hash,
                "options_json": options_json,
                "summary": report_db.get_summary_by_analysis_key(analysis_key),
            }
        _log("acquired lock after wait")
    else:
        _log("acquired lock immediately")

    # lock 보유 상태로 다시 한번 캐시 체크 (race 회피)
    try:
        if report_db.has_summary(analysis_key):
            _log("cache hit on re-check → reused")
            report_db.update_session(session_id, status="reused")
            return {
                "reused": True,
                "analysis_key": analysis_key,
                "content_hash": content_hash,
                "options_json": options_json,
                "summary": report_db.get_summary_by_analysis_key(analysis_key),
            }

        # 분석 실행 (기존 함수 wrap 만)
        _log("loading CSV files (load_table)...")
        t0 = time.time()
        schools = {p.stem: load_table(p) for p in sorted(file_paths, key=lambda x: x.name)}
        selected_items = _extract_selected_items(options)
        if selected_items:
            schools = _filter_schools_by_items(schools, selected_items)
            _log(f"filtered to {len(selected_items)} selected items")
        _log(f"loaded {len(schools)} schools in {time.time()-t0:.2f}s")

        _log("build_summary_rows...")
        t0 = time.time()
        rows = build_summary_rows(schools)
        _log(f"built {len(rows)} summary rows in {time.time()-t0:.2f}s")

        _log("save_summary_batch...")
        t0 = time.time()
        report_db.save_summary_batch(analysis_key, session_id, rows)
        _log(f"saved in {time.time()-t0:.2f}s")

        report_db.update_session(session_id, status="done")
        _log("DONE")
        return {
            "reused": False,
            "analysis_key": analysis_key,
            "content_hash": content_hash,
            "options_json": options_json,
            "summary": report_db.get_summary_by_analysis_key(analysis_key),
        }
    except Exception as exc:
        _log(f"EXCEPTION: {type(exc).__name__}: {exc}")
        report_db.update_session(
            session_id,
            status="failed",
            error_message=str(exc)[:1000],
        )
        raise
    finally:
        report_db.release_analysis_lock(analysis_key, lock_owner)
        _log("lock released")

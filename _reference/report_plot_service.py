import json
import time
from pathlib import Path

import numpy as np

from database import report_db
from s3_storage import report_s3
from analysis.chart_payload import build_payload
from config import (
    REPORT_LOCK_MAX_WAIT_SEC,
    REPORT_LOCK_POLL_SEC,
)
from analysis.data_loader import load_table
from analysis.preprocess import cumulative_distribution_full, to_numeric_clean
from report.report_analysis_service import (
    compute_analysis_key,
    hash_files_streaming,
    normalize_options,
)
from s3_storage.report_s3 import S3NotConfigured, S3ObjectCorrupted

COLOR_PALETTE = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]


class PlotError(RuntimeError):
    pass


class PlotLockTimeout(RuntimeError):
    pass


def _idx_or(seq, i, default=None):
    return seq[i] if i < len(seq) else default


def _sample(values, n):
    if n is None or n <= 0 or values.size <= n:
        return values
    rng = np.random.default_rng(0)
    idx = rng.choice(values.size, n, replace=False)
    idx.sort()
    return values[idx]


def _scatter_sample_size(options):
    if not isinstance(options, dict):
        return None
    scatter = options.get("scatter") if isinstance(options.get("scatter"), dict) else None
    if not scatter:
        return None
    n = scatter.get("sample")
    try:
        n = int(n)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _build_plotly_for_analysis(analysis_key, file_paths, options):
    sample_n = _scatter_sample_size(options)
    schools = {p.stem: load_table(p) for p in sorted(file_paths, key=lambda x: x.name)}
    names = list(schools.keys())
    color_map = {n: COLOR_PALETTE[i % len(COLOR_PALETTE)] for i, n in enumerate(names)}
    first = schools[names[0]]
    n_subjects = len(first.subjects)

    items = []
    for idx in range(n_subjects):
        traces = []
        for name in names:
            values = to_numeric_clean(schools[name].scores.iloc[:, idx])
            values = _sample(values, sample_n)
            xs, ys = cumulative_distribution_full(values)
            traces.append({
                "school": name, "color": color_map[name], "xs": xs, "ys": ys,
            })
        payload = build_payload(
            idx, first.subjects[idx], _idx_or(first.units, idx, ""),
            _idx_or(first.lower_limits, idx), _idx_or(first.upper_limits, idx), traces,
        )
        items.append(payload)

    return {
        "analysis_key": analysis_key,
        "n_subjects": n_subjects,
        "schools": [{"name": n, "color": color_map[n]} for n in names],
        "items": items,
    }


def _wait_for_object_or_lock(analysis_key, lock_owner):
    deadline = time.time() + REPORT_LOCK_MAX_WAIT_SEC
    while time.time() < deadline:
        info = report_db.get_object_info(analysis_key, "plotly")
        if info and report_s3.s3_object_exists(info["s3_key"]):
            return info
        if report_db.try_acquire_analysis_lock(analysis_key, lock_owner):
            return None
        time.sleep(REPORT_LOCK_POLL_SEC)
    raise PlotLockTimeout(f"plot generation busy > {REPORT_LOCK_MAX_WAIT_SEC}s")


def _resolve_inputs_for_analysis(analysis_key):
    file_path = report_db.get_session_path_by_analysis_key(analysis_key)
    if not file_path:
        raise PlotError(f"no source files known for analysis_key {analysis_key}")
    base = Path(file_path)
    if not base.exists() or not base.is_dir():
        raise PlotError(f"source dir missing: {base}")
    csvs = sorted(base.glob("*.csv"))
    if not csvs:
        raise PlotError(f"no CSV files in {base}")
    return csvs


def _resolve_options(analysis_key, supplied):
    """options 결정 순서: 사용자 입력 → report_object_info → {}"""
    if supplied is not None:
        return supplied
    info = report_db.get_object_info(analysis_key)
    if info:
        try:
            return json.loads(info["options_json"])
        except (TypeError, ValueError):
            return {}
    return {}


def get_or_create_plot(analysis_key, session_id=None, options=None):
    """Returns plotly JSON dict. S3 캐시 우선, 없으면 생성 후 업로드."""
    s3_key = report_s3.make_plotly_s3_key(analysis_key)

    # 1) S3 캐시 빠른 조회
    info = report_db.get_object_info(analysis_key)
    if info:
        try:
            if report_s3.s3_object_exists(info["s3_key"]):
                payload = report_s3.download_json_from_s3(info["s3_key"])
                report_db.touch_object_info(analysis_key, "plotly")
                return payload
        except S3ObjectCorrupted:
            report_s3.delete_s3_object_if_corrupted(info["s3_key"])
        except S3NotConfigured:
            raise

    # 2) lock 획득 또는 다른 워커 완료 대기
    lock_owner = f"plot:{session_id or 'anon'}:{int(time.time())}"
    waited_info = _wait_for_object_or_lock(analysis_key, lock_owner)
    if waited_info is not None:
        payload = report_s3.download_json_from_s3(waited_info["s3_key"])
        report_db.touch_object_info(analysis_key)
        return payload

    try:
        # 3) lock 안에서 다시 확인
        info = report_db.get_object_info(analysis_key, "plotly")
        if info and report_s3.s3_object_exists(info["s3_key"]):
            payload = report_s3.download_json_from_s3(info["s3_key"])
            report_db.touch_object_info(analysis_key, "plotly")
            return payload

        # 4) 입력 + options 결정 + 검증
        csvs = _resolve_inputs_for_analysis(analysis_key)
        opts = _resolve_options(analysis_key, options)
        options_json = normalize_options(opts)
        content_hash = hash_files_streaming(csvs)
        recomputed = compute_analysis_key(content_hash, options_json)
        if recomputed != analysis_key:
            raise PlotError(
                "analysis_key/options mismatch: provided options do not match key"
            )

        # 5) 생성 + 업로드 + 메타데이터 저장
        payload = _build_plotly_for_analysis(analysis_key, csvs, opts)
        report_s3.upload_json_to_s3(s3_key, payload)
        report_db.upsert_object_info(
            analysis_key, content_hash, options_json,
            "plotly", report_s3.bucket_name(), s3_key, report_s3.make_s3_uri(s3_key),
        )
        return payload
    finally:
        report_db.release_analysis_lock(analysis_key, lock_owner)

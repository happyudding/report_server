"""분석 산출물 메타: summary 행 / object_info(S3 참조) / csv_files / sheet_data
(report_db facade 구현)."""
import json

from .core import get_conn, _now, _row

_SUMMARY_COLUMNS = (
    "analysis_key", "session_id", "item_name", "bin_number",
    "yield_percent", "fail_count", "cpk_val", "mean_val", "stdev_val",
    "lsl", "usl", "unit", "created_at",
)


# ── summary ──────────────────────────────────────────────────────────────────

def get_summary_by_analysis_key(analysis_key):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT item_name, bin_number, yield_percent, fail_count, cpk_val, "
            "mean_val, stdev_val, lsl, usl, unit "
            "FROM report_analysis_summary WHERE analysis_key=? "
            "ORDER BY item_name, bin_number IS NULL DESC, bin_number",
            (analysis_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def has_summary(analysis_key):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM report_analysis_summary WHERE analysis_key=? LIMIT 1",
            (analysis_key,),
        ).fetchone()
    return row is not None


def save_summary_batch(analysis_key, session_id, rows):
    if not rows:
        return 0
    now = _now()
    payload = [
        (
            analysis_key, session_id,
            r["item_name"], r.get("bin_number"),
            r.get("yield_percent"), r.get("fail_count"), r.get("cpk_val"),
            r.get("mean_val"), r.get("stdev_val"), r.get("lsl"), r.get("usl"),
            r.get("unit"), now,
        )
        for r in rows
    ]
    placeholders = ",".join(["?"] * len(_SUMMARY_COLUMNS))
    cols = ",".join(_SUMMARY_COLUMNS)
    with get_conn() as conn:
        conn.executemany(
            f"INSERT OR IGNORE INTO report_analysis_summary ({cols}) VALUES ({placeholders})",
            payload,
        )
    return len(payload)


# ── object_info ──────────────────────────────────────────────────────────────

def get_object_info(analysis_key, object_type="plotly"):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM report_object_info WHERE analysis_key=? AND object_type=?",
            (analysis_key, object_type),
        ).fetchone()
    return _row(row)


def get_all_object_infos(analysis_key):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM report_object_info WHERE analysis_key=? ORDER BY object_type",
            (analysis_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_object_info(analysis_key, content_hash, options_json,
                       object_type, bucket, key, uri):
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_object_info "
            "(analysis_key, object_type, content_hash, options_json, "
            " s3_bucket, s3_key, s3_uri, created_at, last_accessed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(analysis_key, object_type) DO UPDATE SET "
            "  content_hash=excluded.content_hash, "
            "  options_json=excluded.options_json, "
            "  s3_bucket=excluded.s3_bucket, "
            "  s3_key=excluded.s3_key, "
            "  s3_uri=excluded.s3_uri, "
            "  last_accessed=excluded.last_accessed",
            (analysis_key, object_type, content_hash, options_json,
             bucket, key, uri, now, now),
        )


def delete_object_info(analysis_key, object_type):
    """object_info 행 1건 삭제 — web_report source 축소 시 stale 행 정리용. 없으면 no-op."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM report_object_info WHERE analysis_key=? AND object_type=?",
            (analysis_key, object_type),
        )


def touch_object_info(analysis_key, object_type="plotly"):
    with get_conn() as conn:
        conn.execute(
            "UPDATE report_object_info SET last_accessed=? "
            "WHERE analysis_key=? AND object_type=?",
            (_now(), analysis_key, object_type),
        )


# ── csv files ─────────────────────────────────────────────────────────────────

def upsert_csv_file(analysis_key, filename, s3_key, s3_uri, file_size):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_csv_files "
            "(analysis_key, filename, s3_key, s3_uri, file_size, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(analysis_key, filename) DO UPDATE SET "
            "  s3_key=excluded.s3_key, s3_uri=excluded.s3_uri, "
            "  file_size=excluded.file_size, uploaded_at=excluded.uploaded_at",
            (analysis_key, filename, s3_key, s3_uri, file_size, _now()),
        )


def get_csv_files(analysis_key):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT filename, s3_key, s3_uri, file_size, uploaded_at "
            "FROM report_csv_files WHERE analysis_key=? ORDER BY filename",
            (analysis_key,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── sheet_data (순수 텍스트 데이터 캐시) ─────────────────────────────────────

def upsert_sheet_data(analysis_key: str, sheet_name: str, data) -> None:
    """data(dict|list) → JSON 직렬화해 upsert. 스타일 없는 셀 텍스트 데이터."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_sheet_data (analysis_key, sheet_name, data_json, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(analysis_key, sheet_name) DO UPDATE SET "
            "  data_json=excluded.data_json, updated_at=excluded.updated_at",
            (analysis_key, sheet_name, json.dumps(data, ensure_ascii=False), _now()),
        )


def get_sheet_data(analysis_key: str, sheet_name: str):
    """없으면 None. JSON 역직렬화해 반환."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data_json FROM report_sheet_data "
            "WHERE analysis_key=? AND sheet_name=?",
            (analysis_key, sheet_name),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


def get_all_sheet_data(analysis_key: str) -> dict:
    """{'summary':..., 'yield':..., 'issue_table':...} 존재하는 것만."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sheet_name, data_json FROM report_sheet_data WHERE analysis_key=?",
            (analysis_key,),
        ).fetchall()
    result = {}
    for row in rows:
        try:
            result[row[0]] = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            pass
    return result

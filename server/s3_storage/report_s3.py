import json

from config import (
    REPORT_S3_ACCESS_KEY,
    REPORT_S3_BUCKET,
    REPORT_S3_CSV_PREFIX,
    REPORT_S3_ENDPOINT,
    REPORT_S3_FAIL_PREFIX,
    REPORT_S3_ISSUE_PREFIX,
    REPORT_S3_ISSUE_TEXT_PREFIX,
    REPORT_S3_MAX_POOL_CONNECTIONS,
    REPORT_S3_PREFIX,
    REPORT_S3_REGION,
    REPORT_S3_SECRET_KEY,
    REPORT_S3_SOURCE_XLSX_PREFIX,
    REPORT_S3_SUMMARY_TEXT_PREFIX,
    REPORT_S3_THUMB_PREFIX,
    REPORT_S3_CHART_PREFIX,
)


class S3NotConfigured(RuntimeError):
    pass


class S3ObjectCorrupted(RuntimeError):
    pass


_client = None


def _require_config():
    if not REPORT_S3_BUCKET:
        raise S3NotConfigured("REPORT_S3_BUCKET is not configured")


def get_s3_client():
    global _client
    if _client is not None:
        return _client
    _require_config()
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise S3NotConfigured(f"boto3 not installed: {exc}") from exc

    # max_pool_connections: 동시 사용자/스레드가 같은 client 를 공유하므로
    # 기본 10 은 너무 작다. config.py 의 REPORT_S3_MAX_POOL_CONNECTIONS (기본 30) 사용.
    # 풀이 부족하면 추가 요청이 connection 자리가 날 때까지 잠깐 대기 → 사용자간 대기 유발.
    kwargs = {
        "region_name": REPORT_S3_REGION or "us-east-1",
        "config": Config(
            signature_version="s3v4",
            retries={"max_attempts": 3},
            max_pool_connections=REPORT_S3_MAX_POOL_CONNECTIONS,
        ),
    }
    if REPORT_S3_ENDPOINT:
        kwargs["endpoint_url"] = REPORT_S3_ENDPOINT
    if REPORT_S3_ACCESS_KEY and REPORT_S3_SECRET_KEY:
        kwargs["aws_access_key_id"] = REPORT_S3_ACCESS_KEY
        kwargs["aws_secret_access_key"] = REPORT_S3_SECRET_KEY
    _client = boto3.client("s3", **kwargs)
    return _client


def make_plotly_s3_key(analysis_key):
    prefix = REPORT_S3_PREFIX.strip("/") or "pe/report/plotly"
    return f"{prefix}/{analysis_key}.json"


def make_s3_uri(key):
    return f"s3://{REPORT_S3_BUCKET}/{key}"


def s3_object_exists(key):
    client = get_s3_client()
    try:
        client.head_object(Bucket=REPORT_S3_BUCKET, Key=key)
        return True
    except Exception as exc:
        code = getattr(getattr(exc, "response", {}), "get", lambda *_: {})("Error") or {}
        status = code.get("Code") if isinstance(code, dict) else None
        if status in ("404", "NoSuchKey", "NotFound") or "404" in str(exc):
            return False
        raise


def upload_json_to_s3(key, data):
    client = get_s3_client()
    if isinstance(data, (dict, list)):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    elif isinstance(data, str):
        body = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        body = bytes(data)
    else:
        raise TypeError(f"upload_json_to_s3: unsupported data type {type(data)!r}")
    client.put_object(
        Bucket=REPORT_S3_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8",
    )
    return make_s3_uri(key)


def download_json_from_s3(key):
    client = get_s3_client()
    obj = client.get_object(Bucket=REPORT_S3_BUCKET, Key=key)
    raw = obj["Body"].read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise S3ObjectCorrupted(f"corrupted JSON at s3://{REPORT_S3_BUCKET}/{key}: {exc}") from exc


def delete_s3_object_if_corrupted(key):
    client = get_s3_client()
    try:
        client.delete_object(Bucket=REPORT_S3_BUCKET, Key=key)
    except Exception:
        pass


def bucket_name():
    return REPORT_S3_BUCKET


# ── CSV ──────────────────────────────────────────────────────────────────────

def make_csv_s3_key(analysis_key, filename):
    prefix = REPORT_S3_CSV_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}/{filename}"


def upload_bytes_to_s3(key, data, content_type="application/octet-stream"):
    """범용 bytes 업로드. s3_uri 반환."""
    client = get_s3_client()
    client.put_object(
        Bucket=REPORT_S3_BUCKET, Key=key, Body=data, ContentType=content_type
    )
    return make_s3_uri(key)


def download_bytes_from_s3(key):
    """범용 bytes 다운로드."""
    client = get_s3_client()
    obj = client.get_object(Bucket=REPORT_S3_BUCKET, Key=key)
    return obj["Body"].read()


# ── fail_items ────────────────────────────────────────────────────────────────

def make_fail_items_s3_key(analysis_key):
    prefix = REPORT_S3_FAIL_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}.json"


# ── issue_table ───────────────────────────────────────────────────────────────

def make_issue_table_s3_key(analysis_key):
    prefix = REPORT_S3_ISSUE_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}.json"


# ── per-subject SVG thumbnails ───────────────────────────────────────────────

def make_thumb_s3_key(analysis_key, subject_id):
    prefix = REPORT_S3_THUMB_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}/{int(subject_id)}.svg"


def make_thumb_prefix_key(analysis_key):
    prefix = REPORT_S3_THUMB_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}/"


# ── Honey 업로드 산출물 (xlsx + 추출 텍스트) ─────────────────────────────────

def make_source_xlsx_s3_key(analysis_key):
    prefix = REPORT_S3_SOURCE_XLSX_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}.xlsx"


def make_summary_text_s3_key(analysis_key):
    prefix = REPORT_S3_SUMMARY_TEXT_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}.json"


def make_issue_text_s3_key(analysis_key):
    prefix = REPORT_S3_ISSUE_TEXT_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}.json"


# ── 클라이언트(Excel COM)가 렌더한 차트 PNG 갤러리 ───────────────────────────

def make_chart_png_s3_key(analysis_key, idx):
    prefix = REPORT_S3_CHART_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}/{int(idx)}.png"


def make_chart_index_s3_key(analysis_key):
    prefix = REPORT_S3_CHART_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}/index.json"

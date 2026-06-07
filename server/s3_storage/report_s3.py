"""ENTRYPOINT / EXTERNAL_OWNER: low-level S3 adapter.

This module is now called through ``storage_gateway`` by report routes and
upload flow. External S3/server-storage branches can replace or extend this
adapter while preserving the gateway contract.
"""
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
    REPORT_S3_YIELD_TEXT_PREFIX,
    REPORT_S3_ISSUE_IMG_PREFIX,
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


# download_bytes_from_s3() 는 파일 하단(presigned URL 위)에 단일 정의로 통합됨.


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


def make_yield_text_s3_key(analysis_key):
    prefix = REPORT_S3_YIELD_TEXT_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}.json"


# ── Issue_table 행별 분포 이미지 (골격) ──────────────────────────────────────
# xlsx 의 Distribution(I) 열에 박힌 행별 PNG 를 S3 에 보관. 키빌더만 우선 구현하고
# 실제 추출/업로드 활성화는 다음 단계(upload_xlsx.py 의 조건부 훅) 에서.

def make_issue_image_s3_key(analysis_key, row):
    prefix = REPORT_S3_ISSUE_IMG_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}/{int(row)}.png"


def make_issue_image_index_s3_key(analysis_key):
    prefix = REPORT_S3_ISSUE_IMG_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}/index.json"


# ── 클라이언트(Excel COM)가 렌더한 차트 PNG 갤러리 ───────────────────────────

def make_chart_png_s3_key(analysis_key, idx):
    prefix = REPORT_S3_CHART_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}/{int(idx)}.png"


def make_chart_index_s3_key(analysis_key):
    prefix = REPORT_S3_CHART_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}/index.json"


# ── Distribution 합성 PNG (클라이언트 차트 PNG 그리드 합성) ──────────────────

REPORT_S3_DIST_COMBINED_PREFIX = "pe/report_server/distribution_combined"


def make_distribution_combined_s3_key(analysis_key: str) -> str:
    prefix = REPORT_S3_DIST_COMBINED_PREFIX.strip("/")
    return f"{prefix}/{analysis_key}.png"


def download_bytes_from_s3(key: str) -> bytes:
    """S3 객체를 bytes 로 다운로드. S3NotConfigured / S3ObjectCorrupted 발생 가능."""
    client = get_s3_client()  # 미설정 시 S3NotConfigured 가 그대로 전파됨
    try:
        obj = client.get_object(Bucket=bucket_name(), Key=key)
        return obj["Body"].read()
    except S3NotConfigured:
        raise
    except Exception as exc:
        raise S3ObjectCorrupted(key) from exc


# ── presigned URL (외부 프로젝트 브랜치 호환) ────────────────────────────────
# 현재 표시는 서버 프록시(/pe/report/chart, /pe/report/issue_image) 로 일관하되,
# 외부 S3 드라이브 패턴(<img src={presigned_url}>) 과 브랜치하기 쉽도록 헬퍼만 노출.

def get_presigned_url(key, expires=3600):
    """key 에 대한 임시 GET URL 생성. boto3 generate_presigned_url 래퍼."""
    client = get_s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": REPORT_S3_BUCKET, "Key": key},
        ExpiresIn=int(expires),
    )

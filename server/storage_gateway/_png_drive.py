"""ENTRYPOINT / EXTERNAL_OWNER: external-compatible PNG drive adapter.

외부 프로젝트(`S3/s3_drive.py`) API 표면을 모방한 PNG 헬퍼 — 골격(스캐폴드).

목적: report 의 Distribution / Issue_table 분포 차트를 PNG 로 S3 에 올렸다가 세션
재호출 시 다시 표시하는 흐름을, 외부 프로젝트와 **브랜치/병합하기 쉽게** 같은 함수
시그니처로 노출한다. 실제 저장/조회는 내부적으로 기존 `report_s3` 헬퍼를 호출하므로
서버의 단일 S3 client/버킷 설정을 그대로 재사용한다.

외부 키 규칙(참고): `PE_Report/Test/{report_id}_{item_name}_{YYYYMMDD_HHMMSS}.png`
여기서는 충돌 회피를 위해 `REPORT_S3_ISSUE_IMG_PREFIX` 하위에 동일 네이밍으로 둔다.

골격 단계 주의:
- `build_key` / `exists` / `get_presigned_url` 등 순수 헬퍼는 바로 동작한다.
- `upload_png(s)` 는 실제 put 을 수행하지만, 호출부(upload_xlsx.py)는 골격 단계에서
  기본 비활성(조건부 훅) 이다. 외부 프로젝트 브랜치 시 호출부만 활성화하면 된다.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import REPORT_S3_ISSUE_IMG_PREFIX
from . import _s3 as report_s3


def build_key(report_id, item_name, timestamp=None, ext="png"):
    """식별자 조합으로 S3 key 생성.
    `{prefix}/{report_id}_{item_name}_{YYYYMMDD_HHMMSS}.{ext}`."""
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = REPORT_S3_ISSUE_IMG_PREFIX.strip("/")
    return f"{prefix}/{report_id}_{item_name}_{ts}.{ext}"


def upload_png(local_path, report_id, item_name, timestamp=None):
    """로컬 PNG 파일 1개를 업로드하고 key 반환."""
    key = build_key(report_id, item_name, timestamp)
    with open(local_path, "rb") as fp:
        report_s3.upload_bytes_to_s3(key, fp.read(), content_type="image/png")
    return key


def upload_png_bytes(data, report_id, item_name, timestamp=None):
    """PNG bytes 1개를 업로드하고 key 반환 (xlsx 임베드 이미지용)."""
    key = build_key(report_id, item_name, timestamp)
    report_s3.upload_bytes_to_s3(key, data, content_type="image/png")
    return key


def upload_pngs(items, max_workers=8):
    """다수 PNG 병렬 업로드.
    items: list[dict] — 각 {"local_path", "report_id", "item_name"[, "timestamp"]}.
    반환: list[dict] — 각 {"key", "ok", "error"}.
    """
    def _one(it):
        try:
            key = upload_png(it["local_path"], it["report_id"],
                             it["item_name"], it.get("timestamp"))
            return {"key": key, "ok": True, "error": None}
        except Exception as exc:  # noqa: BLE001 — 개별 실패가 전체를 막지 않게
            return {"key": None, "ok": False, "error": str(exc)}

    if not items:
        return []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_one, items))


def download_png(key, local_path):
    """S3 key 의 PNG 를 로컬로 저장하고 경로 반환."""
    data = report_s3.download_bytes_from_s3(key)
    with open(local_path, "wb") as fp:
        fp.write(data)
    return local_path


def get_presigned_url(key, expires=3600):
    """웹 조회용 임시 URL (외부 프로젝트와 동일 시그니처)."""
    return report_s3.get_presigned_url(key, expires)


def list_pngs(prefix=""):
    """접두사 하위 PNG key 목록."""
    client = report_s3.get_s3_client()
    base = REPORT_S3_ISSUE_IMG_PREFIX.strip("/")
    full = f"{base}/{prefix}".rstrip("/") if prefix else base
    out = []
    token = None
    while True:
        kwargs = {"Bucket": report_s3.bucket_name(), "Prefix": full}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []) or []:
            if obj["Key"].lower().endswith(".png"):
                out.append(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return out


def delete_png(key):
    """S3 key 삭제. 성공 여부 bool."""
    try:
        client = report_s3.get_s3_client()
        client.delete_object(Bucket=report_s3.bucket_name(), Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False


def exists(key):
    """key 존재 여부."""
    return report_s3.s3_object_exists(key)

"""ENTRYPOINT / EXTERNAL_OWNER: report artifact storage gateway.

External S3/server-storage branches should integrate here.  Flask routes and
upload parsing call this module instead of reaching into the internal
``_s3`` adapter directly.  The default implementation preserves the existing
S3 + local fallback behavior for Honey.exe compatibility tests.

Internal modules (외부 담당자 영역):
  ``_s3``           — boto3 어댑터 + 키 빌더 + 예외 (구 ``s3_storage.report_s3``)
  ``_issue_images`` — Issue_table 행 이미지 백엔드 (S3 + 로컬 폴백)
  ``_png_drive``    — 외부 호환 PNG 헬퍼 스캐폴드 (현재 미사용)
"""
import io
import math
from pathlib import Path

from config import REPORT_UPLOAD_DIR
from database import report_db
from . import _s3 as report_s3
from ._s3 import S3NotConfigured, S3ObjectCorrupted

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 텍스트 콘텐츠(object_type) → S3 키 빌더 매핑. 호출부가 키 빌더를 알 필요 없도록
# 게이트웨이 내부에서 해소한다(진입점 누수 방지).
_TEXT_KEY_BUILDERS = {
    "summary_text": report_s3.make_summary_text_s3_key,
    "yield_text": report_s3.make_yield_text_s3_key,
    "issue_table_text": report_s3.make_issue_text_s3_key,
}


def _combine_chart_pngs(pngs: list):
    """Compose chart PNG bytes into one grid PNG."""
    if not pngs:
        return None
    try:
        from PIL import Image

        imgs = [Image.open(io.BytesIO(p)).convert("RGB") for p in pngs]
        w = max(im.width for im in imgs)
        h = max(im.height for im in imgs)
        n = len(imgs)
        ncols = max(1, min(10, math.ceil(math.sqrt(n))))
        nrows = math.ceil(n / ncols)
        canvas = Image.new("RGB", (w * ncols, h * nrows), color=(255, 255, 255))
        for i, im in enumerate(imgs):
            r, c = divmod(i, ncols)
            canvas.paste(im, (c * w, r * h))
        buf = io.BytesIO()
        canvas.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def save_upload_artifacts(
    *,
    analysis_key,
    content_hash,
    meta_str,
    xlsx_bytes,
    issue_images=None,
    dist_png=None,
    chart_pngs=None,
):
    """Persist upload artifacts and return status/warnings.

    This intentionally owns S3/local fallback details so upload_xlsx.py can stay
    focused on request validation, parsing, and DB summary rows.
    """
    warnings = []
    s3_ok = True
    issue_imgs_saved = 0
    dist_combined_saved = False
    charts_saved = len(chart_pngs or [])

    try:
        xlsx_key = report_s3.make_source_xlsx_s3_key(analysis_key)
        if not report_s3.s3_object_exists(xlsx_key):
            xlsx_uri = report_s3.upload_bytes_to_s3(
                xlsx_key,
                xlsx_bytes,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        else:
            xlsx_uri = report_s3.make_s3_uri(xlsx_key)
        report_db.upsert_object_info(
            analysis_key, content_hash, meta_str,
            "source_xlsx", report_s3.bucket_name(), xlsx_key, xlsx_uri,
        )
    except S3NotConfigured:
        s3_ok = False
        warnings.append("S3 not configured; source xlsx not persisted")
    except Exception as exc:
        raise RuntimeError(f"S3 upload failed: {exc}") from exc

    if issue_images:
        try:
            from ._issue_images import save_images
            res = save_images(analysis_key, issue_images)
            issue_imgs_saved = len(res.get("rows", []))
        except Exception as exc:
            warnings.append(f"issue_images save failed: {exc}")

    dist_data = dist_png if dist_png and dist_png[:8] == PNG_MAGIC else None
    if dist_data:
        dist_combined_saved = save_distribution_png(
            analysis_key, content_hash, meta_str, dist_data, s3_ok, warnings)

    if not dist_combined_saved and chart_pngs:
        if s3_ok:
            combined = _combine_chart_pngs(chart_pngs)
            if combined:
                dist_combined_saved = save_distribution_png(
                    analysis_key, content_hash, meta_str, combined, s3_ok, warnings)
            else:
                warnings.append("chart PNG grid composition failed (Pillow missing or bad PNG)")
        else:
            warnings.append("charts received but S3 not configured; skipped (use distribution_sheet)")

    return {
        "s3_ok": s3_ok,
        "warnings": warnings,
        "issue_images_saved": issue_imgs_saved,
        "distribution_combined": dist_combined_saved,
        "charts_saved": charts_saved,
    }


def save_distribution_png(analysis_key, content_hash, meta_str, data, s3_ok=True, warnings=None):
    warnings = warnings if warnings is not None else []
    if s3_ok:
        try:
            dist_key = report_s3.make_distribution_combined_s3_key(analysis_key)
            dist_uri = report_s3.upload_bytes_to_s3(
                dist_key, data, content_type="image/png")
            report_db.upsert_object_info(
                analysis_key, content_hash, meta_str,
                "distribution_combined", report_s3.bucket_name(), dist_key, dist_uri,
            )
            return True
        except Exception as exc:
            warnings.append(f"distribution_combined upload failed: {exc}")
    try:
        local_dir = Path(REPORT_UPLOAD_DIR) / "dist_combined"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / f"{analysis_key}.png").write_bytes(data)
        return True
    except Exception as exc:
        warnings.append(f"distribution_sheet local save failed: {exc}")
        return False


def load_json_object(objects, object_type):
    """Load JSON object by object_info map and type. Returns None on failure."""
    if object_type not in objects:
        return None
    try:
        return report_s3.download_json_from_s3(objects[object_type]["s3_key"])
    except (S3NotConfigured, S3ObjectCorrupted, Exception):
        return None


def save_text_object(analysis_key, session, object_type, data):
    """Upload text JSON and refresh report_object_info.

    object_type 에 해당하는 S3 키 빌더는 내부 ``_TEXT_KEY_BUILDERS`` 로 해소한다.
    """
    key = _TEXT_KEY_BUILDERS[object_type](analysis_key)
    uri = report_s3.upload_json_to_s3(key, data)
    existing = report_db.get_object_info(analysis_key, object_type) or {}
    content_hash = existing.get("content_hash") or session.get("content_hash") or ""
    options_json = existing.get("options_json") or "{}"
    report_db.upsert_object_info(
        analysis_key, content_hash, options_json, object_type,
        report_s3.bucket_name(), key, uri,
    )


def list_issue_image_rows(analysis_key):
    from ._issue_images import list_rows
    return list_rows(analysis_key)


def load_issue_image(analysis_key, row):
    from ._issue_images import load_image
    return load_image(analysis_key, row)


def load_chart_png(analysis_key, idx):
    key = report_s3.make_chart_png_s3_key(analysis_key, idx)
    return report_s3.download_bytes_from_s3(key)


def load_distribution_png(analysis_key):
    objs = {o["object_type"]: o for o in report_db.get_all_object_infos(analysis_key)}
    if "distribution_combined" in objs:
        try:
            return report_s3.download_bytes_from_s3(objs["distribution_combined"]["s3_key"])
        except (S3NotConfigured, Exception):
            pass
    local_path = Path(REPORT_UPLOAD_DIR) / "dist_combined" / f"{analysis_key}.png"
    if local_path.exists():
        return local_path.read_bytes()
    raise FileNotFoundError(f"distribution combined PNG not found: {analysis_key}")

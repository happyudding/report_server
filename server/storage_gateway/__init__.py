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
    issue_images=None,
    dist_png=None,
    chart_pngs=None,
):
    """Persist upload artifacts and return status/warnings.

    This intentionally owns S3/local fallback details so upload_xlsx.py can stay
    focused on request validation, parsing, and DB summary rows.

    원본 xlsx 는 더 이상 받지 않는다 — 클라이언트가 추출 텍스트(grid)만 전송하므로
    source_xlsx 아카이브는 폐지되었다. 텍스트 데이터는 DB(sheet_data)에 저장된다.
    """
    warnings = []
    s3_ok = True
    issue_imgs_saved = 0
    dist_combined_saved = False
    charts_saved = len(chart_pngs or [])

    try:
        report_s3._require_config()
    except S3NotConfigured:
        s3_ok = False
        warnings.append("S3 not configured; image artifacts not persisted")

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


def save_webreport_sources(analysis_key, content_hash, sources: list, manifest: dict,
                           upload_root) -> dict:
    """web_report parquet 원본 + manifest 저장. S3 우선, 실패 시 로컬 폴백.

    sources: list[bytes] (parquet 원본). 반환: {"storage": "s3"|"local", "warnings": [...]}.
    """
    warnings = []
    s3_ok = True
    try:
        report_s3._require_config()
    except S3NotConfigured:
        s3_ok = False

    if s3_ok:
        try:
            for idx, data in enumerate(sources):
                key = report_s3.make_webreport_source_s3_key(analysis_key, idx)
                uri = report_s3.upload_bytes_to_s3(
                    key, data, content_type="application/vnd.apache.parquet")
                report_db.upsert_object_info(
                    analysis_key, content_hash, "{}", f"web_report_source_{idx}",
                    report_s3.bucket_name(), key, uri)
            mkey = report_s3.make_webreport_manifest_s3_key(analysis_key)
            muri = report_s3.upload_json_to_s3(mkey, manifest)
            report_db.upsert_object_info(
                analysis_key, content_hash, "{}", "web_report_manifest",
                report_s3.bucket_name(), mkey, muri)
            return {"storage": "s3", "warnings": warnings}
        except Exception as exc:
            warnings.append(f"web_report S3 upload failed, falling back to local: {exc}")

    import json as _json
    session_dir = Path(upload_root) / "web_report" / analysis_key
    session_dir.mkdir(parents=True, exist_ok=True)
    for idx, data in enumerate(sources):
        (session_dir / f"source_{idx}.parquet").write_bytes(data)
    (session_dir / "manifest.json").write_text(
        _json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"storage": "local", "warnings": warnings}


def save_webreport_manifest(analysis_key, manifest: dict, upload_root) -> dict:
    """web_report manifest 만 갱신 저장 (parquet sources 는 건드리지 않음).

    etc_items / issue_comments 처럼 manifest 필드만 바뀌는 편집용. S3 우선, 실패 시 로컬
    폴백. sources 가 불변이므로 object_info 의 content_hash 는 기존 값을 유지한다.
    """
    warnings = []
    s3_ok = True
    try:
        report_s3._require_config()
    except S3NotConfigured:
        s3_ok = False

    if s3_ok:
        try:
            prev = {o["object_type"]: o for o in report_db.get_all_object_infos(analysis_key)}
            content_hash = (prev.get("web_report_manifest") or {}).get("content_hash", "")
            mkey = report_s3.make_webreport_manifest_s3_key(analysis_key)
            muri = report_s3.upload_json_to_s3(mkey, manifest)
            report_db.upsert_object_info(
                analysis_key, content_hash, "{}", "web_report_manifest",
                report_s3.bucket_name(), mkey, muri)
            return {"storage": "s3", "warnings": warnings}
        except Exception as exc:
            warnings.append(f"web_report manifest S3 upload failed, falling back to local: {exc}")

    import json as _json
    session_dir = Path(upload_root) / "web_report" / analysis_key
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "manifest.json").write_text(
        _json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"storage": "local", "warnings": warnings}


def load_webreport_manifest(analysis_key, upload_root) -> dict:
    """web_report manifest 만 재조회 (parquet sources 다운로드 없음). S3 우선, 로컬 폴백."""
    objs = {o["object_type"]: o for o in report_db.get_all_object_infos(analysis_key)}
    if "web_report_manifest" in objs:
        try:
            return report_s3.download_json_from_s3(objs["web_report_manifest"]["s3_key"])
        except (S3NotConfigured, Exception):
            pass

    import json as _json
    manifest_path = Path(upload_root) / "web_report" / analysis_key / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"web_report manifest not found: {analysis_key}")
    return _json.loads(manifest_path.read_text(encoding="utf-8"))


def load_webreport_sources(analysis_key, upload_root):
    """web_report parquet 원본 + manifest 재조회. S3 우선, 실패 시 로컬 폴백.

    반환: (list[bytes] sources, dict manifest). 둘 다 없으면 FileNotFoundError.
    """
    objs = {o["object_type"]: o for o in report_db.get_all_object_infos(analysis_key)}
    source_keys = sorted(
        (k for k in objs if k.startswith("web_report_source_")),
        key=lambda k: int(k.rsplit("_", 1)[1]),
    )
    if source_keys and "web_report_manifest" in objs:
        try:
            # 소스가 여러 개면 병렬 다운로드 (직렬 왕복 누적 방지). boto3 client 는 스레드세이프.
            if len(source_keys) > 1:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=min(4, len(source_keys))) as pool:
                    sources = list(pool.map(
                        lambda k: report_s3.download_bytes_from_s3(objs[k]["s3_key"]),
                        source_keys))
            else:
                sources = [report_s3.download_bytes_from_s3(objs[k]["s3_key"]) for k in source_keys]
            manifest = report_s3.download_json_from_s3(objs["web_report_manifest"]["s3_key"])
            return sources, manifest
        except (S3NotConfigured, Exception):
            pass

    import json as _json
    session_dir = Path(upload_root) / "web_report" / analysis_key
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"web_report sources not found: {analysis_key}")
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    sources = []
    idx = 0
    while True:
        p = session_dir / f"source_{idx}.parquet"
        if not p.exists():
            break
        sources.append(p.read_bytes())
        idx += 1
    if not sources:
        raise FileNotFoundError(f"web_report parquet sources missing on disk: {analysis_key}")
    return sources, manifest


def delete_report_artifacts(analysis_key, upload_root=None) -> dict:
    """analysis_key 산출물 삭제 (S3 오브젝트 + 로컬 폴백 파일). best-effort.

    세션 삭제에서 마지막 참조일 때만 호출한다. object_info 에 기록된 모든 s3_key 와,
    object_info 에 기록되지 않는 issue 이미지(키 패턴 기반: <akey>/<row>.png + index.json),
    로컬 폴백 3경로(web_report/<akey>/, issue_img/<akey>/, dist_combined/<akey>.png)를 지운다.
    실패는 warnings 로 모아 반환하고 예외를 올리지 않는다 — DB 행 정리
    (report_db.delete_analysis_rows)는 호출자 몫.
    """
    import shutil

    warnings = []
    upload_root = Path(upload_root or REPORT_UPLOAD_DIR)

    s3_ok = True
    try:
        report_s3._require_config()
    except S3NotConfigured:
        s3_ok = False

    if s3_ok:
        try:
            objs = report_db.get_all_object_infos(analysis_key)
        except Exception as exc:
            objs = []
            warnings.append(f"object_info lookup failed: {exc}")
        for obj in objs:
            key = str(obj.get("s3_key") or "").strip()
            if not key:
                continue
            try:
                report_s3.delete_object_from_s3(key)
            except Exception as exc:
                warnings.append(f"S3 delete failed ({key}): {exc}")
        # issue 이미지는 object_info 미기록 — index 로 행을 열거해 지우고 index 는 마지막에
        # 지운다 (행 삭제 실패 시 index 가 남아 재시도 가능).
        try:
            from ._issue_images import list_rows
            for row in list_rows(analysis_key):
                try:
                    report_s3.delete_object_from_s3(
                        report_s3.make_issue_image_s3_key(analysis_key, int(row)))
                except Exception as exc:
                    warnings.append(f"S3 issue image delete failed (row {row}): {exc}")
            try:
                report_s3.delete_object_from_s3(
                    report_s3.make_issue_image_index_s3_key(analysis_key))
            except Exception:
                pass
        except Exception as exc:
            warnings.append(f"issue image cleanup failed: {exc}")

    for path in (upload_root / "web_report" / analysis_key,
                 upload_root / "issue_img" / analysis_key):
        try:
            if path.is_dir():
                shutil.rmtree(path)
        except Exception as exc:
            warnings.append(f"local delete failed ({path}): {exc}")
    png = upload_root / "dist_combined" / f"{analysis_key}.png"
    try:
        if png.exists():
            png.unlink()
    except Exception as exc:
        warnings.append(f"local delete failed ({png}): {exc}")

    return {"warnings": warnings}


def load_json_object(objects, object_type):
    """Load JSON object by object_info map and type. Returns None on failure."""
    if object_type not in objects:
        return None
    try:
        return report_s3.download_json_from_s3(objects[object_type]["s3_key"])
    except (S3NotConfigured, S3ObjectCorrupted, Exception):
        return None


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

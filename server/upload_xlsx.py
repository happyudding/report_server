"""/pe/report/upload_xlsx 라우트.

Honey 클라이언트가 xlsx 한 파일 + product_type/product/lot_id 메타를 multipart 로
전송. 서버는:
1) sha256(xlsx + meta) → analysis_key
2) S3 에 xlsx 본문 업로드 (이미 있으면 skip)
3) report_session row 생성 (source='xlsx_upload')
4) xlsx_parser 로 summary/yield/issue_table 텍스트 추출
5) yield rows → report_analysis_summary 저장
6) summary/issue_table JSON → S3 + report_object_info upsert
7) status='done' 으로 마무리

CSV 분석 흐름(/pe/report/analyze) 과는 완전 분리됨.
"""
import hashlib
import json
import re
import secrets
import time

from flask import abort, jsonify, request
from werkzeug.utils import secure_filename

from database import report_db
from report.report_extension import report_bp
from s3_storage import report_s3
from s3_storage.report_s3 import S3NotConfigured

_PRODUCT_TYPES = {"MD", "PD", "PM", "SE"}
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,80}$")
_PIN_RE = re.compile(r"^\d{4}$")

_MAX_CHARTS = 10000   # 차트 수 제한 없음 (2000개 이상 지원)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _collect_chart_pngs(files):
    """multipart 의 chart_0, chart_1, ... 를 순서대로 PNG bytes 리스트로.
    클라이언트(Excel COM)가 렌더해 동봉한 차트 이미지. PNG 매직바이트 검증."""
    out = []
    for i in range(_MAX_CHARTS):
        f = files.get(f"chart_{i}")
        if f is None:
            break
        data = f.read()
        if data[:8] == _PNG_MAGIC:
            out.append(data)
    return out


def _combine_chart_pngs(pngs: list):
    """PNG bytes 리스트 → 그리드 합성 단일 PNG bytes.
    각 차트 이미지는 그대로 유지하고, 격자 배치로 하나의 큰 PNG 를 만든다.
    Pillow 미설치 / 빈 입력 시 None 반환."""
    if not pngs:
        return None
    try:
        import io
        import math
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


def _validate_meta(form) -> dict:
    pt = (form.get("product_type") or "").strip()
    product = (form.get("product") or "").strip()
    lot_id = (form.get("lot_id") or "").strip()

    if pt not in _PRODUCT_TYPES:
        abort(400, f"product_type must be one of {sorted(_PRODUCT_TYPES)}")
    if not product or not _SAFE_TOKEN_RE.match(product):
        abort(400, "product is required (alphanumeric / _ - . only)")
    if not lot_id or not _SAFE_TOKEN_RE.match(lot_id):
        abort(400, "lot_id is required (alphanumeric / _ - . only)")

    return {"product_type": pt, "product": product, "lot_id": lot_id}


def _canonical_meta_bytes(meta: dict) -> bytes:
    return json.dumps(meta, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _compute_analysis_key(xlsx_bytes: bytes, meta: dict) -> str:
    h = hashlib.sha256()
    h.update(xlsx_bytes)
    h.update(b"|")
    h.update(_canonical_meta_bytes(meta))
    return h.hexdigest()


def _yield_row_to_summary(row: dict) -> dict:
    """yield 시트의 한 행 dict 를 report_analysis_summary 컬럼으로 매핑.

    templete 레이아웃의 yield 행은 `bin | Item | {src}_count | {src}_yield | avg | comment`.
    - yield_percent : avg(소스 평균 수율%). legacy `portion(%)`/`yield` 도 fallback.
    - fail_count    : {src}_count 합(전체 개수). legacy `count`/`fail_count` fallback.
    """
    bin_val = row.get("bin")
    bin_num = None
    try:
        if bin_val is not None and str(bin_val).strip() not in ("", "Total"):
            bin_num = int(float(bin_val))
    except (ValueError, TypeError):
        bin_num = None

    yp = row.get("avg")
    if yp is None:
        yp = (row.get("portion(%)") or row.get("portion")
              or row.get("yield") or row.get("yield_percent"))

    src_count = sum(int(v) for k, v in row.items()
                    if k.endswith("_count") and isinstance(v, (int, float)))
    fail_count = src_count if src_count else _to_int(row.get("count") or row.get("fail_count"))

    return {
        "item_name": str(row.get("bin") if bin_val is not None else "unknown"),
        "bin_number": bin_num,
        "yield_percent": _to_float(yp),
        "fail_count": fail_count,
        "cpk_val": None,
        "mean_val": _to_float(row.get("avg") or row.get("average")),
        "stdev_val": None,
        "lsl": None,
        "usl": None,
        "unit": None,
    }


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


@report_bp.post("/upload_xlsx")
def upload_xlsx():
    if "xlsx" not in request.files:
        abort(400, "missing 'xlsx' file field")
    f = request.files["xlsx"]
    raw_name = f.filename or ""
    name = secure_filename(raw_name) or "upload.xlsx"
    if not name.lower().endswith(".xlsx"):
        abort(400, "file must be .xlsx")

    xlsx_bytes = f.read()
    if not xlsx_bytes:
        abort(400, "empty file")

    meta = _validate_meta(request.form)
    # password 는 접근 제어용이라 analysis_key 산출(meta)에는 포함하지 않는다.
    # 빈 문자열 허용 — 미설정 시 웹에서 비밀번호 없이 수정/삭제 가능 (legacy 세션과 동일).
    password = (request.form.get("password") or "").strip()
    if password and not _PIN_RE.match(password):
        abort(400, "password must be 4 digits or empty")
    analysis_key = _compute_analysis_key(xlsx_bytes, meta)
    content_hash = hashlib.sha256(xlsx_bytes).hexdigest()
    session_id = f"{int(time.time())}_{secrets.token_hex(3)}"

    report_db.create_session(
        session_id=session_id,
        file_name=name,
        file_path=None,
        product_type=meta["product_type"],
        product=meta["product"],
        lot_id=meta["lot_id"],
        password=password,
        source="xlsx_upload",
    )
    report_db.update_session(
        session_id, analysis_key=analysis_key,
        content_hash=content_hash, status="uploading",
    )

    # ── S3: 원본 xlsx 업로드 ────────────────────────────────────────────────
    s3_ok = True
    try:
        xlsx_key = report_s3.make_source_xlsx_s3_key(analysis_key)
        if not report_s3.s3_object_exists(xlsx_key):
            xlsx_uri = report_s3.upload_bytes_to_s3(
                xlsx_key, xlsx_bytes,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        else:
            xlsx_uri = report_s3.make_s3_uri(xlsx_key)
        report_db.upsert_object_info(
            analysis_key, content_hash, _canonical_meta_bytes(meta).decode("utf-8"),
            "source_xlsx", report_s3.bucket_name(), xlsx_key, xlsx_uri,
        )
    except S3NotConfigured:
        s3_ok = False
    except Exception as exc:
        report_db.update_session(session_id, status="failed",
                                 error_message=f"S3 upload failed: {exc}"[:500])
        return jsonify({"session_id": session_id, "status": "failed",
                        "error": f"S3 upload failed: {exc}"}), 500

    # ── xlsx 파싱 ──────────────────────────────────────────────────────────
    try:
        from xlsx_parser import parse_report_xlsx, XlsxTooLarge, XlsxLoadTimeout
        parsed = parse_report_xlsx(xlsx_bytes)
    except XlsxTooLarge as exc:
        report_db.update_session(session_id, status="failed",
                                 error_message=f"xlsx too large: {exc}"[:500])
        return jsonify({"session_id": session_id, "status": "failed",
                        "error": f"xlsx too large: {exc}"}), 413
    except XlsxLoadTimeout as exc:
        report_db.update_session(session_id, status="failed",
                                 error_message=f"xlsx parse timeout: {exc}"[:500])
        return jsonify({"session_id": session_id, "status": "failed",
                        "error": f"xlsx parse timeout: {exc}"}), 422
    except Exception as exc:
        report_db.update_session(session_id, status="failed",
                                 error_message=f"parse failed: {exc}"[:500])
        return jsonify({"session_id": session_id, "status": "failed",
                        "error": f"parse failed: {exc}"}), 400

    # ── 이하 처리는 부분 실패(비치명적)와 치명적 실패를 구분한다. ─────────────
    # 치명적(yield rows DB 저장 실패 등): status='failed' 로 마감하고 500.
    # 비치명적(sheet_data/이미지/차트 합성 실패): warnings 에 수집, status='done' 유지.
    # 어떤 경로로도 세션 status 가 'uploading' 에 멈춰있지 않도록 보장한다.
    warnings = []

    # ── yield rows → DB (report_analysis_summary) : 치명적 단계 ─────────────
    try:
        summary_rows = [_yield_row_to_summary(r) for r in parsed["yield_rows"]]
        summary_rows = [r for r in summary_rows
                        if r["item_name"] and r["item_name"] != "unknown"]
        saved = report_db.save_summary_batch(analysis_key, session_id, summary_rows)
    except Exception as exc:
        report_db.update_session(session_id, status="failed",
                                 error_message=f"summary save failed: {exc}"[:500])
        return jsonify({"session_id": session_id, "status": "failed",
                        "error": f"summary save failed: {exc}"}), 500

    # ── sheet_data (순수 텍스트) → DB (S3 유무와 무관하게 항상 저장) : 비치명적 ─
    sheet_data = parsed.get("sheet_data") or {}
    sheet_data_saved = []
    for sheet_name, data in sheet_data.items():
        try:
            report_db.upsert_sheet_data(analysis_key, sheet_name, data)
            sheet_data_saved.append(sheet_name)
        except Exception as exc:
            warnings.append(f"sheet_data[{sheet_name}] save failed: {exc}")

    # ── Issue_table 행별 분포 PNG → 저장소(S3 또는 로컬 폴백) : 비치명적 ───────
    # ISSUE_IMAGES_ENABLED=True 면 Distribution 열의 행별 PNG 가 추출된다.
    # issue_image_store 가 S3 설정 유무에 따라 S3/로컬을 자동 선택 → S3 미설정
    # 로컬 환경에서도 PNG 가 표시된다("임시로 PNG 나오게").
    issue_imgs_saved = 0
    if parsed.get("issue_images"):
        try:
            from issue_image_store import save_images
            res = save_images(analysis_key, parsed["issue_images"])
            issue_imgs_saved = len(res.get("rows", []))
        except Exception as exc:
            warnings.append(f"issue_images save failed: {exc}")

    # ── 차트 PNG 수신 → 그리드 합성 → S3 단일 PNG : 비치명적 ─────────────────
    # 클라이언트(Excel COM)가 렌더한 개별 차트 PNG 를 Pillow 로 격자 합성한다.
    # 각 차트 이미지는 픽셀 그대로 유지 — 합성만 수행. S3 미설정이면 건너뜀.
    chart_pngs = _collect_chart_pngs(request.files)
    charts_saved = len(chart_pngs)
    dist_combined_saved = False
    if chart_pngs and not s3_ok:
        warnings.append("charts received but S3 not configured; distribution_combined skipped")
    if s3_ok and chart_pngs:
        combined = _combine_chart_pngs(chart_pngs)
        if combined:
            try:
                meta_str = _canonical_meta_bytes(meta).decode("utf-8")
                dist_key = report_s3.make_distribution_combined_s3_key(analysis_key)
                dist_uri = report_s3.upload_bytes_to_s3(
                    dist_key, combined, content_type="image/png")
                report_db.upsert_object_info(
                    analysis_key, content_hash, meta_str,
                    "distribution_combined", report_s3.bucket_name(), dist_key, dist_uri,
                )
                dist_combined_saved = True
            except Exception as exc:
                warnings.append(f"distribution_combined upload failed: {exc}")
        else:
            warnings.append("chart PNG grid composition failed (Pillow missing or bad PNG)")

    # S3 미설정으로 원본 xlsx 가 저장되지 않았으면 경고로 명시 (조회 시 본문 누락).
    if not s3_ok:
        warnings.append("S3 not configured; source xlsx not persisted")

    # 비치명적 경고는 error_message 에 보존(조회/디버깅용)하되 status 는 done.
    if warnings:
        report_db.update_session(
            session_id, status="done",
            error_message=("; ".join(warnings))[:500],
        )
    else:
        report_db.update_session(session_id, status="done")

    return jsonify({
        "session_id": session_id,
        "analysis_key": analysis_key,
        "status": "done",
        "rows_saved": saved,
        "s3_uploaded": s3_ok,
        "charts_saved": charts_saved,
        "distribution_combined": dist_combined_saved,
        "issue_images_saved": issue_imgs_saved,
        "sheet_data_saved": sorted(sheet_data_saved),
        "summary_keys": list(parsed["summary"].keys()),
        "yield_row_count": len(parsed["yield_rows"]),
        "issue_row_count": len(parsed["issue_rows"]),
        "warnings": warnings,
    })

"""/pe/report/upload_webreport route."""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

from flask import abort, jsonify, redirect, request
from werkzeug.exceptions import HTTPException

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from config import REPORT_UPLOAD_DIR
from database import report_db
from report.report_extension import report_bp
from web_report import service as web_report_service

_log = logging.getLogger(__name__)

_MAX_WEBREPORT_BYTES = 512 * 1024 * 1024


def _client_meta():
    fwd = request.headers.get("X-Forwarded-For")
    ip = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
    return ip, str(request.user_agent)


def _read_manifest():
    raw = request.form.get("manifest")
    if not raw:
        abort(400, "missing manifest")
    try:
        manifest = json.loads(raw)
    except Exception:
        abort(400, "manifest must be valid JSON")
    if not isinstance(manifest, dict):
        abort(400, "manifest must be a JSON object")
    return manifest


def _read_files():
    items = []
    total = 0
    limit = max(1, int(config.REPORT_WEBREPORT_TOTAL_MB)) * 1024 * 1024
    idx = 0
    while True:
        f = request.files.get(f"webreport_{idx}")
        if f is None:
            break
        data = f.read()
        if not data:
            abort(400, f"webreport_{idx} is empty")
        if len(data) > _MAX_WEBREPORT_BYTES:
            abort(413, f"webreport_{idx} payload is too large")
        # 개별 상한만으로는 파일 수 × 512MB 가 그대로 웹 프로세스 메모리에 쌓인다
        # (parquet 은 전량 메모리 적재 후 디코드) — 합계 상한으로 한 번 더 막는다.
        total += len(data)
        if total > limit:
            abort(413, f"webreport 파일 합계가 상한({limit // (1024 * 1024)}MB)을 넘습니다")
        items.append({"name": f"webreport_{idx}", "filename": f.filename, "data": data})
        idx += 1
    if not items:
        abort(400, "missing webreport parquet files")
    return items


def _read_dist_blobs():
    """클라 프리컴퓨트 Distribution blob(gzip) 필드 — 선택 첨부(구 클라는 없음).

    dist_blob = 전체 기준 ECDF, dist_blob_bin1 = 양품(Bin1)만 ECDF. 검증·시딩은
    ingest(web_report.ingest._seed_client_dist_blobs)가 담당한다. 선택 최적화 첨부물이므로
    크기 초과는 업로드 실패(413)가 아니라 **그 변형만 건너뛰기**(서버 폴백 계산) —
    실측상 전 값이 고유한 worst case 데이터(10k행×1500항목×7소스)에서 상한 근접.
    """
    blobs = {}
    for variant, field in (("all", "dist_blob"), ("bin1", "dist_blob_bin1")):
        f = request.files.get(field)
        if f is None:
            continue
        data = f.read()
        if not data or len(data) > _MAX_WEBREPORT_BYTES:
            continue
        blobs[variant] = data
    return blobs


@report_bp.post("/upload_webreport")
def upload_webreport():
    started = time.perf_counter()
    try:
        manifest = _read_manifest()
        files = _read_files()
        dist_blobs = _read_dist_blobs()
        ip, ua = _client_meta()
        result = web_report_service.ingest_webreport(
            manifest, files, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            client_ip=ip, user_agent=ua, dist_blobs=dist_blobs,
            request_started=started)
    except HTTPException:
        # abort() 가 정한 상태코드(413 등)를 아래 catch-all 이 400 으로 뭉개지 않게 통과시킨다.
        raise
    except RuntimeError as exc:
        return jsonify({"status": "failed", "error": str(exc)}), 503
    except ValueError as exc:
        # 입력 데이터 문제(잘못된 parquet·모드별 파일 수 등) — 클라이언트 오류.
        return jsonify({"status": "failed", "error": str(exc)}), 400
    except Exception as exc:
        # 저장/DB/디코드 등 서버 측 장애를 400 으로 돌려주면 클라가 "내 파일이 잘못됐다"로
        # 오인하고 재시도도 안 한다. 5xx 로 알리고 traceback 을 서버에 남긴다.
        _log.exception("[upload_webreport] failed")
        return jsonify({"status": "failed", "error": str(exc)}), 500

    return jsonify(result)


@report_bp.get("/web_report/<session_id>")
def web_report_page(session_id):
    return redirect(f"/pe/report/view/{session_id}", code=302)

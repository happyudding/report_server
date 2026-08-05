"""/pe/report/upload_webreport route."""
from __future__ import annotations

import json
import logging
import sys
import threading
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

# 동시 업로드 처리 수 제한 — 업로드 1건은 parquet bytes 전량(합계 최대 1GB)과 디코드된
# tables(대형 세션이면 수백 MB)를 동시에 들고 있다. waitress 스레드(13)가 전부 업로드에
# 몰리면 그 피크가 그대로 겹쳐 웹 프로세스가 죽는다.
# ⚠️ acquire 는 request.form/files 에 손대기 **전에** 해야 한다 — werkzeug 는 멀티파트를
# 디스크에 스풀해 두므로, 대기 중인 요청은 RAM 이 아니라 임시파일만 점유한다.
_UPLOAD_SEM = threading.BoundedSemaphore(max(1, int(config.WEB_REPORT_UPLOAD_CONCURRENCY)))


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


def _read_dist_pack():
    """클라 Distribution pack 필드 — 선택 첨부(구 클라는 없음).

    dist_pack_index(form JSON) + dist_pack_chunk_<n>(파일). 검증·영구 저장은
    ingest(web_report.ingest.save_client_dist_pack)가 담당한다. dist_blob 과 같은 이유로
    크기 초과·결손은 업로드 실패가 아니라 **pack 전체 건너뛰기**(서버 폴백 계산)다 —
    부분 pack 을 저장하면 조회가 항목을 잃는다.
    """
    index_text = request.form.get("dist_pack_index")
    if not index_text:
        return None
    # chunk 는 전부 메모리에 쌓이므로 parquet 과 같은 합계 상한을 건다.
    budget = max(1, int(config.REPORT_WEBREPORT_TOTAL_MB)) * 1024 * 1024
    chunks = {}
    idx = 0
    total = 0
    while True:
        f = request.files.get(f"dist_pack_chunk_{idx}")
        if f is None:
            break
        data = f.read()
        if not data or len(data) > _MAX_WEBREPORT_BYTES:
            return None
        total += len(data)
        if total > budget:
            return None
        chunks[idx] = data
        idx += 1
    if not chunks:
        return None
    return {"index": index_text, "chunks": chunks}


@report_bp.post("/upload_webreport")
def upload_webreport():
    started = time.perf_counter()
    if not _UPLOAD_SEM.acquire(timeout=float(config.WEB_REPORT_UPLOAD_WAIT_SEC)):
        _log.warning("[upload_webreport] 동시 업로드 상한 대기 초과 — 거절")
        return jsonify({"status": "failed",
                        "error": "서버가 다른 업로드를 처리 중입니다. 잠시 후 다시 시도해 주세요."}), 503
    try:
        manifest = _read_manifest()
        files = _read_files()
        dist_blobs = _read_dist_blobs()
        dist_pack = _read_dist_pack()
        ip, ua = _client_meta()
        result = web_report_service.ingest_webreport(
            manifest, files, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            client_ip=ip, user_agent=ua, dist_blobs=dist_blobs, dist_pack=dist_pack,
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
    finally:
        _UPLOAD_SEM.release()

    return jsonify(result)


@report_bp.get("/web_report/<session_id>")
def web_report_page(session_id):
    return redirect(f"/pe/report/view/{session_id}", code=302)

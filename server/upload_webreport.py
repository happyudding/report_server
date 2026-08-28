"""/pe/report/upload_webreport route."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from contextlib import nullcontext
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

# 단계 계측 (2026-08-19) — 업로드는 동기 구간이 13단계나 되는데 지금까지 남는 것은 성공 시
# 총합 1줄뿐이었다. 그래서 300초 타임아웃이 나도 **어디서** 썼는지 알 방법이 없었다.
# metrics 가 이미 진행 중 요청을 스레드 단위로 들고 있으므로(admin 'stuck' 감지) 거기에
# 단계만 얹는다 — 별도 레지스트리를 만들면 같은 것이 두 벌이 된다.
try:
    from admin_panel import metrics as _metrics
except Exception:      # 관리자 패널 없이 뜨는 구성(테스트 등)에서도 업로드는 돌아야 한다
    _metrics = None


def _stage(name, source=""):
    """계측이 없으면 아무것도 하지 않는 with 블록. ingest 에도 이 함수를 그대로 넘긴다."""
    return _metrics.stage(name, source) if _metrics is not None else nullcontext()


_MAX_WEBREPORT_BYTES = 512 * 1024 * 1024

# 동시 업로드 처리 수 제한 — 업로드 1건은 parquet bytes 전량(합계 최대 1GB)과 디코드된
# tables(대형 세션이면 수백 MB)를 동시에 들고 있다. waitress 스레드(13)가 전부 업로드에
# 몰리면 그 피크가 그대로 겹쳐 웹 프로세스가 죽는다.
# ⚠️ acquire 는 request.form/files 에 손대기 **전에** 해야 한다 — werkzeug 는 멀티파트를
# 디스크에 스풀해 두므로, 대기 중인 요청은 RAM 이 아니라 임시파일만 점유한다.
_UPLOAD_SEM = threading.BoundedSemaphore(max(1, int(config.WEB_REPORT_UPLOAD_CONCURRENCY)))
# 대기는 RAM 을 안 쓰지만 waitress 스레드는 문다 — 줄 설 수 있는 요청 수를 따로 막지
# 않으면 업로드 폭주 1회로 스레드가 전멸해 조회·/healthz 까지 수 분간 멎는다.
_UPLOAD_MAX_WAITERS = max(0, int(config.WEB_REPORT_UPLOAD_MAX_WAITERS))
_upload_waiters = 0
_upload_waiters_lock = threading.Lock()


def _acquire_upload_slot():
    """업로드 슬롯을 잡는다. (성공, 거절사유) 반환.

    빈 슬롯이 있으면 즉시 통과(종전과 동일). 없으면 대기열 길이를 보고 **대기할지
    즉시 거절할지**를 정한다 — 소수 버스트는 종전처럼 기다렸다 성공하고, 폭주만 잘라
    스레드 고갈을 막는다.
    """
    global _upload_waiters
    if _UPLOAD_SEM.acquire(blocking=False):
        return True, ""
    with _upload_waiters_lock:
        if _upload_waiters >= _UPLOAD_MAX_WAITERS:
            return False, "queue_full"
        _upload_waiters += 1
    try:
        ok = _UPLOAD_SEM.acquire(timeout=float(config.WEB_REPORT_UPLOAD_WAIT_SEC))
    finally:
        with _upload_waiters_lock:
            _upload_waiters -= 1
    return (True, "") if ok else (False, "wait_timeout")


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


def _normalize_eval_sensitivity(manifest):
    """manifest.options 의 민감도 설정을 검증·정규화해 **제자리에서** 바꾼다.

    ingest 는 options dict 를 그대로 세션에 굳히므로, 여기서 걸러야 잘못된 임계값이
    세션에 남지 않는다. 기본 설정이면 키를 통째로 지운다 — 옵션 원문이 캐시 키의
    원소라, 기본값도 실으면 기존 세션과 키가 갈려 콜드 재빌드가 된다.
    구 클라(키 없음)는 아무 일도 일어나지 않는다.
    """
    options = manifest.get("options")
    if not isinstance(options, dict) or "eval_sensitivity" not in options:
        return
    from report.eval_sensitivity import SensitivityError, normalize
    from web_report import eval_debug
    try:
        normalized = normalize(options.get("eval_sensitivity"),
                               rules_rev=eval_debug.rules_rev())
    except SensitivityError as exc:
        abort(400, f"AI Comment 민감도 설정이 올바르지 않습니다: {exc}")
    if normalized:
        options["eval_sensitivity"] = normalized
    else:
        options.pop("eval_sensitivity", None)


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


def _emit_dist_dropped(what, detail):
    """선택 첨부(dist blob/pack)를 버렸음을 진단 사건으로 남긴다 (best-effort).

    로그 한 줄만으로는 나중에 "이 세션만 조회가 느리다" 신고가 왔을 때 세션과 이어붙일
    수 없다. 업로드는 정상 완료되므로 실패가 아니라 warning 이며, 클라 쪽에는 응답의
    dist_pack_saved/dist_blob_seeded 로 이미 사실이 나간다."""
    try:
        import diagnostics
        diagnostics.emit("warning", "server", "dist_precompute_dropped",
                         http_status=200, error_type="dropped",
                         message=f"{what} 건너뜀 — {detail}"[:500],
                         **diagnostics.current_ids())
    except Exception:
        pass


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
            # 조용히 버리면 "왜 이 세션만 조회가 느리지"를 나중에 추적할 수 없다.
            _log.warning("[upload_webreport] dist_blob(%s) 건너뜀 — %s bytes", variant,
                         len(data) if data else 0)
            _emit_dist_dropped(f"dist_blob({variant})",
                               f"{len(data) if data else 0} bytes")
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
            _log.warning("[upload_webreport] dist_pack 건너뜀 — chunk %d 크기 %s bytes",
                         idx, len(data) if data else 0)
            _emit_dist_dropped("dist_pack",
                               f"chunk {idx} 크기 {len(data) if data else 0} bytes")
            return None
        total += len(data)
        if total > budget:
            _log.warning("[upload_webreport] dist_pack 건너뜀 — 합계 %d bytes > 상한 %d",
                         total, budget)
            _emit_dist_dropped("dist_pack", f"합계 {total} bytes > 상한 {budget}")
            return None
        chunks[idx] = data
        idx += 1
    if not chunks:
        _log.warning("[upload_webreport] dist_pack 건너뜀 — index 는 있는데 chunk 가 없음")
        _emit_dist_dropped("dist_pack", "index 는 있는데 chunk 가 없음")
        return None
    return {"index": index_text, "chunks": chunks}


def _record_upload_failure(manifest, exc, status, severity):
    """업로드 실패를 서버에 남긴다 (2026-08-11).

    지금까지 400/503 경로는 클라 화면에만 뜨고 서버에는 흔적이 0 이라, 사용자가
    "업로드가 안 된다"고 신고하면 확인할 방법이 없었다. xlsx 흐름은 세션 행
    error_message 로 남기는데 web_report 만 비어 있던 비대칭을 메운다."""
    meta = (manifest or {}).get("meta") if isinstance(manifest, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    detail = f"{type(exc).__name__}: {exc}"
    try:
        ip, ua = _client_meta()
    except Exception:
        ip = ua = ""
    try:
        report_db.log_audit(
            "upload", product_type=meta.get("product_type"), product=meta.get("product"),
            lot_id=meta.get("lot_id"), file_name=meta.get("file_name"),
            changed_fields=f"upload_webreport {status}: {detail}"[:1500],
            client_ip=ip, user_agent=ua, result="fail")
    except Exception:
        _log.warning("[upload_webreport] 실패 감사 기록 실패", exc_info=True)
    try:
        import diagnostics
        diagnostics.emit(severity, "server", "upload_failed", http_status=status,
                         error_type=type(exc).__name__, message=detail[:500],
                         product=meta.get("product"), lot_id=meta.get("lot_id"),
                         source=meta.get("file_name"), client_ip=ip, **diagnostics.current_ids())
    except Exception:
        pass


def _upload_summary(started, cpu0, files, session_id, ok):
    """업로드 1건의 단계별 소요를 한 줄로 남긴다 — **느리지 않은 업로드도 항상**.

    기준선이 없으면 "느려졌다"를 판정할 수 없다. cpu 는 프로세스 CPU 시간 / 실제 시간
    비율이라 낮을수록 계산이 아니라 대기(다른 프로세스와의 CPU 경합·IO)에 시간을 쓴
    것이다 — 콜드 빌드 워커가 코어를 채워 업로드 디코드가 굶는 현상
    (web_report/compute.py `_lower_worker_priority`)을 사후에 가려내는 지표다.
    """
    try:
        total = round(time.perf_counter() - started, 1)
        stages = _metrics.stages_done() if _metrics is not None else {}
        cpu = (_metrics.cpu_ratio(cpu0, _metrics.cpu_snapshot())
               if (_metrics is not None and cpu0) else None)
        mb = round(sum(len(f["data"]) for f in (files or [])) / (1024 * 1024), 1)
        parts = " ".join(f"{k}={v}" for k, v in
                         sorted(stages.items(), key=lambda kv: -kv[1]))
        starved = cpu is not None and cpu < 0.3 and total >= 20
        _log.info("[upload_webreport] %s session=%s %sMB/%d파일 total=%ss cpu=%s%s %s",
                  "완료" if ok else "실패", session_id or "-", mb, len(files or []),
                  total, cpu if cpu is not None else "?",
                  " ⚠CPU경합의심" if starved else "", parts)
    except Exception:
        pass


@report_bp.post("/upload_webreport")
def upload_webreport():
    started = time.perf_counter()
    cpu0 = _metrics.cpu_snapshot() if _metrics is not None else None
    manifest = None
    files = None
    result = None
    # 슬롯 대기는 최대 WEB_REPORT_UPLOAD_WAIT_SEC 초를 먹는데 지금까지 무계측이라,
    # 총 소요만 보고는 "서버가 느린 것"과 "줄을 선 것"을 구분할 수 없었다.
    with _stage("slot_wait"):
        ok, reason = _acquire_upload_slot()
    if not ok:
        _log.warning("[upload_webreport] 동시 업로드 상한 — 거절(%s, 대기 %d건)",
                     reason, _upload_waiters)
        # Retry-After: 클라가 언제 다시 시도하면 되는지 알린다(현재 클라는 수동 재시도).
        return jsonify({"status": "failed",
                        "error": "서버가 다른 업로드를 처리 중입니다. 잠시 후 다시 시도해 주세요."}), 503, \
                       {"Retry-After": "30"}
    try:
        # ⚠️ 첫 request.form 접근이 werkzeug 의 멀티파트 lazy 파싱을 통째로 트리거한다
        # (디스크 스풀 → 파싱). 즉 이 한 줄이 곧 '바디 파싱' 구간이다. 네트워크 수신은
        # waitress 가 태스크 큐에 넣기 전에 이미 끝나 있어(channel.py) 여기서는 안 잡힌다 —
        # 그 구간은 클라가 재는 body_sec 로만 알 수 있다.
        with _stage("multipart"):
            manifest = _read_manifest()
        _normalize_eval_sensitivity(manifest)
        with _stage("read_files"):
            files = _read_files()
        with _stage("read_dist"):
            dist_blobs = _read_dist_blobs()
            dist_pack = _read_dist_pack()
        ip, ua = _client_meta()
        result = web_report_service.ingest_webreport(
            manifest, files, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            client_ip=ip, user_agent=ua, dist_blobs=dist_blobs, dist_pack=dist_pack,
            request_started=started, trace=_stage)
    except HTTPException as exc:
        # abort() 가 정한 상태코드(413 등)를 아래 catch-all 이 400 으로 뭉개지 않게 통과시킨다.
        _record_upload_failure(manifest, exc, exc.code or 400, "info")
        raise
    except RuntimeError as exc:
        _log.warning("[upload_webreport] rejected(503): %s", exc)
        _record_upload_failure(manifest, exc, 503, "warning")
        return jsonify({"status": "failed", "error": str(exc)}), 503
    except ValueError as exc:
        # 입력 데이터 문제(잘못된 parquet·모드별 파일 수 등) — 클라이언트 오류.
        _log.warning("[upload_webreport] rejected(400): %s", exc)
        _record_upload_failure(manifest, exc, 400, "info")
        return jsonify({"status": "failed", "error": str(exc)}), 400
    except Exception as exc:
        # 저장/DB/디코드 등 서버 측 장애를 400 으로 돌려주면 클라가 "내 파일이 잘못됐다"로
        # 오인하고 재시도도 안 한다. 5xx 로 알리고 traceback 을 서버에 남긴다.
        _log.exception("[upload_webreport] failed")
        _record_upload_failure(manifest, exc, 500, "critical")
        return jsonify({"status": "failed", "error": str(exc)}), 500
    finally:
        _UPLOAD_SEM.release()
        _upload_summary(started, cpu0, files,
                        (result or {}).get("session_id"), result is not None)

    return jsonify(result)


@report_bp.get("/web_report/<session_id>")
def web_report_page(session_id):
    return redirect(f"/pe/report/view/{session_id}", code=302)

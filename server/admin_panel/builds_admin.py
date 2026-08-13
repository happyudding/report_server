"""진행 중 콜드 빌드 상세 + 관리자 개입 (2026-08-13).

"진행 중 콜드 빌드 2건 / 장기화 2건" 만 뜨고 **누가 무엇을 기다리는지, 어디서 멎었는지**
알 수 없어 관리자가 손 쓸 수단이 없었다. 필요한 사실은 이미 서버 안에 흩어져 있고,
여기서 한 행으로 합치는 것이 전부다(새로 계산하는 값은 없다):

  build_status.snapshot_all()  진행 중 (세션, stage, 경과, **trigger**)
  report_db.get_session()      제품/LOT/파일명/업로더 — "누가 만든 세션인가"
  metrics.active_users()       그 세션을 최근 요청한 사용자 — "지금 누가 기다리나"
  compute.worker_states()      워커 sidecar: 지금 어느 단계·어느 source 에서 몇 초째
  eta.session_eta()            입력 규모 기반 예상초 — 경과가 정상인지 판정하는 기준

현황 탭이 10초마다 부르므로 비용에 주의한다: DB 왕복은 **진행 중 빌드 건수만큼**
(보통 0~3건, 0건이면 아무것도 안 함)이고 그마저 짧게 캐시한다. sidecar 는 워커 수만큼의
작은 JSON 읽기다.

## 개입 (POST)

개별 빌드만 취소하는 것은 구조적으로 불가능하다 — ProcessPoolExecutor 는 실행 중 잡을
cancel 할 수 없고, 워커 1개만 죽여도 풀 전체가 BrokenProcessPool 이 된다
(web_report/compute.py run()). 그래서 여기 액션은 **막힌 것을 푸는 쪽**만 다룬다:

  clear_failure  연속 실패 차단(기본 10분 쿨다운) 즉시 해제
  clear_stuck    등록 잔재 정리 — 워커 타임아웃을 넘긴 건만(그 미만은 정상 진행이라 거부)
  rebuild        온디맨드 큐에 빌드 요청 투입

전부 관측 상태만 건드리고 캐시·편집·산출물은 손대지 않는다.
"""
import logging
import threading
import time

from admin_panel import metrics
from config import REPORT_UPLOAD_DIR
from database import report_db

_log = logging.getLogger(__name__)

# 세션 메타 + eta 캐시 — 현황 탭 10초 폴링 × 진행 중 빌드 수만큼의 DB 왕복을 줄인다.
# 빌드가 도는 동안 제품/LOT/업로더가 바뀔 일은 없으므로 짧은 TTL 로 충분하다.
_META_TTL_SEC = 60.0
_meta_cache: dict = {}
_meta_lock = threading.Lock()

# trigger(build_status) → 사람이 읽는 유발 원인. 값의 정본은 compute 의 큐 컨텍스트
# (build_log.context(trigger=..., kind=...)) 이다.
_CAUSE = {
    "ondemand:report": "사용자가 세션을 여는 중 — 리포트 캐시 미스(202 대기)",
    "ondemand:map": "Map 탭 / Issue Table Map 컬럼 첫 진입",
    "ondemand:ai": "AI Comment 백그라운드 평가 — 사용자 화면은 이미 열려 있음",
    "prewarm": "업로드 직후 프리웜 — 기다리는 사용자 없음",
    "distpack": "Distribution pack 생성 — 기다리는 사용자 없음",
    "": "사용자 요청 스레드가 직접 빌드 중 (큐 경유 아님)",
}


def _timeout_sec() -> float:
    try:
        from web_report import compute
        return float(compute.status().get("timeout_sec") or 300.0)
    except Exception:
        return 300.0


def _session_meta(session_id: str) -> dict:
    """세션 메타 + 예상 소요 (TTL 캐시). 실패하면 빈 dict — 표시만 줄어든다."""
    now = time.time()
    with _meta_lock:
        hit = _meta_cache.get(session_id)
        if hit and now - hit[0] < _META_TTL_SEC:
            return hit[1]
    meta = {}
    try:
        s = report_db.get_session(session_id) or {}
        created = s.get("created_at")
        meta = {
            "file_name": s.get("file_name") or "",
            "product": s.get("product") or "",
            "product_type": s.get("product_type") or "",
            "lot_id": s.get("lot_id") or "",
            "uploader": s.get("uploaded_by") or "",
            "created_at": created,
            "akey": str(s.get("analysis_key") or "")[:12],
        }
        if meta["uploader"]:
            # 화면 표기는 전 관리자 화면이 '이름(ID)' 로 통일돼 있다 (users_admin.attach_names
            # 와 같은 규칙). 여기는 세션 1건이라 캐시에 실어 두고 배치 조회는 생략한다.
            key = meta["uploader"].split("\\")[-1].lower()
            meta["uploader_name"] = report_db.display_names([key]).get(key, "")
        try:
            from pathlib import Path
            from web_report import eta as web_report_eta
            meta["eta"] = web_report_eta.session_eta(s, Path(REPORT_UPLOAD_DIR))
        except Exception:
            meta["eta"] = None
    except Exception:
        _log.debug("build meta 조회 실패 session=%s", session_id, exc_info=True)
    with _meta_lock:
        _meta_cache[session_id] = (now, meta)
        if len(_meta_cache) > 200:      # 상한 — 오래된 것부터 버린다
            for k in sorted(_meta_cache, key=lambda k: _meta_cache[k][0])[:100]:
                _meta_cache.pop(k, None)
    return meta


def _waiting_by_session(window_sec=300) -> dict:
    """세션별 '최근 그 세션을 요청한 사용자' 목록 — 누가 기다리는지의 근거."""
    out: dict = {}
    try:
        rows = (metrics.active_users(window_sec) or {}).get("users") or []
    except Exception:
        return out
    for r in rows:
        sid = r.get("session_id")
        if not sid:
            continue
        out.setdefault(sid, []).append({
            "user": r.get("user") or r.get("key") or "",
            "ago": r.get("ago"),
        })
    # 표시용 실명 — 배치 1회 (행마다 조회하면 N+1)
    try:
        flat = [w for lst in out.values() for w in lst]
        names = report_db.display_names(
            [str(w["user"]).split("\\")[-1].lower() for w in flat])
        for w in flat:
            w["name"] = names.get(str(w["user"]).split("\\")[-1].lower(), "")
    except Exception:
        pass
    return out


def active_builds() -> list[dict]:
    """진행 중 콜드 빌드 — 세션 메타·대기자·워커 현재 단계·예상 대비 초과를 붙인 행 목록.

    빌드가 0건이면 DB·파일 접근을 전혀 하지 않는다(현황 탭 상시 폴링 비용 0).
    """
    try:
        from web_report import build_status, compute
    except Exception:
        return []
    rows = build_status.snapshot_all()
    if not rows:
        return []
    try:
        states = {st.get("session"): st for st in compute.worker_states()}
    except Exception:
        states = {}
    waiting = _waiting_by_session()
    timeout = _timeout_sec()
    now = time.time()
    out = []
    for b in rows:
        sid = b.get("session_id") or ""
        trigger = b.get("trigger") or ""
        meta = _session_meta(sid)
        elapsed = float(b.get("elapsed") or 0)
        eta = meta.get("eta")
        st = states.get(sid)
        row = dict(b)
        row.update(meta)
        row["kind"] = trigger.split(":")[1] if ":" in trigger else (trigger or "inline")
        row["cause"] = _CAUSE.get(trigger, f"{trigger} 경로")
        row["waiting"] = waiting.get(sid, [])
        row["over"] = round(elapsed / eta, 1) if eta else None
        # 워커가 지금 어느 단계에서 무엇을 하고 있나 (sidecar — 실패 때만 읽던 정보)
        row["worker"] = {
            "pid": st.get("pid"), "stage": st.get("stage") or "",
            "source": st.get("source") or "",
            "stage_elapsed": st.get("stage_elapsed"),
            "elapsed": st.get("elapsed"),
            "stages_done": st.get("stages_done") or {},
            "build_id": st.get("build_id") or "",
        } if st else None
        # 워커 타임아웃을 넘겼는데 체크포인트도 없다 = 정상 진행으로 보기 어렵다.
        row["stuck"] = elapsed > timeout and st is None
        if meta.get("created_at"):
            row["session_age_sec"] = round(max(0.0, now - float(meta["created_at"])), 0)
        out.append(row)
    return out


def queues() -> dict:
    """큐 대기/실행 중 목록 + 차단된 세션 — "왜 시작조차 안 되나"의 근거."""
    out = {"ondemand_queued": [], "ondemand_running": [], "prewarm_queued": [],
           "failures": []}
    try:
        from web_report import compute
        out.update(compute.pending_items())
    except Exception:
        pass
    try:
        from web_report import build_status
        out["failures"] = build_status.failures()
    except Exception:
        pass
    return out


# ── 개입 ────────────────────────────────────────────────────────────────────

def act(action: str, session_id: str, kind: str = "report") -> dict:
    """관리자 개입 1건. 반환 {"ok", "message"} — 실패해도 예외를 던지지 않는다."""
    if not session_id:
        return {"ok": False, "message": "session_id 가 필요합니다"}
    try:
        from web_report import build_status, compute
    except Exception:
        return {"ok": False, "message": "web_report 모듈을 불러올 수 없습니다"}

    if action == "clear_failure":
        build_status.clear_failure(session_id, kind)
        return {"ok": True, "message": f"{kind} 실패 차단 해제 — 다음 조회부터 재시도됩니다"}

    if action == "clear_stuck":
        # 정상 진행 중인 빌드를 지우면 프런트가 "끝났다"고 오판하므로, 워커 타임아웃을
        # 넘긴 건만 허용한다(그 이상 걸리는 빌드는 어차피 compute.run 이 끊는다).
        timeout = _timeout_sec()
        cur = [b for b in build_status.snapshot_all() if b.get("session_id") == session_id]
        live = [b for b in cur if float(b.get("elapsed") or 0) <= timeout]
        if live:
            return {"ok": False,
                    "message": f"아직 정상 범위입니다 (경과 {live[0]['elapsed']}s ≤ "
                               f"타임아웃 {timeout:.0f}s) — 기다리거나 타임아웃 후 다시 시도"}
        for b in cur:
            build_status.end(session_id, b.get("stage") or "report")
        dropped = compute.drop_pending(session_id, kind)
        return {"ok": True,
                "message": f"등록 잔재 정리 — 진행 표시 {len(cur)}건"
                           f"{', 큐 등록 해제' if dropped else ''}"}

    if action == "rebuild":
        build_status.clear_failure(session_id, kind)
        queued = compute.request_build(session_id, str(REPORT_UPLOAD_DIR), kind)
        return {"ok": True,
                "message": f"{kind} 빌드 큐 투입" if queued
                           else f"이미 {kind} 빌드가 대기/진행 중입니다 (중복 등록 안 함)"}

    return {"ok": False, "message": f"알 수 없는 action: {action}"}

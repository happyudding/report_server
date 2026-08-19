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
from identity_norm import normalize_uid

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
            # 표기용 ID 는 정규화한다 — DB 원문('SECDS\\HGD123')을 그대로 그리면
            # 다른 관리자 표의 같은 사람과 다른 이름으로 보인다 (identity_norm).
            "uploader": normalize_uid(s.get("uploaded_by")),
            "created_at": created,
            "akey": str(s.get("analysis_key") or "")[:12],
        }
        if meta["uploader"]:
            # 화면 표기는 전 관리자 화면이 '이름(ID)' 로 통일돼 있다 (users_admin.attach_names
            # 와 같은 규칙). 여기는 세션 1건이라 캐시에 실어 두고 배치 조회는 생략한다.
            key = meta["uploader"]
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


# ── 콜드 폭풍 판정 ──────────────────────────────────────────────────────────
# 순간 포화는 정상이므로 이만큼 지속돼야 '폭풍'으로 부른다.
_STORM_MIN_SEC = 60.0
_storm_since = None      # 포화가 시작된 시각 (풀리면 None)
_storm_lock = threading.Lock()


def _uptime_sec() -> int:
    """서버 프로세스 기동 후 경과초. 실패하면 0 (표시만 줄어든다).

    sysinfo.snapshot() 에도 같은 값이 있지만 그쪽은 disk_usage 까지 재는 무거운 함수라,
    10초 폴링에 얹히는 여기서는 필요한 한 줄만 따로 잰다."""
    try:
        import psutil
        return int(time.time() - psutil.Process().create_time())
    except Exception:
        return 0


def storm_status() -> dict:
    """지금 **콜드 폭풍** 중인가 — 관리자 화면 배지의 판정 (2026-08-19).

    콜드 폭풍 = 캐시가 통째로 무효화돼(대개 `REPORT_SCHEMA_VERSION` bump 후 재기동)
    전 세션이 한꺼번에 재빌드되는 상태다. 이때는 워커가 코어를 채워 조회도 업로드도 함께
    느려지는데, **지금까지 "지금이 그 상태"라는 표시가 화면 어디에도 없었다.** 그래서
    2026-08-19 업로드 지연 신고 때 코드 변경부터 의심하며 시간을 썼다 — 원인(스키마 bump
    직후)이 아무 데도 안 보였기 때문이다.

    판정: **풀 포화(실행 중 ≥ 워커 수) + 대기 큐 있음** 이 `_STORM_MIN_SEC` 이상 지속.
    새로 재는 값은 없고 기존 스냅샷 2개를 합칠 뿐이라 상시 폴링에 얹어도 비용이 없다.

    함께 돌려주는 `schema_version`·`uptime_sec` 이 핵심이다 — "스키마 v41, 기동 후 8분"이
    나란히 보이면 폭풍의 **원인까지** 화면에서 바로 읽힌다.
    """
    global _storm_since
    try:
        from web_report import build_status, cache_policy, compute
        st = compute.status()
        running = len(build_status.snapshot_all())
        items = compute.pending_items()
        queued = (len(items.get("ondemand_queued") or [])
                  + len(items.get("prewarm_queued") or []))
    except Exception:
        return {"storm": False}
    workers = int(st.get("workers") or 0)
    saturated = bool(workers > 0 and running >= workers and queued > 0)
    now = time.time()
    with _storm_lock:
        if saturated:
            if _storm_since is None:
                _storm_since = now
            since = _storm_since
        else:
            _storm_since = None
            since = None
    duration = round(now - since) if since else 0
    stats = st.get("stats") or {}
    return {
        "storm": bool(since and duration >= _STORM_MIN_SEC),
        "saturated": saturated,
        "duration_sec": duration,
        "running": running,
        "queued": queued,
        "workers": workers,
        "schema_version": getattr(cache_policy, "REPORT_SCHEMA_VERSION", None),
        "uptime_sec": _uptime_sec(),
        # 기동 후 누적 콜드 빌드 — 폭풍이 걷혀 가는지(더는 안 늘어나는지) 보는 눈금.
        "builds_done": int(stats.get("prewarm_done") or 0)
                       + int(stats.get("ondemand_done") or 0),
    }


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

    if action == "kill_wait":
        # 방치된 탭이 콜드 빌드를 폴링하며 서버를 계속 먹을 때 **사용자 대기를 끊는다**.
        # ⚠️ 워커에서 이미 도는 계산 자체는 못 끊는다(ProcessPool 은 실행 중 잡을 cancel
        # 할 수 없고, 풀을 버리면 무고한 동시 빌드까지 전멸한다) — 그 계산은 워커
        # 타임아웃까지 돌다 스스로 끝난다. 여기서 하는 일은 재등록을 막고(pending 해제),
        # 진행 표시를 지우고, 실패를 FAIL_LIMIT 만큼 세워 /full 이 202 대신 즉시 503 을
        # 주게 하는 것이다. 쿨다운이 지나면 자동으로 다시 열린다(clear_failure 로 즉시 해제).
        for b in [x for x in build_status.snapshot_all() if x.get("session_id") == session_id]:
            build_status.end(session_id, b.get("stage") or "report")
        compute.drop_pending(session_id, kind)
        for _ in range(build_status.FAIL_LIMIT):
            build_status.mark_failure(session_id, kind, "관리자 중단(kill_wait)")
        return {"ok": True,
                "message": f"{kind} 대기 중단 — 이 세션 조회는 "
                           f"{build_status.FAIL_COOLDOWN_SEC / 60:.0f}분간 즉시 실패 안내로 "
                           f"응답합니다(진행 중 계산은 워커 타임아웃까지 계속됨). "
                           f"바로 풀려면 '실패 차단 해제'를 누르세요"}

    if action == "rebuild":
        build_status.clear_failure(session_id, kind)
        queued = compute.request_build(session_id, str(REPORT_UPLOAD_DIR), kind)
        return {"ok": True,
                "message": f"{kind} 빌드 큐 투입" if queued
                           else f"이미 {kind} 빌드가 대기/진행 중입니다 (중복 등록 안 함)"}

    return {"ok": False, "message": f"알 수 없는 action: {action}"}

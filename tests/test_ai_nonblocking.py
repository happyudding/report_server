# -*- coding: utf-8 -*-
"""AI Comment 가 사용자 첫 조회를 막지 않는다 — 2026-09-02 회귀(첫 조회 100초) 방지.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_ai_nonblocking.py

**무엇이 깨졌었나**: 'ai' 백그라운드 잡이 `report_job(ai_inline=True)` 하나였다. 온디맨드
소비자 스레드가 부모에서 `load_webreport` 를 부르고 그 안의 `keyed_lock(("report",)+key)`
를 **엔진 평가가 끝날 때까지(실측 100초+) 쥔 채** 워커를 기다린다. 사용자의 1초짜리
pending 빌드가 잡아야 하는 락이 바로 그것이라, "AI Comment 를 켜면 첫 조회가 100초,
뒤로가기 후 재진입은 즉시" 가 됐다. 프리웜도 ai_inline=True 라 그동안 디스크에 즉시 열
수 있는 산출물(pending 본)이 하나도 없었다.

검증 항목:
  (a) 'ai' 잡이 도는 **동안** 사용자 조회가 즉시 pending payload 로 열린다 (락 비보유)
  (b) 프리웜은 pending 본을 먼저 만들고 AI 는 pending_kinds 로 넘긴다 (엔진 미호출)
  (c) AI 계열 잡 동시 실행 상한 — 넘치면 큐 뒤로, 'report' 는 그 사이에도 즉시 집힌다
  (d) 'ai' 잡이 끝나면 최종본에 comments 가 들어간다 (2단계가 결과를 잃지 않는다)

엔진은 느린 스텁으로 대체한다 — 이 테스트가 재는 것은 "엔진이 도는 동안 다른 요청이
막히는가" 이지 엔진 자체가 아니다. pytest 미사용(tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="ai_nonblock_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""              # S3 비활성 → 로컬 폴백
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 인라인 — 스텁 몽키패치가 통해야 한다
# 진단 사건이 운영 로그를 오염시키지 않게 격리(tests 관례).
os.environ["REPORT_DIAG_DIR"] = str(_TMP / "diag")

import pandas as pd  # noqa: E402

import storage_gateway  # noqa: E402
from database import report_db  # noqa: E402
from web_report import ai_comment as wr_ai_comment  # noqa: E402
from web_report import cache as wr_cache  # noqa: E402
from web_report import compute as wr_compute  # noqa: E402
from web_report import service as wr_service  # noqa: E402
from web_report.honeyform import META_COLUMNS, encode_honeyform_parquet  # noqa: E402
from web_report.validation import canon  # noqa: E402

report_db.init_report_db()

USER = "tester"
UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])
ITEM = "ItemA"
AI_OPTS = json.dumps({"ai_comment": True, "ai_comment_optin": True})

# 엔진 스텁이 도는 시간 — 실제 100초를 재현할 필요는 없고, "그동안 막히는가" 만 본다.
EVAL_SEC = 2.0
_eval_calls = []
_eval_started = threading.Event()


def _slow_stub(tables, session, selected_items=None, fail_only=None,
               generate_comment=True):
    """느린 엔진 — 호출 사실을 기록하고 EVAL_SEC 동안 잡고 있는다."""
    _eval_calls.append(generate_comment)
    _eval_started.set()
    time.sleep(EVAL_SEC)
    cell = "[MAJOR] [현상] 스텁\n[과거사례] 없음\n [제안] 조치"
    result = {"comments": {f"Yield|5|{ITEM}": cell, f"CPK|{ITEM}": cell,
                           f"ETC|{ITEM}": cell},
              "etc_auto_items": [], "row_signatures": {}, "signature_options": [],
              "prompts": {}}
    if not generate_comment:
        result = dict(result, comments={})
    return result, True


wr_ai_comment.safe_build_ex = _slow_stub


def _make_parquet():
    cols = META_COLUMNS + [ITEM]
    rows = [["TSEQ", "", "", "", "", "", "", 1],
            ["TNO", "", "", "", "", "", "", 100],
            ["STEP", "", "", "", "", "", "", "P1"],
            ["UNIT", "", "", "", "", "", "", "V"],
            ["HILIM", "", "", "", "", "", "", 12],
            ["LOLIM", "", "", "", "", "", "", 8]]
    for i in range(20):
        a, bin_code, failtno = 10 + (i % 5) * 0.1, 1, ""
        if i == 18:
            a, bin_code, failtno = 11.9, 5, 100
        rows.append([f"s{i}", 1, 1, i % 5, i // 5, bin_code, failtno, a])
    return encode_honeyform_parquet(pd.DataFrame(rows, columns=cols))


def _setup(sid, akey, opts=AI_OPTS):
    blob = _make_parquet()
    chash = hashlib.sha256(canon({"files": [hashlib.sha256(blob).hexdigest()]})).hexdigest()
    report_db.create_session(sid, "x.parquet", None, product_type="MDDI", lot_id="LOT1",
                             product="P1", source="web_report", uploaded_by=USER)
    report_db.update_session(sid, analysis_key=akey, content_hash=chash, status="done",
                             webreport_options=opts)
    storage_gateway.save_webreport_sources(
        akey, chash, [blob],
        {"sources": [{"name": "Lot1", "file_name": "lot1.csv"}],
         "selected_items": [], "mode": "Normal"},
        upload_root=UPLOAD_ROOT)
    return chash


def _reset_caches():
    wr_cache.invalidate_all() if hasattr(wr_cache, "invalidate_all") else None
    for name in ("REPORT_CACHE", "AI_COMMENT_CACHE", "COMPARE_CACHE", "TABLES_CACHE"):
        cache = getattr(wr_cache, name, None)
        if cache is not None:
            with wr_cache.CACHE_LOCK:
                cache.clear()


def _load(sid, ai_inline=False):
    return wr_service.load_webreport(sid, report_db=report_db,
                                     upload_root=UPLOAD_ROOT, ai_inline=ai_inline)


# ── (a) 'ai' 잡이 도는 동안 사용자 조회가 막히지 않는다 ──────────────────────

def test_user_read_not_blocked_by_ai_job():
    """핵심 회귀 — AI 평가 중에도 pending payload 로 즉시 열려야 한다."""
    sid, akey = "NB01", "a" * 12
    _setup(sid, akey)
    _reset_caches()
    _eval_calls.clear()
    _eval_started.clear()

    # 'ai' 잡 = 2단계(캐시 채우기 → 짧은 재빌드). 백그라운드 스레드로 돌린다.
    done = threading.Event()

    def _ai_job():
        try:
            wr_compute._ONDEMAND_JOBS["ai"](sid, str(UPLOAD_ROOT))
        finally:
            done.set()

    th = threading.Thread(target=_ai_job, daemon=True)
    th.start()
    assert _eval_started.wait(10), "엔진 스텁이 시작되지 않았다"

    # 엔진이 도는 동안 사용자 조회 — 락에 걸리면 EVAL_SEC 만큼 기다리게 된다.
    t0 = time.time()
    _, report = _load(sid)
    elapsed = time.time() - t0

    assert elapsed < EVAL_SEC * 0.5, (
        f"사용자 조회가 AI 평가에 막혔다 ({elapsed:.2f}s) — 'ai' 잡이 report 락을 "
        f"쥐고 있다(2026-09-02 회귀)")
    assert report.get("ai_comment_pending") is True, "pending payload 가 아니다"
    assert done.wait(30), "'ai' 잡이 끝나지 않았다"
    print(f"  (a) AI 평가 중 사용자 조회 {elapsed:.3f}s (엔진 {EVAL_SEC}s) OK")


# ── (b) 프리웜은 pending 먼저, AI 는 잡으로 ─────────────────────────────────

def test_prewarm_makes_pending_and_defers_ai():
    """프리웜이 엔진을 부르지 않고 pending 본 + pending_kinds 를 돌려준다."""
    sid, akey = "NB02", "b" * 12
    _setup(sid, akey)
    _reset_caches()
    _eval_calls.clear()

    out = wr_compute.prewarm_job(sid, str(UPLOAD_ROOT), False)

    assert _eval_calls == [], "프리웜이 엔진을 동기로 돌렸다(ai_inline=True 회귀)"
    assert out.get("pending_kinds") == ("ai",), out.get("pending_kinds")
    # pending 본이 디스크에 남아 다음 조회가 콜드가 아니어야 한다.
    session = report_db.get_session(sid)
    assert not wr_service.report_is_cold(sid, report_db=report_db,
                                         upload_root=UPLOAD_ROOT, session=session), \
        "프리웜 후에도 콜드 — pending 본이 디스크에 없다"
    print("  (b) 프리웜 pending 우선 + pending_kinds=('ai',) OK")


# ── (c) AI 계열 잡 동시 실행 상한 ───────────────────────────────────────────

def test_ai_job_limit_defers_but_keeps_report_first():
    """상한을 넘은 AI 잡은 큐 뒤로 가고, 그 사이 'report' 는 먼저 집힌다."""
    order = []
    orig_jobs = dict(wr_compute._ONDEMAND_JOBS)
    orig_limit = wr_compute._AI_JOB_LIMIT
    gate = threading.Event()
    try:
        wr_compute._AI_JOB_LIMIT = 1

        def _slow_ai(sid, root):
            order.append(f"ai:{sid}")
            gate.wait(10)          # 첫 AI 잡을 붙잡아 상한을 채운다

        def _fast_report(sid, root):
            order.append(f"report:{sid}")

        wr_compute._ONDEMAND_JOBS["ai"] = _slow_ai
        wr_compute._ONDEMAND_JOBS["report"] = _fast_report

        wr_compute.request_build("LIM1", str(UPLOAD_ROOT), "ai")
        time.sleep(0.4)            # 첫 AI 잡이 슬롯을 잡을 시간
        wr_compute.request_build("LIM2", str(UPLOAD_ROOT), "ai")   # 상한 초과 → 대기
        wr_compute.request_build("LIM3", str(UPLOAD_ROOT), "report")
        time.sleep(1.2)

        assert "report:LIM3" in order, f"report 가 AI 대기에 막혔다: {order}"
        assert "ai:LIM2" not in order, f"상한을 넘은 AI 잡이 실행됐다: {order}"
        assert wr_compute.STATS.get("ai_deferred", 0) > 0, "ai_deferred 카운터 미증가"
        gate.set()
        time.sleep(1.0)
        assert "ai:LIM2" in order, f"대기하던 AI 잡이 끝내 실행되지 않았다: {order}"
    finally:
        gate.set()
        wr_compute._ONDEMAND_JOBS.clear()
        wr_compute._ONDEMAND_JOBS.update(orig_jobs)
        wr_compute._AI_JOB_LIMIT = orig_limit
        time.sleep(0.3)
    print("  (c) AI 잡 상한: report 우선 통과, 초과분은 대기 후 실행 OK")


# ── (d) 2단계 잡이 결과를 잃지 않는다 ───────────────────────────────────────

def test_ai_job_two_stage_produces_final_payload():
    """캐시 채우기 → 재빌드 후 최종본에 comments 가 들어간다."""
    sid, akey = "NB04", "d" * 12
    _setup(sid, akey)
    _reset_caches()
    _eval_calls.clear()

    wr_compute._ONDEMAND_JOBS["ai"](sid, str(UPLOAD_ROOT))

    # 1단계가 엔진을 1회만 부르고(2단계는 캐시 히트), 최종본에 코멘트가 있다.
    assert _eval_calls.count(True) == 1, (
        f"엔진이 {_eval_calls.count(True)}회 — 2단계가 캐시를 못 쓰고 재평가했다")
    _, report = _load(sid)
    assert not report.get("ai_comment_pending"), "최종본인데 pending 플래그가 남아 있다"
    sheet = report.get("sheets", {}).get("Issue Table") or []
    cells = [r.get("AI Comment") for r in sheet if r.get("AI Comment")]
    assert cells, "최종 payload 에 AI Comment 셀이 없다"
    print("  (d) 2단계 잡 최종본 comments 반영 (엔진 1회) OK")


def main():
    print("AI Comment 논블로킹 검증")
    try:
        test_user_read_not_blocked_by_ai_job()
        test_prewarm_makes_pending_and_defers_ai()
        test_ai_job_limit_defers_but_keeps_report_first()
        test_ai_job_two_stage_produces_final_payload()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

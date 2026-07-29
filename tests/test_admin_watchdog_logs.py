"""admin 패널의 watchdog 원인 추적 기능 검증 — 집계 + 로그 파일 브라우저.

배경: watchdog 재기동이 폭주해도(2026-07 관측 142회/일) 대시보드는 '재기동 N회' 만
보여줘 원인(healthz_503=DB 체크 실패 / healthz_timeout=지연 / not_listening=프로세스
사망)을 알 수 없었고, console log 탭은 최신 server_*.txt 1개만 tail 해서 부검 스냅샷도
못 봤다. 이 테스트가 다음을 고정한다:
  (a) sysinfo.watchdog_status  — reason 분포 / 백오프 억제 집계
  (b) sysinfo.watchdog_checks  — 매 점검 결과 집계 + healthz 응답시간 추이
  (c) maintenance.log_list/log_tail — 화이트리스트 열람 + 경로 조작 차단

실행:
    python tests/test_admin_watchdog_logs.py

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="wd_admin_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

import config  # noqa: E402
from admin_panel import maintenance, sysinfo  # noqa: E402

config.ROOT_DIR = _TMP          # server/log 를 임시 트리로 (운영 로그 무오염)
LOG_DIR = _TMP / "server" / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _ts(minutes_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.localtime(time.time() - minutes_ago * 60))


def _write_lines(path, records):
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_watchdog_status():
    _write_lines(LOG_DIR / "watchdog_events.log", [
        {"ts": _ts(60 * 30), "event": "restart", "reason": "healthz_503"},   # 24h 밖
        {"ts": _ts(600), "event": "restart", "reason": "healthz_503"},
        {"ts": _ts(400), "event": "restart", "reason": "healthz_503"},
        {"ts": _ts(300), "event": "restart_fail", "reason": "not_listening"},
        {"ts": _ts(200), "event": "healthz_fail", "reason": "healthz_timeout"},
        {"ts": _ts(150), "event": "healthz_fail", "reason": "healthz_connect"},
        {"ts": _ts(30), "event": "backoff_skip", "reason": "healthz_503",
         "detail": "재기동 억제: 최근1h 3회"},
    ])
    st = sysinfo.watchdog_status()
    assert st["restarts_24h"] == 3, st["restarts_24h"]        # 24h 밖 1건 제외
    assert st["restarts_total"] == 4, st["restarts_total"]     # 전체는 4건
    assert st["reasons_24h"] == {"healthz_503": 2, "not_listening": 1}, st["reasons_24h"]
    # 재기동 reason(위)과 실패 감지 reason(아래)은 별개 집계다 — 세분 원인
    # (healthz_timeout/healthz_connect)은 재기동 이벤트에 안 붙으므로 여기서만 보인다.
    assert st["fail_reasons_24h"] == {"healthz_timeout": 1, "healthz_connect": 1,
                                      "healthz_503": 1}, st["fail_reasons_24h"]
    assert st["backoff_skips_24h"] == 1, st["backoff_skips_24h"]
    assert st["last_backoff"]["reason"] == "healthz_503"
    assert st["events"][0]["event"] == "backoff_skip", "최신 먼저 정렬이 아님"
    print("[a] watchdog_status reason 분포·백오프 집계 OK")


def test_watchdog_checks():
    recs = [{"ts": _ts(60 * 30), "result": "ok", "listen": 1, "code": 200, "ms": 12}]  # 24h 밖
    for i in range(5):
        recs.append({"ts": _ts(100 - i * 10), "result": "ok", "listen": 1,
                     "code": 200, "ms": 10 + i})
    # 신형 레코드(wstat/err/reason 포함) — 구형 레코드와 섞여도 파싱돼야 한다
    recs.append({"ts": _ts(40), "result": "healthz_fail", "reason": "healthz_timeout",
                 "listen": 1, "code": 0, "ms": 30012, "wstat": "Timeout",
                 "err": "작업 시간을 초과했습니다.", "fails": 1})
    recs.append({"ts": _ts(35), "result": "backoff_skip", "listen": 1, "code": 503,
                 "ms": 24, "fails": 2, "detail": "재기동 억제"})
    recs.append({"ts": _ts(30), "result": "mutex_busy"})
    _write_lines(LOG_DIR / "watchdog_checks.log", recs)

    ck = sysinfo.watchdog_checks(hours=24)
    assert ck["total"] == 8, ck["total"]                      # 24h 밖 1건 제외
    assert ck["counts"] == {"ok": 5, "healthz_fail": 1,
                            "backoff_skip": 1, "mutex_busy": 1}, ck["counts"]
    # mutex_busy 는 ms 가 없어 healthz 추이에서 빠진다
    assert len(ck["hz_series"]["ms"]) == 7, ck["hz_series"]
    assert ck["hz_series"]["ts"] == sorted(ck["hz_series"]["ts"]), "시계열 정렬 아님"
    assert ck["recent"][0]["result"] == "mutex_busy", "최신 먼저 정렬이 아님"
    assert ck["coverage_from"] is not None
    # 신형 진단 필드가 손실 없이 프런트까지 통과하는가 (admin 상세 '사유'/'오류' 열의 재료)
    hz = next(r for r in ck["recent"] if r["result"] == "healthz_fail")
    assert hz["wstat"] == "Timeout" and hz["reason"] == "healthz_timeout", hz
    assert "시간" in hz["err"], hz
    # 구형 레코드(wstat/err 없음)도 그대로 통과 — 프런트가 "-" 로 처리한다
    assert "wstat" not in ck["recent"][0], ck["recent"][0]

    # 다운샘플: max_points 를 넘으면 줄어들되 추이는 남는다
    small = sysinfo.watchdog_checks(hours=24, max_points=3)
    assert len(small["hz_series"]["ms"]) <= 3, small["hz_series"]
    print("[b] watchdog_checks 집계·healthz 추이·다운샘플 OK")


def test_log_browser():
    (LOG_DIR / "server_20260723_120000.txt").write_text("line1\nline2\n", encoding="utf-8")
    (LOG_DIR / "watchdog_snap_20260723_130000.txt").write_text(
        "﻿reason : healthz_fail_x2\nautopsy: procs=0\n", encoding="utf-8")
    (LOG_DIR / "metrics_20260723.log").write_text("2026-07-23T13:00:00,1.5,100,200,0,1\n",
                                                  encoding="utf-8")
    (LOG_DIR / "secret.txt").write_text("do not expose", encoding="utf-8")

    names = {f["name"] for f in maintenance.log_list()}
    assert "server_20260723_120000.txt" in names
    assert "watchdog_snap_20260723_130000.txt" in names
    assert "metrics_20260723.log" in names
    assert "watchdog_events.log" in names
    assert "secret.txt" not in names, "화이트리스트 밖 파일이 노출됨"

    # 이름 지정 tail — 부검 스냅샷 열람 (선두 BOM 제거)
    out = maintenance.log_tail(65536, name="watchdog_snap_20260723_130000.txt")
    assert out["file"] == "watchdog_snap_20260723_130000.txt"
    assert out["text"].startswith("reason : healthz_fail_x2"), repr(out["text"][:40])

    # 이름 생략 = 기존 동작 (최신 server_*.txt)
    out = maintenance.log_tail(65536)
    assert out["file"] == "server_20260723_120000.txt", out["file"]

    # 경로 조작·화이트리스트 밖 거부 (빈 값은 라우트에서 None 으로 정규화 = 미지정)
    for bad in ("../wsgi.py", "..\\wsgi.py", "/etc/passwd",
                str(LOG_DIR / "server_20260723_120000.txt"), "secret.txt",
                "server_x.txt/../../wsgi.py"):
        try:
            maintenance.log_tail(1024, name=bad)
        except ValueError:
            continue
        raise AssertionError("거부되지 않음: %r" % bad)

    # 없는 파일이지만 형식은 맞는 경우 — 예외 대신 안내 텍스트
    out = maintenance.log_tail(1024, name="server_nonexistent.txt")
    assert out["file"] is None and "파일 없음" in out["text"]
    print("[c] 로그 브라우저 화이트리스트·경로조작 차단 OK")


def main():
    try:
        test_watchdog_status()
        test_watchdog_checks()
        test_log_browser()
        print("\nALL OK")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

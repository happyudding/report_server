"""안 끝나는 요청(hang) 감지 자체 검증 — self-run.

실행:  server\\.venv\\Scripts\\python.exe tests\\test_stuck_request.py

왜 이 테스트가 있나: 느린 요청 계측(`_emit_slow_event`)은 teardown, 즉 **요청이 끝난 뒤**
에만 돈다. 그래서 영원히 안 끝나는 요청은 서버에 한 줄도 남기지 못했다 — 2026-08-19
업로드 hang 때 "클라는 300초에 끊겼는데 서버 로그·진단 사건 모두 무기록" 이었던 이유다.
여기서 검증하는 것은 그 공백이 메워졌는지 하나다: **끝나기 전에** 사건과 스레드 덤프가
남는가.

운영 로그를 오염시키지 않도록 REPORT_DIAG_DIR 를 임시 폴더로 잡는다. env 는 metrics
모듈이 import 시점에 굳으므로 **import 보다 먼저** 설정해야 한다.
"""
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="stuckreq-"))

os.environ["REPORT_DIAG_DIR"] = str(_TMP)
os.environ["REPORT_STUCK_REQ_SEC"] = "2"          # 임계 (기본 120초)
os.environ["REPORT_METRICS_INTERVAL_SEC"] = "1"   # 샘플러 주기 (기본 10초)
os.environ["REPORT_METRICS_FILE_KEEP_DAYS"] = "0"  # flight recorder 파일은 만들지 않는다

sys.path.insert(0, str(_ROOT / "server"))
sys.path.insert(0, str(_ROOT))

from flask import Flask                      # noqa: E402
from admin_panel import metrics              # noqa: E402


def _diag_lines():
    """임시 폴더에 쌓인 진단 사건 JSON line 전부."""
    out = []
    for p in _TMP.glob("diagnostic_*.log"):
        out += [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return out


def _dumps():
    return sorted(_TMP.glob("diagnose_stuck_*.txt"))


def main():
    release = threading.Event()
    release2 = threading.Event()
    app = Flask(__name__)

    # 라우트는 **첫 요청 전에** 전부 등록해야 한다 (Flask 가 이후 추가를 막는다).
    @app.get("/hang")
    def hang():
        release.wait(30)      # 테스트가 풀어줄 때까지 안 끝나는 요청
        return "done"

    @app.get("/hang2")
    def hang2():
        release2.wait(30)
        return "done"

    @app.get("/quick")
    def quick():
        return "ok"

    metrics.init_app(app)
    client = app.test_client()

    # 정상 요청은 아무 사건도 남기지 않는다 (오탐 방지 — 이게 무너지면 로그가 쓰레기가 된다)
    assert client.get("/quick").status_code == 200
    metrics._check_stuck_requests()
    assert not _diag_lines(), f"정상 요청에 stuck 사건이 남았다: {_diag_lines()}"
    assert metrics.inflight_detail() == [], metrics.inflight_detail()
    print("(1) 정상 요청은 진행 중 목록·사건 모두 비어 있음 ok")

    # hang 요청을 별도 스레드로 띄운다
    done = []
    t = threading.Thread(target=lambda: done.append(client.get("/hang").status_code),
                         daemon=True)
    t.start()

    deadline = time.time() + 5
    while time.time() < deadline and not (metrics.inflight_detail() or []):
        time.sleep(0.05)
    rows = metrics.inflight_detail() or []
    assert len(rows) == 1, f"진행 중 요청이 잡히지 않았다: {rows}"
    assert rows[0]["route"] == "hang", rows[0]
    print(f"(2) 진행 중 요청이 route 와 함께 잡힘 ok ({rows[0]['route']})")

    # 임계(2초) 전에는 사건이 없어야 한다
    metrics._check_stuck_requests()
    assert not _diag_lines(), f"임계 전에 사건이 떴다: {_diag_lines()}"
    print("(3) 임계 전에는 사건 없음 ok")

    # 임계를 넘기면 — 요청이 **아직 안 끝났는데도** 사건 + 스레드 덤프가 남아야 한다
    time.sleep(2.2)
    metrics._check_stuck_requests()
    lines = _diag_lines()
    assert any("stuck_request" in ln for ln in lines), f"stuck_request 사건이 없다: {lines}"
    dumps = _dumps()
    assert len(dumps) == 1, f"스레드 덤프가 1개가 아니다: {dumps}"
    text = dumps[0].read_text(encoding="utf-8")
    assert "=== Thread" in text, "덤프에 스레드 스택이 없다"
    assert "hang" in text, "덤프에 문제의 요청(route=hang) 표기가 없다"
    assert not done, "요청이 끝난 뒤에야 감지됐다 — 그러면 기존 계측과 다를 게 없다"
    print(f"(4) 요청이 끝나기 전에 사건 + 덤프 남김 ok ({dumps[0].name})")

    # 같은 요청을 10초마다 다시 찍으면 디스크만 먹고 첫 증거를 밀어낸다
    metrics._check_stuck_requests()
    assert len(_dumps()) == 1, "같은 요청에 덤프가 중복 생성됐다"
    assert len([ln for ln in _diag_lines() if "stuck_request" in ln]) == 1, \
        "같은 요청에 사건이 중복 기록됐다"
    print("(5) 같은 요청 중복 기록 안 함 ok")

    # 요청이 끝나면 진행 중 목록에서 빠지고, 표식도 함께 정리된다
    release.set()
    t.join(timeout=10)
    assert done == [200], f"hang 요청이 끝나지 않았다: {done}"
    assert metrics.inflight_detail() == [], metrics.inflight_detail()
    assert not metrics._stuck_seen, f"stuck 표식이 남았다: {metrics._stuck_seen}"
    print("(6) 요청 종료 시 진행 중 목록·표식 정리 ok")

    # 샘플러 스레드도 같은 검사를 도는지 (배선 확인 — 주기 1초로 낮춰 뒀다)
    metrics._stuck_dumped = True     # 덤프는 기동당 1회라 이미 떴다. 사건만 본다
    before = len([ln for ln in _diag_lines() if "stuck_request" in ln])
    t2 = threading.Thread(target=lambda: client.get("/hang2"), daemon=True)
    t2.start()
    deadline = time.time() + 12
    while time.time() < deadline:
        if len([ln for ln in _diag_lines() if "stuck_request" in ln]) > before:
            break
        time.sleep(0.2)
    after = len([ln for ln in _diag_lines() if "stuck_request" in ln])
    release2.set()
    t2.join(timeout=10)
    assert after > before, "샘플러 스레드가 stuck 검사를 돌지 않았다 (_loop 배선 확인)"
    print("(7) 샘플러 스레드가 자동으로 감지 ok")

    print("\n전체 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

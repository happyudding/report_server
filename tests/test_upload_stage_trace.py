"""업로드 단계 계측(metrics.stage) 자체 검증 — self-run.

실행:  server\\.venv\\Scripts\\python.exe tests\\test_upload_stage_trace.py

왜 이 테스트가 있나: 진행 중 요청 감지(test_stuck_request.py)는 "무엇이 몇 초째"까지만
알려준다. 업로드는 동기 구간이 13단계라 그것만으로는 조치할 수 없다 — 2026-08-19
"업로드가 100%에서 멈춘다" 신고 때 서버가 멀쩡히 살아 있었는데도 멀티파트 수신인지
S3 저장인지 DB 쓰기인지 가릴 방법이 없었다. 여기서 검증하는 것은 그 공백이다:
**아직 안 끝난 요청의 현재 단계**가 화면·사건·로그에 실리는가.

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
_TMP = Path(tempfile.mkdtemp(prefix="upstage-"))

os.environ["REPORT_DIAG_DIR"] = str(_TMP)
# 업로드 임계는 짧게, 범용 임계는 길게 — 둘이 **따로** 적용되는지 보려는 것이다.
os.environ["REPORT_UPLOAD_SLOW_SEC"] = "2"
os.environ["REPORT_STUCK_REQ_SEC"] = "3600"
os.environ["REPORT_SLOW_REQ_MS"] = "100"           # 완료 후 slow 사건에 단계가 실리는지
os.environ["REPORT_METRICS_INTERVAL_SEC"] = "3600"  # 샘플러가 끼어들지 않게 (수동 호출로 검증)
os.environ["REPORT_METRICS_FILE_KEEP_DAYS"] = "0"

sys.path.insert(0, str(_ROOT / "server"))
sys.path.insert(0, str(_ROOT))

from flask import Blueprint, Flask              # noqa: E402
from admin_panel import metrics                 # noqa: E402


def _diag(kind=""):
    """임시 폴더에 쌓인 진단 사건 JSON line (kind 로 거를 수 있다)."""
    out = []
    for p in _TMP.glob("diagnostic_*.log"):
        out += [ln for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip() and (not kind or f'"event": "{kind}"' in ln)]
    return out


def _unit_checks():
    """요청 밖에서도 성립해야 하는 stage() 규약 — 누적/복원/예외 안전."""
    metrics._req_stages.pop(threading.get_ident(), None)

    # 같은 이름 반복 = 누적 (decode 는 파일마다 열린다)
    for i in range(3):
        with metrics.stage("decode", f"{i + 1}/3 lot.csv"):
            time.sleep(0.01)
    done = metrics.stages_done()
    assert done.get("decode", 0) >= 0.03, f"반복 호출이 누적되지 않았다: {done}"
    print(f"(1) 같은 단계 반복 호출이 누적됨 ok (decode={done['decode']}s)")

    # 중첩하면 안쪽이 끝날 때 바깥 단계로 되돌아온다
    with metrics.stage("outer"):
        with metrics.stage("inner"):
            pass
        st = metrics._req_stages[threading.get_ident()]
        assert st["cur"] == "outer", f"중첩 복원 실패: {st['cur']}"
    print("(2) 중첩 단계가 끝나면 바깥 단계로 복원 ok")

    # 단계 안에서 예외가 나도 기록은 남고 예외는 그대로 전파된다
    try:
        with metrics.stage("boom"):
            raise ValueError("x")
    except ValueError:
        pass
    else:
        raise AssertionError("예외가 삼켜졌다 — 계측이 오류를 감추면 안 된다")
    assert "boom" in metrics.stages_done(), "예외 경로에서 단계가 기록되지 않았다"
    print("(3) 단계 내 예외에도 기록 유지 + 예외 전파 ok")

    metrics._req_stages.pop(threading.get_ident(), None)


def main():
    _unit_checks()

    release = threading.Event()
    seen = {}
    app = Flask(__name__)
    # endpoint 가 정확히 report.upload_webreport 여야 업로드 임계가 적용된다
    # (metrics._UPLOAD_ROUTES). blueprint 이름까지 운영과 같게 맞춘다.
    bp = Blueprint("report", __name__)

    @bp.post("/upload_webreport")
    def upload_webreport():
        with metrics.stage("slot_wait"):
            pass
        with metrics.stage("multipart"):
            pass
        with metrics.stage("storage_save", "3/7 lot_c.parquet"):
            release.wait(30)         # 저장소가 응답하지 않는 상황을 흉내
        seen["done"] = metrics.stages_done()
        return "ok"

    @bp.get("/other")
    def other():
        with metrics.stage("slow_bit"):
            time.sleep(0.15)         # SLOW_REQ_MS(100ms) 초과 → slow_request 사건
        return "ok"

    app.register_blueprint(bp)
    metrics.init_app(app)
    client = app.test_client()

    # ── 완료된 느린 요청: slow_request 사건에 단계 분해가 실린다 ──────────────
    assert client.get("/other").status_code == 200
    slow = _diag("slow_request")
    assert slow, f"느린 요청 사건이 없다: {_diag()}"
    assert '"slow_bit"' in slow[0], f"slow_request 에 단계가 안 실렸다: {slow[0]}"
    assert "최장 slow_bit" in slow[0], f"message 에 최장 단계가 없다: {slow[0]}"
    print("(4) 완료된 느린 요청의 사건에 단계 분해 첨부 ok")
    assert metrics._req_stages == {}, f"단계 기록이 회수되지 않았다: {metrics._req_stages}"
    print("(5) 요청 종료 시 단계 기록 회수 ok")

    # ── 진행 중 업로드: 아직 안 끝났는데 현재 단계가 보인다 ──────────────────
    done = []
    t = threading.Thread(target=lambda: done.append(client.post("/upload_webreport").status_code),
                         daemon=True)
    t.start()

    deadline = time.time() + 5
    while time.time() < deadline:
        rows = metrics.inflight_detail() or []
        if rows and rows[0].get("stage") == "storage_save":
            break
        time.sleep(0.05)
    rows = metrics.inflight_detail() or []
    assert rows, "진행 중 업로드가 잡히지 않았다"
    r = rows[0]
    assert r["stage"] == "storage_save", f"현재 단계가 안 보인다: {r}"
    assert r["stage_source"] == "3/7 lot_c.parquet", f"단계의 대상 파일이 없다: {r}"
    assert "slot_wait" in r["stages_done"] and "multipart" in r["stages_done"], \
        f"끝난 단계가 안 실렸다: {r}"
    assert not done, "요청이 이미 끝났다 — 진행 중 관찰이 아니다"
    print(f"(6) 진행 중 업로드의 현재 단계가 보임 ok ({r['stage']} / {r['stage_source']})")

    # ── 임계는 경로별이다: 업로드 2초 vs 그 외 3600초 ────────────────────────
    assert metrics._stuck_threshold("report.upload_webreport") == 2.0
    assert metrics._stuck_threshold("report.session_full") == 3600.0
    assert metrics.stuck_now() == [], "임계 전인데 stuck 으로 잡혔다"
    time.sleep(2.2)
    stuck = metrics.stuck_now()
    assert len(stuck) == 1 and stuck[0]["route"] == "report.upload_webreport", \
        f"업로드가 업로드 임계(2초)로 잡히지 않았다: {stuck}"
    print("(7) 업로드에 범용보다 짧은 전용 임계 적용 ok")

    # ── stuck 사건 message 에 단계가 들어간다 (사람이 바로 조치할 수 있게) ────
    metrics._check_stuck_requests()
    ev = _diag("stuck_request")
    assert ev, f"stuck 사건이 없다: {_diag()}"
    assert "storage_save" in ev[0], f"사건에 단계가 없다: {ev[0]}"
    assert '"stage_source": "3/7 lot_c.parquet"' in ev[0], f"사건에 대상 파일이 없다: {ev[0]}"
    assert not done, "요청이 끝난 뒤에 잡혔다 — 기존 계측과 다를 게 없다"
    print("(8) stuck 사건 message·필드에 단계 포함 ok")

    # 덤프 머리말에도 단계가 있어야 스택과 대조할 수 있다
    dumps = sorted(_TMP.glob("diagnose_stuck_*.txt"))
    assert dumps, "스레드 덤프가 없다"
    head = dumps[0].read_text(encoding="utf-8").splitlines()[1]
    assert "storage_save" in head, f"덤프 머리말에 단계가 없다: {head}"
    print("(9) 스레드 덤프 머리말에 단계 포함 ok")

    # ── 종료 정리 ────────────────────────────────────────────────────────────
    release.set()
    t.join(timeout=10)
    assert done == [200], f"업로드 요청이 끝나지 않았다: {done}"
    assert "storage_save" in seen.get("done", {}), \
        f"라우트가 자기 단계 분해를 못 읽었다: {seen}"
    assert metrics.inflight_detail() == [], metrics.inflight_detail()
    assert metrics._req_stages == {}, f"단계 기록이 남았다: {metrics._req_stages}"
    print("(10) 요청 종료 후 진행 중 목록·단계 기록 모두 정리 ok")

    print("\n전체 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

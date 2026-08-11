"""진단 사건 수집 e2e 검증 (server/diagnostics.py + ops.py + 수집 라우트).

배경: 서버 500 이 나도 traceback 은 콘솔 로그 한 곳에만 있고 응답에는 아무 상관 키가
없어, 사용자가 신고해도 그 로그 줄을 찾을 수 없었다. Honey 앱 오류는 아예 서버에
흔적이 없었다. 여기서 그 두 구멍이 실제로 막혔는지 확인한다.

시나리오:
  (a) 모든 응답에 X-Request-ID 헤더가 붙는다
  (b) 처리되지 않은 예외 → 500 본문 error_id == X-Request-ID == 진단 사건 request_id
      (= 사용자가 읽어주는 번호 하나로 서버 기록을 찾을 수 있다)
  (c) 503(컴퓨트 지연)도 같은 규약 + severity=warning
  (d) HTTPException(404 등)은 사건을 만들지 않는다 (정상 응답까지 사건이 되면 못 쓴다)
  (e) POST /pe/report/api/client_diagnostic — Honey 오류 수집, event_id 는 클라 값 유지
      (오프라인 재전송이 중복 사건이 되지 않아야 한다)
  (f) 상관 ID 로 타임라인이 이어지고, 근거 없는 사건에는 원인을 지어내지 않는다
  (g) 전체 경로는 basename 으로 정제된다 (사용자 PC 폴더 구조 미수집)

실행:
    python tests/test_diagnostics.py

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="diag_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
# 사건 로그도 임시 폴더로 — 운영 server/log 를 테스트가 오염시키면 안 된다.
os.environ["REPORT_DIAG_DIR"] = str(_TMP / "log")

from flask import Flask  # noqa: E402

import diagnostics  # noqa: E402
from ops import init_ops  # noqa: E402
from report.report_extension import report_bp  # noqa: E402
from database import report_db  # noqa: E402

app = Flask(__name__)
app.secret_key = "diag-test"
app.register_blueprint(report_bp)
report_db.init_report_db()
init_ops(app)


@app.get("/_test/boom")
def _boom():
    raise ValueError("테스트용 폭발")


@app.get("/_test/busy")
def _busy():
    raise TimeoutError("compute 지연")


def _events():
    return diagnostics.history(hours=24, limit=500)


def main():
    client = app.test_client()

    # ── (a) 모든 응답에 상관 ID ──────────────────────────────────────────────
    res = client.get("/healthz")
    rid = res.headers.get("X-Request-ID")
    assert rid and len(rid) == 8, res.headers
    print(f"(a) X-Request-ID 헤더 ok ({rid})")

    # ── (b) 500 → error_id 3자 일치 ─────────────────────────────────────────
    res = client.get("/_test/boom")
    assert res.status_code == 500, res.status_code
    body = res.get_json()
    eid = body.get("error_id")
    assert eid, body
    assert eid == res.headers.get("X-Request-ID"), (eid, res.headers)
    hits = [e for e in _events() if e.get("request_id") == eid]
    assert len(hits) == 1, hits
    ev = hits[0]
    assert ev["severity"] == "critical" and ev["event"] == "unhandled_exception", ev
    assert ev["http_status"] == 500 and ev["endpoint"] == "/_test/boom", ev
    assert "ValueError" in ev["error_type"] and "Traceback" in ev["stack"], ev
    print(f"(b) 500 → 응답 error_id == 헤더 == 사건 request_id ok ({eid}, 스택 보존)")

    # ── (c) 503 컴퓨트 지연 ─────────────────────────────────────────────────
    res = client.get("/_test/busy")
    assert res.status_code == 503, res.status_code
    eid2 = res.get_json().get("error_id")
    ev2 = [e for e in _events() if e.get("request_id") == eid2][0]
    assert ev2["event"] == "compute_unavailable" and ev2["severity"] == "warning", ev2
    assert "stack" not in ev2, "503 은 서버 버그가 아니라 용량 문제 — 스택 불필요"
    print("(c) 503 사건(warning, 스택 없음) ok")

    # ── (d) 의도된 4xx 는 사건이 아니다 ─────────────────────────────────────
    before = len(_events())
    client.get("/_test/nope")            # 404
    assert len(_events()) == before, "HTTPException 은 사건을 만들지 않아야 한다"
    print("(d) 404 는 사건 미생성 ok")

    # ── (e) Honey 오류 수집 (event_id 유지 = 재전송 중복 방지) ───────────────
    res = client.post("/pe/report/api/client_diagnostic", json={
        "event_id": "honeyevt0001", "kind": "honey_upload_fail",
        "message": r"ConnectionError: C:\Users\hong\data\lot_a.csv 전송 실패",
        "version": "3.1.1", "operation_id": "op123456",
    })
    assert res.status_code == 204, res.status_code
    hits = [e for e in _events() if e.get("event_id") == "honeyevt0001"]
    assert len(hits) == 1, hits
    hev = hits[0]
    assert hev["component"] == "honey" and hev["operation_id"] == "op123456", hev
    # (g) 경로 정제 — 파일명은 남고 폴더 구조는 사라진다
    assert "lot_a.csv" in hev["message"] and "Users" not in hev["message"], hev
    # 감사 로그에도 1행 (관리자 User Action Monitoring 에서 보인다)
    logs = report_db.get_audit_logs(action="client_error", limit=10)
    assert any("honey_upload_fail" in (r["changed_fields"] or "") for r in logs), logs
    print("(e)(g) Honey 오류 수집 + event_id 유지 + 경로 정제 ok")

    # 같은 event_id 로 재전송해도 사건이 늘지 않는다 (오프라인 큐 flush 시나리오)
    client.post("/pe/report/api/client_diagnostic", json={
        "event_id": "honeyevt0001", "kind": "honey_upload_fail", "message": "재전송"})
    same = [e for e in _events() if e.get("event_id") == "honeyevt0001"]
    assert len({e["event_id"] for e in same}) == 1, same
    print("(e2) 재전송이 같은 event_id 로 묶인다 ok")

    # ── (f) 타임라인 + 근거 없으면 '확인 불가' ──────────────────────────────
    from admin_panel import diagnostics_admin
    detail = diagnostics_admin.event_detail(eid)
    assert detail["found"] and detail["event"]["event_id"] == eid, detail
    assert detail["explain"]["cause"], detail["explain"]

    lone = diagnostics.emit("info", "browser", "load_failed", message="원인 단서 없음")
    ex = diagnostics_admin.event_detail(lone)["explain"]
    assert ex["confident"] is False and "확인 불가" in ex["cause"], ex
    print("(f) 타임라인 조회 + 근거 없을 때 '확인 불가' ok")

    # 콜드 빌드 기록이 있으면 마지막 단계를 근거로 원인을 말한다
    ex2 = diagnostics_admin.explain(
        {"component": "build"}, [],
        [{"result": "timeout", "total": 300, "last_stage": "decode",
          "last_source": "3/7 lot_c.csv"}], [])
    assert ex2["confident"] and "decode" in ex2["cause"], ex2
    ex3 = diagnostics_admin.explain(
        {"component": "build"}, [], [{"result": "timeout", "total": 300,
                                      "last_stage": ""}], [])
    assert ex3["confident"] and "큐" in ex3["cause"], ex3
    print("(f2) 증거 기반 원인 안내(단계 특정 / 큐 대기 구분) ok")

    # ── (i) 업로드 실패가 서버에 남는다 ─────────────────────────────────────
    # 종전에는 400/503 경로가 클라 화면에만 뜨고 서버에는 흔적이 0 이었다.
    res = client.post("/pe/report/upload_webreport",
                      data={"manifest": "{not json"},
                      content_type="multipart/form-data")
    assert res.status_code == 400, res.status_code
    logs = report_db.get_audit_logs(action="upload", limit=20)
    fails = [r for r in logs if r["result"] == "fail"]
    assert fails and "upload_webreport 400" in (fails[0]["changed_fields"] or ""), fails
    assert any(e["event"] == "upload_failed" for e in _events()), "업로드 실패 사건"
    print("(i) 업로드 실패 → 감사(result=fail) + 진단 사건 ok")

    # ── (j) 세션 열람 기록 (사용자·세션당 1시간 1회) ────────────────────────
    report_db.create_session("S-VIEW", "view.xlsx", None, product_type="MDDI",
                             product="P1", lot_id="L1", source="web_report")
    ua = {"User-Agent": "python-requests HoneyUser/kim"}
    for _ in range(3):
        assert client.get("/pe/report/session/S-VIEW/my_access",
                          headers=ua).status_code == 200
    views = [r for r in report_db.get_audit_logs(action="view", limit=50)
             if r["session_id"] == "S-VIEW"]
    assert len(views) == 1, f"3회 열람 = 1행이어야 한다: {len(views)}"
    assert views[0]["product"] == "P1", views[0]
    print("(j) 세션 열람 감사 1행(중복 제거) ok")

    # ── ack ────────────────────────────────────────────────────────────────
    assert diagnostics.ack(eid, by="tester")
    assert diagnostics.summary()["unacked"]["critical"] >= 0
    assert not [e for e in diagnostics.history(unacked_only=True)
                if e.get("event_id") == eid], "확인 처리한 사건은 미확인 목록에서 빠진다"
    print("(h) 확인 처리(ack) ok")

    print("\n전체 통과")


if __name__ == "__main__":
    main()

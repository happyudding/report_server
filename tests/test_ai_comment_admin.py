# -*- coding: utf-8 -*-
"""AI Comment 클라 대행 — 관리자 모니터링 탭 데이터·라우트 (docs/23).

실행:
    server\\.venv\\Scripts\\python.exe tests/test_ai_comment_admin.py

이 화면이 없으면 관리자는 기능이 도는지 알 수 없다 — 실패해도 화면에는 에러가 아니라
룰 폴백 문장이 나오기 때문이다. 그래서 여기서 고정하는 것은 "비어 있음의 사유가
구분되는가" 와 "실패 신호가 실제로 도달하는가" 다.

검증 항목:
  (a) 커버리지 — 대상 세션 판정(ai_model=claude AND optin) · push marker 로 반영 분류
  (b) "비었다"의 사유 구분 — 대상 0건 / 전부 반영 / 일부 미반영이 각각 다른 note
  (c) push 집계 — action='ai_suggest' 감사 파싱(정상/형식파괴 → unparsed 카운트)
  (d) 클라 실패 — 진단 사건 중 component=honey + ai_suggest* 만 골라 kind 별 집계
  (e) session_suggestions — store 파일 읽기 · 파일 없음 · 대상 아님 각각의 note
  (f) 구성요소 격리 — 하나가 터져도 나머지 키는 살아 있다
  (g) 라우트 — 인증 게이트(401) · 인증 후 200 · 없는 세션 404
  (h) 화면 정합 — 탭 버튼 / 패널 id / TAB_LOADERS 3지점이 서로 맞는지(정적 검사)

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import hashlib
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

_TMP = Path(tempfile.mkdtemp(prefix="ai_admin_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
# ⚠ 진단 로그를 임시 폴더로 격리 — 없으면 테스트 사건이 실제 server/log 에 섞이고
# (조회를 못 믿게 된다) 재실행마다 누적돼 건수 assert 가 깨진다.
os.environ["REPORT_DIAG_DIR"] = str(_TMP / "diag")
os.environ["REPORT_ADMIN_SECRET"] = "testsec"
os.environ["REPORT_ADMIN_PASSWORD"] = "testpw"

from flask import Flask  # noqa: E402

import diagnostics  # noqa: E402
from admin_panel import ai_comment_admin as A  # noqa: E402
from admin_panel import register_admin_panel  # noqa: E402
from database import report_db  # noqa: E402
from web_report import ai_suggest_store  # noqa: E402

app = Flask(__name__)
register_admin_panel(app)
report_db.init_report_db()
client = app.test_client()

UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])
ADMIN = "/pe/admin-testsec"

AI_OPTS = json.dumps({"ai_comment": True, "ai_comment_optin": True, "ai_model": "claude"})
DEFAULT_OPTS = json.dumps({"ai_comment": True, "ai_comment_optin": True})


def _session(sid, akey, opts, uploaded_by="tester"):
    chash = hashlib.sha256(sid.encode()).hexdigest()
    report_db.create_session(sid, f"{sid}.parquet", None, product_type="MDDI",
                             lot_id="LOT1", product="P1", source="web_report",
                             uploaded_by=uploaded_by)
    report_db.update_session(sid, analysis_key=akey, content_hash=chash, status="done",
                             webreport_options=opts)
    return chash


def _mark_pushed(sid, count=3):
    report_db.apply_webreport_edits(
        sid, [("ai_suggest", "push", json.dumps({"ts": int(time.time()), "count": count}))],
        updated_by="tester")


def _audit(sid, accepted=2, skipped=1, changed=None):
    report_db.log_audit("ai_suggest", session_id=sid, analysis_key="A", product_type="MDDI",
                        product="P1", lot_id="LOT1", file_name="x.parquet",
                        changed_fields=changed if changed is not None
                        else f"ai_suggest(accepted={accepted},skipped={skipped})",
                        client_ip="1.2.3.4", user_agent="ua")


def test_coverage_empty():
    cov = A._coverage()
    assert cov["total"] == 0 and "업로드된 세션이 아직 없" in cov["note"], cov
    print("  (b1) 대상 세션 0건 사유 OK")


def test_coverage_classification():
    _session("S_CLAUDE_1", "a" * 64, AI_OPTS)
    _session("S_CLAUDE_2", "b" * 64, AI_OPTS)
    _session("S_DEFAULT", "c" * 64, DEFAULT_OPTS)      # 대상 아님 — 분모에서 빠져야 한다
    _mark_pushed("S_CLAUDE_1", count=5)

    cov = A._coverage()
    assert cov["total"] == 2, cov          # default 세션 제외
    assert cov["covered"] == 1 and cov["pending"] == 1
    assert "1개 세션이 아직" in cov["note"], cov["note"]
    assert [r["session_id"] for r in cov["rows"]] == ["S_CLAUDE_2"]
    assert cov["covered_rows"][0]["push_count"] == 5
    print("  (a) 대상 판정·push marker 분류 OK")

    _mark_pushed("S_CLAUDE_2")
    cov = A._coverage()
    assert cov["pending"] == 0 and "모두 반영" in cov["note"], cov["note"]
    print("  (b2) 전부 반영 사유 OK")


def test_push_parsing():
    _audit("S_CLAUDE_1", accepted=4, skipped=2)
    _audit("S_CLAUDE_2", accepted=1, skipped=0)
    _audit("S_CLAUDE_2", changed="ai_suggest 형식이 바뀐 경우")   # 파싱 실패 행
    push = A._push(days=14)
    assert push["pushes"] == 3, push
    assert push["accepted"] == 5 and push["skipped"] == 2, push
    assert push["unparsed"] == 1, push       # 조용히 0 으로 만들지 않는다
    assert push["rows"][0]["session_id"]
    # 다른 action 은 섞이지 않는다
    report_db.log_audit("edit", session_id="S_CLAUDE_1", changed_fields="preprocess(off)")
    assert A._push(days=14)["pushes"] == 3
    print("  (c) push 파싱·형식불명 카운트·action 분리 OK")


def test_failures():
    diagnostics.emit("warning", "honey", "ai_suggest_no_cli",
                     message="claude CLI 를 찾지 못했습니다", session_id="S_CLAUDE_2",
                     user="tester")
    diagnostics.emit("warning", "honey", "ai_suggest_empty",
                     message="결과 0건", session_id="S_CLAUDE_2", user="tester")
    diagnostics.emit("warning", "honey", "ai_suggest_empty",
                     message="결과 0건(2)", session_id="S_CLAUDE_1", user="tester")
    diagnostics.emit("warning", "honey", "upload_failed", message="무관한 사건")
    diagnostics.emit("warning", "browser", "ai_suggest_empty", message="컴포넌트 다름")

    fail = A._failures(days=14)
    assert fail["total"] == 3, fail          # honey + ai_suggest* 만
    kinds = {k["kind"]: k["cnt"] for k in fail["by_kind"]}
    assert kinds == {"ai_suggest_empty": 2, "ai_suggest_no_cli": 1}, kinds
    assert fail["by_kind"][0]["label"] == "생성 결과 0건"      # 많은 순
    assert fail["rows"][0]["event_id"]
    print("  (d) 클라 실패 필터·kind 집계 OK")


def test_session_suggestions():
    session = report_db.get_session("S_CLAUDE_1")
    from web_report import service as web_report_service
    akey, chash, mode, prep = web_report_service._ai_suggest_coords(
        session, "S_CLAUDE_1", report_db=report_db)
    ai_suggest_store.save_merge(UPLOAD_ROOT, akey, chash, mode,
                                {"ItemA": {"sha": "a" * 12, "suggestion": "- 점검하세요"}},
                                by="tester", prep_digest=prep)

    out = A.session_suggestions("S_CLAUDE_1")
    assert len(out["items"]) == 1
    assert out["items"][0]["item"] == "ItemA" and out["items"][0]["suggestion"] == "- 점검하세요"
    # 평가 캐시가 없으므로 stale 은 확인 불가(None) + 사유 note
    assert out["items"][0]["stale"] is None and "sha" in out["note"], out["note"]

    # 파일 없는 세션 / 대상 아닌 세션 각각 다른 note
    assert A.session_suggestions("S_CLAUDE_2")["items"] == []
    assert "push" in A.session_suggestions("S_CLAUDE_2")["note"]
    assert "대상 세션이 아닙" in A.session_suggestions("S_DEFAULT")["note"]
    try:
        A.session_suggestions("NO_SUCH")
        raise AssertionError("없는 세션은 KeyError 여야 한다")
    except KeyError:
        pass
    print("  (e) session_suggestions 3분기 + KeyError OK")


def test_overview_isolation():
    o = A.overview(days=14)
    assert set(o) >= {"days", "coverage", "push", "failures"}
    assert o["days"] == 14
    assert A.overview("bad")["days"] == 14 and A.overview(999)["days"] == 90   # clamp

    # 한 구성요소가 터져도 나머지는 살아 있다
    orig = A._push
    A._push = lambda days: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        o = A.overview(days=7)
        assert "error" in o["push"] and "boom" in o["push"]["error"]
        assert o["coverage"]["total"] >= 1        # 나머지는 정상
    finally:
        A._push = orig
    print("  (f) 구성요소 격리 OK")


def test_routes():
    # 인증 없이는 401
    r = client.get(f"{ADMIN}/api/ai_comment")
    assert r.status_code == 401, r.status_code
    r = client.post(f"{ADMIN}/login", json={"password": "testpw"},
                    headers={"X-Admin-Request": "1"})
    assert r.status_code == 200, (r.status_code, r.data[:200])

    r = client.get(f"{ADMIN}/api/ai_comment?days=14")
    assert r.status_code == 200, (r.status_code, r.data[:200])
    body = r.get_json()
    assert body["coverage"]["total"] == 2 and body["push"]["pushes"] == 3
    assert body["failures"]["total"] == 3

    r = client.get(f"{ADMIN}/api/ai_comment/session/S_CLAUDE_1")
    assert r.status_code == 200 and len(r.get_json()["items"]) == 1
    r = client.get(f"{ADMIN}/api/ai_comment/session/NO_SUCH")
    assert r.status_code == 404, r.status_code
    print("  (g) 라우트 401/200/404 OK")


def test_html_wiring():
    html = (Path(_ROOT) / "server" / "admin_panel" / "admin_panel.html").read_text(
        encoding="utf-8")
    assert 'data-panel="pAiComment"' in html, "탭 버튼 없음"
    assert 'id="pAiComment" class="panel"' in html, "패널 div 없음"
    assert "pAiComment: loadAiCommentTab" in html, "TAB_LOADERS 등록 없음"
    assert "async function loadAiCommentTab()" in html, "로더 함수 없음"
    # 로더가 $() 로 만지는 id 가 마크업에 전부 있어야 한다 (eval_panel 테스트와 같은 취지)
    for dom_id in ("aicTiles", "aicDays", "btnAicRefresh", "aicFailCard", "aicFailKinds",
                   "aicFailBody", "aicCovNote", "aicCovBody", "aicSugNote", "aicSugBody",
                   "aicPushBody"):
        assert f'id="{dom_id}"' in html, f"DOM id 누락: {dom_id}"
    # 감사 화면 필터·라벨
    assert '<option value="ai_suggest">' in html and 'ai_suggest: "AI 제안 반영"' in html
    print("  (h) 화면 정합(탭·패널·로더·DOM id·감사 라벨) OK")


def main():
    test_coverage_empty()
    test_coverage_classification()
    test_push_parsing()
    test_failures()
    test_session_suggestions()
    test_overview_isolation()
    test_routes()
    test_html_wiring()
    print("test_ai_comment_admin: 전부 통과")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()

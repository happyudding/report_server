"""web_report 세션 동시편집 충돌 감지 E2E 검증.

실행:
    python tests/test_note_conflict.py

배경: 편집 권한 위임(report_session_editor) 때문에 같은 세션을 두 사람이 동시에
편집할 수 있다. Note 시트는 통째 치환이라 무방비면 상대 작업 전체가 사라지고,
Summary Engr 는 클라가 3칸을 통째로 보내면 다른 칸까지 덮어썼다.

시나리오:
  (a) Engr 부분 payload — 서로 다른 칸을 순차 저장하면 둘 다 보존
  (b) Note stale 저장 — 남이 먼저 저장했으면 409 + conflict.updated_by
  (c) force 덮어쓰기 — 409 이후 force 재전송은 200, 내용 교체 + rev 증가
  (d) 신규 작성 경합 — Note 없음 상태에서 둘 다 base=null 이면 나중 요청 409
  (e) 하위호환 — base 키 없는 요청(캐시된 구 JS)은 무검사 저장
  (f) 충돌 거부는 쓰기·rev 를 남기지 않는다

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="note_conflict_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""          # S3 비활성 → 로컬 폴백

from flask import Flask  # noqa: E402

from report.report_extension import report_bp  # noqa: E402
from database import report_db  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()

SID = "s-note-conflict"
AKEY = "a" * 64


def _setup_session():
    """web_report 세션 1건 — 업로더 alice + 위임 편집자 bob.

    (2026-07-22 이후 uploaded_by 가 빈 web_report 세션은 편집이 막힌다. 이 테스트가
    필요로 하는 '두 사람이 같은 세션을 편집'하는 조건은 편집 위임으로 만든다 — 원래
    docstring 이 말하는 report_session_editor 시나리오 그대로다.)"""
    report_db.create_session(SID, "note.parquet", None, product_type="MDDI",
                             lot_id="LOT1", product="P1", source="web_report",
                             uploaded_by="alice")
    report_db.update_session(SID, analysis_key=AKEY, status="done")
    report_db.add_session_editor(SID, "bob", granted_by="alice")
    # Engr 저장 경로의 ensure_seeded 가 manifest 를 지연 로드한다 (rev==0 인 첫 편집).
    man = Path(os.environ["REPORT_UPLOAD_DIR"]) / "web_report" / AKEY
    man.mkdir(parents=True, exist_ok=True)
    (man / "manifest.json").write_text(json.dumps({}), encoding="utf-8")


def _csrf():
    client.get(f"/pe/report/view/{SID}")      # after_request 가 쿠키 발급
    cookie = client.get_cookie("report_csrf")
    assert cookie is not None, "CSRF 쿠키가 발급되지 않음"
    return cookie.value


def _headers(user):
    return {"User-Agent": f"Mozilla/5.0 HoneyUser/{user}",
            "X-CSRF-Token": _csrf()}


def _sheet(text):
    return {"sheets": [{"name": "Sheet1", "celldata": [{"r": 0, "c": 0, "v": {"v": text}}]}]}


def _note_get():
    r = client.get(f"/pe/report/session/{SID}/web_report/note")
    assert r.status_code == 200, (r.status_code, r.data)
    return r.get_json()


def _note_save(text, base="__omit__", force=False, user="alice"):
    payload = {"sheet": _sheet(text)}
    if base != "__omit__":
        payload["base"] = base
    if force:
        payload["force"] = True
    return client.post(f"/pe/report/session/{SID}/web_report/note",
                       json=payload, headers=_headers(user))


def _engr_save(values, user="alice"):
    return client.post(f"/pe/report/session/{SID}/web_report/summary/engr",
                       json={"values": values}, headers=_headers(user))


def _engr_state():
    rows = report_db.get_webreport_edits(SID, kinds=("summary_engr",))
    return {r["item_key"]: r["value"] for r in rows}


def test_engr_partial_payload_preserves_other_fields():
    """(a) 두 사용자가 서로 다른 칸을 저장 — 부분 payload 라 상대 칸이 남는다."""
    r = _engr_save({"yield": "A 가 쓴 yield"}, user="alice")
    assert r.status_code == 200, (r.status_code, r.data)
    r = _engr_save({"cpk": "B 가 쓴 cpk"}, user="bob")
    assert r.status_code == 200, (r.status_code, r.data)
    state = _engr_state()
    assert state.get("yield") == "A 가 쓴 yield", state
    assert state.get("cpk") == "B 가 쓴 cpk", state
    # 서버 응답도 병합 전체 상태를 돌려준다 (클라가 로컬 갱신에 그대로 쓴다).
    assert r.get_json()["summary_engr"]["yield"] == "A 가 쓴 yield"
    print("  (a) Engr 부분 payload - 서로 다른 칸 양쪽 보존 OK")


def test_note_stale_save_conflicts():
    """(b) A 가 저장한 뒤, 옛 base 를 든 B 의 저장은 409."""
    r = _note_save("A 의 첫 저장", base=None, user="alice")
    assert r.status_code == 200, (r.status_code, r.data)
    base_after_a = r.get_json()["base"]
    assert base_after_a, "저장 응답에 base 토큰이 없다"

    r = _note_save("A 의 두 번째 저장", base=base_after_a, user="alice")
    assert r.status_code == 200, (r.status_code, r.data)

    # B 는 첫 저장 시점의 base 를 들고 있다 → stale
    r = _note_save("B 의 저장", base=base_after_a, user="bob")
    assert r.status_code == 409, (r.status_code, r.data)
    j = r.get_json()
    assert j["conflict"]["updated_by"] == "alice", j
    assert "다른 사용자" in j["error"], j
    print("  (b) Note stale 저장 409 + 마지막 저장자 통지 OK")
    return base_after_a


def test_conflict_rejects_without_writing(stale_base):
    """(f) 거부된 저장은 내용도 rev 도 건드리지 않는다."""
    before = _note_get()
    rev_before = report_db.get_webreport_edit_rev(SID)
    r = _note_save("거부돼야 할 저장", base=stale_base, user="bob")
    assert r.status_code == 409, (r.status_code, r.data)
    after = _note_get()
    assert after["sheet"] == before["sheet"], "409 인데 시트가 바뀌었다"
    assert after["base"] == before["base"], "409 인데 base 가 바뀌었다"
    assert report_db.get_webreport_edit_rev(SID) == rev_before, "409 인데 rev 가 올랐다"
    print("  (f) 충돌 거부는 쓰기·rev 무영향 OK")


def test_force_overwrite(stale_base):
    """(c) 사용자가 덮어쓰기를 택하면 force 재전송이 통과한다."""
    rev_before = report_db.get_webreport_edit_rev(SID)
    r = _note_save("B 가 덮어씀", base=stale_base, force=True, user="bob")
    assert r.status_code == 200, (r.status_code, r.data)
    saved = _note_get()
    cell = saved["sheet"]["sheets"][0]["celldata"][0]["v"]["v"]
    assert cell == "B 가 덮어씀", saved
    assert saved["base"] == r.get_json()["base"], "GET/POST 의 base 토큰이 어긋난다"
    assert report_db.get_webreport_edit_rev(SID) > rev_before, "rev 가 오르지 않았다(캐시 무효화 실패)"
    print("  (c) force 덮어쓰기 200 + rev 증가 OK")


def test_new_note_race():
    """(d) Note 가 없던 세션에서 둘 다 base=null — 나중 요청은 409."""
    sid2 = "s-note-race"
    report_db.create_session(sid2, "n2.parquet", None, product_type="MDDI",
                             lot_id="LOT2", product="P1", source="web_report",
                             uploaded_by="alice")
    report_db.update_session(sid2, analysis_key=AKEY, status="done")
    report_db.add_session_editor(sid2, "bob", granted_by="alice")
    global SID
    prev, SID = SID, sid2
    try:
        assert _note_get()["base"] is None, "새 세션에 Note 가 이미 있다"
        r = _note_save("A 가 새로 만듦", base=None, user="alice")
        assert r.status_code == 200, (r.status_code, r.data)
        # B 도 "없음"(base=null)을 보고 작성을 시작했었다
        r = _note_save("B 가 새로 만듦", base=None, user="bob")
        assert r.status_code == 409, (r.status_code, r.data)
        assert r.get_json()["conflict"]["updated_by"] == "alice"
        print("  (d) 신규 작성 경합 409 OK")
    finally:
        SID = prev


def test_legacy_client_without_base():
    """(e) base 키가 아예 없는 요청(캐시된 구 JS)은 종전대로 무검사 저장."""
    r = _note_save("구버전 클라 저장", user="bob")   # base 미전송
    assert r.status_code == 200, (r.status_code, r.data)
    cell = _note_get()["sheet"]["sheets"][0]["celldata"][0]["v"]["v"]
    assert cell == "구버전 클라 저장"
    print("  (e) base 미전송 = 무검사 저장(하위호환) OK")


if __name__ == "__main__":
    _setup_session()
    print("web_report 동시편집 충돌 감지 테스트")
    test_engr_partial_payload_preserves_other_fields()
    stale = test_note_stale_save_conflicts()
    test_conflict_rejects_without_writing(stale)
    test_force_overwrite(stale)
    test_new_note_race()
    test_legacy_client_without_base()
    print("\n전부 통과")

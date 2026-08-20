"""Compare 탭 행 코멘트(kind=compare_note) + new_items 계약 스모크.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_compare_notes.py

왜 이 파일이 필요한가 (2026-08-20 신설):
    Compare 탭의 Comment 열은 종전까지 **저장소가 없는 장식 컬럼**이었다(서버가 항상
    "" 를 주고 프런트는 정적 td 를 그렸다). 이번에 세션 편집 DB 채널을 새로 만들었는데,
    사용자가 직접 입력한 값은 소실되면 복구할 방법이 없다(CLAUDE.md §5-12). 그래서
    **키 규약과 왕복(저장 → /full 재조회)** 을 기계로 고정한다.

검증 항목:
  (a) Compare 세션 payload 에 new_items — **그룹 전체 합집합** 기준(After 에만 있는 항목)
  (b) POST .../web_report/compare_notes 200 → GET /full extras 로 되읽힌다 (gl / bm 두 키)
  (c) 빈 문자열 저장 = 삭제
  (d) 키 규약 위반("xx:1") → 400
  (e) CSRF 헤더 없음 → 403 / 업로더가 아닌 사람 → 403

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# config 는 import 시점에 env 를 읽는다 — 반드시 import 앞에서 지정할 것.
_TMP = Path(tempfile.mkdtemp(prefix="cmp_notes_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import compute as wr_compute  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()

SEP = chr(31)                       # item_key 구분자 (issue_comment 와 같은 관례)
UA = {"User-Agent": "Mozilla/5.0 HoneyUser/owner"}
UA_OTHER = {"User-Agent": "Mozilla/5.0 HoneyUser/stranger"}
CSRF = "test-csrf-token"
BEFORE_ITEMS = ["IT00", "IT01"]
AFTER_ITEMS = ["IT00", "IT01", "NEW1"]
N_ROWS = 6


def make_parquet(items, flip_bin=False) -> bytes:
    """합성 7-meta honeyform → parquet bytes (계약 정본은 CLAUDE.md 규칙 #9).

    두 source 가 **같은 좌표**를 갖게 해 common_map / bin_matrix 가 만들어지게 한다.
    flip_bin 이면 die 하나의 BIN 을 바꿔 '불일치 좌표' 를 1개 만든다.
    """
    rng = np.random.default_rng(0)
    rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row[META_COLUMNS[0]] = label
        for j, it in enumerate(items):
            row[it] = {"TSEQ": j + 1, "TNO": 1000 + j, "STEP": "P1",
                       "UNIT": "V", "HILIM": 10.0, "LOLIM": -10.0}[label]
        rows.append(row)
    for i in range(N_ROWS):
        bin_v = 1 if i % 3 else 2
        if flip_bin and i == 0:
            bin_v = 5
        row = {"SERIAL": f"S{i:04d}", "SHOT": 0, "DUT": 0,
               "XPOS": i % 3 + 1, "YPOS": i // 3 + 1, "BIN": bin_v,
               "FAILTNO": "" if bin_v == 1 else 1000}
        for it in items:
            row[it] = round(float(rng.normal(0, 1)), 4)
        rows.append(row)
    df = pd.DataFrame(rows, columns=META_COLUMNS + list(items))
    return encode_honeyform_parquet(df)


def upload_compare_session() -> str:
    """Compare 모드 세션 1개 업로드 (After=WF3 가 NEW1 을 더 가진다)."""
    manifest = {
        "meta": {"product_type": "MDDI", "product": "SMOKE", "lot_id": "CMPNOTE"},
        "mode": "Compare",
        # 업로드 순서는 After → Before (배치 다이얼로그 result_groups.order 관례)
        "sources": [{"name": "WF3", "file_name": "WF3.csv"},
                    {"name": "WF1", "file_name": "WF1.csv"}],
        "selected_items": [],
        "options": {"compare": {"before": ["WF1"], "after": ["WF3"]}},
        "client": {"user": "owner", "host": "smokehost"},
    }
    data = {
        "manifest": json.dumps(manifest),
        "webreport_0": (io.BytesIO(make_parquet(AFTER_ITEMS, flip_bin=True)), "WF3.csv"),
        "webreport_1": (io.BytesIO(make_parquet(BEFORE_ITEMS)), "WF1.csv"),
    }
    resp = client.post("/pe/report/upload_webreport", data=data, headers=UA,
                       content_type="multipart/form-data")
    assert resp.status_code == 200, f"업로드 실패 {resp.status_code}: {resp.get_data(as_text=True)[:400]}"
    sid = resp.get_json().get("session_id")
    assert sid, resp.get_json()
    return sid


def get_full(sid: str, want_compare: bool = True, timeout: float = 120) -> dict:
    """GET /full — 콜드(202)와 compare 계산 대기(compare_pending)를 넘겨 200 본문을 준다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/pe/report/session/{sid}/full", headers=UA)
        if resp.status_code == 200:
            body = resp.get_json()
            wr = body.get("web_report") or {}
            if not want_compare or wr.get("compare") or not wr.get("compare_pending"):
                return body
        time.sleep(0.1)
    raise AssertionError("/full 이 200(+compare) 으로 돌아오지 않았다")


def post_notes(sid, ops, headers=UA, csrf=CSRF):
    hdr = dict(headers)
    if csrf is not None:
        hdr["X-CSRF-Token"] = csrf
    return client.post(f"/pe/report/session/{sid}/web_report/compare_notes",
                       json={"ops": ops}, headers=hdr)


def test_new_items(sid) -> None:
    """(a) new_items = After 그룹 합집합 − Before 그룹 합집합."""
    wr = get_full(sid)["web_report"]
    cmp_payload = wr.get("compare")
    assert cmp_payload, "compare payload 가 없다 (Compare 모드 인식 실패)"
    assert cmp_payload.get("new_items") == ["NEW1"], cmp_payload.get("new_items")
    print("  [ok] new_items = ['NEW1'] (After 에만 있는 항목)")


def test_note_roundtrip(sid) -> None:
    """(b) 저장 → /full extras 로 되읽힌다. 키 2종(gl / bm) 모두."""
    gl_key = "gl:NEW1" + SEP          # After 에만 있는 행(before 이름이 빈 문자열)
    bm_key = "bm:1,1"
    resp = post_notes(sid, [{"key": gl_key, "value": "신규 항목 확인 필요"},
                            {"key": bm_key, "value": "이 die 만 Bin 다름"}])
    assert resp.status_code == 200, f"{resp.status_code}: {resp.get_data(as_text=True)[:300]}"
    body = resp.get_json()
    assert body["updated"] == 2, body
    assert body["compare_notes"][gl_key]["text"] == "신규 항목 확인 필요", body["compare_notes"]

    notes = get_full(sid, want_compare=False).get("compare_notes") or {}
    assert notes.get(gl_key, {}).get("text") == "신규 항목 확인 필요", notes
    assert notes.get(bm_key, {}).get("text") == "이 die 만 Bin 다름", notes
    assert notes[gl_key].get("updated_by") == "owner", notes[gl_key]
    print("  [ok] gl/bm 코멘트 저장 → /full extras 왕복")


def test_delete(sid) -> None:
    """(c) 빈 문자열 = 삭제 (프런트가 셀을 비우면 행이 지워져야 한다)."""
    bm_key = "bm:1,1"
    assert post_notes(sid, [{"key": bm_key, "value": "   "}]).status_code == 200
    notes = get_full(sid, want_compare=False).get("compare_notes") or {}
    assert bm_key not in notes, notes
    assert "gl:NEW1" + SEP in notes, "다른 키까지 지워졌다"
    print("  [ok] 빈 값 = 삭제 (다른 키는 유지)")


def test_bad_key(sid) -> None:
    """(d) 키 규약 위반 → 400. 접두(gl:/bm:)가 화면을 가르므로 느슨해지면 안 된다."""
    assert post_notes(sid, [{"key": "xx:1", "value": "a"}]).status_code == 400
    assert post_notes(sid, [{"key": "", "value": "a"}]).status_code == 400
    assert post_notes(sid, [{"key": "gl:" + "x" * 400, "value": "a"}]).status_code == 400
    print("  [ok] 잘못된 키 → 400")


def test_guards(sid) -> None:
    """(e) CSRF 없음 → 403 / 업로더도 편집자도 아닌 사람 → 403."""
    assert post_notes(sid, [{"key": "bm:2,2", "value": "x"}], csrf=None).status_code == 403
    r = post_notes(sid, [{"key": "bm:2,2", "value": "x"}], headers=UA_OTHER)
    assert r.status_code == 403, f"타인 편집이 {r.status_code} 로 통과했다"
    notes = get_full(sid, want_compare=False).get("compare_notes") or {}
    assert "bm:2,2" not in notes, notes
    print("  [ok] CSRF / 편집 권한 가드")


def settle(timeout=120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)


def main():
    print("[compare_note 스모크]")
    client.set_cookie("report_csrf", CSRF)
    try:
        sid = upload_compare_session()
        test_new_items(sid)
        test_note_roundtrip(sid)
        test_delete(sid)
        test_bad_key(sid)
        test_guards(sid)
        settle()
    finally:
        try:
            settle(timeout=10)
        except Exception:
            pass
        shutil.rmtree(_TMP, ignore_errors=True)
    print("[통과] Compare 행 코멘트 + new_items 정상")


if __name__ == "__main__":
    main()

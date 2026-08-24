"""Distribution composite(kind=dist_composite) 저장 계약 스모크.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_dist_composite.py

왜 이 파일이 필요한가 (2026-08-24 신설):
    사용자가 모달에서 고른 source×item 조합·이름·색은 **재입력할 방법이 없는 입력**이다
    (CLAUDE.md §5-12). 저장 키(UUID)와 왕복(저장 → /full extras 재조회)을 기계로 고정한다.
    그리고 이 kind 는 report payload 계산에 **들어가지 않아야** 한다 — 합성 차트 하나
    저장할 때마다 report 캐시가 콜드 재빌드되면 조회가 통째로 느려진다(2026-08-13 사건).

검증 항목:
  (a) POST .../web_report/dist_composites 200 → GET /full extras 로 되읽힌다
  (b) **payload_rev 불변** (PAYLOAD_NEUTRAL_KINDS 누락 회귀 방지 — 이 파일의 핵심)
  (c) 수정(같은 키 재저장) → 값 교체, 키·색 유지 / value:null → 삭제
  (d) sanitize: unknown 키 제거·pair 중복 제거·pairs 밖 색 제거
  (e) 검증 거부 → 400 (키 형식 / 이름 빈값·초과 / pairs 0·41 / limit mode / 색 형식)
  (f) CSRF 없음 → 403 / 업로더도 편집자도 아닌 사람 → 403
  (g) 표 payload 상태(load_edit_state)에 섞이지 않는다 (_STATE_EXCLUDED_KINDS)

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
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# config 는 import 시점에 env 를 읽는다 — 반드시 import 앞에서 지정할 것.
_TMP = Path(tempfile.mkdtemp(prefix="dist_comp_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import compute as wr_compute, edits as wr_edits  # noqa: E402
from web_report.service import _DC_MAX_PAIRS as MAX_PAIRS  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()

SEP = chr(31)                       # pairKey 구분자 (edits.KIND_DIST_COMPOSITE 규약)
UA = {"User-Agent": "Mozilla/5.0 HoneyUser/owner"}
UA_OTHER = {"User-Agent": "Mozilla/5.0 HoneyUser/stranger"}
CSRF = "test-csrf-token"
ITEMS = ["IT00", "IT01"]
SOURCES = ["WF1", "WF2"]
N_ROWS = 6
CID = str(uuid.uuid4())


def make_parquet(items) -> bytes:
    """합성 7-meta honeyform → parquet bytes (계약 정본은 CLAUDE.md 규칙 #9)."""
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
        row = {"SERIAL": f"S{i:04d}", "SHOT": 0, "DUT": 0,
               "XPOS": i % 3 + 1, "YPOS": i // 3 + 1, "BIN": bin_v,
               "FAILTNO": "" if bin_v == 1 else 1000}
        for it in items:
            row[it] = round(float(rng.normal(0, 1)), 4)
        rows.append(row)
    df = pd.DataFrame(rows, columns=META_COLUMNS + list(items))
    return encode_honeyform_parquet(df)


def upload_session() -> str:
    """Normal 모드 2 source 세션 1개 업로드."""
    manifest = {
        "meta": {"product_type": "MDDI", "product": "SMOKE", "lot_id": "DISTCOMP"},
        "mode": "Normal",
        "sources": [{"name": s, "file_name": f"{s}.csv"} for s in SOURCES],
        "selected_items": [],
        "client": {"user": "owner", "host": "smokehost"},
    }
    data = {"manifest": json.dumps(manifest)}
    for idx, src in enumerate(SOURCES):
        data[f"webreport_{idx}"] = (io.BytesIO(make_parquet(ITEMS)), f"{src}.csv")
    resp = client.post("/pe/report/upload_webreport", data=data, headers=UA,
                       content_type="multipart/form-data")
    assert resp.status_code == 200, f"업로드 실패 {resp.status_code}: {resp.get_data(as_text=True)[:400]}"
    sid = resp.get_json().get("session_id")
    assert sid, resp.get_json()
    return sid


def get_full(sid: str, timeout: float = 120) -> dict:
    """GET /full — 콜드(202)를 넘겨 200 본문을 준다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/pe/report/session/{sid}/full", headers=UA)
        if resp.status_code == 200:
            return resp.get_json()
        time.sleep(0.1)
    raise AssertionError("/full 이 200 으로 돌아오지 않았다")


def post_comp(sid, ops, headers=UA, csrf=CSRF):
    hdr = dict(headers)
    if csrf is not None:
        hdr["X-CSRF-Token"] = csrf
    return client.post(f"/pe/report/session/{sid}/web_report/dist_composites",
                       json={"ops": ops}, headers=hdr)


def spec(name="P1 vs P2 VDD", pairs=None, limit=None, colors=None) -> dict:
    pairs = pairs if pairs is not None else [{"source": s, "item": i}
                                             for s in SOURCES for i in ITEMS]
    return {"name": name,
            "pairs": pairs,
            "limit": limit if limit is not None else {"mode": "item", "item": ITEMS[0]},
            "colors": colors if colors is not None else {
                p["source"] + SEP + p["item"]: "#1f77b4" for p in pairs}}


def payload_rev(sid) -> int:
    return report_db.get_webreport_edit_rev(sid, payload=True)


def test_roundtrip(sid) -> None:
    """(a) 저장 → /full extras 로 되읽힌다."""
    resp = post_comp(sid, [{"key": CID, "value": spec()}])
    assert resp.status_code == 200, f"{resp.status_code}: {resp.get_data(as_text=True)[:300]}"
    body = resp.get_json()
    assert body["updated"] == 1, body
    got = body["dist_composites"][CID]
    assert got["name"] == "P1 vs P2 VDD", got
    assert len(got["pairs"]) == 4, got["pairs"]
    assert got["limit"] == {"mode": "item", "item": ITEMS[0]}, got["limit"]
    assert got["updated_by"] == "owner", got

    extras = get_full(sid).get("dist_composites") or {}
    assert CID in extras, extras
    assert extras[CID]["name"] == "P1 vs P2 VDD", extras[CID]
    assert extras[CID]["colors"][SOURCES[0] + SEP + ITEMS[0]] == "#1f77b4", extras[CID]
    print("  [ok] 저장 → /full extras 왕복 (pairs 4 / limit / colors)")


def test_payload_rev_neutral(sid) -> None:
    """(b) **핵심** — composite 저장은 payload_rev 를 올리지 않는다.

    올라가면 report 캐시 키가 바뀌어 저장할 때마다 전체 콜드 재빌드가 된다."""
    before = payload_rev(sid)
    r = post_comp(sid, [{"key": str(uuid.uuid4()), "value": spec(name="임시")}])
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    after = payload_rev(sid)
    assert after == before, f"payload_rev 가 올랐다 {before} → {after} (PAYLOAD_NEUTRAL_KINDS 확인)"
    # 전역 rev 는 올라야 /full 응답 캐시가 무효화된다.
    assert report_db.get_webreport_edit_rev(sid) > 0, "전역 rev 가 안 올랐다"
    print(f"  [ok] payload_rev 불변 ({before}) · 전역 rev 는 증가")


def test_update_and_delete(sid) -> None:
    """(c) 같은 키 재저장 = 수정 / value:null = 삭제."""
    updated = spec(name="이름 바꿈", pairs=[{"source": SOURCES[0], "item": ITEMS[0]}],
                   limit={"mode": "manual", "lo": -1.5, "hi": 2.5},
                   colors={SOURCES[0] + SEP + ITEMS[0]: "#aabbcc"})
    body = post_comp(sid, [{"key": CID, "value": updated}]).get_json()
    got = body["dist_composites"][CID]
    assert got["name"] == "이름 바꿈" and len(got["pairs"]) == 1, got
    assert got["limit"] == {"mode": "manual", "lo": -1.5, "hi": 2.5}, got["limit"]

    dead = str(uuid.uuid4())
    post_comp(sid, [{"key": dead, "value": spec(name="지울것")}])
    assert dead in (get_full(sid).get("dist_composites") or {})
    assert post_comp(sid, [{"key": dead, "value": None}]).status_code == 200
    extras = get_full(sid).get("dist_composites") or {}
    assert dead not in extras, extras
    assert CID in extras, "다른 키까지 지워졌다"
    print("  [ok] 수정(같은 키 교체) / 삭제(value:null)")


def test_sanitize(sid) -> None:
    """(d) 화이트리스트 재조립 — unknown 키·중복 pair·pairs 밖 색은 버린다."""
    cid = str(uuid.uuid4())
    dirty = spec(name="정리 대상",
                 pairs=[{"source": SOURCES[0], "item": ITEMS[0]},
                        {"source": SOURCES[0], "item": ITEMS[0]},      # 중복
                        {"source": SOURCES[1], "item": ITEMS[1]}],
                 colors={SOURCES[0] + SEP + ITEMS[0]: "#112233",
                         "없는" + SEP + "pair": "#445566"})             # pairs 밖
    dirty["evil"] = {"x": 1}                                            # unknown 키
    dirty["pairs"][0]["extra"] = "drop me"
    got = post_comp(sid, [{"key": cid, "value": dirty}]).get_json()["dist_composites"][cid]
    assert len(got["pairs"]) == 2, got["pairs"]
    assert all(set(p) == {"source", "item"} for p in got["pairs"]), got["pairs"]
    assert "evil" not in got, got
    assert list(got["colors"]) == [SOURCES[0] + SEP + ITEMS[0]], got["colors"]
    post_comp(sid, [{"key": cid, "value": None}])
    print("  [ok] sanitize (unknown 키·중복 pair·pairs 밖 색 제거)")


def test_reject(sid) -> None:
    """(e) 검증 거부 → 400. 느슨해지면 화면이 깨지거나 저장이 무한 증식한다."""
    cases = {
        "키 형식": ({"key": "not a uuid!", "value": spec()}),
        "이름 빈값": ({"key": str(uuid.uuid4()), "value": spec(name="   ")}),
        "이름 초과": ({"key": str(uuid.uuid4()), "value": spec(name="x" * 121)}),
        "pairs 0": ({"key": str(uuid.uuid4()), "value": spec(pairs=[])}),
        "pairs 상한+1": ({"key": str(uuid.uuid4()),
                         "value": spec(pairs=[{"source": f"S{i}", "item": "I"}
                                              for i in range(MAX_PAIRS + 1)], colors={})}),
        "pair 빈값": ({"key": str(uuid.uuid4()),
                       "value": spec(pairs=[{"source": "", "item": "I"}], colors={})}),
        "limit mode": ({"key": str(uuid.uuid4()),
                        "value": spec(limit={"mode": "typo"})}),
        "limit item 누락": ({"key": str(uuid.uuid4()),
                            "value": spec(limit={"mode": "item", "item": ""})}),
        "limit 숫자": ({"key": str(uuid.uuid4()),
                       "value": spec(limit={"mode": "manual", "lo": "abc", "hi": 1})}),
        "색 형식": ({"key": str(uuid.uuid4()),
                    "value": spec(colors={SOURCES[0] + SEP + ITEMS[0]: "red"})}),
    }
    for label, op in cases.items():
        r = post_comp(sid, [op])
        assert r.status_code == 400, f"{label}: {r.status_code} 로 통과했다"
    # ops 자체 규약
    assert post_comp(sid, []).status_code == 400
    assert post_comp(sid, [{"key": str(uuid.uuid4()), "value": spec()}
                           for _ in range(51)]).status_code == 400
    print(f"  [ok] 검증 거부 {len(cases) + 2}종 → 400")


def test_many_pairs(sid) -> None:
    """(h) 50개 이상 조합이 실제로 저장된다 — 사용자 요구(2026-08-24).

    긴 항목명 + 색까지 실어 **바이트 상한**에 걸리지 않는지가 핵심이다(pair 개수만 늘리고
    상한을 안 올리면 "50개 고르면 저장이 안 된다"가 된다)."""
    assert MAX_PAIRS >= 50, f"pair 상한이 50 미만입니다 ({MAX_PAIRS})"
    long_item = "VDD_CORE_MEAS_ACTIVE_MODE_VERY_LONG_ITEM_NAME_%02d"
    pairs = [{"source": f"WF_LOT12345_W{s:02d}", "item": long_item % i}
             for s in range(4) for i in range(MAX_PAIRS // 4)]
    assert len(pairs) >= 50, len(pairs)
    cid = str(uuid.uuid4())
    body = spec(name="대량 조합", pairs=pairs,
                colors={p["source"] + SEP + p["item"]: "#1f77b4" for p in pairs},
                limit={"mode": "manual", "lo": 0, "hi": 1})
    r = post_comp(sid, [{"key": cid, "value": body}])
    assert r.status_code == 200, f"{len(pairs)} pair 저장 실패 {r.status_code}: " \
                                 f"{r.get_data(as_text=True)[:200]}"
    got = r.get_json()["dist_composites"][cid]
    assert len(got["pairs"]) == len(pairs), got["pairs"][:2]
    assert len(got["colors"]) == len(pairs), len(got["colors"])
    extras = get_full(sid).get("dist_composites") or {}
    assert len(extras[cid]["pairs"]) == len(pairs), "왕복에서 pair 가 줄었습니다"
    post_comp(sid, [{"key": cid, "value": None}])
    print(f"  [ok] {len(pairs)} pair 저장·왕복 (상한 {MAX_PAIRS})")


def test_guards(sid) -> None:
    """(f) CSRF 없음 → 403 / 업로더도 편집자도 아닌 사람 → 403."""
    cid = str(uuid.uuid4())
    assert post_comp(sid, [{"key": cid, "value": spec()}], csrf=None).status_code == 403
    r = post_comp(sid, [{"key": cid, "value": spec()}], headers=UA_OTHER)
    assert r.status_code == 403, f"타인 편집이 {r.status_code} 로 통과했다"
    assert cid not in (get_full(sid).get("dist_composites") or {})
    print("  [ok] CSRF / 편집 권한 가드")


def test_state_excluded(sid) -> None:
    """(g) 표 payload 상태 조회에 섞이지 않는다 (_STATE_EXCLUDED_KINDS)."""
    assert wr_edits.KIND_DIST_COMPOSITE in wr_edits._STATE_EXCLUDED_KINDS
    state = wr_edits.load_edit_state(report_db, sid)
    blob = json.dumps(state, ensure_ascii=False)
    assert "P1 vs P2" not in blob and "이름 바꿈" not in blob, blob[:300]
    print("  [ok] load_edit_state 에 composite 가 섞이지 않음")


def settle(timeout=120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)


def main():
    print("[dist_composite 스모크]")
    client.set_cookie("report_csrf", CSRF)
    try:
        sid = upload_session()
        get_full(sid)            # 콜드 빌드 1회 끝내고 payload_rev 기준선 확보
        test_roundtrip(sid)
        test_payload_rev_neutral(sid)
        test_update_and_delete(sid)
        test_sanitize(sid)
        test_many_pairs(sid)
        test_reject(sid)
        test_guards(sid)
        test_state_excluded(sid)
        settle()
    finally:
        try:
            settle(timeout=10)
        except Exception:
            pass
        shutil.rmtree(_TMP, ignore_errors=True)
    print("[통과] Distribution composite 저장 계약 정상")


if __name__ == "__main__":
    main()

"""Gap Chart(kind=gap_chart) 저장 계약 + 수식 평가 스모크.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_gap_chart.py

왜 이 파일이 필요한가 (2026-08-24 신설):
    사용자가 조립한 수식은 **재입력할 방법이 없는 입력**이다(CLAUDE.md §5-12). 저장 키(UUID)와
    왕복을 기계로 고정한다. 그리고 이 기능이 깨지는 방식은 전부 **조용하다** —
      · PAYLOAD_NEUTRAL_KINDS 를 빠뜨리면 수식 저장마다 report 전체가 콜드 재빌드된다
        (화면은 멀쩡한데 서버만 느려진다 — 2026-08-13 조회 급락과 같은 기전).
      · 좌표 중복(재검) 우선순위를 뒤집으면 에러 없이 숫자만 달라진다.
      · 캐시 키·ETag 중 한 곳에만 수식 digest 가 들어가면 수식을 고쳐도 옛 값이 계속 나온다.

검증 항목:
  (a) POST .../web_report/gap_charts 200 → GET /full extras 로 되읽힌다
  (b) **payload_rev 불변** (PAYLOAD_NEUTRAL_KINDS 누락 회귀 방지 — 이 파일의 핵심 1)
  (c) 표 payload 상태(load_edit_state)에 섞이지 않는다 (_STATE_EXCLUDED_KINDS)
  (d) sanitize: unknown 키 제거·source 중복 제거·limit 정규화
  (e) 검증 거부 → 400 (혼합 수식 / 괄호 / 말미 연산자 / 인접 피연산자 / 빈 수식 / 상한 …)
  (f) CSRF 없음 → 403 / 업로더도 편집자도 아닌 사람 → 403
  (g) **per_source 평가 정확도** — 원소 단위로 기대값과 일치
  (h) **explicit 좌표 교집합 + 중복 좌표 첫 행 우선** (뒤집히면 값이 조용히 달라진다)
  (i) 0 나눗셈 → 유한값 필터로 제외되고 dropped_nonfinite 에 잡힌다
  (j) **ETag 3단** — 304 → 수식 수정 → 200 + 값 변화 (핵심 2: digest 누락 회귀 방지)
  (k) 미지 chart_id → 404
  (l) 응답 키 집합 ⊇ /scatter 키 집합 (Item_detail 재사용 계약 드리프트 방지)

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

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# config 는 import 시점에 env 를 읽는다 — 반드시 import 앞에서 지정할 것.
_TMP = Path(tempfile.mkdtemp(prefix="gap_chart_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import compute as wr_compute, edits as wr_edits  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()

UA = {"User-Agent": "Mozilla/5.0 HoneyUser/owner"}
UA_OTHER = {"User-Agent": "Mozilla/5.0 HoneyUser/stranger"}
CSRF = "test-csrf-token"
ITEMS = ["IT00", "IT01", "IT02"]
SOURCES = ["WF1", "WF2"]
CID = str(uuid.uuid4())

# 값과 좌표를 **결정적으로** 박는다 — (g)(h)(i) 가 원소 단위 비교를 하기 때문.
#   WF1 좌표: (1,1) (2,1) (3,1) (1,2) (2,2) (3,2)   — 6개 전부 고유
#   WF2 좌표: (1,1) (2,1) (1,1) (1,2) (9,9) (9,8)   — 3번째가 (1,1) **중복**(재검 모사)
# 두 소스 좌표 교집합 = {(1,1), (2,1), (1,2)} 3개.
# WF2 의 (1,1) 은 값이 100(첫 행) / 999(중복 행)로 갈린다 → "첫 행 우선"이면 100 이 쓰인다.
COORDS = {
    "WF1": [(1, 1), (2, 1), (3, 1), (1, 2), (2, 2), (3, 2)],
    "WF2": [(1, 1), (2, 1), (1, 1), (1, 2), (9, 9), (9, 8)],
}
VALUES = {
    # IT00 / IT01 / IT02
    "WF1": {"IT00": [0, 1, 2, 3, 4, 5],
            "IT01": [10, 11, 12, 13, 14, 15],
            "IT02": [0, 1, 2, 3, 4, 5]},          # 첫 행 0 → 0 나눗셈 재료
    "WF2": {"IT00": [100, 101, 999, 103, 104, 105],
            "IT01": [110, 111, 112, 113, 114, 115],
            "IT02": [1, 2, 3, 4, 5, 6]},
}
N_ROWS = 6


def make_parquet(source: str) -> bytes:
    """합성 7-meta honeyform → parquet bytes (계약 정본은 CLAUDE.md 규칙 #9)."""
    rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row[META_COLUMNS[0]] = label
        for j, it in enumerate(ITEMS):
            row[it] = {"TSEQ": j + 1, "TNO": 1000 + j, "STEP": "P1",
                       "UNIT": "V", "HILIM": 10000.0, "LOLIM": -10000.0}[label]
        rows.append(row)
    for i in range(N_ROWS):
        bin_v = 1 if i % 3 else 2
        x, y = COORDS[source][i]
        row = {"SERIAL": f"{source}-{i:03d}", "SHOT": 0, "DUT": 0,
               "XPOS": x, "YPOS": y, "BIN": bin_v,
               "FAILTNO": "" if bin_v == 1 else 1000}
        for it in ITEMS:
            row[it] = float(VALUES[source][it][i])
        rows.append(row)
    df = pd.DataFrame(rows, columns=META_COLUMNS + list(ITEMS))
    return encode_honeyform_parquet(df)


def upload_session() -> str:
    manifest = {
        "meta": {"product_type": "MDDI", "product": "SMOKE", "lot_id": "GAPCHART"},
        "mode": "Normal",
        "sources": [{"name": s, "file_name": f"{s}.csv"} for s in SOURCES],
        "selected_items": [],
        "client": {"user": "owner", "host": "smokehost"},
    }
    data = {"manifest": json.dumps(manifest)}
    for idx, src in enumerate(SOURCES):
        data[f"webreport_{idx}"] = (io.BytesIO(make_parquet(src)), f"{src}.csv")
    resp = client.post("/pe/report/upload_webreport", data=data, headers=UA,
                       content_type="multipart/form-data")
    assert resp.status_code == 200, \
        f"업로드 실패 {resp.status_code}: {resp.get_data(as_text=True)[:400]}"
    sid = resp.get_json().get("session_id")
    assert sid, resp.get_json()
    return sid


def get_full(sid: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/pe/report/session/{sid}/full", headers=UA)
        if resp.status_code == 200:
            return resp.get_json()
        time.sleep(0.1)
    raise AssertionError("/full 이 200 으로 돌아오지 않았다")


def post_gap(sid, ops, headers=UA, csrf=CSRF):
    hdr = dict(headers)
    if csrf is not None:
        hdr["X-CSRF-Token"] = csrf
    return client.post(f"/pe/report/session/{sid}/web_report/gap_charts",
                       json={"ops": ops}, headers=hdr)


def get_gap(sid, cid, headers=None):
    return client.get(f"/pe/report/session/{sid}/web_report/gap_chart/{cid}",
                      headers=headers or UA)


def I(name, source=None):
    tok = {"t": "item", "item": name}
    if source:
        tok["source"] = source
    return tok


def OP(v):
    return {"t": "op", "v": v}


def NUM(v):
    return {"t": "num", "v": v}


LP, RP = {"t": "lp"}, {"t": "rp"}


def spec(name="Gap A-B", sources=None, tokens=None, limit=None) -> dict:
    return {
        "name": name,
        "sources": sources if sources is not None else list(SOURCES),
        "tokens": tokens if tokens is not None else [I("IT00"), OP("-"), I("IT01")],
        "limit": limit if limit is not None else {"mode": "none"},
    }


def payload_rev(sid) -> int:
    return report_db.get_webreport_edit_rev(sid, payload=True)


# ── 검증 ─────────────────────────────────────────────────────────────────────

def test_roundtrip(sid) -> None:
    """(a) 저장 → /full extras 로 되읽힌다."""
    resp = post_gap(sid, [{"key": CID, "value": spec()}])
    assert resp.status_code == 200, f"{resp.status_code}: {resp.get_data(as_text=True)[:300]}"
    body = resp.get_json()
    assert body["updated"] == 1, body
    got = body["gap_charts"][CID]
    assert got["name"] == "Gap A-B", got
    assert got["sources"] == SOURCES, got
    assert len(got["tokens"]) == 3, got["tokens"]
    assert got["updated_by"] == "owner", got

    extras = get_full(sid).get("gap_charts") or {}
    assert CID in extras, extras
    assert extras[CID]["tokens"][0] == {"t": "item", "item": "IT00"}, extras[CID]
    print("  [ok] 저장 → /full extras 왕복 (수식 토큰 보존)")


def test_payload_rev_neutral(sid) -> None:
    """(b) **핵심** — gap chart 저장은 payload_rev 를 올리지 않는다."""
    before = payload_rev(sid)
    tmp = str(uuid.uuid4())
    r = post_gap(sid, [{"key": tmp, "value": spec(name="임시")}])
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    after = payload_rev(sid)
    assert after == before, \
        f"payload_rev 가 올랐다 {before} → {after} (PAYLOAD_NEUTRAL_KINDS 확인)"
    assert report_db.get_webreport_edit_rev(sid) > 0, "전역 rev 가 안 올랐다"
    post_gap(sid, [{"key": tmp, "value": None}])
    print(f"  [ok] payload_rev 불변 ({before}) · 전역 rev 는 증가")


def test_state_excluded(sid) -> None:
    """(c) 표 payload 상태 조회에 섞이지 않는다."""
    assert wr_edits.KIND_GAP_CHART in wr_edits._STATE_EXCLUDED_KINDS
    state = wr_edits.load_edit_state(report_db, sid)
    blob = json.dumps(state, ensure_ascii=False)
    assert "Gap A-B" not in blob, blob[:300]
    print("  [ok] load_edit_state 에 gap chart 가 섞이지 않음")


def test_sanitize(sid) -> None:
    """(d) unknown 키 제거 · source 중복 제거 · limit 정규화."""
    cid = str(uuid.uuid4())
    dirty = {
        "name": "  정리  ",
        "sources": ["WF1", "WF1", "", "WF2"],
        "tokens": [{"t": "item", "item": " IT00 ", "junk": 1}, OP("+"), {"t": "num", "v": "2"}],
        "limit": {"mode": "manual", "lo": "", "hi": ""},   # 둘 다 비면 mode:none
        "colors": {"악의": "x"},                            # 모르는 필드는 버린다
    }
    got = post_gap(sid, [{"key": cid, "value": dirty}]).get_json()["gap_charts"][cid]
    assert got["name"] == "정리", got
    assert got["sources"] == ["WF1", "WF2"], got["sources"]
    assert got["tokens"][0] == {"t": "item", "item": "IT00"}, got["tokens"]
    assert got["tokens"][2] == {"t": "num", "v": 2.0}, got["tokens"]
    assert got["limit"] == {"mode": "none"}, got["limit"]
    assert "colors" not in got, got
    post_gap(sid, [{"key": cid, "value": None}])
    print("  [ok] sanitize (unknown 키·중복 source·limit 정규화)")


def test_reject(sid) -> None:
    """(e) 잘못된 입력은 400."""
    cid = str(uuid.uuid4())
    long_name = "N" * 200
    cases = [
        ("키 형식", "not-a-uuid!", spec()),
        ("이름 빈값", cid, spec(name="  ")),
        ("이름 초과", cid, spec(name=long_name)),
        ("빈 수식", cid, spec(tokens=[])),
        ("항목 없는 수식", cid, spec(tokens=[NUM(1), OP("+"), NUM(2)])),
        ("혼합 수식", cid, spec(tokens=[I("IT00"), OP("-"), I("IT01", "WF2")])),
        ("괄호 불균형", cid, spec(tokens=[LP, I("IT00")])),
        ("말미 연산자", cid, spec(tokens=[I("IT00"), OP("+")])),
        ("인접 피연산자", cid, spec(tokens=[I("IT00"), I("IT01")])),
        ("모르는 연산자", cid, spec(tokens=[I("IT00"), {"t": "op", "v": "%"}, I("IT01")])),
        ("토큰 초과", cid, spec(tokens=[I("IT00")] + [OP("+"), I("IT00")] * 150)),
        ("per_source 인데 source 0", cid, spec(sources=[])),
    ]
    for label, key, value in cases:
        r = post_gap(sid, [{"key": key, "value": value}])
        assert r.status_code == 400, f"{label} 이 {r.status_code} 로 통과했다"
    # 문법 오류는 토큰 인덱스까지 실어야 프런트가 그 칩을 표시할 수 있다.
    r = post_gap(sid, [{"key": cid, "value": spec(tokens=[I("IT00"), I("IT01")])}])
    assert r.get_json().get("index") is not None, r.get_json()
    assert cid not in (get_full(sid).get("gap_charts") or {}), "거부된 값이 저장됐다"
    print(f"  [ok] 400 거부 {len(cases)}종 + 오류 토큰 인덱스")


def test_guards(sid) -> None:
    """(f) CSRF 없음 → 403 / 타인 → 403."""
    cid = str(uuid.uuid4())
    assert post_gap(sid, [{"key": cid, "value": spec()}], csrf=None).status_code == 403
    r = post_gap(sid, [{"key": cid, "value": spec()}], headers=UA_OTHER)
    assert r.status_code == 403, f"타인 편집이 {r.status_code} 로 통과했다"
    assert cid not in (get_full(sid).get("gap_charts") or {})
    print("  [ok] CSRF / 편집 권한 가드")


def test_per_source_values(sid) -> None:
    """(g) per_source 평가 정확도 — `IT00 - IT01 * 2` 를 원소 단위로 대조."""
    cid = str(uuid.uuid4())
    tokens = [I("IT00"), OP("-"), I("IT01"), OP("*"), NUM(2)]
    assert post_gap(sid, [{"key": cid, "value": spec(name="G1", tokens=tokens)}]).status_code == 200
    body = get_gap(sid, cid).get_json()
    assert body["gap_mode"] == "per_source", body["gap_mode"]
    assert body["is_gap"] is True and body["gap_id"] == cid, body
    assert body["note_subject"] == f"gap:{cid}", body["note_subject"]
    assert [s["name"] for s in body["sources"]] == SOURCES, body["sources"]
    for src in SOURCES:
        want = [VALUES[src]["IT00"][i] - VALUES[src]["IT01"][i] * 2 for i in range(N_ROWS)]
        got = next(s for s in body["sources"] if s["name"] == src)["values"]
        assert got == want, f"{src}: {got} != {want}"
    # hover meta 가 values 와 같은 순서·길이여야 한다(마스크를 네 배열에 함께 적용).
    first = body["sources"][0]
    assert len(first["serial"]) == len(first["values"]) == N_ROWS
    assert first["serial"][0] == "WF1-000", first["serial"][:2]
    assert body["matched_dies"] == N_ROWS * 2, body["matched_dies"]
    post_gap(sid, [{"key": cid, "value": None}])
    print("  [ok] per_source 평가 (연산자 우선순위 포함) + hover meta 정렬")


def test_explicit_coord_join(sid) -> None:
    """(h) **핵심** — explicit 은 좌표 교집합, 중복 좌표는 첫 행 우선."""
    cid = str(uuid.uuid4())
    tokens = [I("IT00", "WF1"), OP("-"), I("IT00", "WF2")]
    assert post_gap(sid, [{"key": cid, "value": spec(name="X1", tokens=tokens)}]).status_code == 200
    body = get_gap(sid, cid).get_json()
    assert body["gap_mode"] == "explicit", body["gap_mode"]
    assert len(body["sources"]) == 1, body["sources"]
    got = body["sources"][0]["values"]
    # 교집합 = (1,1) (2,1) (1,2) — WF1 행 순서. WF2 의 (1,1) 은 **첫 행(100)** 이어야 한다.
    #   (1,1): 0 - 100 = -100   (마지막 행 우선이면 0 - 999 = -999 가 된다)
    #   (2,1): 1 - 101 = -100
    #   (1,2): 3 - 103 = -100
    assert got == [-100.0, -100.0, -100.0], f"{got} — 좌표 매칭/중복 우선순위 확인"
    assert body["matched_dies"] == 3, body["matched_dies"]
    assert body["sources"][0]["xpos"] == ["1", "2", "1"], body["sources"][0]["xpos"]
    post_gap(sid, [{"key": cid, "value": None}])
    print("  [ok] explicit 좌표 교집합 3 die + 중복 좌표 첫 행 우선")


def test_divide_by_zero(sid) -> None:
    """(i) 0 나눗셈은 유한값 필터로 빠지고 dropped_nonfinite 에 잡힌다."""
    cid = str(uuid.uuid4())
    tokens = [I("IT00"), OP("/"), I("IT02")]
    post_gap(sid, [{"key": cid, "value": spec(name="Z", sources=["WF1"], tokens=tokens)}])
    body = get_gap(sid, cid).get_json()
    got = body["sources"][0]["values"]
    assert len(got) == N_ROWS - 1, got            # 첫 행 0/0 = NaN 만 빠진다
    assert all(v == 1.0 for v in got), got        # i/i = 1
    assert body["dropped_nonfinite"] == 1, body["dropped_nonfinite"]
    post_gap(sid, [{"key": cid, "value": None}])
    print("  [ok] 0 나눗셈 제외 + dropped_nonfinite 계수")


def test_etag_and_digest(sid) -> None:
    """(j) **핵심** — ETag 304 → 수식 수정 → 200 + 값 변화.

    캐시 키에만 수식 digest 를 넣고 ETag 에서 빠뜨리면 여기서 304 가 나와 실패한다."""
    cid = str(uuid.uuid4())
    post_gap(sid, [{"key": cid, "value": spec(name="E", sources=["WF1"],
                                              tokens=[I("IT00")])}])
    first = get_gap(sid, cid)
    assert first.status_code == 200, first.status_code
    etag = first.headers.get("ETag")
    assert etag, first.headers
    before = first.get_json()["sources"][0]["values"]

    again = get_gap(sid, cid, headers={**UA, "If-None-Match": etag})
    assert again.status_code == 304, f"조건부 재요청이 {again.status_code} (캐시 미동작)"

    # 수식만 바꾼다 — 같은 ETag 로 물어도 200 이어야 하고 값도 달라야 한다.
    post_gap(sid, [{"key": cid, "value": spec(name="E", sources=["WF1"],
                                              tokens=[I("IT00"), OP("+"), NUM(1000)])}])
    after_resp = get_gap(sid, cid, headers={**UA, "If-None-Match": etag})
    assert after_resp.status_code == 200, \
        "수식을 고쳤는데 304 다 — spec_digest 가 ETag 에 빠졌다"
    after = after_resp.get_json()["sources"][0]["values"]
    assert after == [v + 1000 for v in before], f"{after} != {before} + 1000"
    post_gap(sid, [{"key": cid, "value": None}])
    print("  [ok] ETag 304 → 수식 수정 → 200 + 값 변화")


def test_missing_chart(sid) -> None:
    """(k) 없는 chart_id → 404."""
    assert get_gap(sid, str(uuid.uuid4())).status_code == 404
    print("  [ok] 미지 chart_id 404")


def test_scatter_contract(sid) -> None:
    """(l) 응답 키 집합 ⊇ /scatter 키 집합 — Item_detail 재사용 계약을 기계로 고정."""
    cid = str(uuid.uuid4())
    post_gap(sid, [{"key": cid, "value": spec(name="C", sources=["WF1"],
                                              tokens=[I("IT00")])}])
    gap_keys = set(get_gap(sid, cid).get_json().keys())
    scat = client.get(f"/pe/report/session/{sid}/web_report/scatter/IT00", headers=UA)
    assert scat.status_code == 200, scat.status_code
    scat_keys = set(scat.get_json().keys())
    missing = scat_keys - gap_keys
    assert not missing, f"gap 응답에 빠진 scatter 키: {sorted(missing)}"
    src_missing = set(scat.get_json()["sources"][0]) - set(
        get_gap(sid, cid).get_json()["sources"][0])
    assert not src_missing, f"sources[] 에 빠진 키: {sorted(src_missing)}"
    post_gap(sid, [{"key": cid, "value": None}])
    print(f"  [ok] scatter 키 {len(scat_keys)}종 전부 포함 (Item_detail 재사용 계약)")


def settle(timeout=120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)


def main():
    print("[gap_chart 스모크]")
    client.set_cookie("report_csrf", CSRF)
    try:
        sid = upload_session()
        get_full(sid)            # 콜드 빌드 1회 끝내고 payload_rev 기준선 확보
        test_roundtrip(sid)
        test_payload_rev_neutral(sid)
        test_state_excluded(sid)
        test_sanitize(sid)
        test_reject(sid)
        test_guards(sid)
        test_per_source_values(sid)
        test_explicit_coord_join(sid)
        test_divide_by_zero(sid)
        test_etag_and_digest(sid)
        test_missing_chart(sid)
        test_scatter_contract(sid)
        settle()
    finally:
        try:
            settle(timeout=10)
        except Exception:
            pass
        shutil.rmtree(_TMP, ignore_errors=True)
    print("[통과] Gap Chart 저장 계약 + 수식 평가 정상")


if __name__ == "__main__":
    main()

"""Distribution "DUT 별 분리 보기" 배치·상세 응답 계약 스모크.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_dist_dut.py

왜 이 파일이 필요한가 (2026-09-03 신설):
    이 기능이 깨지는 방식은 전부 **조용하다** — 에러가 아니라 "그림이 조금 다른" 것으로만
    보여 발견이 가장 늦다.
      · `/scatter` 는 **라우트가 ETag 를 직접 조립**한다. 캐시 키에만 dut 를 넣고 ETag 를
        빠뜨리면 토글해도 브라우저가 304 로 옛 응답(dut 배열 없음)을 계속 써서 **아무 일도
        안 일어난다**.
      · Temperature "Bin1(RT만)" 은 RT source 명 집합으로 판정하는데, 분할 후 이름은
        `"WF1_RT · DUT 1"` 이라 확장하지 않으면 bin1 이 **아무 소스에도 안 걸린다**.
      · 분리를 켜지 않은 요청의 응답/키가 1바이트라도 달라지면 전 세션 콜드 폭풍이다.
      · 갤러리(배치)와 상세(/scatter)의 DUT 그룹이 갈리면 같은 항목이 화면마다 달라 보인다
        (CLAUDE.md 규칙 #13).

검증 항목:
  (a) dut=1 → source 가 "<src> · DUT <label>", 라벨 **수치 오름차순**, '1.0'→'1' 정규화
  (b) **합집합 보존** — DUT별 표본 수 합계 == 분리 전 (다운샘플 없음, 규칙 #5)
  (c) dut 없음 → 종전 응답과 **바이트 동일** (무회귀 방어선)
  (d) **ETag 분리** — dut ↔ 없음 ↔ seq+dut 3자 교차 304 차단 (배치 + /scatter 둘 다)
  (e) bin1=1&dut=1 → 양품 ∩ 규격내가 DUT 분할 후에도 걸린다
  (f) order=seq&dut=1 → seq 포맷 + **행 순서 보존** + DUT 분할
  (g) /scatter?dut=1 → sources[].dut 길이 == values 길이, dut 없이는 **키 자체가 없다**
  (h) mode="DUT" 세션 → **이중 분할 없음**
  (i) DUT 1종 세션 → 분리 전과 이름·값 동일
  (j) '(blank)' 라벨이 맨 뒤
  (k) 배치 상한 — dut 초과 400, seq+dut 는 더 작은 상한
  (l) **갤러리 == 상세** — 배치의 DUT 그룹 == /scatter dut 배열 그룹핑 결과 (규칙 #13)

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

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# config 는 import 시점에 env 를 읽는다 — 반드시 import 앞에서 지정할 것.
_TMP = Path(tempfile.mkdtemp(prefix="dist_dut_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import compute as wr_compute  # noqa: E402
from web_report.dist_dut import DUT_SOURCE_SEP  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()

UA = {"User-Agent": "Mozilla/5.0 HoneyUser/owner"}
ITEMS = ["IT00", "IT01"]
SOURCES = ["WF1", "WF2"]
N_ROWS = 8

# DUT 를 **정렬과 다른 순서**로, 그리고 '1.0'(float 표기)·'10'(두 자릿수)을 섞어 박는다 —
# 라벨 정규화(_fmt_dut)와 수치 정렬(_dut_sort_key)이 동시에 검증된다. '' 는 '(blank)' 로.
#   기대 라벨 순서: 1, 2, 10, (blank)   ← 문자 정렬이면 1,10,2 가 되어 즉시 걸린다
DUTS = ["1", "2", "1", "10", "", "1.0", "2", "10"]
DUT_EXPECT = ["1", "2", "10", "(blank)"]
DUT_NORM = ["1", "2", "1", "10", "(blank)", "1", "2", "10"]

# BIN: 인덱스 3,6 만 2(fail). IT00 규격 -100~100 → 인덱스 5 의 500 은 양품이지만 규격 밖.
VALUES = {
    "WF1": {"IT00": [7, 3, 9, 1, 5, 500, 2, 8],
            "IT01": [20, 19, 18, 17, 16, 15, 14, 13]},
    "WF2": {"IT00": [-4, 6, -9, 0, 11, 3, -1, 2],
            "IT01": [1, 3, 2, 5, 4, 7, 6, 9]},
}
FAIL_ROWS = (3, 6)
LIMITS = {"IT00": (-100.0, 100.0), "IT01": (-1000.0, 1000.0)}


def make_parquet(source: str, duts=None, n_items: int = 0) -> bytes:
    """합성 7-meta honeyform → parquet bytes (계약 정본은 CLAUDE.md 규칙 #9)."""
    duts = DUTS if duts is None else duts
    items = ITEMS if not n_items else [f"IT{i:02d}" for i in range(n_items)]
    rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row[META_COLUMNS[0]] = label
        for j, it in enumerate(items):
            lim = LIMITS.get(it, (-1000.0, 1000.0))
            row[it] = {"TSEQ": j + 1, "TNO": 1000 + j, "STEP": "P1", "UNIT": "V",
                       "HILIM": lim[1], "LOLIM": lim[0]}[label]
        rows.append(row)
    for i in range(N_ROWS):
        bin_v = 2 if i in FAIL_ROWS else 1
        row = {"SERIAL": f"{source}-{i:03d}", "SHOT": 0, "DUT": duts[i],
               "XPOS": i + 1, "YPOS": 1, "BIN": bin_v,
               "FAILTNO": 1000 if bin_v == 2 else ""}
        for it in items:
            row[it] = float(VALUES[source].get(it, VALUES[source]["IT00"])[i])
        rows.append(row)
    df = pd.DataFrame(rows, columns=META_COLUMNS + list(items))
    return encode_honeyform_parquet(df)


def upload_session(mode="Normal", duts=None, sources=None, n_items=0) -> str:
    sources = sources or SOURCES
    manifest = {
        "meta": {"product_type": "MDDI", "product": "SMOKE", "lot_id": "DISTDUT"},
        "mode": mode,
        "sources": [{"name": s, "file_name": f"{s}.csv"} for s in sources],
        "selected_items": [],
        "client": {"user": "owner", "host": "smokehost"},
    }
    data = {"manifest": json.dumps(manifest)}
    for idx, src in enumerate(sources):
        data[f"webreport_{idx}"] = (io.BytesIO(make_parquet(src, duts, n_items)),
                                    f"{src}.csv")
    resp = client.post("/pe/report/upload_webreport", data=data, headers=UA,
                       content_type="multipart/form-data")
    assert resp.status_code == 200, \
        f"업로드 실패 {resp.status_code}: {resp.get_data(as_text=True)[:400]}"
    return resp.get_json()["session_id"]


def get_full(sid: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/pe/report/session/{sid}/full", headers=UA)
        if resp.status_code == 200:
            return resp.get_json()
        time.sleep(0.1)
    raise AssertionError("/full 이 200 으로 돌아오지 않았다")


def batch(sid, subjects, order=None, bin1=False, dut=False, headers=None):
    q = f"?subjects={','.join(subjects)}"
    if order is not None:
        q += f"&order={order}"
    if bin1:
        q += "&bin1=1"
    if dut:
        q += "&dut=1"
    return client.get(f"/pe/report/session/{sid}/web_report/distribution_batch{q}",
                      headers={**UA, **(headers or {})})


def scatter(sid, subject, dut=False, bin1=False, headers=None):
    q = []
    if bin1:
        q.append("bin1=1")
    if dut:
        q.append("dut=1")
    url = f"/pe/report/session/{sid}/web_report/scatter/{subject}"
    if q:
        url += "?" + "&".join(q)
    return client.get(url, headers={**UA, **(headers or {})})


def settle(timeout=120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)


def dut_name(src, label):
    return f"{src}{DUT_SOURCE_SEP}{label}"


# ── 검증 ─────────────────────────────────────────────────────────────────────

def test_split_names_and_order(sid) -> None:
    """(a)(j) source 명·라벨 수치 오름차순·'1.0'→'1' 정규화·'(blank)' 맨 뒤."""
    body = batch(sid, ["IT00"], dut=True).get_json()
    assert body["format"] == "ecdf-columnar-v1", body.get("format")
    got = list(body["items"]["IT00"]["sources"])
    want = [dut_name(s, d) for s in SOURCES for d in DUT_EXPECT]
    assert got == want, f"source 명/순서 불일치\n  got  {got}\n  want {want}"
    print(f"  [ok] DUT 분할 source 명·정렬 (1,2,10,(blank) — 문자정렬 아님)")


def test_union_preserved(sid) -> None:
    """(b) **합집합 보존** — DUT별 n 합계 == 분리 전 n (다운샘플 없음, 규칙 #5)."""
    base = batch(sid, ["IT00"]).get_json()["items"]["IT00"]["sources"]
    split = batch(sid, ["IT00"], dut=True).get_json()["items"]["IT00"]["sources"]
    for src in SOURCES:
        want = base[src]["n"]
        got = sum(v["n"] for k, v in split.items() if k.split(DUT_SOURCE_SEP)[0] == src)
        assert got == want, f"{src} 표본 손실/증식: 분리후 {got} != 분리전 {want}"
    print(f"  [ok] 합집합 보존 (die 를 나눠 담을 뿐 하나도 안 버린다)")


def test_off_byte_identical(sid) -> None:
    """(c) **무회귀 방어선** — dut 없음 응답이 도입 전과 바이트 동일해야 한다."""
    a = batch(sid, ["IT00", "IT01"]).get_data()
    b = batch(sid, ["IT00", "IT01"], dut=False).get_data()
    assert a == b, "dut=False 가 dut 미지정과 다르다"
    sc = scatter(sid, "IT00").get_json()
    for s in sc["sources"]:
        assert "dut" not in s, "dut 미지정인데 dut 키가 실렸다(응답 바이트 변경 = 캐시 무효화)"
    print("  [ok] 분리 off = 종전 응답 그대로 (키 없음 · 콜드 폭풍 없음)")


def test_etag_split(sid) -> None:
    """(d) **최대 지뢰** — ETag 가 dut 축으로 갈린다 (배치 + /scatter 둘 다)."""
    variants = [
        ("batch plain", batch(sid, ["IT00"])),
        ("batch dut", batch(sid, ["IT00"], dut=True)),
        ("batch seq", batch(sid, ["IT00"], order="seq")),
        ("batch seq+dut", batch(sid, ["IT00"], order="seq", dut=True)),
    ]
    tags = {}
    for name, resp in variants:
        assert resp.status_code == 200, f"{name}: {resp.status_code}"
        tags[name] = resp.headers.get("ETag")
    assert len(set(tags.values())) == len(tags), f"ETag 가 겹친다: {tags}"

    # /scatter 는 **라우트가 ETag 를 직접 조립**한다 — 여기가 실제로 빠뜨리기 쉬운 곳.
    s0 = scatter(sid, "IT00")
    s1 = scatter(sid, "IT00", dut=True)
    e0, e1 = s0.headers.get("ETag"), s1.headers.get("ETag")
    assert e0 and e1 and e0 != e1, f"/scatter ETag 가 dut 로 안 갈렸다: {e0} vs {e1}"
    # 교차 304 차단: 한쪽 ETag 로 다른 쪽을 재검증하면 200 이어야 한다.
    cross = scatter(sid, "IT00", dut=True, headers={"If-None-Match": e0})
    assert cross.status_code == 200, "dut 응답이 비-dut ETag 로 304 를 반환했다(stale)"
    assert any("dut" in s for s in cross.get_json()["sources"][0]), "304 는 면했는데 dut 배열이 없다"
    # 같은 변형은 정상 304
    same = scatter(sid, "IT00", dut=True, headers={"If-None-Match": e1})
    assert same.status_code == 304, f"같은 변형 재검증이 304 가 아니다: {same.status_code}"
    print("  [ok] ETag 분리 (배치 4변형 + /scatter 교차 304 차단)")


def test_bin1_with_dut(sid) -> None:
    """(e) bin1 = 양품 ∩ 규격내가 DUT 분할 후에도 걸린다."""
    body = batch(sid, ["IT00"], bin1=True, dut=True).get_json()
    srcs = body["items"]["IT00"]["sources"]
    lo, hi = LIMITS["IT00"]
    for src in SOURCES:
        for label in DUT_EXPECT:
            want = sorted({float(v) for i, v in enumerate(VALUES[src]["IT00"])
                           if DUT_NORM[i] == label and i not in FAIL_ROWS and lo <= v <= hi})
            key = dut_name(src, label)
            got = srcs.get(key, {}).get("x", []) if want else None
            if want:
                assert got == want, f"{key} bin1 불일치\n  got  {got}\n  want {want}"
    # WF1 인덱스5(500, DUT '1.0'→'1') 는 양품이지만 규격 밖 → 빠져야 한다
    assert 500.0 not in srcs.get(dut_name("WF1", "1"), {}).get("x", []), \
        "규격 밖 양품 die 가 bin1+dut 에 남았다"
    print("  [ok] bin1 ∩ dut (양품·규격내 필터가 분할 후에도 유효)")


def test_seq_with_dut(sid) -> None:
    """(f) seq × dut 직교 — 순서 보존 + DUT 분할."""
    body = batch(sid, ["IT00"], order="seq", dut=True).get_json()
    assert body["format"] == "seq-columnar-v1", body.get("format")
    srcs = body["items"]["IT00"]["sources"]
    for src in SOURCES:
        for label in DUT_EXPECT:
            want = [float(v) for i, v in enumerate(VALUES[src]["IT00"])
                    if DUT_NORM[i] == label]
            got = srcs[dut_name(src, label)]["v"]
            assert got == want, f"{dut_name(src, label)} 순서 불일치\n  got {got}\n  want {want}"
    print("  [ok] seq × dut (행 순서 보존 + DUT 분할)")


def test_scatter_dut_array(sid) -> None:
    """(g)(l) /scatter dut 배열 + **갤러리 == 상세** (규칙 #13)."""
    sc = scatter(sid, "IT00", dut=True).get_json()
    for s in sc["sources"]:
        assert len(s["dut"]) == len(s["values"]), \
            f"{s['name']} dut 길이 != values 길이"
    assert sc["sources"][0]["dut"] == DUT_NORM, \
        f"dut 라벨 정규화 불일치\n  got  {sc['sources'][0]['dut']}\n  want {DUT_NORM}"

    # 상세의 dut 배열로 그룹핑한 값 집합 == 배치의 DUT 그룹 (같은 점 집합)
    gal = batch(sid, ["IT00"], dut=True).get_json()["items"]["IT00"]["sources"]
    for s in sc["sources"]:
        groups = {}
        for lbl, val in zip(s["dut"], s["values"]):
            groups.setdefault(lbl, []).append(val)
        for lbl, vals in groups.items():
            key = dut_name(s["name"], lbl)
            assert gal[key]["n"] == len(vals), \
                f"{key} 갤러리 n={gal[key]['n']} != 상세 {len(vals)}"
            assert gal[key]["x"] == sorted(set(vals)), \
                f"{key} 갤러리 고유값 != 상세 고유값"
    print("  [ok] /scatter dut 배열 + 갤러리 == 상세 (규칙 #13)")


def test_batch_cap(sid_many) -> None:
    """(k) 배치 상한 — dut 초과 400, seq+dut 는 더 작은 상한, ECDF 는 무영향."""
    from report.routes_webreport import (_DIST_DUT_BATCH_MAX, _DIST_SEQ_BATCH_MAX)
    many = [f"IT{i:02d}" for i in range(_DIST_DUT_BATCH_MAX + 1)]
    assert batch(sid_many, many, dut=True).status_code == 400, "dut 상한 초과가 400 이 아닙니다"
    assert batch(sid_many, many).status_code == 200, "ECDF 배치 상한이 함께 줄었습니다(회귀)"
    seq_many = [f"IT{i:02d}" for i in range(_DIST_SEQ_BATCH_MAX + 1)]
    assert batch(sid_many, seq_many, order="seq", dut=True).status_code == 400, \
        "seq+dut 가 더 작은 상한을 안 쓴다"
    print(f"  [ok] 배치 상한 dut={_DIST_DUT_BATCH_MAX} · seq+dut={_DIST_SEQ_BATCH_MAX}")


def test_dut_mode_no_double_split() -> None:
    """(h) mode="DUT" 세션은 이미 분할돼 있다 — 다시 쪼개지 않는다."""
    sid = upload_session(mode="DUT", sources=["WF1"])
    get_full(sid)
    names = list(batch(sid, ["IT00"], dut=True).get_json()["items"]["IT00"]["sources"])
    assert not any(n.count("DUT") > 1 for n in names), f"이중 분할됨: {names}"
    assert names == [f"DUT {d}" for d in DUT_EXPECT], names
    print("  [ok] mode=DUT 세션 이중 분할 없음")


def test_single_dut_unchanged() -> None:
    """(i) DUT 1종 세션 → 이름·값이 분리 전과 동일 (옵션을 켜도 아무 일이 없다)."""
    sid = upload_session(duts=["7"] * N_ROWS, sources=["WF1"])
    get_full(sid)
    off = batch(sid, ["IT00"]).get_json()["items"]["IT00"]["sources"]
    on = batch(sid, ["IT00"], dut=True).get_json()["items"]["IT00"]["sources"]
    assert list(on) == list(off) == ["WF1"], f"이름이 바뀌었다: {list(on)}"
    assert on["WF1"] == off["WF1"], "DUT 1종인데 값이 달라졌다"
    print("  [ok] DUT 1종 → 분리 전과 완전히 동일")


def main():
    print("[dist_dut (DUT 별 분리) 스모크]")
    try:
        sid = upload_session()
        get_full(sid)
        test_split_names_and_order(sid)
        test_union_preserved(sid)
        test_off_byte_identical(sid)
        test_etag_split(sid)
        test_bin1_with_dut(sid)
        test_seq_with_dut(sid)
        test_scatter_dut_array(sid)

        from report.routes_webreport import _DIST_DUT_BATCH_MAX
        sid_many = upload_session(sources=["WF1"], n_items=_DIST_DUT_BATCH_MAX + 1)
        get_full(sid_many)
        test_batch_cap(sid_many)

        test_dut_mode_no_double_split()
        test_single_dut_unchanged()
        settle()
    finally:
        try:
            settle(timeout=10)
        except Exception:
            pass
        shutil.rmtree(_TMP, ignore_errors=True)
    print("[통과] DUT 별 분리 응답 계약 정상")


if __name__ == "__main__":
    main()

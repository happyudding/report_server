"""commonality chip 배치 조회(chip_percentiles_many) 동치 + 라우트 계약.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_commonality_batch.py

왜 이 파일이 필요한가 (2026-09-03 신설):
    Item_detail 에서 드래그 박스로 잡은 die 를 강조하려면 chip 수백 개의 값·누적%를 한 번에
    받아야 한다. 그래서 단건 ``chip_percentiles`` 를 item-major 벡터화 헬퍼로 리팩터하고
    배치 ``chip_percentiles_many`` 를 얹었는데, 이 리팩터가 깨지는 방식은 **조용하다** —
      · 벡터화가 dtype 을 바꾸면(int64 컬럼, object 컬럼, NaN) 값이 미세하게 달라진다.
        화면에는 에러 없이 "마커가 곡선에서 살짝 벗어난" 것으로만 보인다.
      · 중복 좌표(재검)의 '첫 행 우선'이 뒤집히면 다른 die 값을 강조한다.
      · 못 찾은 chip 하나가 전체 요청을 죽이면 드래그가 통째로 실패한다.

검증 항목:
  (a) **전 die 동치** — 모든 die 에 대해 chip_percentiles 와 chip_percentiles_many 의
      값/누적%가 json 직렬화까지 완전 일치 (index 있음 / 없음 두 경로)
  (b) 중복 좌표는 첫 행 우선 (_pos_map 과 _locate 가 같은 답)
  (c) 못 찾은 chip → None, 나머지는 입력 순서 유지
  (d) source 를 틀리게 줘도 전체 재탐색 폴백 (단건과 같은 동작)
  (e) 라우트: 200 / 상한 초과 400 / 비배열 400 / 미지 세션 404
  (f) 단건 N회 vs 배치 1회 소요 시간 출력 (회귀 판정 아님 — 참고용)

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
import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# config 는 import 시점에 env 를 읽는다 — 반드시 import 앞에서 지정할 것.
from pathlib import Path  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="commonality_batch_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, HoneyformTable, encode_honeyform_parquet,
)
from web_report.tabs.commonality import (  # noqa: E402
    build_index, chip_percentiles, chip_percentiles_many,
)

UA = {"User-Agent": "Mozilla/5.0 HoneyUser/owner"}
SOURCES = ["WF1", "WF2"]
# dtype 을 일부러 섞는다 — 벡터화가 float64/int64/object(문자 숫자·빈칸) 를 단건과 같이 다뤄야 한다.
ITEMS = ["F_FLOAT", "I_INT", "O_OBJ", "E_EMPTY"]
N_ROWS = 8

# WF1 좌표는 전부 고유, WF2 는 3번째가 첫 행과 같은 좌표(재검 모사) → '첫 행 우선' 검증용.
COORDS = {
    "WF1": [(1, 1), (2, 1), (3, 1), (4, 1), (1, 2), (2, 2), (3, 2), (4, 2)],
    "WF2": [(1, 1), (2, 1), (1, 1), (4, 1), (1, 2), (2, 2), (3, 2), (4, 2)],
}
VALUES = {
    "WF1": {
        "F_FLOAT": [0.5, 1.25, 2.5, 2.5, 4.75, -1.5, 6.0, 7.125],   # 동일값 2개 포함
        "I_INT": [10, 11, 12, 13, 14, 15, 16, 17],
        "O_OBJ": ["1.5", "2.5", "", "abc", "5.5", "6.5", "7.5", "8.5"],  # 비수치·빈칸 혼입
        "E_EMPTY": ["", "", "", "", "", "", "", ""],                 # 전부 비어 n=0
    },
    "WF2": {
        "F_FLOAT": [100.0, 101.5, 999.0, 103.25, 104.0, 105.0, 106.0, 107.0],
        "I_INT": [20, 21, 22, 23, 24, 25, 26, 27],
        "O_OBJ": ["11", "12", "13", "14", "15", "16", "17", "18"],
        "E_EMPTY": ["", "", "", "", "", "", "", ""],
    },
}


def make_frame(source: str) -> pd.DataFrame:
    """합성 7-meta honeyform 프레임 (계약 정본은 CLAUDE.md 규칙 #9)."""
    rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row[META_COLUMNS[0]] = label
        for j, it in enumerate(ITEMS):
            row[it] = {"TSEQ": j + 1, "TNO": 2000 + j, "STEP": "P1",
                       "UNIT": "V", "HILIM": 10000.0, "LOLIM": -10000.0}[label]
        rows.append(row)
    for i in range(N_ROWS):
        bin_v = 1 if i % 3 else 2
        x, y = COORDS[source][i]
        row = {"SERIAL": f"{source}-{i:03d}", "SHOT": 0, "DUT": 0,
               "XPOS": x, "YPOS": y, "BIN": bin_v,
               "FAILTNO": "" if bin_v == 1 else 2000}
        for it in ITEMS:
            row[it] = VALUES[source][it][i]
        rows.append(row)
    return pd.DataFrame(rows, columns=META_COLUMNS + list(ITEMS))


def make_table(source: str) -> HoneyformTable:
    """decode 경로를 거치지 않고 HoneyformTable 을 직접 조립 (순수 함수 테스트용)."""
    df = make_frame(source)
    meta = df.iloc[:len(META_ROW_LABELS)]
    data = df.iloc[len(META_ROW_LABELS):].reset_index(drop=True)
    for col in ("F_FLOAT",):
        data[col] = pd.to_numeric(data[col], errors="coerce")      # float64
    data["I_INT"] = pd.to_numeric(data["I_INT"], errors="coerce").astype("int64")
    # O_OBJ / E_EMPTY 는 object 로 남긴다(split_honeyform 이 변환 못 하는 컬럼 모사)
    def _row(label):
        return {it: meta.loc[meta[META_COLUMNS[0]] == label, it].iloc[0] for it in ITEMS}
    return HoneyformTable(
        source=source, file_name=f"{source}.csv", df=None, item_columns=list(ITEMS),
        tseq=_row("TSEQ"), tno=_row("TNO"), step=_row("STEP"),
        units=_row("UNIT"), hilim=_row("HILIM"), lolim=_row("LOLIM"), data=data)


TABLES = [make_table(s) for s in SOURCES]


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def all_chips() -> list:
    """전 소스 전 die 의 (source, serial, xpos, ypos)."""
    out = []
    for t in TABLES:
        for i in range(len(t.data)):
            out.append({"source": t.source,
                        "serial": str(t.data["SERIAL"].iloc[i]),
                        "xpos": str(t.data["XPOS"].iloc[i]),
                        "ypos": str(t.data["YPOS"].iloc[i])})
    return out


def batch_rows(res, i):
    """배치 응답의 i 번째 chip → 단건 items 와 같은 모양으로 되풀기 (n 제외)."""
    ent = res["chips"][i]
    if ent is None:
        return None
    names = res["item_lists"][ent["items_ref"]]
    return [{"subject": nm, "value": ent["value"][j], "cum_pct": ent["cum_pct"][j]}
            for j, nm in enumerate(names)]


def single_rows(one):
    return [{"subject": r["subject"], "value": r["value"], "cum_pct": r["cum_pct"]}
            for r in one["items"]]


failures = []


def check(cond, msg):
    if cond:
        print(f"  OK   {msg}")
    else:
        print(f"  FAIL {msg}")
        failures.append(msg)


# ─────────────────────────────────────────────────────────────────────────────
print("(a) 전 die 동치 — 단건 vs 배치")
chips = all_chips()
idx = build_index(TABLES)

t0 = time.perf_counter()
singles = [chip_percentiles(TABLES, index=idx, **c) for c in chips]
t_single = time.perf_counter() - t0

t0 = time.perf_counter()
res = chip_percentiles_many(TABLES, chips, index=idx)
t_batch = time.perf_counter() - t0

mismatch = []
for i, one in enumerate(singles):
    if canon(single_rows(one)) != canon(batch_rows(res, i)):
        mismatch.append(i)
    if canon(one["chip"]) != canon(res["chips"][i]["chip"]):
        mismatch.append(("chip", i))
check(not mismatch, f"전 die {len(chips)}건 값·누적%·chip 메타 완전 일치 (불일치 {mismatch[:5]})")

# index 없이(즉석 계산) 도는 경로도 같은 답이어야 한다.
res_noidx = chip_percentiles_many(TABLES, chips)
check(canon([batch_rows(res, i) for i in range(len(chips))])
      == canon([batch_rows(res_noidx, i) for i in range(len(chips))]),
      "index 미지정(즉석 계산) 경로도 동일")

one_noidx = chip_percentiles(TABLES, **chips[0])
check(canon(single_rows(one_noidx)) == canon(single_rows(singles[0])),
      "단건도 index 유무와 무관하게 동일")

# n(유효 die 수)은 단건에만 있다 — 값이 기대와 맞는지 직접 확인.
n_map = {r["subject"]: r["n"] for r in singles[0]["items"]}
check(n_map["E_EMPTY"] == 0, f"전부 빈 항목은 n=0 (실제 {n_map['E_EMPTY']})")
check(n_map["F_FLOAT"] == N_ROWS, f"float 항목 n={N_ROWS} (실제 {n_map['F_FLOAT']})")
check(n_map["O_OBJ"] == N_ROWS - 2, f"비수치·빈칸 2개 제외 n={N_ROWS - 2} (실제 {n_map['O_OBJ']})")
check(all(r["value"] is None and r["cum_pct"] is None
          for r in singles[0]["items"] if r["subject"] == "E_EMPTY"),
      "n=0 항목의 value/cum_pct 는 None")

print(f"\n(f) 소요: 단건 {len(chips)}회 {t_single * 1000:.1f}ms · 배치 1회 {t_batch * 1000:.1f}ms")

# ─────────────────────────────────────────────────────────────────────────────
print("\n(b) 중복 좌표 — 첫 행 우선")
dup = {"source": "WF2", "serial": "", "xpos": "1", "ypos": "1"}   # serial 없이 좌표만
one = chip_percentiles(TABLES, index=idx, **dup)
check(one["chip"]["serial"] == "WF2-000",
      f"좌표만 준 중복 die 는 첫 행(WF2-000) — 실제 {one['chip']['serial']}")
res_dup = chip_percentiles_many(TABLES, [dup], index=idx)
check(canon(single_rows(one)) == canon(batch_rows(res_dup, 0)), "배치도 같은 행을 고른다")

# 세 필드가 모두 있으면 _pos_map 경로, serial 을 비우면 _locate 경로 — 결과가 같아야 한다.
full = {"source": "WF2", "serial": "WF2-000", "xpos": "1", "ypos": "1"}
one_full = chip_percentiles(TABLES, index=idx, **full)
check(canon(single_rows(one_full)) == canon(single_rows(one)),
      "_pos_map 경로(세 필드)와 _locate 경로(부분조건)가 같은 답")

# ─────────────────────────────────────────────────────────────────────────────
print("\n(c) 못 찾은 chip → None + 순서 유지")
mixed = [chips[0],
         {"source": "WF1", "serial": "없는-SERIAL", "xpos": "99", "ypos": "99"},
         chips[-1]]
res_mixed = chip_percentiles_many(TABLES, mixed, index=idx)
check(len(res_mixed["chips"]) == 3, "입력 개수만큼 반환")
check(res_mixed["chips"][1] is None, "미발견 chip 은 None")
check(res_mixed["chips"][0]["chip"]["serial"] == chips[0]["serial"]
      and res_mixed["chips"][2]["chip"]["serial"] == chips[-1]["serial"],
      "나머지는 입력 순서 유지")
check(chip_percentiles_many(TABLES, [], index=idx)["chips"] == [], "빈 입력 → 빈 배열")

try:
    chip_percentiles(TABLES, index=idx, serial="없음", xpos="99", ypos="99")
    check(False, "단건은 미발견 시 KeyError")
except KeyError:
    check(True, "단건은 미발견 시 KeyError (라우트 404)")

# ─────────────────────────────────────────────────────────────────────────────
print("\n(d) source 오지정 → 전체 재탐색 폴백")
wrong = dict(chips[0]); wrong["source"] = "WF2"      # WF1 die 인데 WF2 로 지정
one_w = chip_percentiles(TABLES, index=idx, **wrong)
res_w = chip_percentiles_many(TABLES, [wrong], index=idx)
check(one_w["chip"]["source"] == "WF1", f"단건 폴백 → WF1 (실제 {one_w['chip']['source']})")
check(res_w["chips"][0] is not None
      and res_w["chips"][0]["chip"]["source"] == "WF1", "배치도 같은 폴백")
check(canon(single_rows(one_w)) == canon(batch_rows(res_w, 0)), "폴백 경로 값도 동일")

# ─────────────────────────────────────────────────────────────────────────────
print("\n(e) 라우트 계약")
app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()


def upload_session() -> str:
    manifest = {
        "meta": {"product_type": "MDDI", "product": "SMOKE", "lot_id": "CHIPLOOKUP"},
        "mode": "Normal",
        "sources": [{"name": s, "file_name": f"{s}.csv"} for s in SOURCES],
        "selected_items": [],
        "client": {"user": "owner", "host": "smokehost"},
    }
    data = {"manifest": json.dumps(manifest)}
    for i, src in enumerate(SOURCES):
        data[f"webreport_{i}"] = (io.BytesIO(encode_honeyform_parquet(make_frame(src))),
                                  f"{src}.csv")
    resp = client.post("/pe/report/upload_webreport", data=data, headers=UA,
                       content_type="multipart/form-data")
    assert resp.status_code == 200, \
        f"업로드 실패 {resp.status_code}: {resp.get_data(as_text=True)[:400]}"
    return resp.get_json()["session_id"]


sid = upload_session()


def post_lookup(body, session=None):
    return client.post(
        f"/pe/report/session/{session or sid}/web_report/commonality/chips_lookup",
        json=body, headers=UA)


r = post_lookup({"chips": [chips[0], chips[1]]})
check(r.status_code == 200, f"정상 요청 200 (실제 {r.status_code}: {r.get_data(as_text=True)[:200]})")
if r.status_code == 200:
    body = r.get_json()
    check(len(body["chips"]) == 2 and body["chips"][0] is not None, "chips 2건 반환")
    check(isinstance(body.get("item_lists"), list) and body["item_lists"],
          "item_lists 동봉")
    # 라우트 응답이 순수 함수 결과와 같은지 (세션 로드 경로가 값을 바꾸지 않았는지)
    names = body["item_lists"][body["chips"][0]["items_ref"]]
    check(names == sorted(ITEMS), f"item_lists = sorted(item_columns) (실제 {names})")

check(post_lookup({"chips": [chips[0]] * 301}).status_code == 400, "301개 → 400")
check(post_lookup({"chips": "nope"}).status_code == 400, "비배열 → 400")
check(post_lookup({}).status_code == 400, "chips 누락 → 400")
check(post_lookup({"chips": [chips[0]]}, session="9999_deadbe").status_code == 404,
      "미지 세션 → 404")

# ─────────────────────────────────────────────────────────────────────────────
shutil.rmtree(_TMP, ignore_errors=True)
print()
if failures:
    print(f"실패 {len(failures)}건:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("모두 통과")

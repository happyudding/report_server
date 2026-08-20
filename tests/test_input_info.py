"""Input File Information (세션 상세 ℹ 모달) 계약 — 라우트 + 그룹 배치 공유 규칙.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_input_info.py

왜 이 파일이 필요한가 (2026-08-20 신설):
    모달이 보여주는 값은 전부 **업로드 시점 manifest** 에서 온다. 그래서 조용히 깨지는
    방식이 둘이다 — ① Honey 가 manifest["sources"] 에 넣는 키 이름이 서버가 읽는 이름과
    어긋나면 화면이 통째로 '-' 가 되고(에러가 아니라서 아무도 모른다), ② Compare/
    Temperature 의 그룹 배치를 리포트 본문과 **다른 코드로** 정하면 같은 source 가 모달과
    리포트에서 다른 그룹에 뜬다(CLAUDE.md 규칙 #13).

검증 항목:
  (a) GET .../web_report/input_info → 200, manifest 의 파일 정보가 그대로 나온다
  (b) 파일 정보가 없는 옛 세션도 200 이고 has_file_info=False (에러가 아니다)
  (c) Compare 세션의 Before/After 배치가 `compare.resolve_groups`(리포트 본문)와 일치
  (d) Temperature 세션의 RT/CT/HT 역할이 payload sources 의 temp_corner 와 일치
  (e) resolve_group_names ↔ resolve_groups 등가 (추출 리팩터링 회귀 방지)
  (f) 클라 헬퍼 source_file_info 가 만드는 키 == 서버가 읽는 키

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
_TMP = Path(tempfile.mkdtemp(prefix="wr_input_info_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""              # S3 비활성 → 로컬 폴백
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 워커 오프로드 없이 인라인(결정성)

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import compute as wr_compute  # noqa: E402
from web_report import metrics as wr_metrics  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)
from web_report.tabs import compare as compare_tab  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()

UA = {"User-Agent": "Mozilla/5.0 HoneyUser/iinfotester"}
URL = "/pe/report/upload_webreport"
N_ITEMS = 2
N_ROWS = 6


def make_parquet(seed: int = 0) -> bytes:
    """합성 7-meta honeyform → parquet bytes (계약 정본은 CLAUDE.md 규칙 #9)."""
    rng = np.random.default_rng(seed)
    items = [f"IT{j:02d}" for j in range(N_ITEMS)]
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
    df = pd.DataFrame(rows, columns=META_COLUMNS + items)
    return encode_honeyform_parquet(df)


def upload(lot_id, sources, *, mode="Normal", options=None):
    """source 목록(manifest 그대로)으로 세션 1개를 만든다 → session_id."""
    manifest = {
        "meta": {"product_type": "PMIC", "product": "IINFO", "lot_id": lot_id},
        "mode": mode,
        "sources": sources,
        "selected_items": [],
        "client": {"user": "iinfotester", "host": "iinfohost"},
    }
    if options:
        manifest["options"] = options
    data = {"manifest": json.dumps(manifest)}
    for idx, _src in enumerate(sources):
        data[f"webreport_{idx}"] = (io.BytesIO(make_parquet(idx)), f"src{idx}.std")
    resp = client.post(URL, data=data, headers=UA, content_type="multipart/form-data")
    assert resp.status_code == 200, f"업로드 실패 {resp.status_code}: {resp.get_data(as_text=True)[:300]}"
    return resp.get_json()["session_id"]


def get_info(sid):
    resp = client.get(f"/pe/report/session/{sid}/web_report/input_info", headers=UA)
    assert resp.status_code == 200, f"input_info {resp.status_code}: {resp.get_data(as_text=True)[:300]}"
    return resp.get_json()


# ── (a) 파일 정보가 실린 세션 ────────────────────────────────────────────────
FULL_SOURCE = {
    "index": 0, "name": "602XX2_3", "file_name": "602XX2_3_final.std",
    "file_path": r"D:\lot\602XX2\602XX2_3_final.std",
    "file_size": 123456, "file_created": "2026-08-01 09:30:00",
    "file_modified": "2026-08-01 10:05:00",
    "stdf": {"lot_id": "602XX2", "wafer_id": "3", "start_time": "2026-08-01 09:31:00",
             "finish_time": "2026-08-01 10:04:00", "test_time_sec": 1980,
             "part_count": 2500, "good_count": 2410},
}


def test_full_info():
    sid = upload("IINFO1", [FULL_SOURCE])
    info = get_info(sid)
    assert info["mode"] == "Normal", info["mode"]
    assert info["has_file_info"] is True, info
    assert info["has_stdf"] is True, info
    src = info["sources"][0]
    assert src["name"] == "602XX2_3", src
    assert src["file_name"] == "602XX2_3_final.std", src
    assert src["file_path"] == FULL_SOURCE["file_path"], src
    assert src["file_size"] == 123456, src
    assert src["file_created"] == "2026-08-01 09:30:00", src
    assert src["stdf"]["lot_id"] == "602XX2", src["stdf"]
    assert src["stdf"]["wafer_id"] == "3", src["stdf"]
    assert src["stdf"]["test_time_sec"] == 1980, src["stdf"]
    print("  [ok] (a) manifest 파일 정보 + STDF 가 그대로 나온다")


# ── (b) 파일 정보가 없는 옛 세션 ─────────────────────────────────────────────
def test_legacy_session():
    sid = upload("IINFO2", [{"index": 0, "name": "Lot0", "file_name": "Lot0.csv"}])
    info = get_info(sid)
    assert info["has_file_info"] is False, info
    assert info["has_stdf"] is False, info
    src = info["sources"][0]
    # 없는 키는 빈 값으로 정규화 — 화면이 '-' 로 그린다(예외가 아니다).
    assert src["file_path"] == "" and src["file_size"] is None, src
    assert src["stdf"] == {} and src["input_files"] == [], src
    assert src["name"] == "Lot0" and src["file_name"] == "Lot0.csv", src
    print("  [ok] (b) 파일 정보 없는 옛 세션도 200 + has_file_info=False")


# ── (c) Compare 그룹이 리포트 본문과 일치 ────────────────────────────────────
class _FakeTable:
    def __init__(self, name):
        self.source = name


def test_compare_groups():
    names = ["AFT1", "AFT2", "BEF1"]
    sources = [{"index": i, "name": n, "file_name": f"{n}.std"} for i, n in enumerate(names)]
    opts = {"compare": {"after": ["AFT1", "AFT2"], "before": ["BEF1"]}}
    sid = upload("IINFO3", sources, mode="Compare", options=opts)
    info = get_info(sid)
    assert info["mode"] == "Compare", info["mode"]
    got = {s["name"]: s["group"] for s in info["sources"]}
    assert got == {"AFT1": "After", "AFT2": "After", "BEF1": "Before"}, got

    # 리포트 본문(resolve_groups)과 같은 배치여야 한다 — 사본이면 여기서 갈라진다.
    before_t, after_t = compare_tab.resolve_groups(
        [_FakeTable(n) for n in names], opts["compare"])
    assert [t.source for t in before_t] == ["BEF1"], before_t
    assert [t.source for t in after_t] == ["AFT1", "AFT2"], after_t

    # 정렬용 group_index: Before(0) 가 After(1) 앞. 화면이 이 값으로 그룹을 묶는다.
    order = {s["name"]: s["group_index"] for s in info["sources"]}
    assert order == {"AFT1": 1, "AFT2": 1, "BEF1": 0}, order
    print("  [ok] (c) Compare Before/After 배치가 리포트 본문과 일치")


def test_compare_legacy_fallback():
    """옵션 없는 legacy Compare 세션 — 종전 관례(after=[0], before=[1])를 그대로 쓴다."""
    names = ["S0", "S1"]
    sources = [{"index": i, "name": n, "file_name": f"{n}.std"} for i, n in enumerate(names)]
    sid = upload("IINFO4", sources, mode="Compare")
    got = {s["name"]: s["group"] for s in get_info(sid)["sources"]}
    assert got == {"S0": "After", "S1": "Before"}, got
    print("  [ok] (c) legacy Compare 폴백 = after[0]/before[1]")


# ── (d) Temperature 역할이 payload 태깅과 일치 ───────────────────────────────
def test_temperature_roles():
    names = ["W1_RT", "W1_CT", "W1_HT"]
    sources = [{"index": i, "name": n, "file_name": f"{n}.std"} for i, n in enumerate(names)]
    groups = [{"rt": "W1_RT", "members": ["W1_CT", "W1_HT"], "member_roles": ["CT", "HT"]}]
    sid = upload("IINFO5", sources, mode="Temperature",
                 options={"temperature": {"groups": groups}})
    info = get_info(sid)
    got = {s["name"]: (s["group"], s["role"]) for s in info["sources"]}
    assert got == {"W1_RT": ("Group 1", "RT"), "W1_CT": ("Group 1", "CT"),
                   "W1_HT": ("Group 1", "HT")}, got

    # payload 태깅(_temperature_context)이 쓰는 그 함수인지 — 사본 방지.
    roles = wr_metrics.temperature_roles(groups)
    assert roles["W1_RT"] == (0, "rt", "RT"), roles
    assert roles["W1_CT"][2] == "CT" and roles["W1_HT"][2] == "HT", roles
    print("  [ok] (d) Temperature RT/CT/HT 역할이 payload 태깅과 같은 함수")


def test_temperature_role_fallback():
    """member_roles 가 없는 옛 세션 — members 순서로 CT→HT 추정(폴백 정본 1곳)."""
    groups = [{"rt": "R", "members": ["C", "H"]}]
    roles = wr_metrics.temperature_roles(groups)
    assert roles["C"][2] == "CT" and roles["H"][2] == "HT", roles
    print("  [ok] (d) member_roles 없는 옛 세션 폴백 유지")


# ── (e) 추출 리팩터링 등가 ──────────────────────────────────────────────────
def test_resolve_equivalence():
    """resolve_group_names 와 resolve_groups 가 항상 같은 배치를 낸다."""
    cases = [
        (["A", "B"], {"before": ["B"], "after": ["A"]}),
        (["A", "B", "C"], {"before": ["B", "C"], "after": ["A"]}),
        (["A", "B"], None),                                   # 옵션 없음 → 폴백
        (["A", "B"], {"before": ["없는것"], "after": ["A"]}),   # 한쪽 무효 → 폴백
    ]
    for names, groups in cases:
        b_names, a_names = compare_tab.resolve_group_names(names, groups)
        b_t, a_t = compare_tab.resolve_groups([_FakeTable(n) for n in names], groups)
        assert [t.source for t in b_t] == b_names, (names, groups, b_names)
        assert [t.source for t in a_t] == a_names, (names, groups, a_names)
    print(f"  [ok] (e) resolve_group_names ↔ resolve_groups 등가 ({len(cases)} 케이스)")


# ── (f) 클라 헬퍼가 만드는 키 == 서버가 읽는 키 ──────────────────────────────
def test_client_keys_match():
    """Honey 의 source_file_info 산출 키가 서버 input_info 가 읽는 키와 같은가.

    둘이 어긋나면 업로드는 성공하고 **화면만 조용히 비는** 형태로 나타난다.
    PyQt 없이 검증하려고 모듈 전체가 아니라 필요한 함수만 소스에서 실행한다.
    """
    import ast

    src_path = Path(_ROOT) / "client" / "honey_ui" / "source_name_dialog.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    wanted = {"_STDF_FIELDS", "_STDF_TIME_KEYS", "_iso_time", "source_display_path",
              "source_input_paths", "source_stdf_meta", "source_file_info"}
    picked = [n for n in tree.body
              if (isinstance(n, (ast.FunctionDef,)) and n.name in wanted)
              or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in wanted)]
    assert len(picked) == len(wanted), f"헬퍼가 사라졌거나 이름이 바뀌었다: {len(picked)}"
    ns = {"__name__": "iinfo_probe"}
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(src_path), "exec"), ns)

    class _RM:
        source_path = str(src_path)          # 실재하는 파일 → os.stat 성공
        stdf = {"lot_id": "L1", "wafer_id": "7", "start_t": 1785000000}

    class _MD:
        report_meta = _RM()

    info = ns["source_file_info"](_MD())
    assert info["file_path"] == str(src_path), info
    assert isinstance(info["file_size"], int) and info["file_size"] > 0, info
    assert info["file_created"] and info["file_modified"], info
    assert info["stdf"]["lot_id"] == "L1" and info["stdf"]["wafer_id"] == "7", info
    # epoch 로 와도 사람이 읽는 시각으로 정규화된다(화면은 그대로 그린다).
    assert info["stdf"]["start_time"][:2] == "20" and ":" in info["stdf"]["start_time"], info

    # 서버가 읽는 키 집합과 대조 — 클라가 만든 키는 전부 서버가 읽어야 한다.
    server_reads = {"file_path", "file_size", "file_created", "file_modified",
                    "input_files", "stdf"}
    assert set(info) <= server_reads, f"서버가 안 읽는 키: {set(info) - server_reads}"

    # 값이 하나도 없으면 키를 만들지 않는다 — 빈 문자열은 "있는데 빈 값"과 구분이 안 된다.
    class _Empty:
        pass

    assert ns["source_file_info"](_Empty()) == {}, "정보 없는 md 인데 키가 생겼다"
    print("  [ok] (f) 클라 source_file_info 키 == 서버 input_info 가 읽는 키")


def settle(timeout=120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("백그라운드 빌드가 끝나지 않음")


def main():
    print("[Input File Information]")
    try:
        test_full_info()
        test_legacy_session()
        test_compare_groups()
        test_compare_legacy_fallback()
        test_temperature_roles()
        test_temperature_role_fallback()
        test_resolve_equivalence()
        test_client_keys_match()
        settle()
    finally:
        try:
            settle(timeout=10)
        except Exception:
            pass
        shutil.rmtree(_TMP, ignore_errors=True)
    print("[통과] Input File Information 계약 정상")


if __name__ == "__main__":
    main()

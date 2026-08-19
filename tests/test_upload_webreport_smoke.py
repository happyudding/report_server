"""web_report 업로드 라우트 스모크 — 아주 가벼운 파일 1개로 POST 가 성공하는지.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_upload_webreport_smoke.py

왜 이 파일이 필요한가 (2026-08-19 신설):
    기존 web_report e2e 테스트는 전부 `wr_ingest.ingest_webreport()` 를 **직접 호출**해
    라우트를 우회한다. 그래서 `POST /pe/report/upload_webreport` 자체의 계약 —
    필드명(`manifest`, `webreport_<idx>`), 멀티파트 파싱, 업로드 슬롯 세마포어,
    상태코드 매핑 — 은 어떤 테스트도 검사하지 않았고, 여기가 깨져도 "클라에서만
    실패하는" 형태로 나타났다. 매 코드 변경마다 이 경로가 살아 있는지 몇 초 안에
    확인하는 것이 목적이므로 데이터는 의도적으로 최소다(2항목 × 6행 × 1소스).

검증 항목:
  (a) manifest + webreport_0 멀티파트 POST → 200 + session_id / web_report_url
  (b) 세션 행이 DB 에 생기고 source='web_report'
  (c) parquet 산출물이 업로드 루트에 저장된다 (S3 미설정 → 로컬 폴백)
  (d) 실패 계약: manifest 누락 → 400 / webreport_0 누락 → 400 (필드명 오타 감지)
  (e) 슬롯 세마포어가 반납된다 — 연속 업로드 2회가 대기 없이 성공

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
_TMP = Path(tempfile.mkdtemp(prefix="wr_upload_smoke_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""              # S3 비활성 → 로컬 폴백
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 워커 오프로드 없이 인라인(테스트 결정성)

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

UA = {"User-Agent": "Mozilla/5.0 HoneyUser/smoketester"}
URL = "/pe/report/upload_webreport"
N_ITEMS = 2
N_ROWS = 6


def make_parquet(seed: int = 0) -> bytes:
    """합성 7-meta honeyform → parquet bytes (계약 정본은 CLAUDE.md 규칙 #9).

    honey_parse 더미 폴백은 구 5-meta 라 여기서 쓰면 안 된다 — 인코더로 직접 만든다.
    """
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


def build_manifest(lot_id: str) -> dict:
    return {
        "meta": {"product_type": "MDDI", "product": "SMOKE", "lot_id": lot_id},
        "mode": "Normal",
        "sources": [{"name": "Lot0", "file_name": "Lot0.csv"}],
        "selected_items": [],
        "client": {"user": "smoketester", "host": "smokehost"},
    }


def post_upload(manifest=None, with_file=True, lot_id="SMOKE1"):
    """업로드 라우트 호출 — 클라(uploader.post_webreport)와 같은 필드명을 쓴다."""
    data = {}
    if manifest is not None:
        data["manifest"] = json.dumps(manifest)
    if with_file:
        data["webreport_0"] = (io.BytesIO(make_parquet()), "Lot0.csv")
    return client.post(URL, data=data, headers=UA,
                       content_type="multipart/form-data")


def settle(timeout=120) -> None:
    """업로드가 예약한 백그라운드 프리웜이 끝날 때까지 기다린다 (tempdir 정리 전 필요)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("백그라운드 빌드가 끝나지 않음")


def test_upload_success():
    """(a)(b)(c) 가벼운 파일 1개 업로드가 200 으로 끝나고 세션·산출물이 남는다."""
    t0 = time.perf_counter()
    resp = post_upload(build_manifest("SMOKE1"))
    took = time.perf_counter() - t0
    assert resp.status_code == 200, f"업로드 실패 {resp.status_code}: {resp.get_data(as_text=True)[:400]}"

    body = resp.get_json()
    sid = body.get("session_id")
    assert sid, f"session_id 없음: {body}"
    assert body.get("web_report_url") == f"/pe/report/view/{sid}", body
    assert body.get("status") == "done", body

    session = report_db.get_session(sid)
    assert session is not None, "세션 행이 DB 에 없다"
    assert session["source"] == "web_report", session["source"]

    saved = list(Path(os.environ["REPORT_UPLOAD_DIR"]).rglob("*"))
    assert any(p.is_file() for p in saved), "업로드 산출물이 저장되지 않았다"

    print(f"  [ok] 업로드 200 — session={sid} ({took:.2f}s, {len(saved)} 파일)")
    return sid


def test_missing_manifest():
    """(d) manifest 누락 → 400. 필드명이 바뀌면 여기서 잡힌다."""
    resp = post_upload(manifest=None)
    assert resp.status_code == 400, f"manifest 누락인데 {resp.status_code}"
    print("  [ok] manifest 누락 → 400")


def test_missing_file():
    """(d) webreport_0 누락 → 400. 파일 필드명 계약(webreport_<idx>) 고정."""
    resp = post_upload(build_manifest("SMOKE2"), with_file=False)
    assert resp.status_code == 400, f"파일 누락인데 {resp.status_code}"
    print("  [ok] webreport_0 누락 → 400")


def test_slot_released():
    """(e) 슬롯 세마포어가 반납된다 — 실패 요청 뒤에도 다음 업로드가 즉시 성공.

    슬롯이 새면 두 번째 업로드부터 WEB_REPORT_UPLOAD_WAIT_SEC 만큼 대기하다 503 이 된다
    (운영에서 '가벼운 파일인데도 타임아웃'으로 나타나는 형태).
    """
    t0 = time.perf_counter()
    resp = post_upload(build_manifest("SMOKE3"))
    took = time.perf_counter() - t0
    assert resp.status_code == 200, f"연속 업로드 실패 {resp.status_code}"
    assert took < 30, f"슬롯 대기 의심 — {took:.1f}s 걸렸다"
    print(f"  [ok] 연속 업로드 200 ({took:.2f}s) — 슬롯 반납 정상")


def main():
    print("[upload_webreport 스모크]")
    try:
        test_upload_success()
        test_missing_manifest()
        test_missing_file()
        test_slot_released()
        settle()
    finally:
        try:
            settle(timeout=10)
        except Exception:
            pass
        shutil.rmtree(_TMP, ignore_errors=True)
    print("[통과] web_report 업로드 라우트 정상")


if __name__ == "__main__":
    main()

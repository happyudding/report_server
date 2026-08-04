"""콜드 /full 조기 202 (extras 생략) + 콜드 빌드 반복 실패 차단(503) 검증.

실행:
    python tests/test_cold_fastpath_and_failguard.py

검증 항목:
  (A1) 콜드 /full 202 응답이 extras(objects/summary/csv/annotations/chart_notes/
       note_info/note_tags) 조회를 **하나도 하지 않는다**. 콜드 세션은 프런트가 최대
       15분간 폴링하므로 이 DB 왕복이 수백 번 반복됐다.
  (A2) 웜 /full 200 은 조기 반환 도입 전과 동일한 body·ETag 를 주고, If-None-Match 는
       그대로 304 다 (extras 는 웜 경로에서 여전히 조회된다).
  (D1) 온디맨드 빌드가 연속 실패하면 FAIL_LIMIT 회 후 /full 이 202 대신 503
       {"build_failed":true} 를 준다 — 프런트가 15분 헛폴링 대신 즉시 안내한다.
  (D2) 쿨다운이 지나면 다시 202 로 돌아가고(자동 회복), 빌드가 성공하면 실패 기록이
       지워진다.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

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
_TMP = Path(tempfile.mkdtemp(prefix="wr_coldfast_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 인라인(테스트 결정성)

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import build_status as wr_build_status  # noqa: E402
from web_report import cache as wr_cache  # noqa: E402
from web_report import compute as wr_compute  # noqa: E402
from web_report import ingest as wr_ingest  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()

UA = {"User-Agent": "Mozilla/5.0 HoneyUser/tester"}
N_ITEMS = 4
N_ROWS = 24


def make_parquet(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    items = [f"IT{j:02d}" for j in range(N_ITEMS)]
    rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row[META_COLUMNS[0]] = label
        for j, it in enumerate(items):
            row[it] = {"TSEQ": j + 1, "TNO": 1000 + j, "STEP": ("P1", "P2")[j % 2],
                       "UNIT": "V", "HILIM": 10.0, "LOLIM": -10.0}[label]
        rows.append(row)
    for i in range(N_ROWS):
        bin_v = 1 if i % 4 else 2
        row = {"SERIAL": f"S{i:04d}", "SHOT": 0, "DUT": 0,
               "XPOS": i % 6, "YPOS": i // 6, "BIN": bin_v,
               "FAILTNO": "" if bin_v == 1 else 1000 + (i % N_ITEMS)}
        for it in items:
            row[it] = round(float(rng.normal(0, 2)), 4)
        rows.append(row)
    df = pd.DataFrame(rows, columns=META_COLUMNS + items)
    return encode_honeyform_parquet(df)


def create_session() -> str:
    files = [{"name": "Lot0", "filename": "Lot0.csv", "data": make_parquet(0)}]
    manifest = {
        "meta": {"product_type": "MDDI", "product": "P1", "lot_id": "L1"},
        "mode": "Normal",
        "sources": [{"name": "Lot0", "file_name": "Lot0.csv"}],
        "selected_items": [],
        "client": {"user": "tester", "host": "testhost"},
    }
    result = wr_ingest.ingest_webreport(
        manifest, files, report_db=report_db,
        upload_root=Path(os.environ["REPORT_UPLOAD_DIR"]),
        client_ip="127.0.0.1", user_agent="Mozilla/5.0 HoneyUser/tester")
    return result["session_id"]


def settle(timeout=120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("백그라운드 빌드가 끝나지 않음")


def drop_caches(session_id: str) -> None:
    settle()
    session = report_db.get_session(session_id)
    wr_cache.invalidate_caches(session.get("analysis_key"))
    root = Path(os.environ["REPORT_UPLOAD_DIR"]) / "web_report"
    for cache_dir in root.glob("*/cache"):
        shutil.rmtree(cache_dir, ignore_errors=True)


class _CallSpy:
    """report_db 함수 호출 횟수를 세는 임시 래퍼 (with 블록 안에서만 적용)."""

    def __init__(self, names):
        self._names = list(names)
        self._orig = {}
        self.counts = {n: 0 for n in self._names}

    def __enter__(self):
        for name in self._names:
            orig = getattr(report_db, name)
            self._orig[name] = orig

            def make(n, f):
                def wrapper(*a, **kw):
                    self.counts[n] += 1
                    return f(*a, **kw)
                return wrapper
            setattr(report_db, name, make(name, orig))
        return self

    def __exit__(self, *exc):
        for name, orig in self._orig.items():
            setattr(report_db, name, orig)
        return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    sid = create_session()
    url = f"/pe/report/session/{sid}/full"
    ok = 0

    # ── (A1) 콜드 202 는 extras 를 조회하지 않는다 ───────────────────────────
    # 조기 반환 전에는 아래 함수들이 202 응답 1건마다 전부 불렸다.
    EXTRAS = ["get_all_object_infos", "get_summary_by_analysis_key",
              "get_csv_files", "get_annotations"]
    drop_caches(sid)
    with _CallSpy(EXTRAS) as spy:
        r = client.get(url, headers=UA)
    assert r.status_code == 202, f"콜드 /full 이 202 가 아님: {r.status_code}"
    assert r.get_json().get("building") is True, r.get_json()
    unexpected = {n: c for n, c in spy.counts.items() if c}
    assert not unexpected, f"202 경로가 extras 를 조회함: {unexpected}"
    print(f"(A1) 콜드 202 — extras 조회 0회 ({', '.join(EXTRAS)})")
    ok += 1

    # ── (A2) 웜 200 은 종전과 동일 (extras 는 여전히 조회) ────────────────────
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        r = client.get(url, headers=UA)
        if r.status_code == 200:
            break
        time.sleep(0.1)
    assert r.status_code == 200, f"빌드 후에도 200 이 아님: {r.status_code}"
    with _CallSpy(EXTRAS) as spy:
        r_warm = client.get(url, headers=UA)
    assert r_warm.status_code == 200, r_warm.status_code
    assert all(spy.counts[n] for n in EXTRAS), \
        f"웜 경로가 extras 를 건너뜀(응답 내용이 달라짐): {spy.counts}"
    etag = r_warm.headers.get("ETag")
    assert etag, "웜 200 에 ETag 가 없음"
    body = json.loads(r_warm.data) if r_warm.headers.get("Content-Encoding") != "gzip" \
        else json.loads(__import__("gzip").decompress(r_warm.data))
    assert body.get("web_report"), "웜 200 payload 에 web_report 가 없음"
    for key in ("session", "summary", "objects", "annotations"):
        assert key in body, f"웜 200 payload 에 {key} 가 빠짐"
    r304 = client.get(url, headers={**UA, "If-None-Match": etag})
    assert r304.status_code == 304, f"If-None-Match 가 304 가 아님: {r304.status_code}"
    print("(A2) 웜 200 — extras 정상 조회 + ETag/304 유지")
    ok += 1

    # ── (D1) 연속 실패 → 503 ────────────────────────────────────────────────
    orig_job = wr_compute._ONDEMAND_JOBS["report"]

    def boom(sid_, root_):
        raise RuntimeError("simulated build failure")

    wr_compute._ONDEMAND_JOBS["report"] = boom
    try:
        drop_caches(sid)
        wr_build_status.clear_failure(sid, "report")
        codes = []
        for _ in range(wr_build_status.FAIL_LIMIT + 1):
            r = client.get(url, headers=UA)
            codes.append(r.status_code)
            if r.status_code == 503:
                break
            settle()          # 실패가 기록될 때까지 대기
        assert codes[-1] == 503, f"연속 실패인데 503 이 안 옴: {codes}"
        assert all(c == 202 for c in codes[:-1]), f"503 이전 응답이 202 가 아님: {codes}"
        body = r.get_json()
        assert body.get("build_failed") is True, body
        assert body.get("error"), "503 에 안내 문구가 없음"
        st = client.get(f"/pe/report/session/{sid}/web_report/build_status", headers=UA)
        assert st.get_json().get("state") == "failed", st.get_json()
        print(f"(D1) 연속 실패 {wr_build_status.FAIL_LIMIT}회 → 503 build_failed "
              f"+ build_status state=failed (응답열 {codes})")
        ok += 1

        # ── (D2) 쿨다운 경과 → 다시 202 (자동 회복) ──────────────────────────
        entry = wr_build_status._FAILED[(sid, "report")]
        entry["t_last"] -= wr_build_status.FAIL_COOLDOWN_SEC + 1
        r = client.get(url, headers=UA)
        assert r.status_code == 202, f"쿨다운 후 재시도가 안 열림: {r.status_code}"
    finally:
        wr_compute._ONDEMAND_JOBS["report"] = orig_job

    # 정상 잡 복구 + 성공하면 실패 기록이 지워진다
    wr_build_status.clear_failure(sid, "report")
    drop_caches(sid)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        r = client.get(url, headers=UA)
        if r.status_code == 200:
            break
        assert r.status_code == 202, f"복구 후 예상 밖 상태 {r.status_code}"
        time.sleep(0.1)
    assert r.status_code == 200, "정상 잡 복구 후에도 200 이 안 옴"
    assert wr_build_status.failure_blocked(sid, "report") is None, "성공인데 실패 기록이 남음"
    print("(D2) 쿨다운 경과 → 202 재개, 빌드 성공 → 실패 기록 해제")
    ok += 1

    print(f"\n전체 통과: {ok}개 그룹")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

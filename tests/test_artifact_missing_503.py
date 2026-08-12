"""유효 세션의 **산출물 유실**은 404 가 아니라 503 이어야 한다 (2026-08-12).

배경:
    세션 행·권한은 멀쩡한데 parquet/manifest 파일만 사라진 상태에서 서버는 여태
    404 "session data not found" 를 돌려줬다. 404 는 "그런 세션 없다"는 뜻이라
    사용자는 세션이 삭제된 줄 알고 포기하고, 관리자도 신고를 "없는 세션 열었나 보다"
    로 넘긴다. 실제로는 복구 가능한 장애다 — 503 + error_id 로 구분한다.

검증 항목:
  (1) 없는 session_id → **404 유지** (의도적 404 를 망가뜨리지 않았는지)
  (2) 세션은 있는데 parquet 유실 → GET /full 이 **503 + error_id**
  (3) 같은 상태에서 web_report 조회 라우트(distribution)도 **503 + error_id**
  (4) 산출물을 되돌리면 다시 정상(200/202) — 503 이 영구 상태가 아님

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
실행:  server/.venv/Scripts/python.exe tests/test_artifact_missing_503.py
"""
from __future__ import annotations

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
_TMP = Path(tempfile.mkdtemp(prefix="wr_artifact503_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 인라인(테스트 결정성)

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
# report_extension 이 라우트 모듈들을 먼저 끌어와야 한다 — 먼저 import 하면 순환된다.
from report import routes_session  # noqa: E402
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


def hide_artifacts(session_id):
    """세션의 parquet/manifest 를 옆으로 치운다 (유실 재현). 되돌릴 경로를 반환."""
    settle()
    session = report_db.get_session(session_id)
    root = Path(os.environ["REPORT_UPLOAD_DIR"]) / "web_report" / session["analysis_key"]
    stash = Path(tempfile.mkdtemp(prefix="stash_"))
    moved = []
    for p in list(root.rglob("*.parquet")) + list(root.rglob("*manifest*.json")):
        dst = stash / p.name
        shutil.move(str(p), str(dst))
        moved.append((dst, p))
    assert moved, "치울 산출물을 찾지 못했다 — 테스트 전제가 깨졌다"
    return moved


def drop_caches(session_id):
    """RAM·디스크 캐시를 비운다. 캐시가 남아 있으면 파일이 없어도 응답이 나온다."""
    session = report_db.get_session(session_id)
    wr_cache.invalidate_caches(session.get("analysis_key"))
    for cache_dir in (Path(os.environ["REPORT_UPLOAD_DIR"]) / "web_report").glob("*/cache"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def get_uncached(url, tries=6):
    """캐시를 비운 직후 요청한다. 배경 프리웜이 캐시를 되채우면 200 이 나올 수 있어
    (기존 테스트의 flaky 원인) 캐시 비우기를 끼워 몇 번 재시도한다."""
    sid = url.split("/session/")[1].split("/")[0]
    r = None
    for _ in range(tries):
        settle()
        drop_caches(sid)
        r = client.get(url, headers=UA)
        if r.status_code != 200:
            return r
        time.sleep(0.2)
    return r


def restore(moved):
    for src, dst in moved:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    sid = create_session()
    settle()
    ok = 0

    # ── (1) 없는 세션은 404 그대로 ──────────────────────────────────────────
    r = client.get("/pe/report/session/no-such-session-xyz/full", headers=UA)
    assert r.status_code == 404, f"없는 세션이 404 가 아님: {r.status_code}"
    print("(1) 없는 session_id → 404 유지 ok")
    ok += 1

    # ── (2)·(3) 산출물 유실 상태 ────────────────────────────────────────────
    # (2) /full 이 산출물 유실(FileNotFoundError)을 만났을 때.
    #     콜드면 202 로 먼저 빠지므로(정상), 이 분기는 "캐시는 있는데 파일이 없다"는
    #     드문 상태에서만 닿는다 — 그 상태를 만들기 어려워 해당 예외를 직접 주입한다.
    orig = routes_session.web_report_response_cache.get_full_gzip

    def boom(*a, **kw):
        raise FileNotFoundError("source_0.parquet")

    routes_session.web_report_response_cache.get_full_gzip = boom
    try:
        r = client.get(f"/pe/report/session/{sid}/full", headers=UA)
        body = r.get_json() or {}
        assert r.status_code == 503, f"/full 이 503 이 아님: {r.status_code} {str(body)[:200]}"
        assert body.get("error_id") is not None, f"error_id 없음: {body}"
        print(f"(2) /full 산출물 유실 → 503 + error_id={body.get('error_id')!r} ok "
              "(종전 404 'session data not found')")
        ok += 1
    finally:
        routes_session.web_report_response_cache.get_full_gzip = orig

    # (2b) 세션 행 자체가 없을 때의 404 는 그대로여야 한다 (위 변경이 삼키지 않았는지).
    orig_get = routes_session.report_db.get_session
    r = client.get(f"/pe/report/session/{sid}/full", headers=UA)
    assert r.status_code in (200, 202), f"정상 세션이 {r.status_code}: {str(r.get_json())[:200]}"
    print("(2b) 정상 세션은 200/202 유지 ok")
    ok += 1
    assert routes_session.report_db.get_session is orig_get

    # (3) parquet 파일 자체가 사라진 상태 — 202 우회가 없는 조회 라우트로 확인한다.
    moved = hide_artifacts(sid)
    try:
        r = get_uncached(f"/pe/report/session/{sid}/web_report/distribution")
        body = r.get_json() or {}
        assert r.status_code == 503, f"distribution 이 503 이 아님: {r.status_code} {str(body)[:200]}"
        assert "error_id" in body, f"error_id 없음: {body}"
        print("(3) parquet 유실 distribution → 503 + error_id ok")
        ok += 1
    finally:
        restore(moved)

    # ── (4) 되돌리면 회복 ───────────────────────────────────────────────────
    deadline = time.monotonic() + 60
    got = None
    while time.monotonic() < deadline:
        r = client.get(f"/pe/report/session/{sid}/full", headers=UA)
        got = r.status_code
        if got == 200:
            break
        assert got in (200, 202), f"회복 경로가 {got} 를 반환: {r.get_json()}"
        time.sleep(0.2)
    assert got == 200, f"산출물 복구 후에도 200 이 되지 않음 (마지막 {got})"
    print("(4) 산출물 복구 후 200 회복 ok")
    ok += 1

    print(f"\n전체 통과: {ok}개")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

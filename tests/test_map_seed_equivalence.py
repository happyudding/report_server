"""Map dies 시딩 정합성 — seed_map 산출이 콜드 빌드 경로와 완전히 같은지 검증.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_map_seed_equivalence.py

배경: CLAUDE.md §5-11 (Map 3초 SLA) 는 report 콜드 빌드가 map dies gzip 을 함께 채우는
``service.seed_map`` 으로 달성한다. 시딩은 **이미 웜인 tables** 를 재사용하므로, 그 tables
준비 순서(_mode_tables → selected_items 필터)가 지연 라우트 경로(``get_map_analysis``)와
어긋나면 화면에 다른 맵이 뜬다. 이 테스트가 그 어긋남을 잡는다.

검증 항목 (모드 4종):
  (a) ingest+프리웜이 끝나면 map 라우트가 202 없이 바로 200 (= 시딩이 실제로 동작)
  (b) 시딩된 blob 을 gunzip 한 payload 가, 캐시를 전부 비우고 콜드 빌드로 다시 만든
      payload 와 **정준 JSON 완전 일치** (다운샘플·순서·필드 차이 없음)
  (c) dies 총수 > 0 (경량 메타만 실려 통과하는 오탐 방지)

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import gzip
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
# 컴퓨트 워커(spawn)는 이 모듈을 __mp_main__ 으로 재실행한다 — 거기서 mkdtemp 를 다시
# 부르면 자식이 **빈 DB 를 새로 만들어** 세션을 못 찾는다. 자식만 부모 경로를 물려받게
# 한다. 자식 판정은 `__name__ == "__mp_main__"` 로 — parent_process() 는 이 시점에
# None 이라 쓸 수 없다(wsgi.py 와 같은 이유).
if __name__ == "__mp_main__":
    _TMP = Path(os.environ["WR_MAPSEED_TMP"])
else:
    _TMP = Path(tempfile.mkdtemp(prefix="wr_mapseed_"))
    os.environ["WR_MAPSEED_TMP"] = str(_TMP)
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""              # S3 비활성 → 로컬 폴백
# 기본은 인라인 고정(테스트 결정성). 운영 경로(콜드 빌드가 워커 프로세스에서 돌고
# 워커가 disk_cache 를 직접 채우는 흐름)를 함께 보려면 밖에서 값을 지정하면 된다:
#   $env:WEB_REPORT_COMPUTE_WORKERS='2'; python tests\test_map_seed_equivalence.py
os.environ.setdefault("WEB_REPORT_COMPUTE_WORKERS", "0")

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import cache as wr_cache  # noqa: E402
from web_report import compute as wr_compute  # noqa: E402
from web_report import edits as wr_edits  # noqa: E402
from web_report import ingest as wr_ingest  # noqa: E402
from web_report import service as wr_service  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)
from web_report.validation import canon  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()
client = app.test_client()

UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])
UA = {"User-Agent": "Mozilla/5.0 HoneyUser/tester"}
N_ITEMS = 6
N_ROWS = 40


def make_parquet(seed: int) -> bytes:
    """합성 7-meta honeyform → parquet bytes. STEP 은 P1/P2 두 종 (STEP 분리 경로 사용)."""
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
        row = {"SERIAL": f"S{i:04d}", "SHOT": 0, "DUT": str(i % 3),
               "XPOS": i % 7, "YPOS": i // 7, "BIN": bin_v,
               "FAILTNO": "" if bin_v == 1 else 1000 + (i % N_ITEMS)}
        for it in items:
            row[it] = round(float(rng.normal(0, 2)), 4)
        rows.append(row)
    df = pd.DataFrame(rows, columns=META_COLUMNS + items)
    return encode_honeyform_parquet(df)


def create_session(lot_id: str, mode: str = "Normal",
                   selected_items: list | None = None) -> str:
    files = [{"name": f"Lot{i}", "filename": f"Lot{i}.csv", "data": make_parquet(i)}
             for i in range(2)]
    manifest = {
        "meta": {"product_type": "MDDI", "product": "P1", "lot_id": lot_id},
        "mode": mode,
        "sources": [{"index": i, "name": f"Lot{i}", "file_name": f"Lot{i}.csv"}
                    for i in range(2)],
        "selected_items": selected_items or [],
        "client": {"user": "tester", "host": "testhost"},
    }
    result = wr_ingest.ingest_webreport(
        manifest, files, report_db=report_db, upload_root=UPLOAD_ROOT,
        client_ip="127.0.0.1", user_agent=UA["User-Agent"])
    return result["session_id"]


def settle(timeout=180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("백그라운드 빌드가 끝나지 않음")


def drop_all(session_id: str) -> None:
    """RAM + 디스크 캐시 전부 비움 → 완전 콜드 상태."""
    settle()
    session = report_db.get_session(session_id)
    wr_cache.invalidate_caches(session.get("analysis_key"))
    for cache_dir in (UPLOAD_ROOT / "web_report").glob("*/cache"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def map_payload(blob: bytes) -> dict:
    return json.loads(gzip.decompress(blob))


def open_full(session_id: str, timeout=300) -> None:
    """세션 열기(/full)를 200 까지 완료 — **시딩의 주체인 report 콜드 빌드 완료 신호**다.

    프리웜으로 대신할 수 없다: `compute.status()["prewarm_pending"]` 은 큐 길이만 세어
    (소비자 스레드가 pop 한 뒤 빌드) settle 이 완료를 보장하지 못한다. 워커 프로세스
    모드에서는 spawn 시간까지 겹쳐 그 레이스가 항상 드러난다.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/pe/report/session/{session_id}/full", headers=UA)
        if r.status_code == 200:
            return
        assert r.status_code == 202, f"/full 예상 밖 상태 {r.status_code}"
        time.sleep(0.1)
    raise AssertionError(f"/full 이 {timeout}s 안에 200 이 되지 않음")


def check(label: str, session_id: str) -> None:
    settle()
    open_full(session_id)

    # (a) 시딩 덕에 첫 조회가 202 없이 바로 200 이어야 한다.
    r = client.get(f"/pe/report/session/{session_id}/web_report/map_analysis",
                   headers=UA)
    assert r.status_code == 200, (
        f"[{label}] 시딩 실패 — map 첫 조회가 200 이 아님: {r.status_code}")

    seeded = wr_service.get_map_gzip(session_id, report_db=report_db,
                                     upload_root=UPLOAD_ROOT, build_if_cold=False)
    seeded_payload = map_payload(seeded)

    # (c) 경량 메타만 들어 통과하는 오탐 방지
    dies = sum(len(m.get("dies") or ()) for m in seeded_payload.get("maps") or ())
    assert dies > 0, f"[{label}] 시딩 payload 에 dies 가 없음"

    # (b) 캐시를 전부 비우고 콜드 빌드로 다시 만든 것과 정준 JSON 완전 일치
    drop_all(session_id)
    rebuilt = wr_service.get_map_gzip(session_id, report_db=report_db,
                                      upload_root=UPLOAD_ROOT, build_if_cold=True)
    rebuilt_payload = map_payload(rebuilt)
    assert canon(seeded_payload) == canon(rebuilt_payload), (
        f"[{label}] 시딩 payload 가 콜드 빌드 결과와 다름 "
        f"(maps {len(seeded_payload.get('maps') or ())} vs "
        f"{len(rebuilt_payload.get('maps') or ())})")

    print(f"  OK [{label}] maps={len(seeded_payload.get('maps') or ())} dies={dies}")


def check_legacy_backfill(session_id: str) -> None:
    """시딩 도입 전 세션(report 캐시는 있고 map 캐시만 없는 상태) 재현 → /full 200 이
    백그라운드 백필을 걸어, 탭 진입 시점에는 콜드 202 없이 200 이 되는지."""
    settle()
    open_full(session_id)
    session = report_db.get_session(session_id)
    akey = session.get("analysis_key")
    wr_cache.invalidate_caches(akey)
    removed = 0
    for path in (UPLOAD_ROOT / "web_report").glob("*/cache/map-*.gz"):
        path.unlink()
        removed += 1
    assert removed, "지울 map 디스크 캐시가 없음 — 시딩이 안 된 상태로 검사 중"

    r = client.get(f"/pe/report/session/{session_id}/full", headers=UA)
    assert r.status_code == 200, f"레거시 /full 이 200 이 아님: {r.status_code}"
    settle()   # 백필(온디맨드 map 빌드) 완료 대기 — 사용자가 탭을 클릭하기까지의 시간
    r = client.get(f"/pe/report/session/{session_id}/web_report/map_analysis",
                   headers=UA)
    assert r.status_code == 200, (
        f"백필 실패 — 탭 진입이 여전히 콜드: {r.status_code}")
    print(f"  OK [레거시 백필] map 캐시 {removed}건 삭제 → /full 200 후 자동 복구")


def main() -> None:
    print("Map 시딩 정합성 검증")

    check("Normal (STEP 분리)", create_session("L_NORMAL"))
    # 백필은 report 디스크 캐시가 살아 있어야 하는 시나리오라 별도 세션으로 검사한다
    # (check() 는 검증 과정에서 캐시를 전부 비운다).
    check_legacy_backfill(create_session("L_BACKFILL"))
    check("selected_items 지정",
          create_session("L_SELECTED", selected_items=["IT00", "IT01", "IT02"]))
    check("DUT 모드", create_session("L_DUT", mode="DUT"))

    sid_prep = create_session("L_PREP")
    settle()
    wr_edits.save_preprocess(report_db, sid_prep, {"exclude_items": ["IT00", "IT03"]})
    # 전처리 저장으로 digest 가 바뀌면 map 키도 바뀐다 — 새 키는 다음 report 콜드
    # 빌드가 시딩한다(check 안의 open_full 이 그 빌드를 유발한다).
    check("전처리(exclude_items)", sid_prep)

    print("전부 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)   # 워커가 물고 있으면 조용히 남는다

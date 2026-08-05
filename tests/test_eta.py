"""콜드 빌드 예상시간(web_report/eta.py) 검증.

1) parquet footer 로 잰 규모 == 디코드한 tables 로 잰 규모 (예측/기록 두 경로 일치)
2) 예상초가 실제 콜드 빌드 시간과 같은 자릿수 (계수가 뒤집히지 않았는지)
3) 202 / build_status 응답에 eta 가 실린다
4) build_log 실측으로 배율이 학습된다
5) 규모를 모르면(로컬 parquet 없음) 조용히 None — 조회를 막지 않는다

실행: server\\.venv\\Scripts\\python.exe -m pytest tests/test_eta.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "server"))

# config 는 import 시점의 env 로 굳는(모듈 전역 상수) 데다 프로세스에 하나뿐이라,
# 함께 pytest 로 돌리면 **가장 먼저 import 한 테스트 모듈**의 환경이 전부를 지배한다.
# 그래서 우리가 첫 번째일 때만 격리 환경을 세우고, 이미 누가 세웠으면 그 위에 얹힌다 —
# env 를 덮어써 봐야 config 에 반영되지 않을뿐더러(무의미), 남의 테스트 전제를 깬다
# (REPORT_METRICS_ENABLED=0 을 넣었더니 admin 활성 사용자 집계 테스트가 빈 목록을 봤다).
_OWNS_ENV = "config" not in sys.modules
if _OWNS_ENV:
    _TMP = Path(tempfile.mkdtemp(prefix="eta_test_"))
    os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
    os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
    os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval.db")
    os.environ["REPORT_VOC_DB_PATH"] = str(_TMP / "voc.db")
    os.environ["REPORT_S3_BUCKET"] = ""
    os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"
    os.environ["REPORT_CLEANUP_ENABLED"] = "0"
    os.environ["REPORT_DB_BACKUP_ENABLED"] = "0"

import config  # noqa: E402
if _OWNS_ENV:
    config.ROOT_DIR = _TMP

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import build_log, eta, ingest as wr_ingest  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, decode_split_honeyform_parquet,
    encode_honeyform_parquet)

UPLOAD_ROOT = Path(config.REPORT_UPLOAD_DIR)
UA = {"User-Agent": "Mozilla/5.0 HoneyUser/etatest"}

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
# init_report 안의 metrics.init_app 은 **프로세스당 1회만** 훅을 등록한다(_started 가드).
# 여기서 그 1회를 써 버리면 뒤이어 자기 app 에 metrics 를 붙이는 테스트(admin 활성 사용자)
# 가 훅 없는 app 을 받아 빈 집계를 본다. 우리는 metrics 가 필요 없으므로 표식을 되돌린다.
try:
    from admin_panel import metrics as _metrics
    _metrics._started = False
except Exception:
    pass
report_db.init_report_db()
client = app.test_client()


def make_df(n_items: int, n_rows: int):
    items = [f"ITEM_{i:04d}" for i in range(n_items)]
    rng = np.random.default_rng(0)
    meta = pd.DataFrame({
        "SERIAL": META_ROW_LABELS, "SHOT": "", "DUT": "", "XPOS": "",
        "YPOS": "", "BIN": "", "FAILTNO": "",
    })
    for i, it in enumerate(items):
        meta[it] = [str(i + 1), str(1000 + i), "P2", "V", "1.0", "0.0"]
    body = pd.DataFrame({
        "SERIAL": [f"S{i}" for i in range(n_rows)],
        "SHOT": ["1"] * n_rows,
        "DUT": ["1"] * n_rows,
        "XPOS": [str(i % 30) for i in range(n_rows)],
        "YPOS": [str(i // 30) for i in range(n_rows)],
        "BIN": ["1"] * n_rows,
        "FAILTNO": [""] * n_rows,
    })
    for it in items:
        body[it] = np.round(rng.normal(0.5, 0.1, n_rows), 5).astype(str)
    return pd.concat([meta, body], ignore_index=True), items


def upload(n_sources: int, n_items: int, n_rows: int, lot_id: str):
    df, items = make_df(n_items, n_rows)
    data = encode_honeyform_parquet(df)
    files = [{"name": f"S{i:02d}", "filename": f"S{i:02d}.csv", "data": data}
             for i in range(n_sources)]
    manifest = {
        "meta": {"product_type": "PMIC", "product": "ETATEST",
                 "lot_id": lot_id, "file_name": "etatest"},
        "mode": "Normal",
        "sources": [{"index": i, "name": f["name"], "file_name": f["filename"]}
                    for i, f in enumerate(files)],
        "selected_items": [], "sheets": [], "options": {},
        "client": {"user": "etatest", "host": "etahost", "domain": ""},
    }
    result = wr_ingest.ingest_webreport(
        manifest, files, report_db=report_db, upload_root=UPLOAD_ROOT,
        client_ip="127.0.0.1", user_agent=UA["User-Agent"])
    return result, [decode_split_honeyform_parquet(
        f["data"], source=f["name"], file_name=f["filename"], keep_df=False) for f in files]


@pytest.fixture(autouse=True)
def _reset_eta_caches():
    eta._shape_cache.clear()
    eta._factor_cache = (0.0, 1.0)
    yield


def test_footer_shape_matches_decoded_tables():
    """예측(footer)과 기록(tables) 두 경로의 규모 정의가 어긋나면 배율 학습이 망가진다."""
    result, tables = upload(3, 40, 120, "ETA_SHAPE")
    from_tables = eta.shape_from_tables(tables)
    from_footer = eta.shape_from_storage(UPLOAD_ROOT, result["analysis_key"])
    assert from_footer == from_tables
    # 정의 자체도 확인 — 3소스 × 40항목 × 120행
    assert from_tables == (round(3 * 40 * 120 / 1e6, 4), round(3 * 40 / 1e3, 4))


def test_shape_ignores_meta_rows_and_columns():
    """META 7컬럼·메타 6행은 규모에서 빠진다 (parquet 원시 크기가 아니라 측정값 기준)."""
    result, _ = upload(1, 10, 50, "ETA_META")
    mcells, kcols = eta.shape_from_storage(UPLOAD_ROOT, result["analysis_key"])
    assert kcols == round(10 / 1e3, 4)              # META_COLUMNS 7개 제외
    assert mcells == round(10 * 50 / 1e6, 4)        # META_ROW_LABELS 6행 제외
    assert len(META_COLUMNS) == 7 and len(META_ROW_LABELS) == 6


def test_estimate_is_same_order_as_real_cold_build():
    """예상초가 실제 콜드 빌드와 같은 자릿수여야 안내로서 의미가 있다."""
    result, _ = upload(4, 300, 400, "ETA_REAL")
    sid, akey = result["session_id"], result["analysis_key"]
    _drop_all(akey, sid)

    session = report_db.get_session(sid)
    predicted = eta.session_eta(session, UPLOAD_ROOT)

    t0 = time.perf_counter()
    r = client.get(f"/pe/report/session/{sid}/full", headers=UA)
    while r.status_code == 202:
        time.sleep(0.05)
        r = client.get(f"/pe/report/session/{sid}/full", headers=UA)
    actual = time.perf_counter() - t0
    assert r.status_code == 200

    if predicted is None:          # 너무 빨라 안내 생략된 규모 — 실측도 짧아야 한다
        assert actual < eta.MIN_ANNOUNCE_SEC * 3
        return
    assert 0.2 <= predicted / actual <= 5.0, f"예상 {predicted}s vs 실측 {actual:.2f}s"


def test_building_response_and_build_status_carry_eta(monkeypatch):
    """202 와 build_status 둘 다 eta 를 실어야 프런트가 이르게·계속 안내할 수 있다."""
    result, _ = upload(2, 200, 300, "ETA_ROUTE")
    sid = result["session_id"]
    # 짧은 세션도 안내되도록 문턱을 낮춘다 (테스트 규모는 실제 대용량보다 훨씬 작다)
    monkeypatch.setattr(eta, "MIN_ANNOUNCE_SEC", 0.0)
    # 캐시를 지워 콜드를 만드는 대신 판정을 강제한다 — 프리웜/온디맨드가 지운 직후
    # 다시 채우는 경합 때문에 "지우고 요청" 방식은 간헐적으로 200 을 받는다. 여기서
    # 보려는 것은 캐시 동작이 아니라 202 응답에 eta 가 실리는지다.
    from web_report import service as wr_service
    monkeypatch.setattr(wr_service, "report_is_cold", lambda *a, **k: True)

    r = client.get(f"/pe/report/session/{sid}/full", headers=UA)
    assert r.status_code == 202, r.status_code
    body = r.get_json()
    assert body["building"] is True
    assert isinstance(body.get("eta"), (int, float)) and body["eta"] > 0

    # build_status 는 building 인 동안에만 eta 를 싣는다
    from web_report import build_status
    build_status.begin(sid, "report")
    try:
        s = client.get(f"/pe/report/session/{sid}/web_report/build_status",
                       headers=UA).get_json()
        assert s["state"] == "building"
        assert isinstance(s.get("eta"), (int, float))
    finally:
        build_status.end(sid, "report")
    idle = client.get(f"/pe/report/session/{sid}/web_report/build_status",
                      headers=UA).get_json()
    assert idle["state"] == "idle" and "eta" not in idle


def test_unknown_shape_returns_none():
    """로컬 parquet 이 없으면(S3 저장·삭제) 안내를 생략할 뿐 예외를 내지 않는다."""
    assert eta.shape_from_storage(UPLOAD_ROOT, "no_such_analysis_key") is None
    assert eta.session_eta({"analysis_key": "no_such_analysis_key"}, UPLOAD_ROOT) is None
    assert eta.session_eta({}, UPLOAD_ROOT) is None


def test_calibration_factor_learns_from_build_log(monkeypatch):
    """운영 실측이 벤치 예측의 3배면 배율도 3배 — 사양 차이를 흡수한다."""
    shape = (5.0, 2.0)
    pred = eta._raw_estimate(*shape)
    recs = [{"kind": "report", "result": "ok", "mcells": shape[0], "kcols": shape[1],
             "build": pred * 3.0} for _ in range(eta._FACTOR_FULL_SAMPLES)]
    monkeypatch.setattr(build_log, "history", lambda **kw: recs)
    assert eta.calibration_factor() == pytest.approx(3.0, rel=0.01)


def test_offloaded_record_carries_shape(monkeypatch):
    """워커 오프로드 빌드도 규모가 부모 레코드에 실려야 배율 학습이 끊기지 않는다.

    운영(workers>0)의 정상 경로다. 워커가 stash 한 finish dict 를 compute._stamp 가
    그대로 부모로 보내고, record_offloaded 가 키를 골라 담는다 — 그 키 목록에
    mcells/kcols 가 빠지면 실측이 영원히 학습되지 않는다.
    """
    written = []
    monkeypatch.setattr(build_log, "record", lambda rec: written.append(rec))
    # 자식 계산 2배 느림 + 풀 대기 1초 — 학습은 대기를 뺀 계산 시간만 봐야 한다
    build_sec = round(eta._raw_estimate(0.18, 0.6) * 2, 3)
    child_timing = {"t_start": 101.0, "t_end": 101.0 + build_sec,
                    "stages": {"decode": build_sec}, "sources": 3, "items": 200,
                    "mcells": 0.18, "kcols": 0.6}
    build_log.record_offloaded("report", "sid1", "akey", 100.0,
                               101.0 + build_sec + 0.5, child_timing)
    assert written, "레코드가 기록되지 않았다"
    rec = written[0]
    assert rec["mcells"] == 0.18 and rec["kcols"] == 0.6
    assert rec["build"] == pytest.approx(build_sec)   # 학습이 쓰는 값
    assert rec["pool_wait"] == pytest.approx(1.0)     # 대기는 따로 (학습 제외)

    # 그 레코드로 배율이 실제로 학습되는지까지 이어서 확인
    monkeypatch.setattr(build_log, "history",
                        lambda **kw: [dict(rec, result="ok", kind="report")]
                        * eta._FACTOR_FULL_SAMPLES)
    assert eta.calibration_factor() == pytest.approx(2.0, rel=0.01)


def test_calibration_shrinks_toward_one_with_few_samples(monkeypatch):
    """표본 1건이면 학습값을 1/5 만 반영 — 운영 첫 빌드부터 점진 보정하되 튀지 않는다."""
    shape = (5.0, 2.0)
    rec = {"kind": "report", "result": "ok", "mcells": shape[0], "kcols": shape[1],
           "build": eta._raw_estimate(*shape) * 3.0}
    monkeypatch.setattr(build_log, "history", lambda **kw: [rec])
    # (1건×3.0 + 4건분 1.0) / 5 = 1.4
    assert eta.calibration_factor() == pytest.approx(1.4, rel=0.01)


def test_calibration_ignores_records_without_shape(monkeypatch):
    """구버전 레코드(규모 미기록)만 있으면 배율 1.0 = 벤치 계수 그대로."""
    recs = [{"kind": "report", "result": "ok", "build": 99.0} for _ in range(20)]
    monkeypatch.setattr(build_log, "history", lambda **kw: recs)
    assert eta.calibration_factor() == 1.0


def test_calibration_rejects_absurd_ratio(monkeypatch):
    """타임아웃 등으로 오염된 극단 비율은 배율에 반영하지 않는다."""
    shape = (1.0, 1.0)
    pred = eta._raw_estimate(*shape)
    recs = [{"kind": "report", "result": "ok", "mcells": shape[0], "kcols": shape[1],
             "build": pred * 1000.0} for _ in range(20)]
    monkeypatch.setattr(build_log, "history", lambda **kw: recs)
    assert eta.calibration_factor() == 1.0


def test_disabled_by_env(monkeypatch):
    result, _ = upload(1, 20, 50, "ETA_OFF")
    monkeypatch.setattr(eta, "ENABLED", False)
    assert eta.session_eta(report_db.get_session(result["session_id"]), UPLOAD_ROOT) is None


# ── 헬퍼 (bench_webreport 와 동일 규약) ──────────────────────────────────────

def _settle(timeout: float = 300) -> None:
    from web_report import compute as wr_compute
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = wr_compute.status()
        if st["prewarm_pending"] == 0 and st["ondemand_pending"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("백그라운드 빌드가 끝나지 않음")


def _drop_all(akey: str, session_id: str | None = None) -> None:
    """RAM + 디스크 캐시 제거 → 완전 콜드.

    session_id 를 주면 실제로 콜드가 됐는지 확인한다 — 업로드 직후 프리웜이 카운터를
    0 으로 내린 뒤에도 디스크 산출물을 마저 쓰는 순간이 있어, 한 번의 삭제로는
    콜드가 보장되지 않는다(200 이 돌아와 202 검증이 헛돈다).
    """
    import shutil
    from web_report import cache as wr_cache
    from web_report import service as wr_service
    for _ in range(5):
        _settle()
        wr_cache.invalidate_caches(akey)
        for cache_dir in (UPLOAD_ROOT / "web_report").glob("*/cache"):
            shutil.rmtree(cache_dir, ignore_errors=True)
        if session_id is None:
            return
        if wr_service.report_is_cold(session_id, report_db=report_db,
                                     upload_root=UPLOAD_ROOT,
                                     session=report_db.get_session(session_id)):
            return
        time.sleep(0.1)
    raise AssertionError("콜드 상태를 만들지 못했다")

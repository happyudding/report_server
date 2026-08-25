"""source 이름만 바꾼 재업로드에서 캐시가 회수되는지 검증.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_webreport_rename_reupload.py

`analysis_key`/`content_hash` 산출식에는 source 이름이 없다(CLAUDE.md 규칙 #3 —
files 해시 + meta + selected_items). 그래서 같은 parquet 을 **이름만 바꿔** 재업로드하면
두 키가 그대로여서 dedup 으로 묶이고, manifest 는 새 이름으로 덮어써지는데 먼저 만들어진
형제 세션의 payload 캐시는 키가 하나도 안 바뀌어 옛 이름을 계속 서빙했다. 그 결과
갤러리(payload)와 Item Detail(`/scatter`, manifest 실시간)의 source 이름이 갈려 legend
색이 죽고(distColorMap 미스), 이름으로 매칭하는 Temperature 그룹 필터·Bin1(RT만)·
CT/HT 의 RT limit 참조가 에러 없이 어긋났다.

여기서 보는 것:
  (1) 이름이 그대로면 캐시를 건드리지 않는다 (기존 업로드 경로 무회귀 — 이게 깨지면
      모든 재업로드가 콜드가 된다)
  (2) 이름이 바뀌면 그 akey 의 디스크 캐시가 회수되고 tables 캐시가 새 이름이 된다
  (3) 회수 범위가 **그 akey 뿐**이다 (다른 akey 캐시는 살아남아야 한다 — 전 세션 콜드
      폭풍 방지)

S3 미설정 → 로컬 폴백 경로로 검증한다(개발 PC 기본 상태와 동일).
pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# config 는 import 시점에 env 를 읽는다 — 반드시 import 앞에서 지정할 것.
_TMP = Path(tempfile.mkdtemp(prefix="wr_rename_reupload_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""              # S3 비활성 → 로컬 폴백
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 워커 오프로드 없이 인라인(결정성)

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from report.report_extension import report_bp, init_app as init_report  # noqa: E402
from web_report import cache, cache_policy, disk_cache  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet,
)
from web_report.ingest import ingest_webreport  # noqa: E402

UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])
UA = "Mozilla/5.0 HoneyUser/renametester"
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


def upload(data: bytes, source_name: str, lot_id: str = "L1") -> dict:
    manifest = {
        "meta": {"product_type": "PMIC", "product": "RENAME", "lot_id": lot_id},
        "mode": "Normal",
        "sources": [{"name": source_name, "file_name": f"{source_name}.csv"}],
        "selected_items": [],
    }
    files = [{"name": "webreport_0", "filename": f"{source_name}.csv", "data": data}]
    return ingest_webreport(manifest, files, report_db=report_db,
                            upload_root=UPLOAD_ROOT, user_agent=UA)


def chash_of(result: dict) -> str:
    """content_hash 는 ingest 반환에 없다 — 세션 행에서 읽는다."""
    return report_db.get_session(result["session_id"])["content_hash"]


def sentinel_key(akey: str, chash: str) -> tuple:
    """프리웜이 만드는 캐시 파일과 겹치지 않는 전용 키.

    프리웜이 같은 세대(chash)의 report 를 저장해도 `_cleanup_stale_generations` 는
    **다른 chash 세대**만 지우므로 이 파일은 살아남는다 → 사라졌다면 그건 오직
    `drop_analysis` 때문이다.
    """
    return (akey, chash, "sentinel-for-test")


def put_sentinel(akey: str, chash: str) -> None:
    disk_cache.save_report(UPLOAD_ROOT, sentinel_key(akey, chash), {"stale": True})


def sentinel_alive(akey: str, chash: str) -> bool:
    return disk_cache.report_exists(UPLOAD_ROOT, sentinel_key(akey, chash))


def cached_source_names(akey: str, chash: str):
    tables = cache.cache_get(cache.TABLES_CACHE,
                             cache_policy.tables_key({"analysis_key": akey,
                                                      "content_hash": chash}))
    return [t.source for t in (tables or [])]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    app = Flask(__name__)
    app.register_blueprint(report_bp)
    init_report(app)                 # runtime 저장소 포트 주입
    report_db.init_report_db()

    data = make_parquet(0)

    # ── (a) 첫 업로드 ─────────────────────────────────────────────────────────
    first = upload(data, "6Z19AFA1_RT")
    akey, chash = first["analysis_key"], chash_of(first)
    assert cached_source_names(akey, chash) == ["6Z19AFA1_RT"], cached_source_names(akey, chash)
    print(f"(a) 첫 업로드 ok — akey={akey[:12]} source=6Z19AFA1_RT")

    # ── (b) 같은 이름으로 재업로드 → 캐시 유지 (기존 경로 무회귀) ────────────
    put_sentinel(akey, chash)
    same = upload(data, "6Z19AFA1_RT")
    assert same["analysis_key"] == akey, (same["analysis_key"], akey)
    assert chash_of(same) == chash, (chash_of(same), chash)
    assert sentinel_alive(akey, chash), "이름이 같은데 캐시를 지웠다 — 모든 재업로드가 콜드가 된다"
    print("(b) 같은 이름 재업로드 ok — akey/chash 동일, 캐시 유지")

    # ── (c) 다른 akey 의 캐시는 회수 대상이 아니다 ───────────────────────────
    other = upload(make_parquet(1), "OTHER_RT", lot_id="L2")
    o_akey, o_chash = other["analysis_key"], chash_of(other)
    assert o_akey != akey
    put_sentinel(o_akey, o_chash)

    # ── (d) 이름만 바꿔 재업로드 → 그 akey 캐시만 회수 ───────────────────────
    assert sentinel_alive(akey, chash)     # (b) 에서 심은 것이 아직 살아 있다
    renamed = upload(data, "NN_RT")
    assert renamed["analysis_key"] == akey, "이름은 akey 산출에 없어야 한다(규칙 #3)"
    assert chash_of(renamed) == chash, "이름은 content_hash 산출에도 없어야 한다"
    assert not sentinel_alive(akey, chash), \
        "이름이 바뀌었는데 옛 payload 캐시가 남았다 — legend 색·이름 매칭이 갈린다"
    assert sentinel_alive(o_akey, o_chash), \
        "다른 akey 캐시까지 지웠다 — 회수 범위가 너무 넓다(콜드 폭풍)"
    assert cached_source_names(akey, chash) == ["NN_RT"], cached_source_names(akey, chash)
    print("(d) 이름 변경 재업로드 ok — 그 akey 캐시만 회수, tables 는 새 이름")

    print("\n[통과] source 이름만 바꾼 재업로드의 캐시 회수 정상")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

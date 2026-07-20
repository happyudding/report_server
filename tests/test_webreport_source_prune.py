"""web_report source 축소 시 잔존물 정리 검증 — storage_gateway.save_webreport_sources.

실행:
    python tests/test_webreport_source_prune.py

Excel 시트 삭제로 source 가 3→2 로 줄면, 옛 source_2 의 object_info 행과 로컬
source_2.parquet 이 남을 수 있다. 로더는 object_info 를 idx 순으로 읽고(로컬 폴백은 파일
존재를 순차 스캔) 남은 것을 그대로 되살리므로, 저장 시점에 정리하지 않으면 지운 source 가
리포트에 다시 나타난다.

S3 미설정 → 로컬 폴백 경로로 검증한다(개발 PC 기본 상태와 동일).

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# config 는 import 시점에 env 를 읽는다 — 반드시 import 앞에서 지정할 것.
_TMP = Path(tempfile.mkdtemp(prefix="source_prune_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""          # S3 비활성 → 로컬 폴백

import storage_gateway  # noqa: E402
from database import report_db  # noqa: E402

AKEY = "ak_prune_test"
UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])


def source_rows():
    return sorted(
        o["object_type"] for o in report_db.get_all_object_infos(AKEY)
        if o["object_type"].startswith("web_report_source_"))


def source_files():
    d = UPLOAD_ROOT / "web_report" / AKEY
    return sorted(p.name for p in d.glob("source_*.parquet")) if d.is_dir() else []


def save(sources, names):
    manifest = {"sources": [{"name": n, "file_name": f"{n}.csv"} for n in names]}
    return storage_gateway.save_webreport_sources(
        AKEY, f"hash_{len(sources)}", sources, manifest, UPLOAD_ROOT)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        report_db.init_report_db()

        # ── (a) 3개 저장 ───────────────────────────────────────────────────────
        result = save([b"p0", b"p1", b"p2"], ["Lot0", "Lot1", "Lot2"])
        print(f"(a) 3개 저장: storage={result['storage']} rows={source_rows()} "
              f"files={source_files()}")
        assert result["storage"] == "local", result
        assert len(source_rows()) == 3 and len(source_files()) == 3

        # ── (b) 2개로 재저장 → 초과 idx 의 행·파일이 사라져야 한다 ──────────────
        result = save([b"p0_edited", b"p2"], ["Lot0", "Lot2"])
        rows, files = source_rows(), source_files()
        print(f"(b) 2개로 재저장: warnings={result['warnings']} rows={rows} files={files}")
        assert rows == ["web_report_source_0", "web_report_source_1"], rows
        assert files == ["source_0.parquet", "source_1.parquet"], files

        # ── (c) 재조회 시 삭제한 source 가 되살아나지 않는가 ────────────────────
        loaded, manifest = storage_gateway.load_webreport_sources(AKEY, UPLOAD_ROOT)
        names = [s["name"] for s in manifest["sources"]]
        print(f"(c) 재조회: sources={loaded} manifest={names}")
        assert loaded == [b"p0_edited", b"p2"], loaded
        assert names == ["Lot0", "Lot2"], names

        print("\nPASS — source 축소 시 초과 idx 행·파일 정리 + 재조회 부활 없음")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()

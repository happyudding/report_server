"""Raw Data 편집 전 원본 백업(1세대) E2E 검증.

실행:
    python tests/test_rawedit_backup.py

Raw Data 편집은 parquet 원본을 덮어쓴다 — 실수 편집 시 복구 수단이 없으면 데이터가
영구 유실된다. rawedit.backup_current_sources 가 그 직전에 1세대를 남기는지 확인한다.

시나리오 (상태 누적 — 순서 의존):
  (a) 1회 편집 → 백업 디렉토리에 편집 전 parquet + manifest 생성, 내용이 '편집 전' 값
  (b) 2회 편집 → 백업은 항상 1세대만 유지(직전 것으로 교체, 옛 세대 삭제)
  (c) 백업 실패 → 편집 자체가 거부되고 저장된 원본은 그대로 (유실 방지가 목적)
  (d) 세션 삭제(마지막 참조) 시 remove_backups 로 백업 디렉토리 회수 + 멱등

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

import pandas as pd  # noqa: E402

from web_report import rawedit, runtime, service  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, decode_honeyform_parquet, encode_honeyform_parquet,
)

SID = "1700000001_bkp001"
AKEY = "ak_backup_test"


def make_df():
    """최소 honeyform — ItemA 측정 4건 (편집 대상)."""
    cols = META_COLUMNS + ["ItemA"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1],
        ["TNO", "", "", "", "", "", "", 100],
        ["STEP", "", "", "", "", "", "", "P2"],
        ["UNIT", "", "", "", "", "", "", "V"],
        ["HILIM", "", "", "", "", "", "", 10],
        ["LOLIM", "", "", "", "", "", "", 0],
        ["s1", 1, 1, 0, 0, 1, "", 5],
        ["s2", 1, 1, 1, 0, 1, "", 6],
        ["s3", 1, 1, 2, 0, 1, "", 7],
        ["s4", 1, 1, 3, 0, 1, "", 8],
    ]
    return pd.DataFrame(rows, columns=cols)


class FakeStorage:
    """StoragePort 최소 구현 — parquet/manifest 를 메모리에 보관.

    fail_load=True 면 load_webreport_sources 가 터진다 (백업 실패 재현용 — 실제 운영의
    S3 장애/디스크 오류에 해당).
    """

    def __init__(self, sources, manifest):
        self.sources = list(sources)
        self.manifest = dict(manifest)
        self.fail_load = False

    def load_webreport_sources(self, analysis_key, upload_root):
        if self.fail_load:
            raise RuntimeError("storage down (백업 재현용)")
        return list(self.sources), dict(self.manifest)

    def load_webreport_manifest(self, analysis_key, upload_root):
        return dict(self.manifest)

    def save_webreport_sources(self, analysis_key, content_hash, sources_bytes,
                               manifest, *, upload_root):
        self.sources = list(sources_bytes)
        self.manifest = dict(manifest)
        return {"storage": "local"}


class FakeReportDB:
    def __init__(self, session):
        self.session = session
        self.audits = []

    def get_session(self, session_id):
        return dict(self.session) if session_id == self.session["session_id"] else None

    def update_session(self, session_id, **fields):
        self.session.update(fields)

    def log_audit(self, action, **kw):
        self.audits.append((action, kw))


def item_values(parquet_bytes):
    """parquet → ItemA 측정값 리스트 (백업/원본 내용 비교용)."""
    df = decode_honeyform_parquet(parquet_bytes)
    meta_rows = {"TSEQ", "TNO", "STEP", "UNIT", "HILIM", "LOLIM"}
    key = df.columns[0]
    body = df[~df[key].isin(meta_rows)]
    return [str(v) for v in body["ItemA"].tolist()]


def backup_dirs(upload_root):
    root = Path(upload_root) / "webreport_backup" / AKEY
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    tmp = Path(tempfile.mkdtemp(prefix="rawedit_backup_test_"))
    try:
        original = encode_honeyform_parquet(make_df())
        # sources[].name 이 loader 가 붙이는 source 명 (편집 요청의 "source" 와 일치해야 함)
        manifest = {"sources": [{"name": "src0", "file_name": "src0.csv"}],
                    "selected_items": ["ItemA"], "sheets": ["Yield"],
                    "meta": {"product_type": "MDDI"}}
        storage = FakeStorage([original], manifest)
        runtime.configure(storage)

        session = {"session_id": SID, "analysis_key": AKEY, "source": "web_report",
                   "product_type": "MDDI", "product": "PRODX", "lot_id": "LOT1",
                   "file_name": "t.xlsx", "mode": "Normal", "content_hash": "hash_v0",
                   "webreport_options": ""}
        db = FakeReportDB(session)

        before = item_values(storage.sources[0])
        print(f"(0) 업로드 원본 ItemA = {before}")
        assert before == ["5", "6", "7", "8"], before

        # ── (a) 1회 편집 → 직전 원본이 백업으로 남는가 ────────────────────────
        service.edit_raw_data(
            SID, report_db=db, upload_root=tmp,
            edits=[{"source": "src0", "row_idx":0, "column": "ItemA", "value": "99"}])
        dirs = backup_dirs(tmp)
        assert len(dirs) == 1, f"백업 1세대여야 함: {dirs}"
        assert (dirs[0] / "manifest.json").exists(), "manifest 백업 누락"
        backed = item_values((dirs[0] / "source_0.parquet").read_bytes())
        now = item_values(storage.sources[0])
        print(f"(a) 편집 후 저장본 = {now} / 백업본 = {backed} (dir={dirs[0].name})")
        assert now[0] == "99", f"편집이 반영되지 않음: {now}"
        assert backed == ["5", "6", "7", "8"], f"백업이 '편집 전' 값이 아님: {backed}"
        assert "backup=" in db.audits[-1][1]["changed_fields"], db.audits[-1]

        # ── (b) 2회 편집 → 1세대만 유지(직전 것으로 교체) ─────────────────────
        db.session["content_hash"] = "hash_v1"
        service.edit_raw_data(
            SID, report_db=db, upload_root=tmp,
            edits=[{"source": "src0", "row_idx":1, "column": "ItemA", "value": "77"}])
        dirs2 = backup_dirs(tmp)
        backed2 = item_values((dirs2[0] / "source_0.parquet").read_bytes())
        print(f"(b) 2회 편집 후 백업 세대수 = {len(dirs2)} / 백업본 = {backed2}")
        assert len(dirs2) == 1, f"1세대만 남아야 함: {[d.name for d in dirs2]}"
        assert backed2 == ["99", "6", "7", "8"], f"직전(1회 편집 후) 상태여야 함: {backed2}"

        # ── (c) 백업 실패 → 편집 거부 + 원본 무손상 ───────────────────────────
        saved_before = list(storage.sources)
        storage.fail_load = True
        try:
            service.edit_raw_data(
                SID, report_db=db, upload_root=tmp,
                edits=[{"source": "src0", "row_idx":2, "column": "ItemA", "value": "0"}])
            raise AssertionError("백업 실패인데 편집이 통과했다 (유실 위험)")
        except RuntimeError as exc:
            print(f"(c) 백업 실패 시 편집 거부됨: {exc}")
        storage.fail_load = False
        assert storage.sources == saved_before, "편집이 거부됐는데 원본이 바뀌었다"
        print(f"(c) 원본 무손상 확인 = {item_values(storage.sources[0])}")

        # ── (d) 백업 정리 — 세션 삭제(마지막 참조) 시 백업 디렉토리 회수 ───────
        assert rawedit.remove_backups(AKEY, tmp) is True, "백업이 있는데 False 반환"
        assert backup_dirs(tmp) == [], "백업 디렉토리가 남았다"
        assert rawedit.remove_backups(AKEY, tmp) is False, "없는 백업에 True 반환(멱등 아님)"
        print("(d) 백업 정리 확인 — remove_backups 후 디렉토리 없음 + 재호출 False")

        print("\nPASS — rawedit 백업 1세대 + 실패 시 편집 거부 + 삭제 시 정리")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

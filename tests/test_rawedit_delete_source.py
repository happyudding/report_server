"""Excel 왕복 편집의 source 삭제(시트 삭제) 반영 검증 — rawedit.replace_sources.

실행:
    python tests/test_rawedit_delete_source.py

시트를 지우면 그 source 를 물리 제거한다 — 되돌릴 수 없는 동작이라 (1) 남긴 source 지정이
엄격히 검증되는지, (2) manifest 의 sources 목록이 함께 축소되는지(안 하면 idx↔parquet 대응이
어긋난다), (3) 삭제 전 원본이 백업되는지, (4) dedup 형제 세션의 content_hash 까지 갱신되는지를
확인한다.

parquet 디코딩은 스텁으로 대체한다 — 여기서 검증할 대상은 replace_sources 의 검증·장부
처리이지 honeyform 인코딩이 아니다(그쪽은 test_rawedit_backup.py 가 실물로 다룬다).

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

# honeyform 이 모듈 상단에서 pandas 를 import 한다 — 이 테스트는 디코딩을 스텁으로 대체하므로
# pandas 가 없는 환경에서도 돌 수 있게 빈 모듈로 채운다(설치돼 있으면 진짜 모듈 사용).
try:
    import pandas  # noqa: F401
except ImportError:
    sys.modules["pandas"] = types.ModuleType("pandas")

from web_report import rawedit, runtime  # noqa: E402

AKEY = "ak_delete_test"
SID = "1700000002_del001"
SIB = "1700000003_del002"          # 같은 analysis_key 를 공유하는 dedup 형제 세션


class FakeStorage:
    def __init__(self, sources, manifest):
        self.sources = list(sources)
        self.manifest = dict(manifest)

    def load_webreport_sources(self, analysis_key, upload_root):
        return list(self.sources), dict(self.manifest)

    def load_webreport_manifest(self, analysis_key, upload_root):
        return dict(self.manifest)

    def save_webreport_sources(self, analysis_key, content_hash, sources_bytes,
                               manifest, *, upload_root):
        self.sources = list(sources_bytes)
        self.manifest = dict(manifest)
        return {"storage": "local"}


class FakeReportDB:
    """세션 2건(형제) + object_info 를 들고 있는 최소 구현."""

    def __init__(self, sessions, source_count):
        self.sessions = {s["session_id"]: s for s in sessions}
        self.source_count = source_count
        self.audits = []

    def get_session(self, session_id):
        s = self.sessions.get(session_id)
        return dict(s) if s else None

    def get_all_object_infos(self, analysis_key):
        return [{"object_type": f"web_report_source_{i}"} for i in range(self.source_count)]

    def update_session(self, session_id, **fields):
        self.sessions[session_id].update(fields)

    def update_content_hash_for_analysis_key(self, analysis_key, content_hash):
        n = 0
        for s in self.sessions.values():
            if s.get("analysis_key") == analysis_key:
                s["content_hash"] = content_hash
                n += 1
        return n

    def log_audit(self, action, **kw):
        self.audits.append((action, kw))


def make_env(tmp, source_count=3):
    sources = [f"parquet_{i}".encode() for i in range(source_count)]
    manifest = {
        "sources": [{"name": f"Lot{i}", "file_name": f"lot{i}.csv"} for i in range(source_count)],
        "selected_items": ["ItemA"], "meta": {"product_type": "MDDI"},
    }
    storage = FakeStorage(sources, manifest)
    runtime.configure(storage)
    sessions = [
        {"session_id": SID, "analysis_key": AKEY, "product_type": "MDDI", "product": "P",
         "lot_id": "L", "file_name": "f.xlsx", "content_hash": "hash_v0"},
        {"session_id": SIB, "analysis_key": AKEY, "product_type": "MDDI", "product": "P",
         "lot_id": "L", "file_name": "f.xlsx", "content_hash": "hash_v0"},
    ]
    return storage, FakeReportDB(sessions, source_count)


def expect_reject(db, tmp, sources, kept, hint):
    """검증 실패 케이스 — ValueError 로 거부되고 저장된 원본이 그대로여야 한다."""
    storage = runtime.storage()
    before = list(storage.sources), dict(storage.manifest)
    try:
        rawedit.replace_sources(SID, report_db=db, upload_root=tmp,
                                sources_bytes=sources, kept_indices=kept)
    except ValueError as exc:
        print(f"  거부됨({hint}): {exc}")
    else:
        raise AssertionError(f"{hint}: 거부돼야 하는데 통과했다 (kept={kept})")
    assert (list(storage.sources), dict(storage.manifest)) == before, \
        f"{hint}: 거부됐는데 저장된 원본이 바뀌었다"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # 디코딩 검증은 이 테스트의 대상이 아니다 — 통과시키고 장부 처리만 본다.
    rawedit.decode_honeyform_parquet = lambda data: None

    tmp = Path(tempfile.mkdtemp(prefix="rawedit_delete_test_"))
    try:
        # ── (a) 3 → 2 삭제 성공 ────────────────────────────────────────────────
        storage, db = make_env(tmp)
        new_sources = [b"parquet_0_edited", b"parquet_2"]
        result = rawedit.replace_sources(
            SID, report_db=db, upload_root=tmp,
            sources_bytes=new_sources, kept_indices=[0, 2])
        names = [s["name"] for s in storage.manifest["sources"]]
        print(f"(a) 3→2 삭제: result={result} manifest sources={names}")
        assert result["sources"] == 2 and result["removed"] == 1, result
        assert storage.sources == new_sources, storage.sources
        assert names == ["Lot0", "Lot2"], names

        # 형제 세션까지 새 content_hash 로 갱신됐는가 (stale 캐시 서빙 방지)
        hashes = {sid: s["content_hash"] for sid, s in db.sessions.items()}
        print(f"(a) 세션별 content_hash = {hashes}")
        assert hashes[SID] == hashes[SIB] != "hash_v0", hashes

        # 삭제된 source 이름이 감사에 남는가 + 백업에 삭제 전 3개가 있는가
        changed = db.audits[-1][1]["changed_fields"]
        backup_root = tmp / "webreport_backup" / AKEY
        backup_dirs = [p for p in backup_root.iterdir() if p.is_dir()]
        backed = sorted(p.name for p in backup_dirs[0].glob("source_*.parquet"))
        print(f"(a) audit={changed}\n(a) 백업 파일={backed}")
        assert "removed=['Lot1']" in changed, changed
        assert backed == ["source_0.parquet", "source_1.parquet", "source_2.parquet"], backed

        # ── (b) 하위호환: kept_indices 없이 동수 교체는 그대로 통과 ─────────────
        storage, db = make_env(tmp)
        result = rawedit.replace_sources(
            SID, report_db=db, upload_root=tmp,
            sources_bytes=[b"a", b"b", b"c"], kept_indices=None)
        print(f"(b) 구클라(동수, indices 없음): result={result}")
        assert result["removed"] == 0 and storage.sources == [b"a", b"b", b"c"], result
        assert len(storage.manifest["sources"]) == 3, storage.manifest

        # ── (c) 검증 거부 케이스 ───────────────────────────────────────────────
        storage, db = make_env(tmp)
        rawedit.remove_backups(AKEY, tmp)      # 앞 단계 백업 제거 — 아래에서 '새로 생기지 않음'을 본다
        expect_reject(db, tmp, [b"a", b"b"], None, "구클라인데 개수 감소")
        expect_reject(db, tmp, [b"a", b"b"], [], "빈 indices")
        expect_reject(db, tmp, [b"a", b"b"], [0, 0], "중복")
        expect_reject(db, tmp, [b"a", b"b"], [2, 0], "내림차순")
        expect_reject(db, tmp, [b"a", b"b"], [0, 5], "범위 밖")
        expect_reject(db, tmp, [b"a", b"b"], [0, 1, 2], "업로드 개수와 불일치")
        expect_reject(db, tmp, [b"a", b"b", b"c", b"d"], [0, 1, 2, 3], "source 추가")

        # 거부만 반복했으니 백업도 남지 않아야 한다(덮어쓰기 자체가 없었음)
        assert not (tmp / "webreport_backup" / AKEY).exists(), "거부된 요청이 백업을 남겼다"
        print("(c) 거부 7종 — 원본·백업 무변동 확인")

        # 전체 유지 indices(= range)는 삭제 없는 교체와 같으므로 통과한다
        result = rawedit.replace_sources(
            SID, report_db=db, upload_root=tmp,
            sources_bytes=[b"a", b"b", b"c"], kept_indices=[0, 1, 2])
        print(f"(c) 전체 유지 indices 통과: result={result}")
        assert result["removed"] == 0, result
        assert len(runtime.storage().manifest["sources"]) == 3, runtime.storage().manifest

        # ── (d) legacy: object_info 무기록이면 manifest 로 기존 개수를 잡는다 ───
        storage, db = make_env(tmp)
        db.source_count = 0
        result = rawedit.replace_sources(
            SID, report_db=db, upload_root=tmp,
            sources_bytes=[b"only"], kept_indices=[1])
        print(f"(d) legacy(무기록) 3→1: manifest sources="
              f"{[s['name'] for s in storage.manifest['sources']]}")
        assert result["removed"] == 2, result
        assert [s["name"] for s in storage.manifest["sources"]] == ["Lot1"], storage.manifest

        print("\nPASS — source 삭제 반영(manifest 축소·형제 hash 동기화·백업) + 검증 거부 7종")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

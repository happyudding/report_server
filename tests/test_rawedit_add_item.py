"""신규 수식 item 추가의 서버 반영 검증 — rawedit.replace_sources(add_items, rows_preserved).

실행:
    python tests/test_rawedit_add_item.py

이 경로가 깨지는 방식은 전부 **조용하다** — 에러도 안 나고 화면도 멀쩡한데 값만 사라진다:
  · manifest.selected_items 에 신규 이름을 안 넣으면 parquet 에는 컬럼이 있는데 리포트
    어디에도 안 보인다(build_report_payload 등 8곳이 selected_items 로 item 을 거른다).
  · 반대로 selected_items 가 비어 있는 세션에 이름을 넣으면 **다른 항목이 전부 사라진다**
    (빈 값 = 전 항목 통과인데, 한 개짜리 목록을 만들면 그것만 남는 필터가 된다).
  · rows_preserved 를 무시하고 전처리 셀 패치를 지우면 사용자가 빠른 수정으로 넣은 값이
    소리 없이 없어진다(CLAUDE.md §5-12 — 다시 입력할 방법이 없다).
  · analysis_key 를 건드리면 세션이 자기 산출물을 못 찾는다.

검증 항목:
  (a) selected_items 가 **비어 있으면** manifest 불변
  (b) selected_items 가 차 있으면 신규 이름이 끝에 1회만 append (반복 호출 멱등)
  (c) analysis_key 불변 · content_hash 변경 · dedup 형제 세션까지 갱신
  (d) **rows_preserved=True 면 전처리 셀 패치가 살아 있다** (이 파일의 핵심)
  (e) rows_preserved 기본값(False)에서는 기존대로 해제 (Excel 왕복 무회귀)
  (f) 업로드 parquet 에 없는 이름 → ValueError(400)
  (g) add_items 상한·길이 초과 → ValueError
  (h) 감사 로그가 add_item 경로와 이름을 남긴다
  (i) add_items=None 경로가 기존과 완전히 동일 (Excel 왕복 회귀 가드)

parquet 검증·프리웜은 test_rawedit_delete_source.py 와 같은 이유로 스텁이다 — 여기서 볼 것은
replace_sources 의 장부 처리이지 honeyform 인코딩이 아니다.

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

try:
    import pandas  # noqa: F401
except ImportError:
    sys.modules["pandas"] = types.ModuleType("pandas")

from web_report import edits, rawedit, runtime  # noqa: E402

AKEY = "ak_add_item_test"
SID = "1700000010_add001"
SIB = "1700000011_add002"          # dedup 형제 세션
NEW = "VREF_MARGIN"


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


# 업로드된 parquet 이 가진 item 이름 — 스텁 parquet_item_columns 가 돌려준다.
_PRESENT = {"ItemA", "ItemB", NEW}
_dropped_calls = []


def make_env(tmp, selected_items, source_count=2):
    sources = [f"parquet_{i}".encode() for i in range(source_count)]
    manifest = {
        "sources": [{"name": f"Lot{i}", "file_name": f"lot{i}.csv"}
                    for i in range(source_count)],
        "selected_items": list(selected_items), "meta": {"product_type": "MDDI"},
    }
    storage = FakeStorage(sources, manifest)
    runtime.configure(storage)
    sessions = [
        {"session_id": SID, "analysis_key": AKEY, "product_type": "MDDI", "product": "P",
         "lot_id": "L", "file_name": "f.xlsx", "content_hash": "hash_v0"},
        {"session_id": SIB, "analysis_key": AKEY, "product_type": "MDDI", "product": "P",
         "lot_id": "L", "file_name": "f.xlsx", "content_hash": "hash_v0"},
    ]
    _dropped_calls.clear()
    return storage, FakeReportDB(sessions, source_count)


def add_item(db, tmp, *, add_items=(NEW,), rows_preserved=True, sources=None):
    return rawedit.replace_sources(
        SID, report_db=db, upload_root=tmp,
        sources_bytes=sources if sources is not None else [b"p0_new", b"p1_new"],
        add_items=list(add_items) if add_items is not None else None,
        rows_preserved=rows_preserved)


def expect_reject(db, tmp, hint, **kw):
    storage = runtime.storage()
    before = list(storage.sources), dict(storage.manifest)
    try:
        add_item(db, tmp, **kw)
    except ValueError as exc:
        print(f"  거부됨({hint}): {exc}")
    else:
        raise AssertionError(f"{hint}: 거부돼야 하는데 통과했다")
    assert (list(storage.sources), dict(storage.manifest)) == before, \
        f"{hint}: 거부됐는데 저장된 원본이 바뀌었다"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    rawedit.validate_parquet_bytes = lambda data: None
    rawedit.parquet_item_columns = lambda data: sorted(_PRESENT)
    from web_report import compute as _compute
    _compute.prewarm = lambda *a, **kw: None

    # 전처리 셀 패치 해제 호출을 기록만 한다 (진짜 DB 가 없다).
    def _spy_drop(report_db, analysis_key, user_agent=""):
        _dropped_calls.append(analysis_key)
        return 3
    edits.drop_preprocess_edits_for_akey = _spy_drop

    tmp = Path(tempfile.mkdtemp(prefix="rawedit_add_item_test_"))
    try:
        # ── (a) selected_items 가 비면 manifest 불변 ────────────────────────────
        storage, db = make_env(tmp, [])
        result = add_item(db, tmp)
        print(f"(a) selected_items=[] → manifest.selected_items={storage.manifest['selected_items']}")
        assert storage.manifest["selected_items"] == [], storage.manifest
        assert result["added_items"] == 1, result

        # ── (b) 차 있으면 끝에 1회만 append (멱등) ──────────────────────────────
        storage, db = make_env(tmp, ["ItemA", "ItemB"])
        add_item(db, tmp)
        sel1 = list(storage.manifest["selected_items"])
        add_item(db, tmp)                      # 같은 이름 재신고
        sel2 = list(storage.manifest["selected_items"])
        print(f"(b) 1회={sel1}  2회={sel2}")
        assert sel1 == ["ItemA", "ItemB", NEW], sel1
        assert sel2 == sel1, "같은 이름을 두 번 넣었다"

        # ── (c) analysis_key 불변 · content_hash · 형제 세션 ────────────────────
        storage, db = make_env(tmp, ["ItemA"])
        add_item(db, tmp)
        keys = {sid: s["analysis_key"] for sid, s in db.sessions.items()}
        hashes = {sid: s["content_hash"] for sid, s in db.sessions.items()}
        print(f"(c) analysis_key={keys}\n(c) content_hash={hashes}")
        assert set(keys.values()) == {AKEY}, keys
        assert hashes[SID] == hashes[SIB] != "hash_v0", hashes

        # ── (d) rows_preserved=True → 전처리 셀 패치 유지 (핵심) ────────────────
        storage, db = make_env(tmp, ["ItemA"])
        add_item(db, tmp, rows_preserved=True)
        print(f"(d) rows_preserved=True → drop 호출 {len(_dropped_calls)}회")
        assert _dropped_calls == [], "열만 추가했는데 전처리 셀 패치를 지웠다"

        # ── (e) 기본값(False)에서는 기존대로 해제 ───────────────────────────────
        storage, db = make_env(tmp, ["ItemA"])
        add_item(db, tmp, add_items=None, rows_preserved=False)
        print(f"(e) rows_preserved=False → drop 호출 {len(_dropped_calls)}회")
        assert _dropped_calls == [AKEY], _dropped_calls

        # ── (f) 업로드 parquet 에 없는 이름 → 거부 ──────────────────────────────
        storage, db = make_env(tmp, ["ItemA"])
        expect_reject(db, tmp, "없는 item", add_items=["NOT_IN_PARQUET"])

        # ── (g) 상한·길이 초과 → 거부 ───────────────────────────────────────────
        storage, db = make_env(tmp, ["ItemA"])
        expect_reject(db, tmp, "개수 상한",
                      add_items=[f"X{i}" for i in range(rawedit._MAX_ADD_ITEMS + 1)])
        expect_reject(db, tmp, "이름 길이", add_items=["Z" * 201])

        # ── (h) 감사 로그 ───────────────────────────────────────────────────────
        storage, db = make_env(tmp, ["ItemA"])
        add_item(db, tmp)
        changed = db.audits[-1][1]["changed_fields"]
        print(f"(h) audit={changed}")
        assert "raw_data(add_item" in changed, changed
        assert NEW in changed, changed
        assert "quick_edits_cleared" not in changed, changed

        # ── (i) Excel 왕복 무회귀: add_items 없이 부르면 종전과 동일 ─────────────
        storage, db = make_env(tmp, ["ItemA", "ItemB"])
        result = rawedit.replace_sources(
            SID, report_db=db, upload_root=tmp, sources_bytes=[b"a", b"b"])
        changed = db.audits[-1][1]["changed_fields"]
        print(f"(i) 구경로 result={result}\n(i) audit={changed}")
        assert result["added_items"] == 0, result
        assert storage.manifest["selected_items"] == ["ItemA", "ItemB"], storage.manifest
        assert "raw_data(excel" in changed and "added_items" not in changed, changed
        assert _dropped_calls == [AKEY], "Excel 왕복이 전처리 해제를 안 했다"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("[통과] 신규 item 반영 - manifest/전처리/감사 계약 정상")


if __name__ == "__main__":
    main()

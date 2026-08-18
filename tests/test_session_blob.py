"""세션 단위 큰 본문(Note 시트) 객체 분리 검증 — 2026-08-14 세션 DB 개선.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_session_blob.py

배경: Note 시트 JSON 은 이미지가 base64 로 들어와 최대 10MB 인데 SQLite TEXT 컬럼에
그대로 들어갔다. 본문을 객체 저장으로 빼고 DB 에는 포인터만 남기되, **운영 중인 서버라
기존 세션이 한 순간도 안 열려서는 안 된다** — 그래서 전환 기간에는 dual-write 를 하고
읽기는 blob 우선 + legacy 폴백으로 간다. 이 테스트가 검증하는 것이 그 호환성이다.

시나리오:
  (a) 저장 → 포인터 행 + 객체 파일 생성, legacy 행도 함께 유지 (dual-write)
  (b) 조회는 blob 을 읽는다 (본문이 legacy 와 다르게 조작돼 있어도 blob 값이 나온다)
  (c) 객체가 사라지면 legacy 행으로 폴백 (무손실)
  (d) legacy 행만 있는 구세션(전환 전 저장분)은 종전대로 읽힌다
  (e) 포인터만 있고 legacy 가 없는 상태(cutover 이후)도 읽기·메타·잠금이 동작
  (f) 삭제(clear)는 포인터·객체·legacy 를 모두 정리
  (g) backfill 도구가 legacy 본문을 객체로 옮기고 포인터를 확정 (멱등)
  (h) 세션 삭제 시 객체 파일이 남지 않는다
  (i) cleanup 고아 스캔이 세션 축(note_img/session_blob)·akey 축(issue_img 등)을 모두 훑고,
      dry-run 에서는 지우지 않으며, 참조 세션이 있는 blob 은 건드리지 않는다

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="session_blob_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""          # S3 비활성 → 로컬 확정 저장

from database import report_db  # noqa: E402
import storage_gateway  # noqa: E402

report_db.init_report_db()

AKEY = "b" * 64
KIND, ITEM = "note_sheet", "sheet"


def _mk_session(sid):
    report_db.create_session(sid, "note.parquet", None, product_type="MDDI",
                             lot_id="LOT1", product="P1", source="web_report",
                             uploaded_by="alice")
    report_db.update_session(sid, analysis_key=AKEY, status="done")


def _sheet(text):
    return json.dumps({"sheets": [{"name": "Sheet1",
                                   "celldata": [{"r": 0, "c": 0, "v": {"v": text}}]}]},
                      ensure_ascii=False)


def _save(sid, body, base=None, force=True):
    return report_db.save_note_sheet_checked(sid, KIND, ITEM, body, base,
                                             updated_by="alice", force=force)


def _read(sid):
    rows = report_db.get_webreport_edits(sid, kinds=(KIND,))
    for r in rows:
        if r["item_key"] == ITEM:
            return r["value"]
    return None


def _legacy_value(sid):
    """DB 에 실제로 들어 있는 legacy 행 값 (blob 치환을 거치지 않은 원본)."""
    with report_db.get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM report_webreport_edit "
            "WHERE session_id=? AND kind=? AND item_key=?", (sid, KIND, ITEM)).fetchone()
    return row["value"] if row else None


def test_dual_write_creates_pointer_and_object():
    """(a) 저장하면 포인터 행 + 객체 파일이 생기고 legacy 행도 남는다."""
    sid = "s-blob-a"
    _mk_session(sid)
    body = _sheet("첫 저장")
    ok, info = _save(sid, body)
    assert ok, info

    ptr = report_db.get_session_blob(sid, KIND)
    assert ptr, "포인터 행이 없다"
    assert ptr["backend"] == "local", ptr
    assert ptr["content_encoding"] == "gzip", ptr
    assert ptr["content_hash"] == hashlib.sha256(body.encode("utf-8")).hexdigest(), ptr
    assert ptr["base_token"] == report_db.note_base_token(body), ptr

    path = Path(os.environ["REPORT_UPLOAD_DIR"]) / "session_blob" / ptr["object_key"]
    assert path.exists(), f"객체 파일이 없다: {path}"
    assert gzip.decompress(path.read_bytes()).decode("utf-8") == body

    assert _legacy_value(sid) == body, "dual-write 인데 legacy 행이 비었다"
    print("  (a) dual-write - 포인터+객체+legacy 동시 기록 OK")


def test_read_prefers_blob():
    """(b) 조회는 blob 을 읽는다 — legacy 를 몰래 바꿔도 blob 값이 나온다."""
    sid = "s-blob-b"
    _mk_session(sid)
    body = _sheet("blob 본문")
    assert _save(sid, body)[0]
    with report_db.get_conn() as conn:
        conn.execute("UPDATE report_webreport_edit SET value=? "
                     "WHERE session_id=? AND kind=? AND item_key=?",
                     (_sheet("legacy 본문"), sid, KIND, ITEM))
    assert _read(sid) == body, "blob 우선 읽기가 아니다"
    print("  (b) 조회는 blob 우선 OK")


def test_fallback_to_legacy_when_object_missing():
    """(c) 객체가 사라져도 legacy 행이 서빙된다 (사용자 입력 무손실)."""
    sid = "s-blob-c"
    _mk_session(sid)
    body = _sheet("살아남아야 할 본문")
    assert _save(sid, body)[0]
    ptr = report_db.get_session_blob(sid, KIND)
    (Path(os.environ["REPORT_UPLOAD_DIR"]) / "session_blob" / ptr["object_key"]).unlink()
    assert _read(sid) == body, "객체 유실 시 legacy 폴백이 안 된다"
    print("  (c) 객체 유실 → legacy 폴백 OK")


def test_legacy_only_session_reads():
    """(d) 전환 전에 저장된 세션(legacy 행만) 은 종전대로 읽힌다."""
    sid = "s-blob-d"
    _mk_session(sid)
    body = _sheet("구세션 본문")
    with report_db.get_conn() as conn:
        conn.execute(
            "INSERT INTO report_webreport_edit "
            "(session_id, kind, item_key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, 1, 'alice')", (sid, KIND, ITEM, body))
    assert report_db.get_session_blob(sid, KIND) is None
    assert _read(sid) == body
    meta = report_db.get_webreport_edit_meta(sid, KIND)
    assert meta and meta[0]["item_key"] == ITEM, meta
    # 낙관적 잠금도 legacy 값 기준으로 종전과 동일하게 동작한다.
    ok, info = report_db.save_note_sheet_checked(sid, KIND, ITEM, _sheet("새 본문"),
                                                 "틀린base", force=False)
    assert not ok and info["base"] == report_db.note_base_token(body), info
    print("  (d) legacy 전용 세션 조회/메타/잠금 OK")


def test_pointer_only_session():
    """(e) legacy 행 없이 포인터만 있어도(cutover 이후) 조회·메타·잠금이 동작한다."""
    sid = "s-blob-e"
    _mk_session(sid)
    body = _sheet("cutover 이후 본문")
    assert _save(sid, body)[0]
    with report_db.get_conn() as conn:      # cutover 로 legacy 본문을 지운 상태를 흉내
        conn.execute("DELETE FROM report_webreport_edit "
                     "WHERE session_id=? AND kind=? AND item_key=?", (sid, KIND, ITEM))
    assert _read(sid) == body, "포인터만 있을 때 본문을 못 읽는다"
    meta = report_db.get_webreport_edit_meta(sid, KIND)
    assert meta and meta[0]["updated_by"] == "alice", meta
    ok, info = report_db.save_note_sheet_checked(sid, KIND, ITEM, _sheet("x"),
                                                 "틀린base", force=False)
    assert not ok and info["base"] == report_db.note_base_token(body), info
    ok, _ = report_db.save_note_sheet_checked(sid, KIND, ITEM, _sheet("정상 갱신"),
                                              report_db.note_base_token(body),
                                              force=False)
    assert ok, "포인터 base_token 으로 잠금 통과가 안 된다"
    print("  (e) 포인터 전용 세션 조회/메타/잠금 OK")


def test_clear_removes_everything():
    """(f) clear(blob=None)는 포인터·객체·legacy 를 모두 지운다."""
    sid = "s-blob-f"
    _mk_session(sid)
    assert _save(sid, _sheet("지워질 본문"))[0]
    ptr = report_db.get_session_blob(sid, KIND)
    path = Path(os.environ["REPORT_UPLOAD_DIR"]) / "session_blob" / ptr["object_key"]
    assert path.exists()
    assert _save(sid, None)[0]
    assert report_db.get_session_blob(sid, KIND) is None, "포인터가 남았다"
    assert _legacy_value(sid) is None, "legacy 행이 남았다"
    assert not path.exists(), "객체 파일이 남았다"
    assert _read(sid) is None
    print("  (f) clear - 포인터/객체/legacy 정리 OK")


def test_backfill_is_idempotent():
    """(g) backfill 도구가 legacy 본문을 객체로 옮기고, 재실행해도 중복 작업이 없다."""
    sid = "s-blob-g"
    _mk_session(sid)
    body = _sheet("backfill 대상")
    with report_db.get_conn() as conn:
        conn.execute(
            "INSERT INTO report_webreport_edit "
            "(session_id, kind, item_key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, 1, 'alice')", (sid, KIND, ITEM, body))
    sys.path.insert(0, os.path.join(_ROOT, "server", "tools"))
    import migrate_session_db as mig

    result = mig.step_noteblob(report_db, dry_run=False)
    assert result["moved"] >= 1, result
    ptr = report_db.get_session_blob(sid, KIND)
    assert ptr and ptr["content_hash"] == hashlib.sha256(body.encode()).hexdigest(), ptr
    assert _legacy_value(sid) == body, "backfill 이 legacy 를 지웠다 (cutover 는 별도 배포)"
    assert _read(sid) == body

    again = mig.step_noteblob(report_db, dry_run=False)
    assert again["moved"] == 0, f"멱등이 아니다: {again}"
    print("  (g) backfill 이전 + 멱등 OK")


def test_session_delete_removes_objects():
    """(h) 세션 삭제 시 객체 파일이 남지 않는다."""
    from admin_panel import sessions_admin
    sid = "s-blob-h"
    _mk_session(sid)
    assert _save(sid, _sheet("삭제될 세션의 본문"))[0]
    ptr = report_db.get_session_blob(sid, KIND)
    path = Path(os.environ["REPORT_UPLOAD_DIR"]) / "session_blob" / ptr["object_key"]
    assert path.exists()
    sessions_admin._delete_one(report_db.get_session(sid))
    assert not path.exists(), "세션을 지웠는데 객체 파일이 남았다"
    assert not path.parent.exists(), "세션 디렉토리가 남았다"
    assert report_db.get_session_blob(sid, KIND) is None
    print("  (h) 세션 삭제 시 객체 정리 OK")


def test_orphan_scan_covers_new_roots():
    """(i) 고아 스캔이 세션 축(note_img/session_blob)과 akey 축 4범주를 모두 훑는다.

    종전에는 web_report/ 하나만 스캔해 issue_img·note_img 등의 고아가 영구 잔존했다.
    dry-run 에서는 세지만 지우지 않고, 실모드에서만 지운다."""
    import report_cleanup
    up = Path(os.environ["REPORT_UPLOAD_DIR"])
    old = 1  # mtime 을 아주 옛날로 → 48h 유예 통과

    # 세션 축 고아 2개 (참조 세션 없음)
    for name in ("note_img", "session_blob"):
        d = up / name / "s-gone-forever"
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.bin").write_bytes(b"x")
        os.utime(d, (old, old))
    # akey 축 고아 — 종전 스캔이 못 보던 issue_img
    ghost = "c" * 64
    d = up / "issue_img" / ghost
    d.mkdir(parents=True, exist_ok=True)
    (d / "0.png").write_bytes(b"p")
    os.utime(d, (old, old))

    assert report_cleanup._purge_session_orphans(dry_run=True) == 2
    assert (up / "note_img" / "s-gone-forever").exists(), "dry-run 인데 지웠다"
    assert report_cleanup._purge_fs_orphans(dry_run=True) >= 1, "issue_img 고아를 못 봤다"

    assert report_cleanup._purge_session_orphans(dry_run=False) == 2
    assert not (up / "note_img" / "s-gone-forever").exists()
    assert not (up / "session_blob" / "s-gone-forever").exists()
    report_cleanup._purge_fs_orphans(dry_run=False)
    assert not (up / "issue_img" / ghost).exists(), "akey 고아가 안 지워졌다"

    # 살아있는 세션의 blob 은 건드리지 않는다
    sid = "s-blob-alive"
    _mk_session(sid)
    assert _save(sid, _sheet("살아있는 세션"))[0]
    ptr = report_db.get_session_blob(sid, KIND)
    alive = up / "session_blob" / ptr["object_key"]
    os.utime(alive.parent.parent, (old, old))
    report_cleanup._purge_session_orphans(dry_run=False)
    assert alive.exists(), "참조 세션이 있는데 blob 을 지웠다"
    print("  (i) 고아 스캔 확장(세션축+akey축) · 살아있는 세션 보존 OK")


def test_pin_and_log_rollup():
    """덤: 평문 PIN 미저장 + 챗봇 원문 롤업(집계 보존)."""
    sid = "s-blob-pin"
    report_db.create_session(sid, "x.xlsx", None, product_type="MDDI", lot_id="L",
                             product="P1", password="1234", source="xlsx_upload")
    with report_db.get_conn() as conn:
        row = conn.execute("SELECT password FROM report_session WHERE session_id=?",
                           (sid,)).fetchone()
    assert row["password"] is None, f"평문 PIN 이 저장됐다: {row['password']}"

    report_db.log_chat(question="q1", answer="a1", intent="yield", planner="rule",
                       total_ms=100, wait_ms=10, llm_ms=20, result="ok")
    purged = report_db.rollup_chat_daily(2 ** 31)      # 전부 대상
    assert purged == 1, purged
    daily = report_db.chat_daily()
    assert daily and daily[0]["cnt"] == 1 and daily[0]["total_ms_sum"] == 100, daily
    assert report_db.list_chats()["total"] == 0, "롤업 후 원문이 남았다"
    print("  (+) 평문 PIN 미저장 · 챗봇 원문→일별 집계 OK")


if __name__ == "__main__":
    print("세션 blob 분리 테스트")
    test_dual_write_creates_pointer_and_object()
    test_read_prefers_blob()
    test_fallback_to_legacy_when_object_missing()
    test_legacy_only_session_reads()
    test_pointer_only_session()
    test_clear_removes_everything()
    test_backfill_is_idempotent()
    test_session_delete_removes_objects()
    test_orphan_scan_covers_new_roots()
    test_pin_and_log_rollup()
    print("\n전부 통과")

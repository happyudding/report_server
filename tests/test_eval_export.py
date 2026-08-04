"""eval_export (Issue Table 코멘트 → eval.db 스키마 적재) E2E 검증.

실행:
    python tests/test_eval_export.py

시나리오 (상태 누적 — 순서 의존):
  (a) PTE+개발 코멘트 export → fail_case/item_master/item_spec/label(병합)/
      raw_metrics/ingest_run.session_id 검증
  (b) 재-export 멱등 (행 수 불변, run_id 재사용)
  (c) 코멘트 수정 → 해당 case 의 label 만 교체 (건수 불변)
  (e) 읽기 계약 — eval_engine store.search_precedents(conn 주입) 로 코멘트가
      선례로 조회되는지 확인
  (d) 코멘트 전부 삭제 → label/run_case 정리 + fail_case 잔존 (reconciliation)

pytest 미사용(그건 eval_analyzer 전용) — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))  # import config (eval_export.db_path)

_TMP = Path(tempfile.mkdtemp(prefix="eval_export_test_"))
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")

import pandas as pd  # noqa: E402

from web_report import eval_export  # noqa: E402
from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402

_SEP = "\x1f"
SID = "1700000000_abc123"


def make_table():
    """합성 honeyform 테이블 (test_yield_step_selected_items.py 픽스처 변형).

    ItemA: TNO 100, unit V, limit 0~10, 측정 [5,5,5,15] → bin4 fail 1건.
    ItemB: TNO 200, 측정 없음 → bin5 fail 2건.
    """
    cols = META_COLUMNS + ["ItemA", "ItemB"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P2"],
        ["UNIT", "", "", "", "", "", "", "V", "V"],
        ["HILIM", "", "", "", "", "", "", 10, 10],
        ["LOLIM", "", "", "", "", "", "", 0, 0],
        ["s1", 1, 1, 0, 0, 1, "", 5, ""],
        ["s2", 1, 1, 1, 0, 5, 200, 5, ""],
        ["s3", 1, 1, 2, 0, 5, 200, 5, ""],
        ["s4", 1, 1, 3, 0, 4, 100, 15, ""],
    ]
    return split_honeyform(pd.DataFrame(rows, columns=cols),
                           source="src0", file_name="src0")


class FakeReportDB:
    """export 가 쓰는 3개 메서드만 흉내낸다 (세션/편집행은 테스트가 직접 세팅)."""

    def __init__(self, session):
        self.session = session
        self.rev = 1
        self.edit_rows = []

    def get_session(self, session_id):
        return self.session if session_id == self.session["session_id"] else None

    def get_webreport_edit_rev(self, session_id):
        return self.rev

    def get_webreport_edits(self, session_id, kinds=None, exclude_kinds=None):
        rows = self.edit_rows
        if kinds:
            rows = [r for r in rows if r["kind"] in kinds]
        if exclude_kinds:
            rows = [r for r in rows if r["kind"] not in exclude_kinds]
        return list(rows)


def comment_row(row_key, col, value, at=100, by="user1"):
    return {"kind": "issue_comment", "item_key": f"{row_key}{_SEP}{col}",
            "value": value, "updated_at": at, "updated_by": by}


def q1(conn, sql, *params):
    return conn.execute(sql, params).fetchone()


def qv(conn, sql, *params):
    return q1(conn, sql, *params)[0]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    session = {
        "session_id": SID, "source": "web_report", "analysis_key": "ak_test",
        "product_type": "MDDI", "product": "PRODX", "lot_id": "LOT1",
        "revision": "1.0", "file_name": "test.xlsx", "uploaded_by": "tester",
    }
    db = FakeReportDB(session)
    db.edit_rows = [
        comment_row("Yield|4|ItemA", "PTE comment", "yield fail 분석중"),
        comment_row("Yield|4|ItemA", "개발 comment", "개발 확인 요청", at=200, by="user2"),
        comment_row("CPK|ItemA", "PTE comment", "cpk marginal"),
        comment_row("ETC|CustomThing", "개발 comment", "기타 항목 메모"),
        comment_row("Yield|1|Pass", "PTE comment", "pass 행 코멘트 (skip 대상)"),
    ]
    export = lambda: eval_export.export_session_comments(  # noqa: E731
        SID, report_db=db, upload_root=_TMP, tables=[make_table()])

    # (a) 최초 export ────────────────────────────────────────────────────────
    r = export()
    assert r == {"cases": 3, "labels": 3, "removed": 0}, r

    conn = eval_export.open_conn(create=False)
    assert conn is not None, "eval DB 파일이 생성되지 않음"
    try:
        assert qv(conn, "PRAGMA user_version") == 4
        assert qv(conn, "SELECT COUNT(*) FROM fail_case") == 3
        assert qv(conn, "SELECT COUNT(*) FROM label") == 3
        assert qv(conn, "SELECT COUNT(*) FROM ingest_run") == 1
        assert qv(conn, "SELECT session_id FROM ingest_run") == SID
        assert qv(conn, "SELECT COUNT(*) FROM run_case") == 3

        # product/item 마스터
        pm = q1(conn, "SELECT * FROM product_master WHERE product_name='PRODX'")
        assert pm["product_type"] == "MDDI" and pm["family_product"] == "MDDI_ETC", dict(pm)
        im = q1(conn, "SELECT * FROM item_master WHERE item_name_raw='ItemA'")
        assert im["item_canonical"] == "itema" and im["value_type"] == "V" \
            and im["unit"] == "V" and im["category_major"] == "NON_TRIM", dict(im)
        spec = q1(conn, "SELECT * FROM item_spec WHERE item_id=?", im["item_id"])
        assert spec["lsl"] == 0.0 and spec["usl"] == 10.0 and spec["revision"] == 1.0, dict(spec)

        # Yield|4|ItemA — 병합 label + yield/dist metrics
        fc = q1(conn, "SELECT * FROM fail_case WHERE item_id=? AND bin=4", im["item_id"])
        assert fc is not None and fc["lot_id"] == "LOT1" and fc["wafer_number"] is None
        assert fc["item_class"] == "NON_TRIM|V|4", fc["item_class"]
        lb = q1(conn, "SELECT * FROM label WHERE case_id=?", fc["case_id"])
        assert lb["human_comment"] == "[PTE] yield fail 분석중\n[개발] 개발 확인 요청", \
            lb["human_comment"]
        assert lb["labeler"] == "web_report" and lb["reviewer"] == "user2" \
            and lb["label_quality"] == "manual", dict(lb)
        m = q1(conn, "SELECT * FROM raw_metrics WHERE case_id=?", fc["case_id"])
        assert m["fail_count"] == 1 and m["total_count"] == 4 \
            and abs(m["yield"] - 0.75) < 1e-9, dict(m)
        assert m["mean"] == 7.5 and m["cpk"] is not None, dict(m)

        # CPK|ItemA → bin=1 / ETC|CustomThing → bin NULL (rawdata 밖 항목)
        assert q1(conn, "SELECT * FROM fail_case WHERE item_id=? AND bin=1",
                  im["item_id"]) is not None
        etc_im = q1(conn, "SELECT * FROM item_master WHERE item_name_raw='CustomThing'")
        assert etc_im["value_type"] == "PF" and etc_im["unit"] is None, dict(etc_im)
        etc_fc = q1(conn, "SELECT * FROM fail_case WHERE item_id=?", etc_im["item_id"])
        assert etc_fc["bin"] is None, dict(etc_fc)

        # Pass 행(Yield|1|Pass) 은 미적재
        assert q1(conn, "SELECT * FROM item_master WHERE item_name_raw='Pass'") is None
    finally:
        conn.close()

    # (b) 재-export 멱등 ─────────────────────────────────────────────────────
    r = export()
    assert r == {"cases": 3, "labels": 3, "removed": 0}, r
    conn = eval_export.open_conn(create=False)
    try:
        assert qv(conn, "SELECT COUNT(*) FROM fail_case") == 3
        assert qv(conn, "SELECT COUNT(*) FROM label") == 3
        assert qv(conn, "SELECT COUNT(*) FROM ingest_run") == 1, "run 이 재사용되지 않음"
        # metrics 는 rawdata 에 있는 항목 2건만 (ETC 자유입력 항목은 통계 없음)
        assert qv(conn, "SELECT COUNT(*) FROM raw_metrics") == 2
    finally:
        conn.close()

    # (b2) tables 미주입 운영 경로: loader 반환 3-tuple 에서 tables 만 사용해야 한다.
    from web_report import loader
    original_load_tables = loader.load_tables
    loader_calls = []

    def fake_load_tables(loaded_session_id, *, report_db, upload_root,
                         use_cache=True, session=None):
        loader_calls.append((loaded_session_id, report_db, Path(upload_root), session))
        return session, [make_table()], {}

    loader.load_tables = fake_load_tables
    try:
        r = eval_export.export_session_comments(
            SID, report_db=db, upload_root=_TMP)
    finally:
        loader.load_tables = original_load_tables

    assert len(loader_calls) == 1, loader_calls
    assert loader_calls[0][0] == SID and loader_calls[0][1] is db, loader_calls[0]
    assert loader_calls[0][2] == _TMP and loader_calls[0][3] is db.session, loader_calls[0]
    assert r == {"cases": 3, "labels": 3, "removed": 0}, r
    conn = eval_export.open_conn(create=False)
    try:
        assert qv(conn, "SELECT COUNT(*) FROM fail_case") == 3
        assert qv(conn, "SELECT COUNT(*) FROM label") == 3
        assert qv(conn, "SELECT COUNT(*) FROM ingest_run") == 1
    finally:
        conn.close()

    # (c) 코멘트 수정 → 해당 label 만 교체
    db.edit_rows[0] = comment_row("Yield|4|ItemA", "PTE comment", "수정된 코멘트",
                                  at=300, by="user3")
    export()
    conn = eval_export.open_conn(create=False)
    try:
        assert qv(conn, "SELECT COUNT(*) FROM label") == 3
        lb = q1(conn, """SELECT l.* FROM label l JOIN fail_case fc ON fc.case_id=l.case_id
                         WHERE fc.bin=4""")
        assert lb["human_comment"] == "[PTE] 수정된 코멘트\n[개발] 개발 확인 요청", \
            lb["human_comment"]
        assert lb["reviewer"] == "user3", lb["reviewer"]

        # (e) 읽기 계약 — eval_engine 선례검색이 이 DB 를 그대로 읽는다
        from eval_engine import store  # eval_export._engine 이 sys.path 추가 완료
        prec = store.search_precedents("V", "itema", family_product=None, conn=conn)
        assert prec, "선례검색 결과 없음"
        top = prec[0]
        assert top["human_comment"] and "수정된 코멘트" in top["human_comment"], top
        assert top["product_name"] == "PRODX", top
    finally:
        conn.close()

    # (d) 코멘트 전부 삭제 → reconciliation ─────────────────────────────────
    db.edit_rows = []
    r = export()
    assert r == {"cases": 0, "labels": 0, "removed": 3}, r
    conn = eval_export.open_conn(create=False)
    try:
        assert qv(conn, "SELECT COUNT(*) FROM label") == 0
        assert qv(conn, "SELECT COUNT(*) FROM run_case") == 0
        assert qv(conn, "SELECT COUNT(*) FROM raw_metrics") == 0
        assert qv(conn, "SELECT COUNT(*) FROM fail_case") == 3, "fail_case 는 보존"
    finally:
        conn.close()

    # 가드: web_report 세션이 아니면 skip
    db.session = dict(session, source="xlsx_upload")
    r = export()
    assert "skipped" in r, r

    print("PASS: test_eval_export (a/b/c/e/d + guard)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

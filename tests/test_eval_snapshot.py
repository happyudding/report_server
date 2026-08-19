"""평가 스냅샷 수집(eval_export.collect_session_snapshot) 검증.

실행:
    python tests/test_eval_snapshot.py

왜 필요한가: 운영 조회 경로는 evaluate(persist=False) 라 엔진이 콜드 빌드마다 L1/L2 를
계산해 놓고 버린다 → features/evaluation/case_signature 가 0행. 업로드 직후 1회만
판단 근거를 남기는 경로가 이 함수이고, 여기서 검증하는 계약은 4가지다.

  (a) 수집 후 features/evaluation/case_signature 가 실제로 쌓인다
  (b) LLM·선례검색을 타지 않는다 (generate_comment=False → comment NULL, precedent 0행)
  (c) 재수집이 멱등이다 (같은 engine_version 이면 skip — 행 증식 없음)
  (d) 엔진 소유 eval.db(config.DB_PATH)는 **생성조차 되지 않는다**
  (f) **case_id 정합** — 사람 코멘트 라벨이 엔진 판정과 같은 case 에 붙는다 (2026-08-19).
      이게 깨지면 채점·선례 부스트가 조용히 0 이 되므로 회귀 가드가 필요하다.

pytest 미사용(그건 eval_analyzer 전용) — 자체 실행 + assert 스타일(tests/ 관례).
⚠ 이 파일을 pytest 로 다른 test_*.py 와 묶어 돌리지 말 것 — env(REPORT_EVAL_DB_PATH)
격리가 import 순서에 따라 깨진다.
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

_TMP = Path(tempfile.mkdtemp(prefix="eval_snapshot_test_"))
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")

import pandas as pd  # noqa: E402

from web_report import eval_export  # noqa: E402
from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402

SID = "1700000001_snap01"


def make_table():
    """합성 honeyform — ItemA 만 측정값이 있고 bin4 fail 1건.

    ItemB 는 측정값이 전부 공란이라 case 가 만들어지지 않는다(엔진 `if not values`).
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
    def __init__(self, session):
        self.session = session
        self.audits = []
        self.rev = 1
        self.edit_rows = []          # (f) 코멘트 export 와 나란히 돌리기 위한 편집 상태

    def get_session(self, session_id):
        return self.session if session_id == self.session["session_id"] else None

    def log_audit(self, **kw):
        self.audits.append(kw)

    def get_webreport_edit_rev(self, session_id):
        return self.rev

    def get_webreport_edits(self, session_id, kinds=None, exclude_kinds=None):
        rows = self.edit_rows
        if kinds:
            rows = [r for r in rows if r["kind"] in kinds]
        if exclude_kinds:
            rows = [r for r in rows if r["kind"] not in exclude_kinds]
        return list(rows)


def qv(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    session = {
        "session_id": SID, "source": "web_report", "analysis_key": "ak_snap",
        "product_type": "MDDI", "product": "PRODX", "lot_id": "LOT1",
        "revision": "1.0", "file_name": "test.xlsx", "uploaded_by": "tester",
        "mode": "Normal",
    }
    db = FakeReportDB(session)
    collect = lambda force=False: eval_export.collect_session_snapshot(  # noqa: E731
        SID, report_db=db, upload_root=_TMP, tables=[make_table()], force=force)

    # (a) 최초 수집 — 판단 근거가 실제로 쌓인다 ────────────────────────────────
    r = collect()
    assert r["sources"] == 1 and r["collected"] == 1 and r["skipped"] == 0, r
    assert r["cases"] >= 1, f"게이트 통과 case 가 0건: {r}"

    conn = eval_export.open_conn(create=False)
    assert conn is not None, "eval DB 파일이 생성되지 않음"
    try:
        n_eval = qv(conn, "SELECT COUNT(*) FROM evaluation")
        assert n_eval == r["cases"], (n_eval, r)
        assert qv(conn, "SELECT COUNT(*) FROM features") == n_eval
        assert qv(conn, "SELECT COUNT(*) FROM raw_metrics") == n_eval
        assert qv(conn, "SELECT COUNT(*) FROM fail_case") >= 1

        # run 은 스냅샷 표식을 달고 있어야 코멘트 export run 과 구분된다.
        assert qv(conn, "SELECT ingested_by FROM ingest_run") == "eval-snapshot"
        assert qv(conn, "SELECT source_file FROM ingest_run") == "eval-snapshot#0"
        assert qv(conn, "SELECT session_id FROM ingest_run") == SID

        # (b) LLM·선례검색을 타지 않았다 — comment NULL, 선례 0행
        assert qv(conn, "SELECT COUNT(*) FROM evaluation WHERE comment IS NOT NULL") == 0
        assert qv(conn, "SELECT COUNT(*) FROM eval_precedent") == 0

        # status 가 실제로 채워져 있어야 채점 재료가 된다(빈 판정 적재 방지).
        assert qv(conn, "SELECT COUNT(*) FROM evaluation WHERE status IS NULL") == 0
        print(f"[a,b] 수집 OK — evaluation {n_eval}행, comment/선례 없음")

        # v9 판정지표가 실제 수집 경로에서 채워진다 — 컬럼만 만들고 화이트리스트에서
        # 빠지면 영원히 NULL 이 되는 함정(shot_fail_ratio 전례)의 배선 가드.
        # 값 자체는 case 마다 None 일 수 있으므로 "컬럼이 존재하고 조회된다"까지 본다.
        from eval_engine import store as _store
        cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
        assert set(_store._V9_FEATURE_COLS) <= cols, sorted(
            set(_store._V9_FEATURE_COLS) - cols)
        filled = qv(conn, "SELECT COUNT(*) FROM features WHERE tail_mass_3s IS NOT NULL")
        assert filled == n_eval, f"tail_mass_3s 가 안 채워짐: {filled}/{n_eval}"
        print(f"[a] v9 판정지표 적재 OK — tail_mass_3s {filled}/{n_eval}행")

        # (c) 재수집 멱등 — 같은 engine_version 이면 건드리지 않는다 ──────────
        r2 = collect()
        assert r2["collected"] == 0 and r2["skipped"] == 1, r2
        assert qv(conn, "SELECT COUNT(*) FROM evaluation") == n_eval, "재수집이 행을 늘렸다"
        assert qv(conn, "SELECT COUNT(*) FROM ingest_run") == 1, "재수집이 run 을 늘렸다"
        print("[c] 재수집 멱등 OK — 행·run 증식 없음")

        # force=True 는 지우지 않고 새 run 으로 다시 쌓는다(사람 라벨 보호).
        r3 = collect(force=True)
        assert r3["collected"] == 1, r3
        assert qv(conn, "SELECT COUNT(*) FROM ingest_run") == 2
        assert qv(conn, "SELECT COUNT(*) FROM evaluation") == n_eval * 2
        print("[c] force 재수집 OK — 기존 행 보존 + 새 run")
    finally:
        conn.close()

    # (d) 엔진 소유 eval.db 는 생성조차 되지 않았다 ───────────────────────────
    from eval_engine import config as eval_config
    assert not Path(eval_config.DB_PATH).exists(), \
        f"엔진 소유 DB 가 생성됨: {eval_config.DB_PATH} — db_path 인자가 안 먹었다"
    print(f"[d] 엔진 DB 무생성 OK — {eval_config.DB_PATH}")

    # 실패 격리 — 없는 세션이어도 예외가 밖으로 나가지 않는다.
    assert eval_export.safe_collect_snapshot(
        "no_such_session", report_db=db, upload_root=_TMP) == {"skipped": "not a web_report session"}
    print("[e] 실패 격리 OK")

    # (f) **case_id 정합** — 사람 코멘트 라벨이 엔진 판정과 같은 case 에 붙는가 ─────
    # 이게 어긋나면 채점(agree_rate)·선례의 signature 부스트가 구조적으로 0 이 된다.
    # 2026-08-19 이전에는 스냅샷만 wafer_number=소스 순번을 써서 교집합이 항상 공집합이었다.
    sep = "\x1f"
    db.edit_rows = [
        {"kind": "issue_comment", "item_key": f"Yield|4|ItemA{sep}PTE comment",
         "value": "bin4 fail 원인 분석", "updated_at": 100, "updated_by": "user1"},
        {"kind": "issue_status", "item_key": "Yield|4",
         "value": "Close", "updated_at": 100, "updated_by": "user1"},
    ]
    rc = eval_export.export_session_comments(SID, report_db=db, upload_root=_TMP,
                                             tables=[make_table()])
    assert rc["labels"] == 1, rc

    conn = eval_export.open_conn(create=False)
    try:
        label_cases = {r[0] for r in conn.execute(
            "SELECT case_id FROM label WHERE labeler='web_report'")}
        snap_cases = {r[0] for r in conn.execute(
            """SELECT DISTINCT ev.case_id FROM evaluation ev
                 JOIN ingest_run ir ON ir.run_id = ev.run_id
                WHERE ir.ingested_by='eval-snapshot'""")}
        assert label_cases, "코멘트 라벨이 적재되지 않음"
        assert label_cases <= snap_cases, (
            "라벨 case_id 가 엔진 스냅샷과 어긋난다 — "
            f"label={label_cases} snapshot={snap_cases}")
        # 그 case 가 실제로 ItemA·bin4 인지(우연한 일치가 아님을 확인)
        fc = conn.execute(
            """SELECT fc.bin, im.item_name_raw FROM fail_case fc
                 JOIN item_master im ON im.item_id = fc.item_id
                WHERE fc.case_id=?""", (next(iter(label_cases)),)).fetchone()
        assert fc["bin"] == 4 and fc["item_name_raw"] == "ItemA", dict(fc)
        print(f"[f] case_id 정합 OK — 라벨 {len(label_cases)}건이 스냅샷 case 와 일치")
    finally:
        conn.close()

    print("\n전부 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

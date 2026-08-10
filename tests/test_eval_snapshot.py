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

    def get_session(self, session_id):
        return self.session if session_id == self.session["session_id"] else None

    def log_audit(self, **kw):
        self.audits.append(kw)


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

    print("\n전부 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

"""옛 eval 스냅샷 run 정리(eval_admin.purge_stale_snapshots) 검증.

실행:
    python tests/test_eval_snapshot_purge.py

왜 필요한가: `force=true` 재수집은 **기존 run 을 지우지 않고 새 run 으로 다시 쌓는다**
(거기 달린 사람 라벨을 잃지 않으려고 — eval_export.collect_session_snapshot). 그래서
재수집을 반복하면 판정 사본이 계속 늘고, 조회는 어차피 최신만 본다. 그 사역 데이터를
걷는 것이 이 정리인데, **잘못 지우면 판정 근거와 사람 입력이 함께 사라진다.**
그래서 지키는 계약 4가지를 못박는다.

  (a) 같은 (세션, 소스)의 **최신 run 은 절대 지우지 않는다**
  (b) 라벨이 달린 run 은 옛것이어도 지우지 않는다 (사람 입력 보호)
  (c) 스냅샷이 아닌 run(web_report/eval-panel/web-signature)은 대상 밖
  (d) fail_case·label·마스터는 남는다 — 사라지는 것은 옛 판정 사본뿐
  (e) dry_run 이면 세기만 한다

pytest 미사용(tests/ 관례) — 자체 실행 + assert.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))
sys.path.insert(0, os.path.join(_ROOT, "eval_analyzer"))

_TMP = Path(tempfile.mkdtemp(prefix="eval_purge_test_"))
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")

from admin_panel import eval_admin  # noqa: E402
from eval_engine import store  # noqa: E402


def _conn():
    p = Path(os.environ["REPORT_EVAL_DB_PATH"])
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    return c


def _run(c, *, session, source, by="eval-snapshot"):
    return c.execute(
        "INSERT INTO ingest_run (product_name, source_file, session_id, ingested_by,"
        " created_at) VALUES ('P',?,?,?,100)", (source, session, by)).lastrowid


def _eval(c, case_id, run_id, *, status="MAJOR"):
    c.execute("INSERT OR IGNORE INTO fail_case (case_id, product_name, item_id, created_at)"
              " VALUES (?, 'P', 1, 100)", (case_id,))
    eid = c.execute(
        "INSERT INTO evaluation (case_id, run_id, engine_version, model_version, status,"
        " created_at) VALUES (?,?, 'ev1','',?,100)", (case_id, run_id, status)).lastrowid
    c.execute("INSERT INTO features (case_id, run_id, engine_version, computed_at)"
              " VALUES (?,?, 'ev1', 100)", (case_id, run_id))
    c.execute("INSERT INTO raw_metrics (case_id, run_id, created_at) VALUES (?,?,100)",
              (case_id, run_id))
    c.execute("INSERT INTO run_case (run_id, case_id, seen_at) VALUES (?,?,100)",
              (run_id, case_id))
    c.execute("INSERT INTO case_signature (eval_id, signature, role) VALUES (?,'LOW_CPK','primary')",
              (eid,))
    return eid


def qv(c, sql, *p):
    return c.execute(sql, p).fetchone()[0]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    c = _conn()

    # 세션 S1/소스 0 — 재수집 3회 (old1, old2, latest)
    old1 = _run(c, session="S1", source="eval-snapshot#0")
    old2 = _run(c, session="S1", source="eval-snapshot#0")
    latest = _run(c, session="S1", source="eval-snapshot#0")
    for r in (old1, old2, latest):
        _eval(c, "C1", r)

    # 세션 S1/소스 1 — run 1개뿐(=최신) → 대상 아님
    only = _run(c, session="S1", source="eval-snapshot#1")
    _eval(c, "C2", only)

    # 세션 S2 — 옛 run 에 **사람 라벨**이 달려 있다 → 보호
    labeled_old = _run(c, session="S2", source="eval-snapshot#0")
    labeled_new = _run(c, session="S2", source="eval-snapshot#0")
    eid = _eval(c, "C3", labeled_old)
    _eval(c, "C3", labeled_new)
    c.execute("INSERT INTO label (case_id, eval_id, labeler, created_at)"
              " VALUES ('C3', ?, 'eval-panel', 100)", (eid,))

    # 코멘트 export run — 스냅샷이 아니므로 대상 밖 (같은 세션에 2개 = 옛것이 존재)
    cmt_old = _run(c, session="S1", source="web_report", by="web_report")
    _run(c, session="S1", source="web_report", by="web_report")
    c.commit()

    targets = set(eval_admin.stale_snapshot_runs(c))
    assert targets == {old1, old2}, (sorted(targets), {old1, old2})
    print(f"[a,b,c] 대상 선정 OK — 옛 스냅샷 2개만 (최신 {latest}·단독 {only}·"
          f"라벨보유 {labeled_old}·코멘트run {cmt_old} 전부 제외)")
    c.close()

    # (e) dry_run 은 세기만 한다
    before_eval = None
    c = sqlite3.connect(str(Path(os.environ["REPORT_EVAL_DB_PATH"])))
    c.row_factory = sqlite3.Row
    before_eval = qv(c, "SELECT COUNT(*) FROM evaluation")
    c.close()
    res = eval_admin.purge_stale_snapshots(dry_run=True)
    assert res == {"runs": 2, "deleted": 0, "exists": True, "dry_run": True}, res
    c = sqlite3.connect(str(Path(os.environ["REPORT_EVAL_DB_PATH"])))
    c.row_factory = sqlite3.Row
    assert qv(c, "SELECT COUNT(*) FROM evaluation") == before_eval
    c.close()
    print(f"[e] dry_run OK — 대상 2건 보고, 삭제 0 (evaluation {before_eval}행 그대로)")

    # 실삭제
    res = eval_admin.purge_stale_snapshots(dry_run=False)
    assert res["runs"] == 2 and res["deleted"] == 2, res

    c = sqlite3.connect(str(Path(os.environ["REPORT_EVAL_DB_PATH"])))
    c.row_factory = sqlite3.Row
    left_runs = {r[0] for r in c.execute("SELECT run_id FROM ingest_run")}
    assert old1 not in left_runs and old2 not in left_runs, sorted(left_runs)
    assert {latest, only, labeled_old, labeled_new} <= left_runs, sorted(left_runs)

    # (d) fail_case·label·마스터는 남는다 — 사라진 것은 옛 판정 사본뿐
    assert qv(c, "SELECT COUNT(*) FROM fail_case") == 3, "fail_case 가 지워졌다"
    assert qv(c, "SELECT COUNT(*) FROM label") == 1, "사람 라벨이 지워졌다"
    assert qv(c, "SELECT COUNT(*) FROM evaluation WHERE run_id=?", latest).__int__() == 1
    # 자식 고아 0건
    assert qv(c, "SELECT COUNT(*) FROM features WHERE run_id IN (?,?)", old1, old2) == 0
    assert qv(c, "SELECT COUNT(*) FROM raw_metrics WHERE run_id IN (?,?)", old1, old2) == 0
    assert qv(c, "SELECT COUNT(*) FROM run_case WHERE run_id IN (?,?)", old1, old2) == 0
    orphan_sig = qv(c, "SELECT COUNT(*) FROM case_signature cs WHERE NOT EXISTS "
                       "(SELECT 1 FROM evaluation ev WHERE ev.eval_id = cs.eval_id)")
    assert orphan_sig == 0, f"case_signature 고아 {orphan_sig}건"
    print("[d] 보존 OK — fail_case 3 · label 1 · 최신/라벨 run 유지 · 자식 고아 0")

    # 멱등 — 다시 돌리면 대상 없음
    c.close()
    assert eval_admin.purge_stale_snapshots(dry_run=False)["runs"] == 0
    print("[f] 멱등 OK — 재실행 시 대상 0")

    print("\n전부 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

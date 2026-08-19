"""eval 룰 지표 일별 집계(database/eval_stats.py) 검증.

실행:
    python tests/test_eval_stats_daily.py

왜 필요한가: 정확도·커버리지가 지금까지 "전체 누적 한 숫자"로만 나와 추이를 볼 수 없었다.
이 모듈이 eval.db 를 읽어 report.db 의 report_eval_daily 로 접는다. 검증하는 계약은 4가지.

  (a) 스냅샷 축적·UNKNOWN 비율 재료가 **run 수집일 + engine_version** 축으로 갈린다
  (b) signature 확정(✓)과 엔진 발화의 집합 대조 (일치/부분일치)
  (c) 사람 코멘트 라벨의 **정합 여부**(엔진 판정과 case 가 이어졌나)
  (d) 재집계가 **덮어쓰기**다 — 같은 날을 두 번 접어도 값이 부풀지 않는다
      (report_chatbot_daily 의 누적 더하기와 규약이 반대라 실수하기 쉽다)

pytest 미사용(tests/ 관례) — 자체 실행 + assert.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))
sys.path.insert(0, os.path.join(_ROOT, "eval_analyzer"))

_TMP = Path(tempfile.mkdtemp(prefix="eval_stats_test_"))
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")

from database import core, eval_stats  # noqa: E402
from eval_engine import store  # noqa: E402

DAY_A_AT = int(time.mktime((2026, 8, 17, 10, 0, 0, 0, 0, -1)))
DAY_B_AT = int(time.mktime((2026, 8, 18, 10, 0, 0, 0, 0, -1)))
DAY_A = time.strftime("%Y-%m-%d", time.localtime(DAY_A_AT))
DAY_B = time.strftime("%Y-%m-%d", time.localtime(DAY_B_AT))


def _eval_conn():
    path = Path(os.environ["REPORT_EVAL_DB_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    return conn


def _run(conn, *, ingested_by, at):
    cur = conn.execute(
        "INSERT INTO ingest_run (product_name, lot_id, source_file, session_id,"
        " ingested_by, created_at) VALUES ('P','L','f','S',?,?)", (ingested_by, at))
    return cur.lastrowid


def _case(conn, case_id, *, fail_count, at):
    conn.execute(
        "INSERT INTO fail_case (case_id, product_name, item_id, created_at)"
        " VALUES (?, 'P', 1, ?)", (case_id, at))
    return case_id


def _evaluation(conn, case_id, run_id, *, status="MAJOR", ver="ev1", at=0,
                primary=None, fail_count=0):
    cur = conn.execute(
        "INSERT INTO evaluation (case_id, run_id, engine_version, model_version,"
        " status, created_at) VALUES (?,?,?,'',?,?)", (case_id, run_id, ver, status, at))
    eval_id = cur.lastrowid
    conn.execute("INSERT INTO raw_metrics (case_id, run_id, fail_count, created_at)"
                 " VALUES (?,?,?,?)", (case_id, run_id, fail_count, at))
    if primary:
        conn.execute("INSERT INTO case_signature (eval_id, signature, role)"
                     " VALUES (?,?, 'primary')", (eval_id, primary))
    return eval_id


def _label(conn, case_id, *, labeler, at, eval_id=None, human_status=None):
    cur = conn.execute(
        "INSERT INTO label (case_id, eval_id, human_status, labeler, created_at)"
        " VALUES (?,?,?,?,?)", (case_id, eval_id, human_status, labeler, at))
    return cur.lastrowid


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    core.init_report_db()

    conn = _eval_conn()
    # ── DAY_A: 스냅샷 run 1개, case 3건 (fail 2건 중 1건이 UNKNOWN) ─────────────
    run_a = _run(conn, ingested_by="eval-snapshot", at=DAY_A_AT)
    _case(conn, "C1", fail_count=3, at=DAY_A_AT)
    _case(conn, "C2", fail_count=1, at=DAY_A_AT)
    _case(conn, "C3", fail_count=0, at=DAY_A_AT)
    ev1 = _evaluation(conn, "C1", run_a, at=DAY_A_AT, primary="LOW_CPK", fail_count=3)
    _evaluation(conn, "C2", run_a, at=DAY_A_AT, primary="UNKNOWN", fail_count=1)
    _evaluation(conn, "C3", run_a, at=DAY_A_AT, primary="OUTLIER", fail_count=0)
    conn.execute("INSERT INTO case_signature (eval_id, signature, role) VALUES (?,?,'sub')",
                 (ev1, "EDGE_FAIL"))

    # ── DAY_B: 사람 입력 — 확정 라벨 2건(정확일치 1 / 부분일치 1) + 코멘트 2건 ──
    sig_run = _run(conn, ingested_by="web-signature", at=DAY_B_AT)
    ph = conn.execute(
        "INSERT INTO evaluation (case_id, run_id, engine_version, model_version,"
        " status, created_at) VALUES ('C1',?, 'engr-label','', NULL, ?)",
        (sig_run, DAY_B_AT)).lastrowid
    lb1 = _label(conn, "C1", labeler="web-signature", at=DAY_B_AT, eval_id=ph)
    for s in ("LOW_CPK", "EDGE_FAIL"):        # 엔진 발화와 완전 일치
        conn.execute("INSERT INTO label_signature (label_id, signature) VALUES (?,?)",
                     (lb1, s))
    lb2 = _label(conn, "C2", labeler="web-signature", at=DAY_B_AT, eval_id=ph)
    for s in ("UNKNOWN", "MEAN_SHIFT"):       # 부분 일치(UNKNOWN 만 겹침)
        conn.execute("INSERT INTO label_signature (label_id, signature) VALUES (?,?)",
                     (lb2, s))
    _label(conn, "C1", labeler="web_report", at=DAY_B_AT)      # 정합 O
    _label(conn, "C_ORPHAN", labeler="web_report", at=DAY_B_AT)  # 정합 X(스냅샷 없음)
    _label(conn, "C1", labeler="eval-panel", at=DAY_B_AT, eval_id=ev1,
           human_status="MAJOR")                                # 엔진과 일치
    conn.commit()

    got = eval_stats.collect_eval_daily(conn=conn)

    # (a) 스냅샷 — engine_version 축으로 갈리고 run 은 '' 버킷
    snap = got[(DAY_A, "ev1")]
    assert snap["cases"] == 3, snap
    assert snap["fail_cases"] == 2, snap          # C1(3), C2(1) — C3 는 fail 0
    assert snap["unknown_cases"] == 1, snap       # C2 만 fail + UNKNOWN
    assert got[(DAY_A, "")]["runs"] == 1, got[(DAY_A, "")]
    print(f"[a] 스냅샷 축적·UNKNOWN OK — {DAY_A}/ev1 case 3 · fail 2 · unknown 1")

    # (b) signature 확정 대조 — 엔진 발화가 있는 최신 스냅샷 판정과 집합 비교
    sig = got[(DAY_B, "ev1")]
    assert sig["sig_labeled"] == 2, sig
    assert sig["sig_exact"] == 1, sig             # C1 = {LOW_CPK, EDGE_FAIL}
    assert sig["sig_overlap"] == 2, sig           # C2 는 UNKNOWN 만 겹침(부분)
    print(f"[b] signature 확정 대조 OK — 확정 2건 중 완전일치 1 · 부분일치 2")

    # (c) 코멘트 라벨 정합 — 스냅샷 case 로 이어진 것만 matched
    cm = got[(DAY_B, "")]
    assert cm["comment_labels"] == 2 and cm["comment_matched"] == 1, cm
    print("[c] 코멘트 정합 OK — 라벨 2건 중 1건이 엔진 판정과 연결")

    # status 채점도 같은 버킷 규약(engine_version 축)
    assert sig["score_pairs"] == 1 and sig["score_agree"] == 1, sig

    # (d) 저장 — 재집계는 **덮어쓰기**여야 한다(누적 더하기면 값이 부푼다)
    assert eval_stats.save_eval_daily(got) == len(got)
    eval_stats.save_eval_daily(got)
    eval_stats.save_eval_daily(got)
    rows = {(r["day"], r["engine_version"]): r
            for r in eval_stats.eval_daily_series()}
    assert len(rows) == len(got), (len(rows), len(got))
    again = rows[(DAY_A, "ev1")]
    assert again["cases"] == 3 and again["unknown_cases"] == 1, dict(again)
    assert rows[(DAY_B, "ev1")]["sig_exact"] == 1, dict(rows[(DAY_B, "ev1")])
    print("[d] 재집계 덮어쓰기 OK — 3회 반복해도 값 불변")

    # since_day 필터 — 스케줄러가 전 기간을 다시 훑지 않게 하는 장치
    recent = eval_stats.collect_eval_daily(since_day=DAY_B, conn=conn)
    assert all(day >= DAY_B for day, _ in recent), sorted(recent)
    assert (DAY_A, "ev1") not in recent
    print("[e] since_day 필터 OK")

    # 보존기간 롤오프
    assert eval_stats.purge_eval_daily(DAY_B) >= 1
    left = {(r["day"], r["engine_version"]) for r in eval_stats.eval_daily_series()}
    assert all(day >= DAY_B for day, _ in left), sorted(left)
    print("[f] 롤오프 OK — cutoff 이전 행 삭제")

    conn.close()
    print("\n전부 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

"""Issue Table Signature 컬럼(ENGR 확정 정답 룰) + eval DB 라벨 동기화 검증.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_issue_signature.py

왜 필요한가 (2026-08-11): 엔진이 발화한 signature 를 화면에 보여주고 ENGR 이 직접
고치게 하면서, 그 정답을 eval DB 에 쌓아 나중에 룰을 다듬는 재료로 쓴다. 여기서
지키는 계약 5가지 — 하나라도 깨지면 "학습 데이터"가 조용히 오염된다.

  (a) payload 계약: ai_comment 세션에만 Signature 컬럼/보조필드가 생기고, 그 외 세션은
      키 자체가 없다(기존 payload 완전 동일). 발화가 없으면 "미분류".
  (b) 저장 검증: 카탈로그에 없는 id·중복·9개 이상은 거부(정규식만으론 UI 우회를 못 막는다).
  (c) 동기화 멱등: 편집 DB 상태를 다시 읽어 재적재 — 연속 편집이 마지막 상태로 수렴하고
      해제하면 라벨이 사라진다.
  (d) **세션 구분**: case_id 에 세션이 없어서, 같은 제품·lot·item·bin 을 두 세션에서
      확정해도 서로 덮어쓰지 않아야 한다(세션 전용 ingest_run + label.eval_id).
  (e) 고아 없음: 라벨이 지워지면 자식(label_signature)도 함께 사라진다.

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례). ⚠ 다른 test_*.py 와 pytest 로
묶어 돌리지 말 것 — env(REPORT_EVAL_DB_PATH) 격리가 import 순서에 따라 깨진다.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))   # import config (eval_export.db_path)

_TMP = Path(tempfile.mkdtemp(prefix="issue_sig_test_"))
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")

from web_report import edits, eval_export, service  # noqa: E402
from web_report.tabs.issue_table import (  # noqa: E402
    SIGNATURE_COL, UNCLASSIFIED, build_issue_table_rows)

SID_A = "1700000001_siga01"
SID_B = "1700000002_sigb01"
ROW_KEY = "Yield|4|ItemA"


class FakeTable:
    """issue_table 이 보는 최소 인터페이스 (source + item 메타)."""

    def __init__(self, source="src0"):
        self.source = source
        self.item_columns = ["ItemA"]
        self.tseq = {"ItemA": 1}
        self.tno = {"ItemA": 100}
        self.step = {"ItemA": "P1"}
        self.units = {"ItemA": "V"}
        self.hilim = {"ItemA": 10}
        self.lolim = {"ItemA": 0}


class FakeReportDB:
    """세션 조회 + 편집행 저장(kind/item_key/value) 최소 구현."""

    def __init__(self, sessions):
        self.sessions = {s["session_id"]: s for s in sessions}
        self.rows = {}          # session_id -> {(kind, item_key): value}
        # rev>0 = 편집 DB 로 이미 이전된 세션 — manifest 시드(ensure_seeded)를 타지 않는다.
        self.revs = {s["session_id"]: 1 for s in sessions}

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def get_webreport_edits(self, session_id, kinds=None, exclude_kinds=None):
        out = []
        for (kind, item_key), value in (self.rows.get(session_id) or {}).items():
            if kinds and kind not in kinds:
                continue
            if exclude_kinds and kind in exclude_kinds:
                continue
            out.append({"kind": kind, "item_key": item_key, "value": value})
        return out

    def get_webreport_edit_rev(self, session_id):
        return self.revs.get(session_id, 0)

    def apply_webreport_edits(self, session_id, changes, updated_by=None):
        store = self.rows.setdefault(session_id, {})
        for kind, item_key, value in changes:
            if value is None:
                store.pop((kind, item_key), None)
            else:
                store[(kind, item_key)] = value
        self.revs[session_id] = self.revs.get(session_id, 0) + 1
        return self.revs[session_id]

    def log_audit(self, **kw):
        pass


def make_session(session_id, lot="LOT1"):
    return {"session_id": session_id, "source": "web_report", "analysis_key": "ak_" + session_id,
            "product_type": "MDDI", "product": "PRODX", "lot_id": lot, "revision": "1.0",
            "file_name": "t.xlsx", "uploaded_by": "tester", "mode": "Normal"}


YIELD_ROWS = [
    {"bin": "1", "Item": "Pass", "avg": 90, "step": "P1", "TNO": "", "src0_yield": 90},
    {"bin": "4", "Item": "ItemA", "avg": 10, "step": "P1", "TNO": 100, "src0_yield": 10},
]


def issue_rows(**kw):
    return build_issue_table_rows([FakeTable()], YIELD_ROWS, [], **kw)


def find_row(rows, item, bin_):
    for r in rows:
        if r.get("Item") == item and str(r.get("Bin")) == str(bin_):
            return r
    raise AssertionError(f"행 없음: {item}/{bin_}")


def qv(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # (a) payload 계약 ────────────────────────────────────────────────────────
    plain = issue_rows()
    assert all(SIGNATURE_COL not in r and "_sig" not in r for r in plain), \
        "ai_comment 를 안 쓰는 세션에 Signature 키가 생겼다 (기존 payload 계약 위반)"

    sigs = {"engine": {ROW_KEY: ["LOW_CPK", "BIMODALITY"]}, "engr": {}}
    rows = issue_rows(ai_comments={}, signatures=sigs)
    row = find_row(rows, "ItemA", "4")
    assert row[SIGNATURE_COL] == "LOW_CPK+BIMODALITY", row[SIGNATURE_COL]
    assert row["_sig"] == ["LOW_CPK", "BIMODALITY"] and row["_sigrev"] == 0

    # 엔진 발화가 없으면 "미분류" — fail 인데 룰이 설명 못 한 케이스를 OK 로 착각하지 않게.
    bare = find_row(issue_rows(ai_comments={},
                               signatures={"engine": {}, "engr": {}}), "ItemA", "4")
    assert bare[SIGNATURE_COL] == UNCLASSIFIED, bare[SIGNATURE_COL]

    # ENGR 확정값은 엔진 제안을 이긴다 + reviewed 표식.
    over = {"engine": {ROW_KEY: ["LOW_CPK"]}, "engr": {ROW_KEY: ["UNKNOWN"]}}
    ov = find_row(issue_rows(ai_comments={}, signatures=over), "ItemA", "4")
    assert ov[SIGNATURE_COL] == "UNKNOWN" and ov["_sigrev"] == 1, ov
    print("[a] payload 계약 OK — 컬럼 조건부 생성 / 미분류 / ENGR 우선")

    # (b) 저장 검증 ──────────────────────────────────────────────────────────
    db = FakeReportDB([make_session(SID_A), make_session(SID_B)])
    save = lambda sid, ids: service.update_issue_signature(  # noqa: E731
        sid, report_db=db, upload_root=_TMP, key=ROW_KEY, value=ids)

    for bad, why in [(["NOT_A_RULE"], "카탈로그 밖 id"),
                     (["UNKNOWN", "UNKNOWN"], "중복"),
                     (["UNKNOWN"] * 9, "9개 이상")]:
        try:
            save(SID_A, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"거부되지 않음: {why}")
    try:
        service.update_issue_signature(SID_A, report_db=db, upload_root=_TMP,
                                       key="Bogus|1", value=["UNKNOWN"])
    except ValueError:
        pass
    else:
        raise AssertionError("row_key 접두 검증이 없다")
    print("[b] 저장 검증 OK — 미등록 id / 중복 / 9개 / 잘못된 row_key 거부")

    # 정상 저장 — 편집 DB 에 JSON 배열(순서 보존)로 남는다.
    res = save(SID_A, ["BIMODALITY", "UNKNOWN"])          # 소문자도 대문자로 정규화
    assert res["signatures"] == ["BIMODALITY", "UNKNOWN"], res
    stored = db.rows[SID_A][(edits.KIND_ISSUE_SIGNATURE, ROW_KEY)]
    assert json.loads(stored) == ["BIMODALITY", "UNKNOWN"], stored
    assert edits.load_issue_signatures(db, SID_A) == {ROW_KEY: ["BIMODALITY", "UNKNOWN"]}

    # (c) 동기화 멱등 ────────────────────────────────────────────────────────
    r = eval_export.sync_session_signatures(SID_A, report_db=db)
    assert r["labels"] == 1, r
    conn = eval_export.open_conn(create=False)
    assert conn is not None, "eval DB 가 생성되지 않았다"
    try:
        assert qv(conn, "SELECT COUNT(*) FROM label WHERE labeler='web-signature'") == 1
        assert qv(conn, "SELECT COUNT(*) FROM label_signature") == 2
        assert qv(conn, "SELECT signature FROM label_signature WHERE rank=1") == "BIMODALITY"
        # 라벨은 세션 전용 run 에 매달려 있어야 세션 역참조가 가능하다.
        assert qv(conn, "SELECT ingested_by FROM ingest_run") == "web-signature"
        assert qv(conn, "SELECT ir.session_id FROM label l "
                        "JOIN evaluation ev ON ev.eval_id=l.eval_id "
                        "JOIN ingest_run ir ON ir.run_id=ev.run_id "
                        "WHERE l.labeler='web-signature'") == SID_A
        # human_status 는 비운다 — 관리자 채점(eval-panel 라벨)에 섞이면 안 된다.
        assert qv(conn, "SELECT COUNT(*) FROM label "
                        "WHERE labeler='web-signature' AND human_status IS NOT NULL") == 0

        eval_export.sync_session_signatures(SID_A, report_db=db)     # 재동기화 = 멱등
        assert qv(conn, "SELECT COUNT(*) FROM label WHERE labeler='web-signature'") == 1
        assert qv(conn, "SELECT COUNT(*) FROM label_signature") == 2
        assert qv(conn, "SELECT COUNT(*) FROM ingest_run") == 1, "재동기화가 run 을 늘렸다"
        print("[c] 동기화 멱등 OK — 라벨/자식/run 증식 없음")

        # (d) 세션 구분 — 같은 제품·lot·item·bin 을 다른 세션에서 확정 ─────────
        save(SID_B, ["OUTLIER"])
        eval_export.sync_session_signatures(SID_B, report_db=db)
        n_label = qv(conn, "SELECT COUNT(*) FROM label WHERE labeler='web-signature'")
        assert n_label == 2, f"세션 B 확정이 세션 A 라벨을 덮어썼다 (label={n_label})"
        assert qv(conn, "SELECT COUNT(DISTINCT run_id) FROM ingest_run") == 2
        got = sorted(r[0] for r in conn.execute("SELECT signature FROM label_signature"))
        assert got == ["BIMODALITY", "OUTLIER", "UNKNOWN"], got
        print("[d] 세션 구분 OK — 같은 case 를 두 세션에서 확정해도 안 덮인다")

        # (e) 해제 → 라벨·자식 동시 소멸 (고아 0건) ─────────────────────────────
        save(SID_A, [])
        assert edits.load_issue_signatures(db, SID_A) == {}
        eval_export.sync_session_signatures(SID_A, report_db=db)
        assert qv(conn, "SELECT COUNT(*) FROM label WHERE labeler='web-signature'") == 1
        orphans = qv(conn, "SELECT COUNT(*) FROM label_signature ls "
                           "LEFT JOIN label l ON l.label_id=ls.label_id "
                           "WHERE l.label_id IS NULL")
        assert orphans == 0, f"고아 label_signature {orphans}행"
        got = sorted(r[0] for r in conn.execute("SELECT signature FROM label_signature"))
        assert got == ["OUTLIER"], got
        print("[e] 해제 OK — 라벨·자식 동시 삭제, 고아 0건")
    finally:
        conn.close()

    print("\n전부 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

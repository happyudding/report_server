"""Eval 표본함(server/eval_panel/review.py) 검증.

실행:
    python tests/test_eval_review.py

검증하는 계약:
  (a) 층화 추출 — 룰당 8건(경계 3/중간 3/극단 2), **재현 가능**(같은 입력 → 같은 표본)
  (b) 층화 기준 metric 을 코드에 박지 않는다 — when_metric / review_metric 에서 뽑는다
  (c) 수집 → 표본함 조회 → 검수 라벨 저장이 실제로 이어진다
  (d) 추천 게이트 — 20건·양쪽 5건 미만이면 만들지 않는다
  (e) **강화 방향만** 추천한다 (느슨해지는 후보는 배제)
  (f) 검수 라벨(labeler='eval-review')이 전체 status 채점을 오염시키지 않는다

pytest 미사용(그건 eval_analyzer 전용) — 자체 실행 + assert 스타일(tests/ 관례).
⚠ 이 파일을 pytest 로 다른 test_*.py 와 묶어 돌리지 말 것 — env 격리가 깨진다.
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

_TMP = Path(tempfile.mkdtemp(prefix="eval_review_test_"))
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")

import pandas as pd  # noqa: E402

from eval_panel import review  # noqa: E402
from web_report import eval_debug, eval_export  # noqa: E402
from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402

SID = "1700000002_review1"


def make_table():
    """item 2개 — 표본함이 다루는 두 경우를 한 세션에 담는다.

    · ItemA: 넓게 퍼진 분포 + limit 바로 밖 fail → **LOW_CPK** (표본 층화 대상).
    · ItemB: 정상 몸통에서 뚝 떨어진 fail → **OUTLIER**, 그래서 이 case 의 LOW_CPK 는
      suppressed_by 로 지워진다(= LOW_CPK 표본이 1건만 남는 것이 억제의 증거다).

    ItemB 의 pass 값에 미세한 잡음을 주는 이유: 전부 같은 값이면 MAD=0 이라 meanAD 폴백을
    타는데, 그 경우 modified z 의 상한이 `n/(1.2533·fail수)`(60/(1.2533·8)≈6.0)로 눌려
    임계 12 를 넘을 수 없다. 잡음이 있으면 MAD 가 정상 몸통 폭을 재어 거리가 그대로 나온다.
    """
    cols = META_COLUMNS + ["ItemA", "ItemB"]
    head = [
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P1"],
        ["UNIT", "", "", "", "", "", "", "V", "V"],
        ["HILIM", "", "", "", "", "", "", 10, 10],
        ["LOLIM", "", "", "", "", "", "", 0, 0],
    ]
    body = []
    for i in range(60):
        a_fail, b_fail = 40 <= i < 48, 50 <= i < 58
        # ItemA — spec 폭(0~10) 대비 넓게 퍼뜨려 cpk 를 떨어뜨린다. fail 은 limit 바로 밖.
        a = 10.2 if a_fail else 1.5 + (i * 7 % 52) * 0.133
        b = 15.0 if b_fail else 5.0 + (i % 5) * 0.02
        bin_ = 4 if a_fail else (5 if b_fail else 1)
        ft = 100 if a_fail else (200 if b_fail else "")
        body.append([f"s{i}", 1, 1, i, 0, bin_, ft, a, b])
    return split_honeyform(pd.DataFrame(head + body, columns=cols),
                           source="src0", file_name="src0")


class FakeReportDB:
    def __init__(self, session):
        self.session = session

    def get_session(self, session_id):
        return self.session if session_id == self.session["session_id"] else None

    def log_audit(self, **kw):
        pass


def row(case_id, **kw):
    """_stratify 입력 형태의 합성 행."""
    base = {"case_id": case_id, "eval_id": int(case_id[1:]), "outlier_ratio": 0.0}
    base.update(kw)
    return base


def test_exceedance_and_direction():
    # ">" 룰은 임계값을 넘을수록 커지고, "<" 룰은 밑으로 내려갈수록 커진다.
    assert review._exceedance(0.10, ">", 0.05) == 1.0
    assert review._exceedance(0.05, ">", 0.05) == 0.0
    assert round(review._exceedance(0.05, "<", 0.10), 4) == 0.5
    assert review._exceedance(None, ">", 0.05) is None
    assert review._exceedance(0.1, ">", None) is None

    assert review._passes(0.10, ">", 0.05) is True
    assert review._passes(0.05, ">", 0.05) is False        # 배타 비교
    assert review._passes(None, ">", 0.05) is False        # 결측을 양호로 읽지 않는다

    # (e) 강화 방향 판정
    assert review._is_stronger(0.08, 0.05, ">") is True
    assert review._is_stronger(0.03, 0.05, ">") is False   # 느슨해지는 쪽은 후보 아님
    assert review._is_stronger(0.03, 0.05, "<") is True
    print("[a] exceedance/passes/방향 OK")


def test_stratify_composition_and_determinism():
    rows = [row(f"c{i:03d}", outlier_ratio=0.05 + i * 0.01) for i in range(50)]
    picked = review._stratify(rows, "outlier_ratio", ">", 0.05)
    assert len(picked) == review.SAMPLE_MAX == 8, len(picked)

    exc = [p["_exceedance"] for p in picked]
    assert exc == sorted(exc), "정렬이 깨졌다"
    # 경계(가장 작은 3개)와 극단(가장 큰 2개)이 실제로 들어 있어야 층화의 의미가 있다.
    # 입력은 이미 "그 룰이 발화한" 케이스라 여기서 임계값으로 다시 거르지 않는다.
    all_sorted = sorted(r["outlier_ratio"] for r in rows)
    got = [p["outlier_ratio"] for p in picked]
    assert got[:3] == all_sorted[:3], (got[:3], all_sorted[:3])
    assert got[-2:] == all_sorted[-2:], (got[-2:], all_sorted[-2:])
    # 중간 구간에서도 뽑혔다(경계·극단만이 아님)
    assert any(all_sorted[5] <= v <= all_sorted[-5] for v in got[3:-2])

    # (a) 재현 가능 — 입력 순서를 섞어도 같은 표본
    again = review._stratify(list(reversed(rows)), "outlier_ratio", ">", 0.05)
    assert [p["case_id"] for p in again] == [p["case_id"] for p in picked]

    # 표본보다 적으면 전부 돌려준다
    few = review._stratify(rows[:5], "outlier_ratio", ">", 0.05)
    assert len(few) == 5
    print("[a] 층화 구성·재현성 OK")


def test_criterion_from_rules_not_hardcoded():
    """(b) 기준 metric 은 배포 yaml 에서 나온다 — 코드에 룰별 분기가 없다."""
    from web_report import eval_debug
    th = eval_debug.effective_thresholds("MDDI", "MDDI_ETC")
    sigs = {s["id"]: s for s in eval_debug.signatures_scoped("MDDI", "MDDI_ETC")}

    assert review._rule_criterion(sigs["LOW_CPK"], th) == ("cpk", "<", "cpk_warn")
    assert review._rule_criterion(sigs["MEAN_SHIFT"], th) \
        == ("center_bias", ">", "mean_shift_warn")
    # BIMODALITY 는 when_metric 이 판정 기준이 아니라 yaml review_metric 을 따른다.
    assert review._rule_criterion(sigs["BIMODALITY"], th) \
        == ("density_gap", ">", "subpop_density_gap_warn")
    # v9(2026-08-19)부터 판정 기준값이 저장돼 OUTLIER·공간 룰도 층화된다.
    # (종전에는 per-DUT 원본이 있어야 계산돼 둘 다 None 이었다.)
    assert review._rule_criterion(sigs["OUTLIER"], th) \
        == ("fail_mad_min", ">=", "outlier_fail_mad_min"), \
        review._rule_criterion(sigs["OUTLIER"], th)
    assert review._rule_criterion(sigs["EDGE_FAIL"], th) \
        == ("edge_fail_share", ">=", "region_fail_share_min"), \
        review._rule_criterion(sigs["EDGE_FAIL"], th)
    # 임계값 키를 참조하지 않는 룰은 층화 불가로 정직하게 None
    assert review._rule_criterion({"when_metric": {"stdev": "<=0"}}, th) is None
    print("[b] 기준 metric 이 yaml 에서 나옴 OK")


def test_score_threshold():
    labeled = [
        {"outlier_ratio": 0.20, "_correct": True},
        {"outlier_ratio": 0.15, "_correct": True},
        {"outlier_ratio": 0.06, "_correct": False},
        {"outlier_ratio": 0.055, "_correct": False},
    ]
    at_current = review._score_threshold(labeled, "outlier_ratio", ">", 0.05)
    assert at_current == {"fired": 4, "kept_ok": 2, "kept_over": 2, "total_ok": 2,
                          "precision": 0.5}, at_current
    tightened = review._score_threshold(labeled, "outlier_ratio", ">", 0.10)
    assert tightened["fired"] == 2 and tightened["precision"] == 1.0, tightened
    assert tightened["kept_ok"] == 2, "맞음 사례를 잃지 않아야 한다"
    print("[e] 임계값 재판정 채점 OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    test_exceedance_and_direction()
    test_stratify_composition_and_determinism()
    test_criterion_from_rules_not_hardcoded()
    test_score_threshold()

    # (c) 수집 → 표본함 → 검수 저장 배선 ─────────────────────────────────────
    session = {
        "session_id": SID, "source": "web_report", "analysis_key": "ak_review",
        "product_type": "MDDI", "product": "PRODX", "lot_id": "LOT1",
        "revision": "1.0", "file_name": "t.xlsx", "uploaded_by": "tester", "mode": "Normal",
    }
    db = FakeReportDB(session)
    got = eval_export.collect_session_snapshot(SID, report_db=db, upload_root=_TMP,
                                               tables=[make_table()])
    assert got["collected"] == 1 and got["cases"] >= 1, got

    q = review.queue("MDDI", "MDDI_ETC")
    assert q["collected"] is True, q
    by_id = {r["id"]: r for r in q["rules"]}
    # 배포 활성 룰만 표본함에 뜬다(꺼진 룰의 표본을 검수시키지 않는다). 목록을 박아 두면
    # 룰을 켤 때마다 깨지므로 배포 yaml 에서 기대값을 유도한다 — 검사하려는 것은
    # "어떤 룰이 켜져 있나" 가 아니라 "꺼진 룰·UNKNOWN 이 새어 들어오지 않나" 다.
    deployed = {s["id"] for s in eval_debug.signatures_scoped("MDDI", "MDDI_ETC")
                if s.get("enabled") is not False} - {eval_debug.unknown_id()}
    assert set(by_id) == deployed, (sorted(by_id), sorted(deployed))
    # UNKNOWN(미분류 명시 발화)은 임계값이 없어 강화할 대상이 없다 — 무판정 트랙의 몫.
    assert eval_debug.unknown_id() not in by_id
    severe = by_id["LOW_CPK"]
    assert severe["criterion"]["threshold_key"] == "cpk_warn"
    assert severe["pending_total"] >= 1 and severe["samples"], severe
    # 2026-08-13 부터 `suppressed_by` 는 **목록에서 지우지 않고 primary 만 양보**한다 —
    # ItemB(원인이 OUTLIER)의 LOW_CPK 도 발화 목록에 남으므로 표본 후보는 두 건이다.
    # cpk 임계를 검수하려면 오히려 이쪽이 맞다(양보했다고 cpk 가 정상인 것은 아니다).
    assert severe["pending_total"] == 2, \
        f"LOW_CPK 발화가 목록에서 사라졌다(양보는 primary 만 바꿔야 한다): {severe}"
    assert by_id["OUTLIER"]["pending_total"] == 1, by_id["OUTLIER"]
    print(f"[c] 표본함 조회 OK — LOW_CPK 후보 {severe['pending_total']}건 "
          f"(OUTLIER case 포함, primary 만 양보) · OUTLIER 후보 1건")

    sample = severe["samples"][0]
    review.save_review_label(sample["eval_id"], correct=False, comment="산발 아님",
                             reviewer="tester")
    q2 = review.queue("MDDI", "MDDI_ETC")
    s2 = {r["id"]: r for r in q2["rules"]}["LOW_CPK"]
    assert s2["labeled"] == 1 and s2["labeled_over"] == 1, s2
    assert all(x["eval_id"] != sample["eval_id"] for x in s2["samples"]), \
        "검수한 케이스가 다시 표본으로 나왔다"
    print("[c] 검수 라벨 저장·재출제 방지 OK")

    # (d) 추천 게이트 — 표본이 적으면 만들지 않는다
    p = review.proposal("LOW_CPK", "MDDI", "MDDI_ETC")
    assert "blocked" in p and "표본 부족" in p["blocked"], p
    print(f"[d] 추천 게이트 OK — {p['blocked']}")

    # (f) 검수 라벨이 전체 status 채점에 섞이지 않는다
    from admin_panel import eval_admin
    sc = eval_admin.scoring()
    assert sc.get("pairs", 0) == 0, f"eval-review 라벨이 status 채점에 샜다: {sc}"
    print("[f] 채점 분리 OK")

    print("\n전부 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

"""evaluate(thresholds_override=...) 배선 — 세션 단위 임계값 주입.

단위 계산이 아니라 **배선**을 본다(tests/CLAUDE.md 규칙): 인자가 case 까지 흘러
`thresholds_for` 병합 맨 뒤에 얹히는지, 그리고 서로 다른 override 가 동시에 돌 때 섞이지
않는지. 마지막 항목이 이 파일의 존재 이유다 — `_rules._scope` 는 모듈 전역이라 override 를
스코프에 넣었다면 조용히 오염됐을 자리다(서버는 컴퓨트 워커 여럿이 병렬로 evaluate 를 부른다).
"""
import threading

import pytest

from eval_engine import api
from eval_engine.pipeline._rules import rules_scope, thresholds_for


def _case(**kw):
    c = {"product_type": "PMIC", "family_product": "SOC", "item_class": None}
    c.update(kw)
    return c


def _run_input(usl=1.4):
    """cpk 가 낮게 나오는 단순 raw_table — LOW_CPK 발화 경계를 override 로 넘나든다."""
    rows = []
    for i in range(40):
        rows.append({"DUT": i + 1, "XCoord": i % 5, "YCoord": i // 5, "Bin": 1,
                     "Serial": f"S{i+1}", "VREF_TRIM": 1.20 + 0.01 * (i % 7)})
    rows.append({"DUT": 99, "XCoord": 2, "YCoord": 2, "Bin": 18, "Serial": "S99",
                 "VREF_TRIM": 1.55})
    return {
        "meta": {"product_name": "S5E_TEST_0000001", "family_product": "SOC",
                 "product_type": "PMIC", "revision": 0.0, "lot_id": "LOT001",
                 "wafer_number": 3},
        "raw_table": {"meta_columns": ["DUT", "XCoord", "YCoord", "Bin", "Serial"],
                      "item_columns": ["VREF_TRIM"], "units": {"VREF_TRIM": "V"},
                      "lower_limit": {"VREF_TRIM": 1.0}, "upper_limit": {"VREF_TRIM": usl},
                      "rows": rows},
    }


# ── 병합 (thresholds_for 수준) ────────────────────────────────────────────────

def test_override_wins_over_file_scopes():
    """case 에 스탬프된 override 가 파일 병합 결과를 덮는다."""
    with rules_scope():
        base = thresholds_for(_case())["cpk_warn"]
        got = thresholds_for(_case(_th_override={"cpk_warn": 0.77},
                                   _th_override_digest="d1"))["cpk_warn"]
    assert base != 0.77
    assert got == 0.77


def test_override_only_touches_declared_keys():
    """선언하지 않은 키는 파일 값 그대로 — 부분 오버라이드다."""
    with rules_scope():
        base = thresholds_for(_case())
        got = thresholds_for(_case(_th_override={"cpk_warn": 0.77},
                                   _th_override_digest="d1"))
    assert got["n_min"] == base["n_min"]
    assert set(got) == set(base)


def test_scope_cache_splits_by_digest():
    """같은 스코프라도 digest 가 다르면 병합 결과가 갈린다(캐시 키에 digest 포함)."""
    with rules_scope():
        a = thresholds_for(_case(_th_override={"cpk_warn": 0.7}, _th_override_digest="a"))
        b = thresholds_for(_case(_th_override={"cpk_warn": 0.9}, _th_override_digest="b"))
        plain = thresholds_for(_case())
    assert (a["cpk_warn"], b["cpk_warn"]) == (0.7, 0.9)
    assert plain["cpk_warn"] not in (0.7, 0.9)


def test_override_does_not_mutate_shared_dict():
    """override 병합이 공유 객체를 고치지 않는다 — 같은 스코프의 다른 case 가 오염되면 안 된다."""
    with rules_scope():
        plain_before = thresholds_for(_case())["cpk_warn"]
        thresholds_for(_case(_th_override={"cpk_warn": 0.55}, _th_override_digest="x"))
        plain_after = thresholds_for(_case())["cpk_warn"]
    assert plain_before == plain_after


# ── evaluate() 인자 배선 ──────────────────────────────────────────────────────

def test_evaluate_without_override_is_unchanged():
    """override 를 안 주면 결과가 종전과 동일해야 한다(기본 경로 보존)."""
    ri = _run_input()
    a = api.evaluate(ri, persist=False)
    b = api.evaluate(ri, persist=False, thresholds_override=None)
    strip = lambda r: [(c["item_canonical"], c["status"], c["primary_signature"],
                        tuple(c["secondary_signatures"])) for c in r["cases"]]
    assert strip(a) == strip(b)


def test_evaluate_override_changes_firing():
    """override 로 임계값을 밀면 발화가 바뀐다 — 인자가 실제 판정까지 흐른다는 증거."""
    ri = _run_input()
    fired = lambda res: {s for c in res["cases"]
                         for s in [c["primary_signature"], *c["secondary_signatures"]] if s}

    loose = api.evaluate(ri, persist=False, thresholds_override={"cpk_warn": 0.01})
    tight = api.evaluate(ri, persist=False, thresholds_override={"cpk_warn": 5.0})
    # cpk_warn 을 사실상 끄면 LOW_CPK 가 사라지고, 크게 올리면 붙는다.
    assert "LOW_CPK" not in fired(loose)
    assert "LOW_CPK" in fired(tight)


def test_evaluate_override_does_not_leak_into_result():
    """`_th_override` 스탬프가 호출자에게 돌려주는 결과 dict 로 새지 않는다."""
    res = api.evaluate(_run_input(), persist=False,
                       thresholds_override={"cpk_warn": 5.0})
    for case in res["cases"]:
        assert not any(str(k).startswith("_th_override") for k in case)


def test_concurrent_evaluate_does_not_cross_contaminate():
    """서로 다른 override 를 가진 evaluate 가 동시에 돌아도 결과가 섞이지 않는다.

    override 를 `_rules._scope`(모듈 전역)에 넣었다면 여기서 깨진다 — case 탑재 설계의 핵심.
    """
    ri = _run_input()
    out = {}

    def run(tag, ovr):
        res = api.evaluate(ri, persist=False, thresholds_override=ovr)
        out[tag] = {s for c in res["cases"]
                    for s in [c["primary_signature"], *c["secondary_signatures"]] if s}

    for _ in range(4):          # 반복해야 경합이 드러난다
        out.clear()
        threads = [threading.Thread(target=run, args=("loose", {"cpk_warn": 0.01})),
                   threading.Thread(target=run, args=("tight", {"cpk_warn": 5.0}))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert "LOW_CPK" not in out["loose"]
        assert "LOW_CPK" in out["tight"]

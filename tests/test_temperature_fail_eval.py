"""Temperature 모드 CT/HT **전 항목** RT limit 재판정 회귀 테스트 (2026-08-05).

실행:
    python tests/test_temperature_fail_eval.py

고정하는 계약 (web_report/tabs/temp_fail.py):
  1. 한 die 가 여러 항목을 벗어나면 **그 항목 전부**에 계상된다 — 구 "TSEQ 첫 fail 하나만"
     (web_report/temperature.py:_clean_member) 과 다른 지점이자 이 개편의 목적.
  2. 그래서 소스별 fail% 합계가 **100% 를 넘을 수 있다** (사용자 확정).
  3. 판정 대상은 RT·member 양쪽에 있고 RT limit 이 있는 항목뿐이며 순서는 RT TSEQ.
  4. 측정 결측(NaN)은 fail 이 아니다 (클라 판정과 동일).
  5. Bin 은 limits 매핑의 **usl_bin**(.lt "20:19" 의 콜론 오른쪽) → 관측 bin → 공백 순.
     관측 bin 폴백에서 "999"(미상 표식)는 쓰지 않는다.
  6. temp_fail_indices 의 die 인덱스는 Map_analysis 의 dies 배열과 정합한다.

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402
from web_report.tabs.Map_analysis import build_map_analysis_rows  # noqa: E402
from web_report.tabs.temp_fail import (build_temp_fail_rows,  # noqa: E402
                                       judge_items, temp_fail_indices)

# item: (TSEQ, TNO, LOLIM, HILIM) — LOLIM/HILIM 이 ""이면 그 항목은 판정 대상에서 빠진다.
ITEMS = {
    "ItemA": (1, 100, 0, 10),
    "ItemB": (2, 200, 0, 10),
    "ItemC": (3, 300, 0, 10),
    "ItemD": (4, 400, "", ""),      # limit 없음 → 판정 제외
}


def make_table(source, values, *, items=None, bins=None, coords=None, limits=None):
    """values: {item: [die 별 값]}. bins: die 별 BIN(기본 전부 "1")."""
    items = items or list(ITEMS)
    limits = limits or ITEMS
    n = len(next(iter(values.values())))
    cols = META_COLUMNS + items
    rows = [
        ["TSEQ", "", "", "", "", "", ""] + [limits[i][0] for i in items],
        ["TNO", "", "", "", "", "", ""] + [limits[i][1] for i in items],
        ["STEP", "", "", "", "", "", ""] + ["P1"] * len(items),
        ["UNIT", "", "", "", "", "", ""] + ["V"] * len(items),
        ["HILIM", "", "", "", "", "", ""] + [limits[i][3] for i in items],
        ["LOLIM", "", "", "", "", "", ""] + [limits[i][2] for i in items],
    ]
    for i in range(n):
        x, y = (coords[i] if coords else (i % 10, i // 10))
        rows.append([f"{source}-{i}", 1, 1, x, y,
                     (bins[i] if bins else "1"), ""] + [values[it][i] for it in items])
    return split_honeyform(pd.DataFrame(rows, columns=cols), source=source, file_name=source)


def rt_table(n=10):
    return make_table("W_RT", {it: [5] * n for it in ITEMS})


def member_table(source="W_CT", n=10, *, overlap=7, bins=None):
    """앞 ``overlap`` die 가 ItemA·ItemC 를 **동시에** 벗어난다 (구 로직은 A 만 봤다).

    ItemB 는 die 0 하나만 LOLIM 미달. ItemD 는 limit 이 없어 값이 벗어나도 판정 대상이 아니다.
    """
    values = {it: [5] * n for it in ITEMS}
    for i in range(overlap):
        values["ItemA"][i] = 15      # > HILIM
        values["ItemC"][i] = 15      # > HILIM
    values["ItemB"][0] = -5          # < LOLIM
    values["ItemD"][0] = 9999        # limit 없음 → 무시
    return make_table(source, values, bins=bins)


GROUPS = [{"rt": "W_RT", "members": ["W_CT"], "member_roles": ["CT"]}]


def test_all_items_counted_not_only_first():
    """전 항목 판정 — 같은 die 가 ItemA·ItemC 양쪽에 계상된다."""
    tables = [rt_table(), member_table()]
    rows = build_temp_fail_rows(tables, GROUPS, {"W_CT": 10})
    by_item = {r["Item"]: r for r in rows if r.get("Item")}
    assert set(by_item) == {"ItemA", "ItemB", "ItemC"}, sorted(by_item)
    assert by_item["ItemA"]["W_CT_yield"] == 70.0, by_item["ItemA"]
    assert by_item["ItemC"]["W_CT_yield"] == 70.0, by_item["ItemC"]
    assert by_item["ItemB"]["W_CT_yield"] == 10.0, by_item["ItemB"]
    # 구 "첫 fail 하나만" 이면 ItemC 는 행 자체가 없었다 (이 개편의 목적)
    assert "ItemD" not in by_item, "limit 없는 항목은 판정 대상이 아니다"
    # 정렬 = fail die 수 내림차순 (동수는 item 이름순)
    order = [r["Item"] for r in rows if r.get("Item")]
    assert order == ["ItemA", "ItemC", "ItemB"], order


def test_sum_may_exceed_100_percent():
    """소스별 fail% 합계 100% 초과 허용 (2026-08-05 사용자 확정)."""
    tables = [rt_table(), member_table(overlap=10)]
    rows = build_temp_fail_rows(tables, GROUPS, {"W_CT": 10})
    total = sum(float(r["W_CT_yield"]) for r in rows if r.get("Item"))
    assert total > 100.0, total


def test_judge_items_follow_rt_tseq_and_intersection():
    """판정 대상 = 양쪽에 있고 RT limit 이 있는 항목, 순서는 RT TSEQ."""
    rt = rt_table()
    member = make_table("W_CT", {it: [5] * 10 for it in ("ItemC", "ItemA")},
                        items=["ItemC", "ItemA"])
    assert [it for it, _lo, _hi in judge_items(rt, member)] == ["ItemA", "ItemC"]


def test_nan_is_pass():
    """측정 결측은 fail 이 아니다."""
    values = {it: [5] * 4 for it in ITEMS}
    values["ItemA"] = [float("nan")] * 4
    tables = [rt_table(4), make_table("W_CT", values)]
    rows = build_temp_fail_rows(tables, GROUPS, {"W_CT": 4})
    assert [r["Item"] for r in rows if r.get("Item")] == [], rows


def test_bin_priority_limits_then_observed_then_blank():
    """Bin = limits usl_bin(콜론 오른쪽) → 관측 bin → 공백. 999 관측은 무시."""
    tables = [rt_table(), member_table(bins=["20"] * 7 + ["1"] * 3)]
    # fail_counts 관측 폴백 — (bin, item) Counter (metrics 가 넘겨주는 형태)
    fail_counts = {"W_CT": {("20", "ItemA"): 7, ("999", "ItemC"): 7}}

    limits = {"ItemA": {"tno": "100", "lsl_bin": "20", "usl_bin": "19"}}
    rows = build_temp_fail_rows(tables, GROUPS, {"W_CT": 10},
                                fail_counts=fail_counts, limits_meta=limits)
    by_item = {r["Item"]: r for r in rows if r.get("Item")}
    assert by_item["ItemA"]["Bin"] == "19", by_item["ItemA"]      # .lt "20:19" 오른쪽
    assert by_item["ItemC"]["Bin"] == "", by_item["ItemC"]        # 관측이 999 뿐 → 공백
    assert by_item["ItemB"]["Bin"] == "", by_item["ItemB"]        # 관측 없음 → 공백

    # limits 없으면 관측 bin 폴백
    rows = build_temp_fail_rows(tables, GROUPS, {"W_CT": 10}, fail_counts=fail_counts)
    by_item = {r["Item"]: r for r in rows if r.get("Item")}
    assert by_item["ItemA"]["Bin"] == "20", by_item["ItemA"]


def test_indices_align_with_map_dies():
    """temp_fail_indices 의 idx 로 Map dies 를 인덱싱하면 그 die 가 실제 fail die 다."""
    # 좌표 결측 die 를 섞어 mask 정합을 확인한다 (Map_analysis 와 같은 규칙이어야 한다).
    coords = [(i % 5, i // 5) for i in range(10)]
    coords[3] = ("", "")
    tables = [rt_table(), make_table(
        "W_CT", {**{it: [5] * 10 for it in ITEMS},
                 "ItemA": [15] * 7 + [5] * 3}, coords=coords)]
    packs = temp_fail_indices(tables, GROUPS)
    assert [p["source"] for p in packs] == ["W_CT"], packs
    pack = packs[0]
    maps = build_map_analysis_rows(tables, "", "", "Normal", include_dies=True)
    dies = next(m["dies"] for m in maps if m["source"] == "W_CT")
    assert pack["n"] == len(dies), (pack["n"], len(dies))
    idx = next(e["idx"] for e in pack["items"] if e["item"] == "ItemA")
    # 좌표 결측(die 3)이 빠져 유효 die 는 9개, 그중 fail 은 6개
    assert len(idx) == 6, idx
    fail_coords = {(dies[k]["x"], dies[k]["y"]) for k in idx}
    assert fail_coords == {(0, 0), (1, 0), (2, 0), (4, 0), (0, 1), (1, 1)}, fail_coords


def test_payload_temp_sheet_end_to_end():
    """build_report_payload 경유로도 같은 결과 (Yield 시트는 RT 만)."""
    payload = build_report_payload([rt_table(), member_table()], mode="Temperature",
                                   temperature_groups={"groups": GROUPS})
    rows = payload["sheets"]["Issue Table Temp"]
    by_item = {r["Item"]: r for r in rows if r.get("Item")}
    assert by_item["ItemA"]["W_CT_yield"] == 70.0, by_item["ItemA"]
    assert "W_CT_yield" not in payload["sheets"]["Yield"][0], payload["sheets"]["Yield"][0]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_all_items_counted_not_only_first,
               test_sum_may_exceed_100_percent,
               test_judge_items_follow_rt_tseq_and_intersection,
               test_nan_is_pass,
               test_bin_priority_limits_then_observed_then_blank,
               test_indices_align_with_map_dies,
               test_payload_temp_sheet_end_to_end):
        fn()
        checks += 1
    print(f"PASS: test_temperature_fail_eval ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

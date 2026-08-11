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
  7. 표시 순서·Bin 묶음(대표 1행 + 접힘)의 기준은 **avg**(소스 평균 fail%) 내림차순이다
     (2026-08-11 — 일반 Yield 표와 같은 기준). 행 자체는 여전히 항목당 1개.

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
    # 정렬 = avg(소스 평균 fail%) 내림차순, 동률은 item 이름순 (2026-08-11 — 구 fail die 수)
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


def multi_group_tables(n_groups=3, n=10, overlap=7):
    """3그룹 × RT/CT/HT = 9 소스 — 21 source 세션의 축소 재현.

    그룹마다 다른 항목이 다르게 이탈하게 해, 소스 컬럼 순서·다소스 합산·정렬을 본다.
    """
    tables, groups = [], []
    for g in range(n_groups):
        rt = f"G{g}_RT"
        tables.append(make_table(rt, {it: [5] * n for it in ITEMS}))
        members, roles = [], []
        for role in ("CT", "HT"):
            name = f"G{g}_{role}"
            values = {it: [5] * n for it in ITEMS}
            # 그룹마다 이탈 항목을 달리한다 (g0=ItemA, g1=ItemB, g2=ItemC)
            target = ["ItemA", "ItemB", "ItemC"][g % 3]
            hi = overlap if role == "CT" else max(1, overlap - 3)
            for i in range(hi):
                values[target][i] = 15
            tables.append(make_table(name, values))
            members.append(name)
            roles.append(role)
        groups.append({"rt": rt, "members": members, "member_roles": roles})
    return tables, groups


def test_multi_group_sources_and_merge():
    """다중 그룹 — 컬럼 소스는 CT/HT 전부, 순서는 groups 배치 순서."""
    tables, groups = multi_group_tables()
    rows = build_temp_fail_rows(tables, groups, {t.source: 10 for t in tables})
    data = [r for r in rows if r.get("Item")]
    assert data, rows
    keys = [k[:-6] for k in data[0] if k.endswith("_yield")]
    assert keys == ["G0_CT", "G0_HT", "G1_CT", "G1_HT", "G2_CT", "G2_HT"], keys
    assert not any(k.endswith("_RT_yield") for k in data[0]), sorted(data[0])
    by_item = {r["Item"]: r for r in data}
    # 그룹 0 의 CT 만 ItemA 70%, 다른 그룹 CT/HT 는 0
    assert by_item["ItemA"]["G0_CT_yield"] == 70.0, by_item["ItemA"]
    assert by_item["ItemA"]["G1_CT_yield"] == 0.0, by_item["ItemA"]
    # 정렬은 avg 내림차순 — 세 항목 모두 (70+40)/6 = 18.33 으로 동률이라 이름순
    assert [r["Item"] for r in data] == ["ItemA", "ItemB", "ItemC"], [r["Item"] for r in data]


def test_member_roles_absent_fallback():
    """member_roles 없는 옛 세션도 판정은 동일 (corner 라벨은 판정에 안 쓰인다)."""
    tables, groups = multi_group_tables(n_groups=1)
    legacy = [{"rt": g["rt"], "members": g["members"]} for g in groups]
    totals = {t.source: 10 for t in tables}
    assert (build_temp_fail_rows(tables, groups, totals)
            == build_temp_fail_rows(tables, legacy, totals))


def test_compute_once_matches_separate_paths():
    """판정 1회화 등가성 — packs 를 넘긴 결과와 각자 계산한 결과가 같다."""
    from web_report.tabs.temp_fail import compute_temp_fail

    tables, groups = multi_group_tables()
    totals = {t.source: 10 for t in tables}
    packs = compute_temp_fail(tables, groups)
    assert build_temp_fail_rows(tables, groups, totals, packs=packs) ==         build_temp_fail_rows(tables, groups, totals)
    assert temp_fail_indices(tables, groups, packs) == temp_fail_indices(tables, groups)
    # idx 는 JSON 직렬화 가능한 list 여야 한다(라우트가 그대로 json.dumps 한다)
    for pack in temp_fail_indices(tables, groups):
        for entry in pack["items"]:
            assert isinstance(entry["idx"], list), entry
            assert all(isinstance(v, int) for v in entry["idx"]), entry


def test_indices_align_with_step_split_maps():
    """STEP 2종 세션 — step 분리 맵 각각의 dies 길이가 인덱스 기준(n)과 같다."""
    n = 8
    steps = {"ItemA": (1, 100, 0, 10), "ItemB": (2, 200, 0, 10)}
    cols = META_COLUMNS + ["ItemA", "ItemB"]

    def tbl(source, vals):
        rows = [
            ["TSEQ", "", "", "", "", "", "", 1, 2],
            ["TNO", "", "", "", "", "", "", 100, 200],
            ["STEP", "", "", "", "", "", "", "P1", "P2"],   # STEP 2종
            ["UNIT", "", "", "", "", "", "", "V", "V"],
            ["HILIM", "", "", "", "", "", "", 10, 10],
            ["LOLIM", "", "", "", "", "", "", 0, 0],
        ]
        for i in range(n):
            rows.append([f"{source}-{i}", 1, 1, i % 4, i // 4,
                         "1", ""] + [vals["ItemA"][i], vals["ItemB"][i]])
        return split_honeyform(pd.DataFrame(rows, columns=cols),
                              source=source, file_name=source)

    rt = tbl("S_RT", {"ItemA": [5] * n, "ItemB": [5] * n})
    ct = tbl("S_CT", {"ItemA": [15] * 3 + [5] * (n - 3), "ItemB": [5] * n})
    groups = [{"rt": "S_RT", "members": ["S_CT"], "member_roles": ["CT"]}]
    packs = temp_fail_indices([rt, ct], groups)
    maps = build_map_analysis_rows([rt, ct], "", "", "Normal", include_dies=True)
    ct_maps = [m for m in maps if m["source"] == "S_CT"]
    assert len(ct_maps) >= 2, [(m["source"], m.get("step")) for m in maps]
    for m in ct_maps:
        assert len(m["dies"]) == packs[0]["n"], (m.get("step"), len(m["dies"]))
    assert steps  # 픽스처 의도 기록용


def test_payload_temp_sheet_end_to_end():
    """build_report_payload 경유로도 같은 결과 (Yield 시트는 RT 만)."""
    payload = build_report_payload([rt_table(), member_table()], mode="Temperature",
                                   temperature_groups={"groups": GROUPS})
    rows = payload["sheets"]["Issue Table Temp"]
    by_item = {r["Item"]: r for r in rows if r.get("Item")}
    assert by_item["ItemA"]["W_CT_yield"] == 70.0, by_item["ItemA"]
    assert "W_CT_yield" not in payload["sheets"]["Yield"][0], payload["sheets"]["Yield"][0]


def test_avg_order_and_bin_groups():
    """표시 순서·Bin 묶음 기준은 **avg** — fail die 수와 갈리는 경우로 고정한다.

    소스마다 분모(die 수)가 다르면 "die 수 많은 항목"과 "avg 큰 항목"의 순위가 뒤집힌다:
    ItemA 는 fail 10 die 지만 분모가 100 이라 avg 5.0, ItemB 는 9 die 에 분모 10 이라 45.0.
    화면에 보이는 숫자(avg)를 따라 ItemB 가 위로 온다(2026-08-11 사용자 확정).
    같은 Bin 은 한 그룹으로 묶이고 대표는 그 Bin 의 avg 최대 행, 그룹 순서도 대표 avg 순.
    """
    n = 10
    rt0 = make_table("G0_RT", {it: [5] * n for it in ITEMS})
    rt1 = make_table("G1_RT", {it: [5] * n for it in ITEMS})
    v0 = {it: [5] * n for it in ITEMS}
    v0["ItemA"] = [15] * n                       # fail 10 die / 분모 100 → 10%
    v1 = {it: [5] * n for it in ITEMS}
    v1["ItemB"] = [15] * 9 + [5]                 # fail 9 die / 분모 10 → 90%
    v1["ItemC"] = [15] * 3 + [5] * 7             # fail 3 die / 분모 10 → 30%
    tables = [rt0, make_table("G0_CT", v0), rt1, make_table("G1_CT", v1)]
    groups = [{"rt": "G0_RT", "members": ["G0_CT"], "member_roles": ["CT"]},
              {"rt": "G1_RT", "members": ["G1_CT"], "member_roles": ["CT"]}]
    # 관측 bin: ItemB·ItemC 는 4, ItemA 는 5 → 그룹 2개
    counts = {"G1_CT": {("4", "ItemB"): 9, ("4", "ItemC"): 3},
              "G0_CT": {("5", "ItemA"): 10}}
    rows = build_temp_fail_rows(tables, groups, {"G0_CT": 100, "G1_CT": 10},
                                fail_counts=counts)
    data = [r for r in rows if r.get("Item")]
    assert [r["Item"] for r in data] == ["ItemB", "ItemC", "ItemA"], [r["Item"] for r in data]
    assert [r["avg"] for r in data] == [45.0, 15.0, 5.0], [r["avg"] for r in data]
    # Bin 묶음: (4: ItemB 대표 + ItemC 접힘) → (5: ItemA 대표, 접힘 없음)
    assert [r["Bin"] for r in data] == ["4", "4", "5"], [r["Bin"] for r in data]
    assert [r["_grp"] for r in data] == ["t0", "t0", "t1"], [r["_grp"] for r in data]
    assert [r["_detail"] for r in data] == [False, True, False], data
    assert data[0]["_ndetail"] == 1 and data[2]["_ndetail"] == 0, data
    assert "_ndetail" not in data[1], data[1]          # 접힘 행에는 없다
    # 항목 행은 여전히 항목당 1개 (집계 대표행을 새로 만들지 않는다 — row_key 규약)
    assert len(data) == 3 and len({r["Item"] for r in data}) == 3, data


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_all_items_counted_not_only_first,
               test_multi_group_sources_and_merge,
               test_member_roles_absent_fallback,
               test_compute_once_matches_separate_paths,
               test_indices_align_with_step_split_maps,
               test_sum_may_exceed_100_percent,
               test_judge_items_follow_rt_tseq_and_intersection,
               test_nan_is_pass,
               test_bin_priority_limits_then_observed_then_blank,
               test_indices_align_with_map_dies,
               test_avg_order_and_bin_groups,
               test_payload_temp_sheet_end_to_end):
        fn()
        checks += 1
    print(f"PASS: test_temperature_fail_eval ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

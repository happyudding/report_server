"""Compare 모드 재정의 + 동일성 검증 회귀 테스트 (2026-07-23).

실행:
    python tests/test_compare_equivalence.py

고정하는 계약:
  1. Grade 판정 — Grade1: AVG차(%) ≤ 5 / Grade2: 5 초과 & 양쪽 CPK ≥ 5 / Grade3: 그 외.
     AVG차·AVG차(%) 는 **절대값**이라 증가/감소 방향이 반대여도 같은 등급이 나온다.
  2. summary.total == grade1 + grade2 + grade3 == 공통 항목 수 (판정 불가도 Grade3 로 집계).
  3. Before 평균 0 / 한쪽 결측 → delta_pct is None 이고 Grade3.
  4. 그룹이 1 source 씩이면 pool == 그 테이블이라 통계가 **CPK 시트 값과 완전히 동일**.
  5. 그룹이 2+1 이면 pool 통계가 concat 프레임 기준 (직접 계산한 기대값과 일치).
  6. compare 옵션이 없으면(legacy) after=sources[0] / before=sources[1] 폴백 — 종전 관례.
  7. bin_matrix — 전 source 의 BIN 이 같은 좌표는 행에 없고, 하나라도 다르면 있다.
  8. dist_shift(산포 비교, 2026-07-28) — Before 분모 지표 6종(meanshift_sigma/cpk_ratio_pct/
     stdev_delta_pct/median_shift/iqr_delta_pct/ks_d) 수식, focus 판정(양쪽 cpk>100·양쪽
     고정값 제외 / 한쪽 cpk<1.33·|Δσ%|≥15 포함), meanshift 내림차순 정렬(None 최하단),
     IQR/KS 모집단이 cpk 통계와 같은 Bin1 임(fail die 주입 불변).

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례). pytest 로 수집해도 동작한다.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402
from web_report.validation import webreport_compare_groups  # noqa: E402

# 항목 4종 — Before 대비 After 의 평균을 의도적으로 다르게 만든다.
#   G1_UP    : +4%   (5% 이하)        → Grade1
#   G1_DOWN  : -4%   (절대값 4%)      → Grade1 (부호가 달라도 같은 등급)
#   G2_ITEM  : +10%, 양쪽 CPK 아주 큼 → Grade2
#   G3_ITEM  : +10%, CPK 작음         → Grade3
_TIGHT = [9.99, 10.0, 10.01] * 5      # σ 가 아주 작아 CPK 가 5 를 크게 넘는다
_WIDE = [8.0, 9.0, 10.0, 11.0, 12.0] * 3   # σ 가 커 CPK < 5


def _shift(values, factor):
    return [round(v * factor, 6) for v in values]


def _make_table(source, cols_values, *, bins=None, n_extra_fail=0):
    """합성 honeyform 1개. cols_values = {item: [Bin1 die 값…]} (모두 길이 같음)."""
    items = list(cols_values)
    cols = META_COLUMNS + items
    rows = [
        ["TSEQ", "", "", "", "", "", ""] + [i + 1 for i in range(len(items))],
        ["TNO", "", "", "", "", "", ""] + [(i + 1) * 100 for i in range(len(items))],
        ["STEP", "", "", "", "", "", ""] + ["P2"] * len(items),
        ["UNIT", "", "", "", "", "", ""] + ["V"] * len(items),
        ["HILIM", "", "", "", "", "", ""] + [20] * len(items),
        ["LOLIM", "", "", "", "", "", ""] + [0] * len(items),
    ]
    n = len(next(iter(cols_values.values())))
    for i in range(n):
        b = 1 if bins is None else bins[i]
        rows.append([f"{source}_p{i}", 1, 1, i, 0, b, ""] + [cols_values[c][i] for c in items])
    for j in range(n_extra_fail):
        rows.append([f"{source}_f{j}", 1, 1, 100 + j, 0, 5, 100] + [999.0] * len(items))
    return split_honeyform(pd.DataFrame(rows, columns=cols), source=source, file_name=source)


def make_pair():
    """Before(WF_B) / After(WF_A) 2 source — 항목 4종의 등급이 각각 1/1/2/3 이 되게."""
    before = {"G1_UP": list(_TIGHT), "G1_DOWN": list(_TIGHT),
              "G2_ITEM": list(_TIGHT), "G3_ITEM": list(_WIDE)}
    after = {"G1_UP": _shift(_TIGHT, 1.04), "G1_DOWN": _shift(_TIGHT, 0.96),
             "G2_ITEM": _shift(_TIGHT, 1.10), "G3_ITEM": _shift(_WIDE, 1.10)}
    # 업로드 순서 = [After…, Before…] (Compare 배치 다이얼로그가 정하는 순서)
    return [_make_table("WF_A", after), _make_table("WF_B", before)]


def _payload(tables, groups=None):
    return build_report_payload(tables, mode="Compare", compare_groups=groups)


def _eq_rows(payload):
    return {r["subject"]: r for r in payload["compare"]["equivalence"]["rows"]}


def test_grade_rules():
    """Grade 1/2/3 판정 + 절대값(증가/감소 대칭)."""
    payload = _payload(make_pair(), {"before": ["WF_B"], "after": ["WF_A"]})
    rows = _eq_rows(payload)
    assert rows["G1_UP"]["grade"] == 1, rows["G1_UP"]
    assert rows["G1_DOWN"]["grade"] == 1, rows["G1_DOWN"]
    assert rows["G2_ITEM"]["grade"] == 2, rows["G2_ITEM"]
    assert rows["G3_ITEM"]["grade"] == 3, rows["G3_ITEM"]

    # 방향만 반대인 두 항목은 AVG차·AVG차(%) 가 부호 없이 같은 크기여야 한다.
    up, down = rows["G1_UP"], rows["G1_DOWN"]
    assert up["delta_pct"] == down["delta_pct"] == 4.0, (up["delta_pct"], down["delta_pct"])
    assert up["delta_avg"] > 0 and down["delta_avg"] > 0, (up["delta_avg"], down["delta_avg"])
    assert abs(up["delta_avg"] - down["delta_avg"]) < 1e-9, (up, down)

    # Grade2 는 양쪽 CPK 가 임계 이상일 때만 — G3_ITEM 은 %가 같아도 CPK 가 작아 Grade3.
    assert min(rows["G2_ITEM"]["before"]["cpk"], rows["G2_ITEM"]["after"]["cpk"]) >= 5.0
    assert min(rows["G3_ITEM"]["before"]["cpk"], rows["G3_ITEM"]["after"]["cpk"]) < 5.0

    # STEP/UNIT/limit 이 채워진다(표 좌측 컬럼).
    assert up["step"] == "P2" and up["units"] == "V", up
    assert up["hilim"] == 20 and up["lolim"] == 0, up


def test_summary_total_is_sum_of_grades():
    eq = _payload(make_pair(), {"before": ["WF_B"], "after": ["WF_A"]})["compare"]["equivalence"]
    s = eq["summary"]
    assert s["total"] == len(eq["rows"]) == 4, s
    assert s["total"] == s["grade1"] + s["grade2"] + s["grade3"], s
    assert (s["grade1"], s["grade2"], s["grade3"]) == (2, 1, 1), s
    assert eq["before"] == "WF_B" and eq["after"] == "WF_A", eq


def test_undecidable_item_is_grade3():
    """Before 평균이 0 이면 상대 변화율을 낼 수 없다 → delta_pct None + Grade3."""
    zero = [0.0] * 15
    before = _make_table("WF_B", {"ZERO": zero})
    after = _make_table("WF_A", {"ZERO": _shift(_TIGHT, 1.0)})
    row = _eq_rows(_payload([after, before], {"before": ["WF_B"], "after": ["WF_A"]}))["ZERO"]
    assert row["delta_pct"] is None, row
    assert row["grade"] == 3, row


def test_single_source_group_matches_cpk_sheet():
    """그룹이 1개씩이면 pool 이 그 테이블 자체 → 통계가 CPK 시트 값과 완전히 동일."""
    payload = _payload(make_pair(), {"before": ["WF_B"], "after": ["WF_A"]})
    cpk = {(r["subject"], r["source"]): r for r in payload["sheets"]["CPK"]}
    for subject, row in _eq_rows(payload).items():
        for side, src in (("before", "WF_B"), ("after", "WF_A")):
            ref = cpk[(subject, src)]
            for key in ("average", "stdev", "cpk"):
                assert row[side][key] == ref[key], (subject, side, key, row[side][key], ref[key])


def test_pooled_group_uses_concat_frame():
    """After 그룹이 2장이면 두 장의 die 를 합친 통계가 나온다(=직접 계산한 기대값)."""
    vals_a1 = list(_TIGHT)
    vals_a2 = _shift(_TIGHT, 1.02)
    tables = [_make_table("WF_A1", {"IT": vals_a1}),
              _make_table("WF_A2", {"IT": vals_a2}),
              _make_table("WF_B", {"IT": list(_TIGHT)})]
    groups = {"before": ["WF_B"], "after": ["WF_A1", "WF_A2"]}
    row = _eq_rows(_payload(tables, groups))["IT"]

    pooled = pd.Series(vals_a1 + vals_a2, dtype="float64")
    assert row["after"]["average"] == round(pooled.mean(), 4), (row["after"], pooled.mean())
    # source 별 CPK 시트에는 이 pooled 평균과 같은 행이 없다(= pool 이 실제로 합쳐졌다).
    per_source = {r["source"]: r["average"] for r in _payload(tables, groups)["sheets"]["CPK"]}
    assert row["after"]["average"] not in per_source.values(), (row["after"], per_source)


def test_legacy_fallback_without_groups():
    """compare 옵션이 없는 기존 세션 — after=sources[0], before=sources[1] (종전 관례)."""
    payload = _payload(make_pair(), None)
    cmp_ = payload["compare"]
    assert cmp_["after_sources"] == ["WF_A"], cmp_["after_sources"]
    assert cmp_["before_sources"] == ["WF_B"], cmp_["before_sources"]
    assert cmp_["equivalence"]["after"] == "WF_A", cmp_["equivalence"]
    # goodlog 도 그 대표 2개로 만들어진다.
    assert cmp_["goodlog"]["after_source"] == "WF_A", cmp_["goodlog"]


def test_bin_matrix_lists_only_mismatch_coords():
    """전 source 의 BIN 이 같은 좌표는 빠지고, 다른 좌표만 1행씩 나온다."""
    n = 15
    bins_same = [1] * n
    bins_diff = [1] * n
    bins_diff[3] = 5                     # 좌표 x=3 에서만 갈린다
    after = _make_table("WF_A", {"IT": list(_TIGHT)}, bins=bins_diff)
    before = _make_table("WF_B", {"IT": list(_TIGHT)}, bins=bins_same)
    bm = _payload([after, before], {"before": ["WF_B"], "after": ["WF_A"]})["compare"]["bin_matrix"]
    assert bm["counts"]["common_dies"] == n, bm["counts"]
    assert bm["counts"]["mismatch"] == 1, bm["counts"]
    assert [r["x"] for r in bm["rows"]] == [3], bm["rows"]
    # bins 는 sources(=업로드) 순서 — [After, Before]
    assert bm["rows"][0]["bins"] == ["5", "1"], bm["rows"][0]
    # Pass→Fail 은 그룹 대표 기준
    assert bm["counts"]["pass_to_fail"] == 1 and bm["counts"]["fail_to_pass"] == 0, bm["counts"]


def test_common_map_carries_per_source_bins():
    """공통성 Map die 에 source 별 BIN 이 실린다(마우스오버 표시용)."""
    cm = _payload(make_pair(), {"before": ["WF_B"], "after": ["WF_A"]})["compare"]["common_map"]
    assert cm["groups"] == {"WF_A": "after", "WF_B": "before"}, cm["groups"]
    assert cm["dies"], cm["counts"]
    for die in cm["dies"]:
        assert len(die["bins"]) == 2, die


def test_compare_groups_option_parsing():
    """webreport_options → 그룹. 없는 source 이름은 걸러지고, 한쪽이 비면 None(폴백)."""
    opts = '{"compare": {"before": ["WF_B", "GONE"], "after": ["WF_A"]}}'
    assert webreport_compare_groups(opts, ["WF_A", "WF_B"]) == {
        "before": ["WF_B"], "after": ["WF_A"]}
    # after 가 현재 source 에 없으면 판단 불가 → None
    assert webreport_compare_groups(opts, ["WF_B", "WF_C"]) is None
    assert webreport_compare_groups("", ["WF_A", "WF_B"]) is None
    assert webreport_compare_groups('{"colors": []}', ["WF_A", "WF_B"]) is None


# ── 산포 비교 (dist_shift) ───────────────────────────────────────────────────
# fixture 의 limit 은 HILIM=20 / LOLIM=0 고정 — 값 구성으로 cpk 를 원하는 구간에 넣는다.
_LOW = [6.0, 8.0, 10.0, 12.0, 14.0] * 3    # σ≈2.93 → cpk≈1.14 (<1.33)
_CONST = [5.0] * 15                        # σ=0 → cpk None (고정값)


def _spread(values, factor):
    """평균은 그대로 두고 산포만 factor 배."""
    m = sum(values) / len(values)
    return [round(m + (v - m) * factor, 6) for v in values]


def _dist_rows(payload):
    return {r["subject"]: r for r in payload["compare"]["dist_shift"]["rows"]}


def test_dist_shift_metrics_formulas():
    """Before 분모 지표 수식 — CPK 시트 값·직접 계산 기대값과 대조."""
    before_vals = list(_WIDE)
    after_vals = _shift(_WIDE, 1.10)         # 평균 +10%, σ 도 ×1.1 (선형 스케일)
    payload = _payload(
        [_make_table("WF_A", {"IT": after_vals}), _make_table("WF_B", {"IT": before_vals})],
        {"before": ["WF_B"], "after": ["WF_A"]})
    dist = payload["compare"]["dist_shift"]
    assert dist["after"] == "WF_A" and dist["before"] == "WF_B", (dist["after"], dist["before"])
    row = _dist_rows(payload)["IT"]

    cpk = {(r["subject"], r["source"]): r for r in payload["sheets"]["CPK"]}
    ra, rb = cpk[("IT", "WF_A")], cpk[("IT", "WF_B")]
    assert row["after"]["n"] == row["before"]["n"] == 15, row
    assert row["meanshift_sigma"] == round(abs(ra["average"] - rb["average"]) / rb["stdev"], 4)
    assert row["cpk_ratio_pct"] == round(ra["cpk"] / rb["cpk"] * 100.0, 2), row
    assert row["stdev_delta_pct"] == round(
        (ra["stdev"] - rb["stdev"]) / rb["stdev"] * 100.0, 6), row

    sa = pd.Series(after_vals, dtype="float64")
    sb = pd.Series(before_vals, dtype="float64")
    iqr_a = sa.quantile(0.75) - sa.quantile(0.25)
    iqr_b = sb.quantile(0.75) - sb.quantile(0.25)
    assert row["median_shift"] == round(abs(sa.median() - sb.median()) / iqr_b, 4), row
    assert row["iqr_delta_pct"] == round((iqr_a - iqr_b) / iqr_b * 100.0, 6), row
    # 손계산 KS: x=12 에서 F_before=1.0, F_after=0.6 → D=0.4 가 최대.
    assert row["ks_d"] == 0.4, row["ks_d"]


def test_dist_shift_ks_hand_cases():
    """KS D — 완전 분리 분포=1.0 / 동일 분포=0.0."""
    lo = [1.0, 2.0, 3.0] * 5
    hi = [10.0, 11.0, 12.0] * 5
    rows = _dist_rows(_payload(
        [_make_table("WF_A", {"SEP": hi, "SAME": list(_WIDE)}),
         _make_table("WF_B", {"SEP": lo, "SAME": list(_WIDE)})],
        {"before": ["WF_B"], "after": ["WF_A"]}))
    assert rows["SEP"]["ks_d"] == 1.0, rows["SEP"]
    assert rows["SAME"]["ks_d"] == 0.0, rows["SAME"]


def test_dist_shift_focus_rules():
    """focus — 양쪽 cpk>100 제외 / 한쪽 cpk<1.33 포함 / |Δσ%|≥15 포함 / 고정값 제외 / 평온 제외."""
    before = {"HIGHCPK": list(_TIGHT), "LOWCPK": list(_LOW), "SPREAD": list(_WIDE),
              "CONST": list(_CONST), "CALM": list(_WIDE)}
    after = {"HIGHCPK": list(_TIGHT), "LOWCPK": list(_LOW), "SPREAD": _spread(_WIDE, 1.2),
             "CONST": list(_CONST), "CALM": _spread(_WIDE, 1.05)}
    dist = _payload([_make_table("WF_A", after), _make_table("WF_B", before)],
                    {"before": ["WF_B"], "after": ["WF_A"]})["compare"]["dist_shift"]
    rows = {r["subject"]: r for r in dist["rows"]}

    # 전제: HIGHCPK 양쪽 >100 / LOWCPK <1.33 / SPREAD·CALM 은 1.33~100 / CONST σ=0.
    assert rows["HIGHCPK"]["after"]["cpk"] > 100 and rows["HIGHCPK"]["before"]["cpk"] > 100
    assert rows["LOWCPK"]["before"]["cpk"] < 1.33, rows["LOWCPK"]
    assert 1.33 <= rows["SPREAD"]["after"]["cpk"] <= 100, rows["SPREAD"]
    assert rows["CONST"]["after"]["cpk"] is None and rows["CONST"]["after"]["stdev"] == 0

    assert rows["HIGHCPK"]["focus"] is False, rows["HIGHCPK"]   # 여유 과대 — 무조건 제외
    assert rows["LOWCPK"]["focus"] is True, rows["LOWCPK"]      # 한쪽 cpk<1.33
    assert rows["SPREAD"]["focus"] is True, rows["SPREAD"]      # Δσ +20% ≥ 15
    assert rows["CONST"]["focus"] is False, rows["CONST"]       # 양쪽 고정값
    assert rows["CALM"]["focus"] is False, rows["CALM"]         # Δσ +5% < 15

    # 고정값 항목은 정규화 지표를 낼 수 없다.
    assert rows["CONST"]["meanshift_sigma"] is None and rows["CONST"]["cpk_ratio_pct"] is None

    assert dist["thresholds"] == {"cpk_high": 100.0, "cpk_low": 1.33,
                                  "stdev_delta_pct": 15.0}, dist["thresholds"]
    s = dist["summary"]
    assert s["total"] == len(dist["rows"]) == 5, s
    assert s["focus"] == sum(1 for r in dist["rows"] if r["focus"]) == 2, s


def test_dist_shift_sort_none_last():
    """정렬 — meanshift_sigma 내림차순, None(고정값) 최하단."""
    before = {"BIG": list(_WIDE), "SMALL": list(_WIDE), "CONST": list(_CONST)}
    after = {"BIG": _shift(_WIDE, 1.20), "SMALL": _shift(_WIDE, 1.02), "CONST": list(_CONST)}
    dist = _payload([_make_table("WF_A", after), _make_table("WF_B", before)],
                    {"before": ["WF_B"], "after": ["WF_A"]})["compare"]["dist_shift"]
    order = [r["subject"] for r in dist["rows"]]
    assert order == ["BIG", "SMALL", "CONST"], order


def test_dist_shift_bin1_population():
    """fail die(999.0) 주입 후에도 모든 지표 불변 — IQR/KS 모집단이 cpk 와 같은 Bin1 임을 고정
    (_bin1_frame 이 cpk.build_cpk_rows 마스크를 복제한 데 대한 드리프트 방어)."""
    groups = {"before": ["WF_B"], "after": ["WF_A"]}
    before = {"G1_UP": list(_TIGHT), "G3_ITEM": list(_WIDE)}
    after = {"G1_UP": _shift(_TIGHT, 1.04), "G3_ITEM": _shift(_WIDE, 1.10)}
    clean = _dist_rows(_payload(
        [_make_table("WF_A", after), _make_table("WF_B", before)], groups))
    dirty = _dist_rows(_payload(
        [_make_table("WF_A", after, n_extra_fail=5),
         _make_table("WF_B", before, n_extra_fail=7)], groups))
    assert clean == dirty, (clean, dirty)


def test_dist_shift_cpk_ratio_none_when_before_nonpositive():
    """Before Cpk ≤ 0 (평균이 limit 밖) → cpk_ratio_pct None (비율 무의미)."""
    bad_before = [20.5, 21.0, 21.5] * 5          # 평균 21 > HILIM 20 → cpk < 0
    row = _dist_rows(_payload(
        [_make_table("WF_A", {"IT": list(_WIDE)}), _make_table("WF_B", {"IT": bad_before})],
        {"before": ["WF_B"], "after": ["WF_A"]}))["IT"]
    assert row["before"]["cpk"] < 0, row["before"]
    assert row["cpk_ratio_pct"] is None, row
    assert row["focus"] is True, row             # cpk<1.33 조건으로 잡힌다


def test_dist_shift_pooled_group_robust():
    """After 2장 pool — median/IQR 지표가 concat 프레임 기대값과 일치."""
    vals_a1 = list(_WIDE)
    vals_a2 = _shift(_WIDE, 1.30)
    vals_b = list(_WIDE)
    tables = [_make_table("WF_A1", {"IT": vals_a1}), _make_table("WF_A2", {"IT": vals_a2}),
              _make_table("WF_B", {"IT": vals_b})]
    row = _dist_rows(_payload(tables, {"before": ["WF_B"], "after": ["WF_A1", "WF_A2"]}))["IT"]
    sa = pd.Series(vals_a1 + vals_a2, dtype="float64")
    sb = pd.Series(vals_b, dtype="float64")
    iqr_a = sa.quantile(0.75) - sa.quantile(0.25)
    iqr_b = sb.quantile(0.75) - sb.quantile(0.25)
    assert row["median_shift"] == round(abs(sa.median() - sb.median()) / iqr_b, 4), row
    assert row["iqr_delta_pct"] == round((iqr_a - iqr_b) / iqr_b * 100.0, 6), row


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_grade_rules, test_summary_total_is_sum_of_grades,
               test_undecidable_item_is_grade3, test_single_source_group_matches_cpk_sheet,
               test_pooled_group_uses_concat_frame, test_legacy_fallback_without_groups,
               test_bin_matrix_lists_only_mismatch_coords,
               test_common_map_carries_per_source_bins,
               test_compare_groups_option_parsing,
               test_dist_shift_metrics_formulas, test_dist_shift_ks_hand_cases,
               test_dist_shift_focus_rules, test_dist_shift_sort_none_last,
               test_dist_shift_bin1_population,
               test_dist_shift_cpk_ratio_none_when_before_nonpositive,
               test_dist_shift_pooled_group_robust):
        fn()
        checks += 1
    print(f"PASS: test_compare_equivalence ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

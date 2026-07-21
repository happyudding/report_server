"""Compare 모드 payload 빌더 — 같은 Wafer 에 대한 2~3 source 비교 분석.

metrics.build_report_payload 가 mode=="Compare" 이고 source 가 2개 이상일 때만 호출한다.
세 가지 비교 산출물을 제공한다:
  - stats     : 항목별 source 통계(n/average/median/cpk/stdev) + source 간 delta.
                이미 계산된 ``cpk_rows`` 를 pivot 해 재사용(재계산 없음).
  - bin_delta : bin 별 source yield% + delta/range.
  - common_map: 공통 좌표의 Bin 을 source 간 비교해 일치(match)/한쪽만 Fail/혼합(mixed)으로
                분류한 단일 Map. Bin 불일치가 강조 대상.
다운샘플 없음(규칙 #6) — 모든 die 를 분류에 반영한다.
"""
from __future__ import annotations

import difflib
from collections import Counter, defaultdict

from .common import PASS_BIN, bin_sort_key, bin_types, fmt_type, json_safe, num, round_num, to_coord


def build_compare_bin_delta(tables) -> list:
    """bin 별 source yield% + delta/range. Pass(BIN==1) 포함, worst-yield(비-Pass) 우선 정렬."""
    source_names = [t.source for t in tables]
    totals = {t.source: len(t.data) for t in tables}
    per: dict = {}
    all_bins: set = set()
    for t in tables:
        counts = Counter(bin_types(t))
        per[t.source] = counts
        all_bins.update(counts.keys())

    rows = []
    for b in sorted(all_bins, key=bin_sort_key):
        srcs = []
        pcts = []
        for s in source_names:
            cnt = int(per[s].get(b, 0))
            total = totals[s]
            pct = round(cnt / total * 100.0, 2) if total else 0.0
            srcs.append({"source": s, "count": cnt, "pct": pct})
            pcts.append(pct)
        rows.append({
            "bin": b,
            "is_pass": b == PASS_BIN,
            "sources": srcs,
            "delta_pct": round(pcts[-1] - pcts[0], 2) if len(pcts) >= 2 else 0.0,
            "range_pct": round(max(pcts) - min(pcts), 2) if pcts else 0.0,
        })
    # Pass 를 맨 위, 그 다음 source 간 편차(range_pct) 큰 순 = 비교상 눈에 띄는 bin 우선.
    rows.sort(key=lambda r: (0 if r["is_pass"] else 1, -r["range_pct"]))
    return rows


def build_common_map(tables) -> dict:
    """die 를 좌표별 Bin 일치/불일치로 분류해 단일 공통성 Map 을 만든다.

    모든 source 에 존재하는 공통 좌표만 대상 (before/after 2-source 가 기본):
      - 모든 source 에서 Bin 동일         → "match" (초록)
      - Bin 이 다르고 한 source 에서만 Fail → 그 source 이름 (빨강/파랑…)
      - Bin 이 다르고 2개 이상 source Fail  → "mixed" (보라)
    한쪽에만 존재하는 좌표는 비교 불가라 제외한다(빈칸). 다운샘플 없음(규칙 #6).
    맵 프레임(bound)은 웨이퍼 형태 유지를 위해 모든 present 좌표 기준으로 잡는다.
    """
    source_names = [t.source for t in tables]
    coord_bins = {t.source: _coord_bin_map(t) for t in tables}

    xs_all, ys_all = [], []
    for s in source_names:
        for (x, y) in coord_bins[s]:
            xs_all.append(x)
            ys_all.append(y)

    # 모든 source 에 존재하는 공통 좌표만 분류
    common = None
    for s in source_names:
        keys = set(coord_bins[s])
        common = keys if common is None else (common & keys)
    common = common or set()

    dies = []
    n_match = n_mixed = 0
    per_source = {s: 0 for s in source_names}
    for coord in common:
        bins = [coord_bins[s][coord] for s in source_names]
        if all(b == bins[0] for b in bins):
            cls = "match"
            n_match += 1
        else:
            fail_srcs = [s for s, b in zip(source_names, bins) if b != PASS_BIN]
            if len(fail_srcs) == 1:
                cls = fail_srcs[0]
                per_source[cls] += 1
            else:
                cls = "mixed"
                n_mixed += 1
        dies.append({"x": coord[0], "y": coord[1], "cls": cls})

    dies.sort(key=lambda d: (d["y"], d["x"]))
    return {
        "sources": source_names,
        "x_min": min(xs_all) if xs_all else None,
        "x_max": max(xs_all) if xs_all else None,
        "y_min": min(ys_all) if ys_all else None,
        "y_max": max(ys_all) if ys_all else None,
        "dies": dies,
        "counts": {
            "common_dies": len(common),
            "match": n_match,
            "mixed": n_mixed,
            "per_source": per_source,
        },
    }


# ── goodlog — Honey Compare Mode(테스트 프로그램 diff) 이식 ─────────────────────
# client/report_generator/compare_algorithm.py 의 로직을 HoneyformTable 인터페이스로
# 포팅. after = tables[0](첫째 업로드 파일), before = tables[1] — Honey 관례 유지.
# 항목명/lolimit/hilimit 일치 여부 + reference die 값 기준 gap% 를 15컬럼 표로 만들고,
# 이름 같고 limit 만 바뀐 항목은 limit_change_map(Distribution 회색 기준선용)에 담는다.

# goodlog 표 컬럼 순서 (Honey 요청서 그대로 — 프런트 헤더 라벨로도 사용).
GOODLOG_HEADER = [
    "after_item_name", "after_lolimit", "after_hilimit", "after_unit", "after_value",
    "compare_item_name", "compare_lolimit", "compare_hilimit", "comment", "gap",
    "Before_item_name", "Before_lolimit", "Before_hilimit", "Before_unit", "Before_value",
]


def _lim_equal(a, b) -> bool:
    """두 limit 동일 여부 (둘 다 결측이면 동일로 간주).

    부동소수 잔차(예: 1.2000000000000002 vs 1.2)로 표시상 같은 limit 이 False 로 뜨던 것을
    막기 위해 소수 4자리로 반올림해 비교한다. 저장·표시되는 limit 값 자체는 원본 그대로다.
    """
    na, nb = num(a), num(b)
    if na is None and nb is None:
        return True
    if na is None or nb is None:
        return False
    return round(na, 4) == round(nb, 4)


def _calc_gap(after_num, before_num):
    """(after-before)/before*100. before 결측/0 또는 after 결측이면 None."""
    a, b = num(after_num), num(before_num)
    if a is None or b is None or b == 0:
        return None
    return round_num((a - b) / b * 100.0, 6)


def _xy_list(table):
    """data 의 (XPOS,YPOS) 조합 리스트 (행 순서, 문자열 정규화)."""
    xs = [fmt_type(v) for v in table.data["XPOS"].tolist()]
    ys = [fmt_type(v) for v in table.data["YPOS"].tolist()]
    return list(zip(xs, ys))


def _common_xy(t_after, t_before):
    """두 source 공통 (X,Y) 중 after 행 순서상 가장 위 좌표. 없으면 None."""
    set_b = set(_xy_list(t_before))
    for xy in _xy_list(t_after):
        if xy in set_b:
            return xy
    return None


def _ref_row_index(table, target_xy=None):
    """compare_reference row 인덱스. target_xy 지정 시 그 좌표 행, 아니면 Bin1 최상단 행."""
    n = len(table.data)
    if n == 0:
        return None
    if target_xy is not None:
        for i, xy in enumerate(_xy_list(table)):
            if xy == target_xy:
                return i
        return None
    for i, b in enumerate(bin_types(table)):
        if b == PASS_BIN:
            return i
    return 0   # Bin1 없으면 최상단 행 fallback


def _cell_value(table, row_idx, col):
    """reference row 의 (표시값 문자열, 수치값) 반환. row/컬럼 없으면 ("", None)."""
    if row_idx is None or col not in table.data.columns:
        return "", None
    raw = table.data[col].iloc[row_idx]
    return fmt_type(raw), num(raw)


def build_goodlog(tables):
    """Honey Compare Mode 의 테스트 프로그램 diff. 정확히 2 source 일 때만 (아니면 None).

    프로그램(항목명/limit)이 완전히 같으면 {"identical": True} — 프런트가 '차이 없음' 표시.
    """
    if len(tables) != 2:
        return None
    t_after, t_before = tables[0], tables[1]
    a_names = list(t_after.item_columns)
    b_names = list(t_before.item_columns)

    def _identical() -> bool:
        if a_names != b_names:
            return False
        for c in a_names:
            if not _lim_equal(t_after.lolim.get(c), t_before.lolim.get(c)):
                return False
            if not _lim_equal(t_after.hilim.get(c), t_before.hilim.get(c)):
                return False
        return True

    base = {"after_source": t_after.source, "before_source": t_before.source}
    if _identical():
        return {**base, "identical": True, "rows": [], "limit_change_map": {},
                "header": GOODLOG_HEADER}

    # compare_reference row (source 당 1행): 공통 die 좌표 우선, 없으면 각자 Bin1 최상단.
    common_xy = _common_xy(t_after, t_before)
    if common_xy is not None:
        ra = _ref_row_index(t_after, common_xy)
        rb = _ref_row_index(t_before, common_xy)
    else:
        ra = _ref_row_index(t_after)
        rb = _ref_row_index(t_before)

    def _mk_row(ai, bi) -> dict:
        row = {
            "after_item_name": "", "after_lolimit": None, "after_hilimit": None,
            "after_unit": "", "after_value": "",
            "compare_item_name": None, "compare_lolimit": None, "compare_hilimit": None,
            "comment": "", "gap": None,
            "before_item_name": "", "before_lolimit": None, "before_hilimit": None,
            "before_unit": "", "before_value": "",
        }
        a_num = b_num = None
        if ai is not None:
            c = a_names[ai]
            row["after_item_name"] = c
            row["after_lolimit"] = num(t_after.lolim.get(c))
            row["after_hilimit"] = num(t_after.hilim.get(c))
            row["after_unit"] = fmt_type(t_after.units.get(c))
            row["after_value"], a_num = _cell_value(t_after, ra, c)
        if bi is not None:
            c = b_names[bi]
            row["before_item_name"] = c
            row["before_lolimit"] = num(t_before.lolim.get(c))
            row["before_hilimit"] = num(t_before.hilim.get(c))
            row["before_unit"] = fmt_type(t_before.units.get(c))
            row["before_value"], b_num = _cell_value(t_before, rb, c)
        if ai is not None and bi is not None:
            row["compare_item_name"] = row["after_item_name"] == row["before_item_name"]
            row["compare_lolimit"] = _lim_equal(row["after_lolimit"], row["before_lolimit"])
            row["compare_hilimit"] = _lim_equal(row["after_hilimit"], row["before_hilimit"])
            row["gap"] = _calc_gap(a_num, b_num)
        return row

    # before → after 정렬. before 순서 기준, 추가/삭제는 한쪽만 채움 (Honey 와 동일).
    sm = difflib.SequenceMatcher(a=b_names, b=a_names, autojunk=False)
    rows = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(_mk_row(j1 + k, i1 + k))
        elif tag == "delete":          # before 에만 (after 에서 삭제)
            for i in range(i1, i2):
                rows.append(_mk_row(None, i))
        elif tag == "insert":          # after 에만 (after 에서 추가)
            for j in range(j1, j2):
                rows.append(_mk_row(j, None))
        elif tag == "replace":         # 양쪽 다름 → before 나열 후 after 나열
            for i in range(i1, i2):
                rows.append(_mk_row(None, i))
            for j in range(j1, j2):
                rows.append(_mk_row(j, None))

    # 이름 같고 limit 변경된 항목의 before limit → Distribution 회색 기준선용.
    # 값이 None 인 쪽은 그 limit 은 동일(회색선 미표시)을 의미한다.
    a_set = set(a_names)
    limit_change_map = {}
    for c in b_names:
        if c not in a_set:
            continue
        lo_changed = not _lim_equal(t_after.lolim.get(c), t_before.lolim.get(c))
        hi_changed = not _lim_equal(t_after.hilim.get(c), t_before.hilim.get(c))
        if lo_changed or hi_changed:
            limit_change_map[c] = [
                num(t_before.lolim.get(c)) if lo_changed else None,
                num(t_before.hilim.get(c)) if hi_changed else None,
            ]

    return {**base, "identical": False, "header": GOODLOG_HEADER, "rows": rows,
            "limit_change_map": limit_change_map}


def _coord_bin_map(table):
    """table 의 (int(XPOS),int(YPOS)) → BIN(문자열) 맵. 좌표 중복(재검) 시 첫 행 우선."""
    xs = [fmt_type(v) for v in table.data["XPOS"].tolist()]
    ys = [fmt_type(v) for v in table.data["YPOS"].tolist()]
    bins = bin_types(table)
    out: dict = {}
    for x, y, b in zip(xs, ys, bins):
        coord = to_coord(x, y)
        if coord is None:
            continue
        if coord not in out:      # 첫 행 우선
            out[coord] = b
    return out


def build_bin_transition(tables) -> dict:
    """동일 좌표에서 Bin 이 before→after 로 어떻게 바뀌었는지 집계. 2 source 일 때만 (아니면 None).

    after=tables[0], before=tables[1] (goodlog 관례). 공통 좌표(둘 다 present)만 대상으로
    (before_bin, after_bin) 조합을 카운트한다. 다운샘플 없음(규칙 #6) — 전량 집계.
    """
    if len(tables) != 2:
        return None
    t_after, t_before = tables[0], tables[1]
    after_map = _coord_bin_map(t_after)
    before_map = _coord_bin_map(t_before)
    common = set(after_map) & set(before_map)

    pair_counts: Counter = Counter()
    pass_to_fail = fail_to_pass = 0
    for coord in common:
        a_bin = after_map[coord]
        b_bin = before_map[coord]
        pair_counts[(b_bin, a_bin)] += 1
        if b_bin == PASS_BIN and a_bin != PASS_BIN:
            pass_to_fail += 1
        elif b_bin != PASS_BIN and a_bin == PASS_BIN:
            fail_to_pass += 1

    rows = []
    for (b_bin, a_bin), cnt in pair_counts.items():
        rows.append({
            "before_bin": b_bin,
            "after_bin": a_bin,
            "count": cnt,
            "changed": b_bin != a_bin,
        })
    # 변경된 조합 먼저, 그 안에서 건수 많은 순.
    rows.sort(key=lambda r: (0 if r["changed"] else 1, -r["count"],
                             bin_sort_key(r["before_bin"]), bin_sort_key(r["after_bin"])))
    changed = sum(r["count"] for r in rows if r["changed"])
    return {
        "after_source": t_after.source,
        "before_source": t_before.source,
        "rows": rows,
        "counts": {
            "common_dies": len(common),
            "changed": changed,
            "pass_to_fail": pass_to_fail,
            "fail_to_pass": fail_to_pass,
        },
    }


def build_dist_shift(tables, cpk_rows) -> list:
    """양쪽 source 에 모두 있는 항목의 산포(average/stdev/cpk) before/after 병기 + delta.

    2 source 일 때만 (아니면 []). cpk_rows(이미 계산됨)를 subject×source 로 pivot 해 재사용.
    after=tables[0], before=tables[1]. 필터 없음 — 공통 항목 전부 나열하고 |Δcpk| 큰 순 정렬.
    """
    if len(tables) != 2:
        return []
    after_src = tables[0].source
    before_src = tables[1].source
    by_item: dict = defaultdict(dict)
    for r in cpk_rows or []:
        by_item[r.get("subject")][r.get("source")] = r

    def _pick(r):
        return {"average": r.get("average"), "stdev": r.get("stdev"), "cpk": r.get("cpk")}

    rows = []
    for subject, per_src in by_item.items():
        if after_src not in per_src or before_src not in per_src:
            continue   # 공통 항목만
        ra, rb = per_src[after_src], per_src[before_src]
        after = _pick(ra)
        before = _pick(rb)
        rows.append({
            "subject": subject,
            "units": json_safe(ra.get("units")) or json_safe(rb.get("units")) or "",
            "lower_limit": ra.get("lower_limit"),
            "upper_limit": ra.get("upper_limit"),
            "after": after,
            "before": before,
            "delta_average": _sub(after["average"], before["average"]),
            "delta_stdev": _sub(after["stdev"], before["stdev"]),
            "delta_cpk": _sub(after["cpk"], before["cpk"]),
            "mean_gap_pct": _calc_gap(after["average"], before["average"]),
        })

    def _sort_key(r):
        dc = num(r["delta_cpk"])
        gp = num(r["mean_gap_pct"])
        return (0 if dc is not None else 1, -abs(dc) if dc is not None else 0.0,
                -abs(gp) if gp is not None else 0.0)

    rows.sort(key=_sort_key)
    return rows


def _sub(after, before):
    """after - before (둘 다 수치일 때만). 하나라도 None 이면 None."""
    a, b = num(after), num(before)
    return None if a is None or b is None else round_num(a - b, 6)


def build_compare_payload(tables, all_items, cpk_rows) -> dict:
    """Compare 모드 통합 payload. metrics 가 report["compare"] 로 내려준다."""
    return {
        "sources": [t.source for t in tables],
        "bin_delta": build_compare_bin_delta(tables),
        "common_map": build_common_map(tables),
        # Honey Compare Mode 성격(테스트 프로그램 diff). 2 source 가 아니면 None.
        "goodlog": build_goodlog(tables),
        # 동일 좌표 Bin before→after 전이. 2 source 가 아니면 None.
        "bin_transition": build_bin_transition(tables),
        # 공통 항목 산포(avg/stdev/cpk) before/after 병기. 2 source 가 아니면 [].
        "dist_shift": build_dist_shift(tables, cpk_rows),
    }

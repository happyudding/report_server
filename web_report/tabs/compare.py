"""Compare 모드 payload 빌더 — Before / After 두 그룹으로 나뉜 N source 비교 분석.

metrics.build_report_payload 가 mode=="Compare" 이고 source 가 2개 이상일 때만 호출한다.
source 는 **몇 개든** 될 수 있고 업로드 시 Honey 배치 다이얼로그가 정한
``compare_groups = {"before": [source명…], "after": [source명…]}`` 로 두 그룹에 나뉜다
(옵션이 없는 legacy 세션은 ``after=[sources[0]], before=[sources[1]]`` 폴백 — 종전 관례와 동일).

산출물별 비교 대상이 다르다:
  - common_map : **전 source** (좌표별 Bin 일치/불일치 분류).
  - bin_delta  : **전 source** (bin 별 yield%).
  - bin_matrix : **전 source** — Bin 이 전부 같지는 않은 공통 좌표를 1행씩 나열.
  - goodlog    : **그룹 대표 2개** (After 최상단 vs Before 최상단) — 테스트 프로그램 diff.
  - dist_shift / equivalence : **그룹 pool 2개** (그룹 전체 die 를 합친 통계).
다운샘플 없음(규칙 #6) — 모든 die 를 분류에 반영한다.
"""
from __future__ import annotations

import difflib
from collections import Counter, defaultdict

import pandas as pd

from ..honeyform import HoneyformTable
from .common import (PASS_BIN, bin_sort_key, bin_types, fmt_type, item_meta, json_safe, num,
                     round_num, to_coord)
from .cpk import build_cpk_rows

# 동일성 검증 임계값 — Grade 판정과 셀 강조가 같은 값을 쓴다(프런트에도 thresholds 로 내려감).
EQUIV_AVG_PCT_LIMIT = 5.0    # Grade1 경계: AVG차(%) 5 이하
EQUIV_CPK_LIMIT = 5.0        # Grade2 조건: Before/After CPK 가 둘 다 5 이상


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
    die 마다 ``bins``(sources 순서의 source 별 BIN)를 함께 담아 마우스오버로 확인할 수 있게 한다.
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
        dies.append({"x": coord[0], "y": coord[1], "cls": cls, "bins": bins})

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


def build_goodlog(t_after, t_before):
    """Honey Compare Mode 의 테스트 프로그램 diff. **그룹 대표 2개**를 받는다.

    (구: tables 리스트를 받아 2 source 일 때만 동작. 지금은 After 최상단 / Before 최상단
    source 를 호출자가 골라 넘기므로 source 가 3개 이상이어도 항상 생성된다.)
    프로그램(항목명/limit)이 완전히 같으면 {"identical": True} — 프런트가 '차이 없음' 표시.
    """
    if t_after is None or t_before is None:
        return None
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


def build_bin_matrix(tables, before_names, after_names) -> dict:
    """공통 좌표 중 **Bin 이 전부 같지는 않은** die 를 좌표 1행씩 나열한다.

    (구 ``build_bin_transition`` 대체 — 2 source 의 (before_bin, after_bin) 조합 집계였다.
    source 가 몇 개든 "어느 die 가 어떻게 갈렸는지"를 그대로 보여 달라는 요구에 맞춰
    조합 집계가 아니라 좌표별 나열로 바꿨다.)

    - 대상: **모든 source 에 존재하는** 좌표 (한쪽에만 있는 좌표는 비교 불가라 제외).
    - 행: 그 좌표의 source 별 BIN 값 리스트(``sources`` 순서와 같은 순서).
    - 정렬: (y, x).
    ``counts.pass_to_fail`` / ``fail_to_pass`` 는 **그룹 대표**(Before 최상단 → After 최상단)
    기준이다 — 그룹이 여러 장이면 대표 1장씩의 전이만 센다.
    다운샘플 없음(규칙 #6) — 불일치 좌표 전량.
    """
    if len(tables) < 2:
        return None
    source_names = [t.source for t in tables]
    coord_bins = {t.source: _coord_bin_map(t) for t in tables}

    common = None
    for s in source_names:
        keys = set(coord_bins[s])
        common = keys if common is None else (common & keys)
    common = common or set()

    rows = []
    for coord in sorted(common, key=lambda c: (c[1], c[0])):
        bins = [coord_bins[s][coord] for s in source_names]
        if all(b == bins[0] for b in bins):
            continue                      # 전 source 동일 = 볼 것 없음
        rows.append({"x": coord[0], "y": coord[1], "bins": bins})

    # Pass→Fail / Fail→Pass 요약은 그룹 대표 1장씩으로만 센다(그룹이 여러 장일 때의 정의를
    # 억지로 만들지 않는다 — 화면에도 "대표 기준" 으로 표기).
    rep_before = before_names[0] if before_names else None
    rep_after = after_names[0] if after_names else None
    pass_to_fail = fail_to_pass = 0
    if rep_before and rep_after:
        b_map, a_map = coord_bins[rep_before], coord_bins[rep_after]
        for coord in common:
            b_bin, a_bin = b_map[coord], a_map[coord]
            if b_bin == PASS_BIN and a_bin != PASS_BIN:
                pass_to_fail += 1
            elif b_bin != PASS_BIN and a_bin == PASS_BIN:
                fail_to_pass += 1

    return {
        "sources": source_names,
        "before_sources": list(before_names),
        "after_sources": list(after_names),
        "rep_before": rep_before,
        "rep_after": rep_after,
        "rows": rows,
        "counts": {
            "common_dies": len(common),
            "mismatch": len(rows),
            "pass_to_fail": pass_to_fail,
            "fail_to_pass": fail_to_pass,
        },
    }


def build_dist_shift(tables, cpk_rows) -> list:
    """양쪽에 모두 있는 항목의 산포(average/stdev/cpk) before/after 병기 + delta.

    호출자가 넘기는 tables 는 **그룹 pool 2개**(``[pool_after, pool_before]``)이고 cpk_rows 도
    그 pool 로 계산한 것이다. 그룹이 1 source 씩이면 pool == 그 source 라 값이 CPK 탭과 같다.
    cpk_rows 를 subject×source 로 pivot 해 재사용한다(재계산 없음).
    필터 없음 — 공통 항목 전부 나열하고 |Δcpk| 큰 순 정렬.
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


# ── Before/After 그룹 ────────────────────────────────────────────────────────

def resolve_groups(tables, compare_groups):
    """세션 옵션의 그룹(source 이름) → (before_tables, after_tables).

    ``compare_groups`` 는 {"before": [이름…], "after": [이름…]} (Honey 배치 다이얼로그가
    업로드 시 넣는다). 이름 기준이라 Excel 왕복으로 source 가 제거돼 index 가 밀려도 안전하다.
    옵션이 없거나(=legacy 세션) 한쪽이 비면 **종전 관례로 폴백**한다
    (after=tables[0], before=tables[1]) — 기존 Compare 세션의 화면이 바뀌지 않는다.
    """
    by_name = {t.source: t for t in tables}
    before, after = [], []
    if isinstance(compare_groups, dict):
        before = [by_name[n] for n in (compare_groups.get("before") or []) if n in by_name]
        after = [by_name[n] for n in (compare_groups.get("after") or []) if n in by_name]
    if not before or not after:
        after, before = [tables[0]], [tables[1]]
    return before, after


def _pool_tables(group, label):
    """그룹의 die 를 하나로 합친 가상 테이블. 1개면 **그 테이블을 그대로** 반환(복사 없음).

    메타(tno/step/units/hilim/lolim)는 최상단 source 우선(setdefault) — 그룹 대표의 limit 이
    기준이다. ``df=None`` 이라 실수로 재인코딩 경로를 탈 수 없다(preprocess 와 같은 관례).
    """
    if len(group) == 1:
        return group[0]
    item_columns, seen = [], set()
    tseq, tno, step, units, hilim, lolim = {}, {}, {}, {}, {}, {}
    for t in group:
        for c in t.item_columns:
            if c not in seen:
                seen.add(c)
                item_columns.append(c)
        for dst, src in ((tseq, t.tseq), (tno, t.tno), (step, t.step),
                         (units, t.units), (hilim, t.hilim), (lolim, t.lolim)):
            for k, v in src.items():
                dst.setdefault(k, v)
    data = pd.concat([t.data for t in group], ignore_index=True)
    return HoneyformTable(source=label, file_name=label, df=None,
                          item_columns=item_columns, tseq=tseq, tno=tno, step=step,
                          units=units, hilim=hilim, lolim=lolim, data=data)


# ── 동일성 검증 ──────────────────────────────────────────────────────────────

def _equiv_delta(before_avg, after_avg):
    """|After − Before| — **절대값**. 한쪽이라도 결측이면 None."""
    b, a = num(before_avg), num(after_avg)
    return None if b is None or a is None else round_num(abs(a - b), 6)


def _equiv_pct(before_avg, after_avg):
    """|After − Before| / |Before| × 100 — **절대값**(Grade 경계가 '5% 이하'라 부호를 남기면
    판정이 어긋난다). Before 가 0 이거나 한쪽이 결측이면 판정 불가(None)."""
    b, a = num(before_avg), num(after_avg)
    if b is None or a is None or b == 0:
        return None
    return round_num(abs(a - b) / abs(b) * 100.0, 4)


def _equiv_grade(pct, cpk_before, cpk_after) -> int:
    """Grade1: AVG차(%) ≤ 5 / Grade2: 5 초과 & 양쪽 CPK ≥ 5 / Grade3: 그 외(판정 불가 포함)."""
    if pct is None:
        return 3
    if pct <= EQUIV_AVG_PCT_LIMIT:
        return 1
    cb, ca = num(cpk_before), num(cpk_after)
    if cb is not None and ca is not None and min(cb, ca) >= EQUIV_CPK_LIMIT:
        return 2
    return 3


def build_equivalence(pool_before, pool_after, pooled_cpk_rows, tables) -> dict:
    """Before/After pool 의 항목별 동일성 등급 판정 표 + 등급별 개수 요약.

    통계는 ``pooled_cpk_rows``(=Bin1 기준, build_cpk_rows 산출) 재사용 — 재계산 없음.
    대상은 **양쪽 pool 에 모두 있는 공통 항목 전부**이며, 통계가 없어 판정할 수 없는 항목도
    행을 남기고 Grade3 으로 집계한다(Total = G1+G2+G3 가 항상 성립).
    행 순서는 After pool 의 item 순서(=테스트 프로그램 순서) — STEP 이 첫 컬럼이라 자연히
    STEP 별로 뭉친다. Pass/Fail·데이터 전무 항목은 cpk 행이 없어 자동으로 빠진다(CPK 탭과 동일).
    """
    before_src, after_src = pool_before.source, pool_after.source
    by_item: dict = defaultdict(dict)
    for r in pooled_cpk_rows or []:
        by_item[r.get("subject")][r.get("source")] = r
    meta = item_meta(tables)

    def _pick(r):
        return {"average": r.get("average"), "stdev": r.get("stdev"), "cpk": r.get("cpk")}

    rows = []
    counts = {1: 0, 2: 0, 3: 0}
    for subject in pool_after.item_columns:
        per_src = by_item.get(subject) or {}
        if before_src not in per_src or after_src not in per_src:
            continue                     # 공통 항목만
        rb, ra = per_src[before_src], per_src[after_src]
        before, after = _pick(rb), _pick(ra)
        pct = _equiv_pct(before["average"], after["average"])
        grade = _equiv_grade(pct, before["cpk"], after["cpk"])
        counts[grade] += 1
        # limit/unit 은 After(=limit 기준 그룹) 우선, 없으면 Before.
        rows.append({
            "step": (meta.get(subject) or {}).get("step", ""),
            "subject": subject,
            "units": json_safe(ra.get("units")) or json_safe(rb.get("units")) or "",
            "hilim": ra.get("upper_limit") if ra.get("upper_limit") is not None
                     else rb.get("upper_limit"),
            "lolim": ra.get("lower_limit") if ra.get("lower_limit") is not None
                     else rb.get("lower_limit"),
            "before": before,
            "after": after,
            "delta_avg": _equiv_delta(before["average"], after["average"]),
            "delta_pct": pct,
            "grade": grade,
        })

    return {
        "before": before_src,
        "after": after_src,
        "thresholds": {"avg_pct": EQUIV_AVG_PCT_LIMIT, "cpk": EQUIV_CPK_LIMIT},
        "summary": {"total": len(rows), "grade1": counts[1],
                    "grade2": counts[2], "grade3": counts[3]},
        "rows": rows,
    }


def build_compare_payload(tables, all_items, cpk_rows, stat_items=None,
                          compare_groups=None) -> dict:
    """Compare 모드 통합 payload. metrics 가 report["compare"] 로 내려준다.

    stat_items: CPK 통계 대상 항목(Pass/Fail·데이터 전무 제외) — pool 통계를 CPK 탭과 같은
    기준으로 내기 위해 metrics 가 넘긴다. 미지정이면 all_items.
    """
    before_tables, after_tables = resolve_groups(tables, compare_groups)
    before_names = [t.source for t in before_tables]
    after_names = [t.source for t in after_tables]
    groups = {n: "before" for n in before_names}
    groups.update({n: "after" for n in after_names})

    # dist_shift/equivalence 는 그룹 pool 기준 — 그룹이 1 source 씩이면 pool 이 그 테이블
    # 자체라 CPK 탭 값과 완전히 동일하다.
    pool_before = _pool_tables(before_tables, "Before")
    pool_after = _pool_tables(after_tables, "After")
    items = list(stat_items if stat_items is not None else all_items)
    if pool_before is before_tables[0] and pool_after is after_tables[0]:
        pooled_cpk_rows = cpk_rows      # 이미 계산된 source 별 행 재사용(재계산 없음)
    else:
        pooled_cpk_rows = build_cpk_rows([pool_after, pool_before], items)

    common_map = build_common_map(tables)
    common_map["groups"] = groups
    return {
        "sources": [t.source for t in tables],
        "groups": groups,
        "before_sources": before_names,
        "after_sources": after_names,
        "bin_delta": build_compare_bin_delta(tables),
        "common_map": common_map,
        # Honey Compare Mode 성격(테스트 프로그램 diff) — 그룹 대표 2개.
        "goodlog": build_goodlog(after_tables[0], before_tables[0]),
        # Bin 이 전부 같지는 않은 공통 좌표 나열(전 source).
        "bin_matrix": build_bin_matrix(tables, before_names, after_names),
        # 공통 항목 산포(avg/stdev/cpk) before/after 병기 — 그룹 pool 기준.
        "dist_shift": build_dist_shift([pool_after, pool_before], pooled_cpk_rows),
        # 항목별 동일성 등급(Grade 1/2/3) 판정 — 그룹 pool 기준.
        "equivalence": build_equivalence(pool_before, pool_after, pooled_cpk_rows, tables),
    }

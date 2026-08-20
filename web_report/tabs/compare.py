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

import numpy as np
import pandas as pd

from ..honeyform import HoneyformTable
from .common import (PASS_BIN, bin_sort_key, bin_types, fmt_type, item_meta, json_safe, num,
                     round_num, to_coord)
from .cpk import CPK_THRESHOLD, build_cpk_rows
from .significance import brown_forsythe_p, welch_p

# 동일성 검증 임계값 — Grade 판정과 셀 강조가 같은 값을 쓴다(프런트에도 thresholds 로 내려감).
EQUIV_AVG_PCT_LIMIT = 5.0    # Grade1 경계: AVG차(%) 5 이하
EQUIV_CPK_LIMIT = 5.0        # Grade2 조건: Before/After CPK 가 둘 다 5 이상

# 산포 비교(dist_shift) focus 판정 임계값 — 프런트에도 thresholds 로 내려간다.
# cpk_low(관심 경계)는 별도 상수 없이 CPK_THRESHOLD(1.33) 를 그대로 쓴다(값 정본 1곳).
DIST_CPK_HIGH = 100.0            # 양쪽 Cpk 가 이 값 초과면 여유 과대 — 무조건 focus 제외
DIST_STDEV_DELTA_PCT = 15.0      # |stdev 증가율(%)| 이 이 값 이상이면 focus
DIST_ALPHA = 0.05                # 노이즈 게이트 유의수준 (_dist_focus 참조)


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
    프로그램(항목명/limit)이 완전히 같으면 ``identical: True`` 지만 **표(rows)는 그대로
    만든다** — limit 이 안 바뀌어도 항목별 값 gap% 를 봐야 한다는 요구 때문(2026-07-28).
    identical 은 프런트의 '차이 없음' 안내용 플래그일 뿐 표 생략 조건이 아니다.
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

    base = {"after_source": t_after.source, "before_source": t_before.source,
            "identical": _identical()}

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

    return {**base, "header": GOODLOG_HEADER, "rows": rows,
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


def _bin1_frame(table, items):
    """Bin1(양품) die 만의 item 프레임 — cpk.build_cpk_rows 와 동일 모집단(정본: cpk.py).

    마스크(`b == PASS_BIN`)·stale 컬럼 numeric 강제를 build_cpk_rows 와 문자 그대로 맞춘다 —
    어긋나면 IQR/KS 모집단이 avg/stdev/cpk(cpk_rows) 통계와 갈린다(회귀 테스트로 고정).
    """
    bin1_mask = [b == PASS_BIN for b in bin_types(table)]
    item_set = set(table.item_columns)
    present = [i for i in items if i in item_set]
    frame = table.data[present]
    stale = [c for c in present if frame[c].dtype.kind not in "if"]
    if stale:
        frame = frame.copy()
        for c in stale:
            frame[c] = pd.to_numeric(frame[c], errors="coerce")
    return frame[bin1_mask]


def _robust_stats(frame):
    """{item: {"median","iqr"}} — robust 지표용 quantile 배치 계산(프레임당 1회).

    pandas quantile(linear 보간)의 **무반올림** 값이다 — cpk_rows 의 median 은 round_num 되어
    있어 정규화 분자로 재사용하지 않는다.
    """
    if frame.shape[1] == 0:
        return {}
    q = frame.quantile([0.25, 0.5, 0.75])
    out = {}
    for item in frame.columns:
        p25, p50, p75 = (num(q.loc[p, item]) for p in (0.25, 0.5, 0.75))
        out[item] = {"median": p50,
                     "iqr": None if p25 is None or p75 is None else p75 - p25}
    return out


def _sorted_values(frame, item):
    """item 컬럼의 NaN 제외 오름차순 ndarray (KS 용).

    프레임 전체를 한 번에 정렬하지 않고 **컬럼 1개씩** 만든다 — 항목 수백×die 수만의 pool 에서
    정렬 결과를 전부 들고 있으면 피크 메모리가 프레임 2배가 된다.
    """
    if item not in frame.columns:
        return np.empty(0)
    col = frame[item].to_numpy(dtype=float)
    return np.sort(col[~np.isnan(col)])


def _ks_d(sa, sb):
    """KS D 통계량(0~1) — 두 정렬 배열의 ECDF 최대거리. 어느 쪽이든 값이 없으면 None."""
    na, nb = len(sa), len(sb)
    if na == 0 or nb == 0:
        return None
    grid = np.concatenate([sa, sb])
    cdf_a = np.searchsorted(sa, grid, side="right") / na
    cdf_b = np.searchsorted(sb, grid, side="right") / nb
    return float(np.abs(cdf_a - cdf_b).max())


def _norm_shift(a, b, denom):
    """|a−b|/denom — σ·IQR 단위 정규화 이동량. denom 결측·0 이면 None."""
    an, bn, dn = num(a), num(b), num(denom)
    if an is None or bn is None or dn is None or dn == 0:
        return None
    return round_num(abs(an - bn) / dn, 4)


def _ratio_pct(a, b):
    """a/b×100 — Cpk 비율(%). b 가 결측·0 이하면 None(0·음수 Cpk 대비 비율은 무의미 —
    그런 행은 cpk<1.33 조건으로 어차피 focus 에 잡힌다)."""
    an, bn = num(a), num(b)
    if an is None or bn is None or bn <= 0:
        return None
    return round_num(an / bn * 100.0, 2)


def _dist_focus(row) -> bool:
    """산포 비교 focus(관심 항목) 판정 — 서버가 정본, 프런트는 이 플래그로 필터만 한다.

    ① 무조건 제외: 양쪽 Cpk>DIST_CPK_HIGH(여유 과대) / 양쪽 σ=0·결측(고정값).
    ② 관심: 한쪽 Cpk<CPK_THRESHOLD — **절대 품질 조건이라 유의성 게이트를 걸지 않는다**
       (before/after 비교와 무관하게 낮은 Cpk 는 봐야 한다).
    ③ 관심: |stdev 증가율|≥DIST_STDEV_DELTA_PCT **이고** 그 산포 변화가 유의(p<DIST_ALPHA)할 때.
       게이트를 거는 이유: σ 추정치의 변동계수는 1/√(2(n−1)) 이라 n=15 면 ≈19% 다 —
       표본이 작으면 15% 변화가 추정 노이즈와 구분되지 않아 오경보가 된다. n 이 수천인
       보통의 pool 에서는 15% 변화가 언제나 유의해 이 게이트가 사실상 무동작이고,
       작은 n(수율 낮은 항목·outlier 마스킹 후)에서만 일한다.
       p 를 낼 수 없으면(n<3·양쪽 고정값) 종전대로 효과크기만 보고 판정한다.
    ④ 그 외 제외. Cpk None(limit 없음·n≤1 등)은 어느 조건도 발동하지 않는다.

    p 는 **억제에만** 쓰고 포함 근거로는 쓰지 않는다 — die 는 공간 상관이 있어 p 가 실제보다
    작게 나오므로 "유의하다→진짜"는 신뢰할 수 없다(significance.py 모듈 docstring).
    """
    ca, cb = num(row["after"]["cpk"]), num(row["before"]["cpk"])
    sa, sb = num(row["after"]["stdev"]), num(row["before"]["stdev"])
    if ca is not None and cb is not None and ca > DIST_CPK_HIGH and cb > DIST_CPK_HIGH:
        return False
    if (sa is None or sa == 0) and (sb is None or sb == 0):
        return False
    if (ca is not None and ca < CPK_THRESHOLD) or (cb is not None and cb < CPK_THRESHOLD):
        return True
    sd = num(row["stdev_delta_pct"])
    if sd is None or abs(sd) < DIST_STDEV_DELTA_PCT:
        return False
    pv = num(row["p_stdev"])
    return pv is None or pv < DIST_ALPHA


def _dist_thresholds() -> dict:
    return {"cpk_high": DIST_CPK_HIGH, "cpk_low": CPK_THRESHOLD,
            "stdev_delta_pct": DIST_STDEV_DELTA_PCT, "alpha": DIST_ALPHA}


def build_dist_shift(tables, cpk_rows) -> dict:
    """산포 비교 — 공통 항목의 Before/After pool 통계 병기 + 정규화 지표 + focus 판정.

    호출자가 넘기는 tables 는 **그룹 pool 2개**(``[pool_after, pool_before]``)이고 cpk_rows 도
    그 pool 로 계산한 것이다. 그룹이 1 source 씩이면 pool == 그 source 라 값이 CPK 탭과 같다.
    avg/stdev/cpk/n 은 cpk_rows 를 subject×source pivot 해 재사용(재계산 없음)하고,
    median/IQR/KS 만 같은 Bin1 pooled frame(_bin1_frame)으로 직접 계산한다.

    지표는 전부 **Before(b) 분모** (a=After):
      meanshift_sigma = |avg_a−avg_b| / σ_b        (평균 이동을 σ 단위로 정규화)
      cpk_ratio_pct   = cpk_a / cpk_b × 100        (>100% = 개선)
      stdev_delta_pct = (σ_a−σ_b) / σ_b × 100      (양수 = After 산포 증가)
      median_shift    = |med_a−med_b| / IQR_b      (robust 이동량)
      iqr_delta_pct   = (IQR_a−IQR_b) / IQR_b × 100
      ks_d            = 두 pool ECDF 최대거리(0~1, 분포 형태 차이)
    유의성 2종(`p_mean`=Welch t, `p_stdev`=Brown-Forsythe)은 표시(ns 마커)와 **노이즈 게이트**
    용이다 — 해석 한계와 게이트 규칙은 significance.py 모듈 docstring · _dist_focus 참조.
    정렬: meanshift_sigma 내림차순(None 최하단, tie |Δσ%|).
    """
    empty = {"after": "", "before": "", "thresholds": _dist_thresholds(),
             "summary": {"total": 0, "focus": 0}, "rows": []}
    if len(tables) != 2:
        return empty
    after_src = tables[0].source
    before_src = tables[1].source
    by_item: dict = defaultdict(dict)
    for r in cpk_rows or []:
        by_item[r.get("subject")][r.get("source")] = r

    def _pick(r):
        return {"average": r.get("average"), "stdev": r.get("stdev"),
                "cpk": r.get("cpk"), "n": r.get("n")}

    subjects = [s for s, per in by_item.items()
                if after_src in per and before_src in per]     # 공통 항목만
    frame_a = _bin1_frame(tables[0], subjects)
    frame_b = _bin1_frame(tables[1], subjects)
    robust_a = _robust_stats(frame_a)
    robust_b = _robust_stats(frame_b)

    rows = []
    focus_count = 0
    for subject in subjects:
        per_src = by_item[subject]
        ra, rb = per_src[after_src], per_src[before_src]
        after = _pick(ra)
        before = _pick(rb)
        rob_a = robust_a.get(subject) or {}
        rob_b = robust_b.get(subject) or {}
        iqr_b = rob_b.get("iqr")
        # 정렬 배열은 KS(_ks_d)와 Brown-Forsythe(p_stdev)가 함께 쓴다 — 항목당 1회만 만든다.
        sorted_a = _sorted_values(frame_a, subject)
        sorted_b = _sorted_values(frame_b, subject)
        row = {
            "subject": subject,
            "units": json_safe(ra.get("units")) or json_safe(rb.get("units")) or "",
            "lower_limit": ra.get("lower_limit"),
            "upper_limit": ra.get("upper_limit"),
            "after": after,
            "before": before,
            "meanshift_sigma": _norm_shift(after["average"], before["average"], before["stdev"]),
            "cpk_ratio_pct": _ratio_pct(after["cpk"], before["cpk"]),
            "stdev_delta_pct": _calc_gap(after["stdev"], before["stdev"]),
            "median_shift": _norm_shift(rob_a.get("median"), rob_b.get("median"), iqr_b),
            "iqr_delta_pct": _calc_gap(rob_a.get("iqr"), iqr_b),
            "ks_d": round_num(_ks_d(sorted_a, sorted_b), 4),
            # 유의성 — 표시(ns 마커)와 노이즈 게이트용. 평균은 Welch t(표시된 avg/stdev/n 을
            # 그대로 써 화면 값과 어긋나지 않게), 산포는 Brown-Forsythe(비정규에 강건).
            "p_mean": round_num(welch_p(num(after["average"]), num(after["stdev"]), after["n"],
                                        num(before["average"]), num(before["stdev"]),
                                        before["n"]), 6),
            "p_stdev": round_num(brown_forsythe_p(sorted_a, sorted_b), 6),
        }
        row["focus"] = _dist_focus(row)
        if row["focus"]:
            focus_count += 1
        rows.append(row)

    def _sort_key(r):
        ms = num(r["meanshift_sigma"])
        sd = num(r["stdev_delta_pct"])
        return (0 if ms is not None else 1, -(ms if ms is not None else 0.0),
                -abs(sd) if sd is not None else 0.0)

    rows.sort(key=_sort_key)
    return {"after": after_src, "before": before_src, "thresholds": _dist_thresholds(),
            "summary": {"total": len(rows), "focus": focus_count}, "rows": rows}


# ── Before/After 그룹 ────────────────────────────────────────────────────────

def resolve_group_names(source_names, compare_groups):
    """source 이름 목록 → (before 이름들, after 이름들). 폴백 포함 **배치 규칙의 정본**.

    ``resolve_groups``(tables 필요)와 Input File Information 모달(service.input_info —
    tables 를 디코드하지 않는다)이 같은 규칙을 써야 하므로 이름만 다루는 층을 분리했다.
    사본을 만들면 모달이 리포트와 다른 그룹을 보여준다.

    perf-guard: allow S01-report-schema — 순수 추출이다. `resolve_groups` 가 돌려주는
    (before, after) 는 이전과 같은 테이블·같은 순서라 compare payload 구조·값이 불변이다.
    """
    names = list(source_names)
    known = set(names)
    before, after = [], []
    if isinstance(compare_groups, dict):
        before = [n for n in (compare_groups.get("before") or []) if n in known]
        after = [n for n in (compare_groups.get("after") or []) if n in known]
    if not before or not after:
        # legacy(옵션 없음) 세션의 종전 관례 — 업로드 순서 [After, Before].
        after, before = names[:1], names[1:2]
    return before, after


def resolve_groups(tables, compare_groups):
    """세션 옵션의 그룹(source 이름) → (before_tables, after_tables).

    ``compare_groups`` 는 {"before": [이름…], "after": [이름…]} (Honey 배치 다이얼로그가
    업로드 시 넣는다). 이름 기준이라 Excel 왕복으로 source 가 제거돼 index 가 밀려도 안전하다.
    옵션이 없거나(=legacy 세션) 한쪽이 비면 **종전 관례로 폴백**한다
    (after=tables[0], before=tables[1]) — 기존 Compare 세션의 화면이 바뀌지 않는다.
    """
    by_name = {t.source: t for t in tables}
    before_names, after_names = resolve_group_names([t.source for t in tables], compare_groups)
    return [by_name[n] for n in before_names], [by_name[n] for n in after_names]


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


# perf-guard: allow S01-report-schema (compare 전용 payload — 전역
# REPORT_SCHEMA_VERSION 이 아니라 COMPARE_SCHEMA_VERSION 을 올린다. 전역 bump 는
# 전 세션 콜드 폭풍을 부른다 — cache_policy.py COMPARE_SCHEMA_VERSION 주석 참조)
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
        # 산포 비교 — 공통 항목 Before/After 통계 병기 + Before 분모 정규화 지표
        # (meanshift σ·Cpk%·stdev 증가율·median/IQR·KS D) + focus 판정. 그룹 pool 기준.
        "dist_shift": build_dist_shift([pool_after, pool_before], pooled_cpk_rows),
        # 항목별 동일성 등급(Grade 1/2/3) 판정 — 그룹 pool 기준.
        "equivalence": build_equivalence(pool_before, pool_after, pooled_cpk_rows, tables),
        # After 에만 있는 신규 test item — Distribution 탭 "신규항목보기" 필터가 쓴다.
        # goodlog(그룹 대표 2개)가 아니라 **그룹 전체 합집합** 기준이다: 그룹에 파일이
        # 여러 개면 대표에만 없는 항목이 신규로 잘못 잡힌다. pool 은 위에서 이미
        # 만들어 둔 것이라 추가 비용이 없다.
        "new_items": sorted(set(pool_after.item_columns) - set(pool_before.item_columns)),
    }

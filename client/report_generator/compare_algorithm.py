"""compare_algorithm — 두 input file 의 test program 차이 비교 (Compare Mode).

report_generator 의 기존 흐름과 분리된 **수동 Compare Mode** 전용 계산 모듈.
analyzer.run(compare_mode=True) 가 유일한 호출자(entrypoint)이며, 결과
CompareResult 를 돌려준다(endpoint). 기존 single/diff 분기는 건드리지 않는다.

after = 첫째 input file(group.names()[0]), before = 둘째 input file([1]).

순수 Python (xlwings·PyQt 비의존). df_honey 의 subjects/units/lower_limits/
upper_limits/scores/numeric_scores/meta property 만 사용한다.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import _builders as B
from .constants import PASS_BIN


# goodlog 표 컬럼 순서 (요청서 그대로). 시트 writer(_xlsx_goodlog)가 공유한다.
GOODLOG_HEADER = [
    "after_item_name", "after_lolimit", "after_hilimit", "after_unit", "after_value",
    "compare_item_name", "compare_lolimit", "compare_hilimit", "comment", "gap",
    "Before_item_name", "Before_lolimit", "Before_hilimit", "Before_unit", "Before_value",
]


@dataclass
class GoodlogRow:
    """goodlog 표의 한 행. 값은 표시용 원형(숫자/None)으로 보관, 서식은 writer 가 결정."""
    after_item_name: str = ""
    after_lolimit: Optional[float] = None
    after_hilimit: Optional[float] = None
    after_unit: str = ""
    after_value: object = None
    compare_item_name: Optional[bool] = None   # None = 공백(한쪽만 존재)
    compare_lolimit: Optional[bool] = None
    compare_hilimit: Optional[bool] = None
    comment: str = ""
    gap: Optional[float] = None                # before 대비 after 의 % 차이 (None = 공백)
    before_item_name: str = ""
    before_lolimit: Optional[float] = None
    before_hilimit: Optional[float] = None
    before_unit: str = ""
    before_value: object = None


@dataclass
class CompareResult:
    """build_compare 결과. goodlog 시트 + 공통 distribution 회색선용 limit 맵."""
    goodlog_rows: list = field(default_factory=list)       # list[GoodlogRow]
    # {subject_name: (before_lo_or_None, before_hi_or_None)} — name 동일·limit 변경 공통 subject.
    # 값 중 None 은 그쪽 limit 은 동일(회색선 미표시)을 의미한다.
    limit_change_map: dict = field(default_factory=dict)


# ── 숫자/limit 헬퍼 ────────────────────────────────────────────────────────────

def _num_or_none(v):
    """유한 숫자면 float, 아니면 None (NaN/None/비수치 → None)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def _lim_equal(a, b) -> bool:
    """두 limit 동일 여부 (둘 다 결측이면 동일로 간주)."""
    na, nb = _num_or_none(a), _num_or_none(b)
    if na is None and nb is None:
        return True
    if na is None or nb is None:
        return False
    return na == nb


def _calc_gap(after_num, before_num) -> Optional[float]:
    """(after-before)/before*100. before 결측/0 또는 after 결측이면 None."""
    a, b = _num_or_none(after_num), _num_or_none(before_num)
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b * 100.0


# ── reference row 선정 ─────────────────────────────────────────────────────────

def _xy_list(md):
    """meta 의 (XCoord,YCoord) 조합 리스트 (행 순서, 문자열 정규화)."""
    meta = md.meta.reset_index(drop=True)
    xs = meta["XCoord"].map(B._fmt_type)
    ys = meta["YCoord"].map(B._fmt_type)
    return list(zip(xs.tolist(), ys.tolist()))


def _common_xy(md_after, md_before):
    """두 파일 공통 (X,Y) 중 after 순서상 가장 위 좌표. 없으면 None."""
    set_b = set(_xy_list(md_before))
    for xy in _xy_list(md_after):
        if xy in set_b:
            return xy
    return None


def _ref_row_index(md, target_xy=None) -> Optional[int]:
    """compare_reference row 인덱스. target_xy 지정 시 그 좌표 행, 아니면 Bin1 최상단 행."""
    meta = md.meta.reset_index(drop=True)
    n = len(meta)
    if n == 0:
        return None
    if target_xy is not None:
        for i, xy in enumerate(_xy_list(md)):
            if xy == target_xy:
                return i
        return None
    bins = meta["Bin"].map(B._fmt_type)
    for i in range(n):
        if bins.iloc[i] == PASS_BIN:
            return i
    return 0   # Bin1 없으면 최상단 행 fallback


def _cell_value(md, row_idx, subj_idx):
    """reference row 의 (raw 표시값, 수치값) 반환. row 없으면 (None, None)."""
    if row_idx is None:
        return None, None
    raw = md.scores.iloc[row_idx, subj_idx]
    num = md.numeric_scores.iloc[row_idx, subj_idx]
    return raw, _num_or_none(num)


# ── 전체 동일성 판정 ───────────────────────────────────────────────────────────

def _all_identical(names_a, lo_a, hi_a, names_b, lo_b, hi_b) -> bool:
    """subject name / lolimit / hilimit 가 두 파일 전체에서 동일하면 True."""
    if names_a != names_b:
        return False
    n = len(names_a)
    for i in range(n):
        if not _lim_equal(lo_a[i], lo_b[i]) or not _lim_equal(hi_a[i], hi_b[i]):
            return False
    return True


# ── entrypoint ─────────────────────────────────────────────────────────────────

def build_compare(group) -> Optional[CompareResult]:
    """두 input file 을 비교. 전체 동일하면 None(기존 로직 유지), 차이가 있으면 CompareResult.

    after = group.names()[0], before = group.names()[1].
    """
    names = group.names()
    if len(names) != 2:
        return None
    md_after = group.mass_data_map[names[0]]
    md_before = group.mass_data_map[names[1]]

    a_names = [str(s) for s in md_after.subjects]
    b_names = [str(s) for s in md_before.subjects]
    a_lo, a_hi, a_unit = md_after.lower_limits, md_after.upper_limits, md_after.units
    b_lo, b_hi, b_unit = md_before.lower_limits, md_before.upper_limits, md_before.units

    if _all_identical(a_names, a_lo, a_hi, b_names, b_lo, b_hi):
        return None   # 차이 없음 → 기존 report_generator 로직 그대로

    # compare_reference row (각 파일 1행)
    common_xy = _common_xy(md_after, md_before)
    if common_xy is not None:
        ra = _ref_row_index(md_after, common_xy)
        rb = _ref_row_index(md_before, common_xy)
    else:
        ra = _ref_row_index(md_after)
        rb = _ref_row_index(md_before)

    def _mk_row(ai: Optional[int], bi: Optional[int]) -> GoodlogRow:
        row = GoodlogRow()
        if ai is not None:
            row.after_item_name = a_names[ai]
            row.after_lolimit = _num_or_none(a_lo[ai])
            row.after_hilimit = _num_or_none(a_hi[ai])
            row.after_unit = a_unit[ai] if ai < len(a_unit) else ""
            row.after_value, a_num = _cell_value(md_after, ra, ai)
        else:
            a_num = None
        if bi is not None:
            row.before_item_name = b_names[bi]
            row.before_lolimit = _num_or_none(b_lo[bi])
            row.before_hilimit = _num_or_none(b_hi[bi])
            row.before_unit = b_unit[bi] if bi < len(b_unit) else ""
            row.before_value, b_num = _cell_value(md_before, rb, bi)
        else:
            b_num = None
        if ai is not None and bi is not None:
            row.compare_item_name = (row.after_item_name == row.before_item_name)
            row.compare_lolimit = _lim_equal(row.after_lolimit, row.before_lolimit)
            row.compare_hilimit = _lim_equal(row.after_hilimit, row.before_hilimit)
            row.gap = _calc_gap(a_num, b_num)
        return row

    # before(=a) → after(=b) 정렬. before item 순서 기준, 추가/삭제는 한쪽만 채움.
    sm = difflib.SequenceMatcher(a=b_names, b=a_names, autojunk=False)
    rows: list = []
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

    # 공통 distribution 회색선용: name 동일·limit 변경 subject 의 before limit
    a_idx_by_name: dict = {}
    for i, nm in enumerate(a_names):
        a_idx_by_name.setdefault(nm, i)
    limit_change_map: dict = {}
    for j, nm in enumerate(b_names):
        ai = a_idx_by_name.get(nm)
        if ai is None:
            continue
        lo_changed = not _lim_equal(a_lo[ai], b_lo[j])
        hi_changed = not _lim_equal(a_hi[ai], b_hi[j])
        if lo_changed or hi_changed:
            limit_change_map[nm] = (
                _num_or_none(b_lo[j]) if lo_changed else None,
                _num_or_none(b_hi[j]) if hi_changed else None,
            )

    return CompareResult(goodlog_rows=rows, limit_change_map=limit_change_map)

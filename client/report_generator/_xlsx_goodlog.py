"""goodlog 시트 writer — Compare Mode 전용 (xlwings COM).

compare_algorithm 가 만든 GoodlogRow 목록을 받아 한 시트에 출력한다. compare 컬럼은
True=초록/False=빨강 배경, gap 은 |값|>=10 이면 빨강 글씨. 기존 표 헬퍼(_fill_table /
_apply_table_col_widths / _style_range)를 재사용해 기존 파일 수정을 피한다.

xlsx_writer 가 result.goodlog_rows 가 있을 때만(summary 와 yield 사이) 호출한다.
"""
from __future__ import annotations

from .compare_algorithm import GOODLOG_HEADER
from ._xlsx_style import _HEADER_ROW, _START_COL, _style_range
from ._xlsx_table_helpers import _apply_table_col_widths, _fill_table

# compare 컬럼 배경 / gap 빨강 글씨 색 (ARGB)
_BG_TRUE = "FFC6EFCE"    # 연초록
_BG_FALSE = "FFFFC7CE"   # 연빨강
_FONT_RED = "FFFF0000"
_GAP_RED_THRESHOLD = 10.0

# header 내 0-based 인덱스
_CMP_COLS = (5, 6, 7)    # compare_item_name / compare_lolimit / compare_hilimit
_GAP_COL = 9


def _disp(v):
    """표시값: None/빈값 → "" , bool 은 "True"/"False" , 그 외 원형."""
    if v is None or (isinstance(v, float) and v != v):  # None 또는 NaN
        return ""
    if isinstance(v, bool):
        return "True" if v else "False"
    return v


def _row_values(r) -> list:
    """GoodlogRow → 15열 표시값 리스트 (GOODLOG_HEADER 순서)."""
    gap = f"{r.gap:.1f}%" if r.gap is not None else ""
    return [
        _disp(r.after_item_name), _disp(r.after_lolimit), _disp(r.after_hilimit),
        _disp(r.after_unit), _disp(r.after_value),
        _disp(r.compare_item_name), _disp(r.compare_lolimit), _disp(r.compare_hilimit),
        _disp(r.comment), gap,
        _disp(r.before_item_name), _disp(r.before_lolimit), _disp(r.before_hilimit),
        _disp(r.before_unit), _disp(r.before_value),
    ]


def write_goodlog_sheet(ws, goodlog_rows) -> None:
    """goodlog 시트 채움 + compare 배경색 + gap 빨강 글씨."""
    rows = [_row_values(r) for r in goodlog_rows]
    _fill_table(ws, GOODLOG_HEADER, rows)
    _apply_table_col_widths(ws, GOODLOG_HEADER, custom_widths={
        "after_item_name": 22, "Before_item_name": 22, "comment": 18,
    })

    # compare 컬럼: True 초록 / False 빨강 배경 (행별 단일 셀 스타일)
    attr_by_col = {
        _CMP_COLS[0]: "compare_item_name",
        _CMP_COLS[1]: "compare_lolimit",
        _CMP_COLS[2]: "compare_hilimit",
    }
    for ri, r in enumerate(goodlog_rows):
        excel_row = _HEADER_ROW + 1 + ri
        for col0 in _CMP_COLS:
            val = getattr(r, attr_by_col[col0])
            if val is None:
                continue
            fill = _BG_TRUE if val else _BG_FALSE
            _style_range(ws.range((excel_row, _START_COL + col0)), fill=fill)
        if r.gap is not None and abs(r.gap) >= _GAP_RED_THRESHOLD:
            _style_range(ws.range((excel_row, _START_COL + _GAP_COL)),
                         font={"color": _FONT_RED})

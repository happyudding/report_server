"""표(table) 시트 채움 공통 헬퍼 — 범위 단위 일괄 기입/스타일.

summary 를 제외한 모든 table 시트 writer(_xlsx_sheets) 가 공통으로 사용한다.
헤더+데이터 bulk write, 열너비/폰트/행높이 일괄 적용, 시트명 정규화·제목 배너 등.
"""
from __future__ import annotations

import math

import numpy as np

from ._xlsx_profile import _flow_prof
from ._xlsx_style import (
    ALL_SHEETS,
    _DATA_FONT,
    _DIST_COL_WIDTH,
    _HDR_FONT,
    _HEADER_ROW,
    _ITEM_COL_WIDTH,
    _NARROW_COL_WIDTH,
    _START_COL,
    _TITLE_FILL_RGB,
    _TITLE_FONT,
    _TITLE_ROW_MAX_COL,
    _XL_CENTER,
    _XL_LEFT,
    _apply_font,
    _data_range,
    _hdr_range,
    _style_range,
)


def _last_row(ws):
    """used_range 의 마지막 행 (없으면 헤더행)."""
    try:
        return ws.used_range.last_cell.row
    except Exception:
        return _HEADER_ROW


def _hdr_cell(ws, r, c, value):
    """단일 셀에 값 기입 + 헤더 스타일 적용."""
    ws.range((r, c)).value = value
    _hdr_range(ws, r, c, c)


def _fill_table(ws, header, rows, header_row=_HEADER_ROW, start_col=_START_COL):
    """헤더+데이터를 범위 단위 일괄 기입 후 헤더행/데이터블록 스타일을 1회씩 적용."""
    ncol = len(header)
    c2 = start_col + ncol - 1
    ws.range((header_row, start_col), (header_row, c2)).value = list(header)
    _hdr_range(ws, header_row, start_col, c2)
    if not rows:
        return
    nrow = len(rows)
    data = [[_sanitize_cell(v) for v in row] for row in rows]
    ws.range((header_row + 1, start_col), (header_row + nrow, c2)).value = data
    _data_range(ws, header_row + 1, start_col, header_row + nrow, c2)


def _safe_set(ws, coord, value):
    """단일 셀 값 기입 (summary 고정 좌표용)."""
    ws.range(coord).value = value


def _is_cpk_header(header):
    return list(header[:4]) == ["TEST NAME", "LOW SPEC", "HIGH SPEC", "SCALE"] and "cpk" in header


def _apply_table_col_widths(ws, header, start_col=_START_COL, custom_widths=None, col_multiplier=1.0):
    if _is_cpk_header(header):
        with _flow_prof("fill_cpk.col_widths"):
            return _apply_table_col_widths_inner(ws, header, start_col, custom_widths, col_multiplier)
    return _apply_table_col_widths_inner(ws, header, start_col, custom_widths, col_multiplier)


def _apply_table_col_widths_inner(ws, header, start_col=_START_COL, custom_widths=None, col_multiplier=1.0):
    """헤더 이름 기반 열너비 일괄 설정.

    Distribution → _DIST_COL_WIDTH, Item/Category → _ITEM_COL_WIDTH, 나머지 → _NARROW_COL_WIDTH.
    custom_widths dict 에 있는 열은 해당 너비로 우선 적용. col_multiplier 로 전체 배율 조정.
    """
    _WIDE = {"Item", "Category"}
    for i, name in enumerate(header):
        col = start_col + i
        if custom_widths and name in custom_widths:
            w = custom_widths[name] * col_multiplier
        elif name == "Distribution":
            w = _DIST_COL_WIDTH * col_multiplier
        elif name in _WIDE:
            w = _ITEM_COL_WIDTH * col_multiplier
        else:
            w = _NARROW_COL_WIDTH * col_multiplier
        ws.range((1, col)).column_width = w


def _apply_table_font(ws, header, size=None, bold=None,
                      header_row=_HEADER_ROW, start_col=_START_COL):
    last = max(_last_row(ws), header_row)
    c2 = start_col + len(header) - 1
    _style_range(ws.range((header_row, start_col), (last, c2)),
                 font={"size": size, "bold": bold})


def _apply_used_cell_font(ws, size=None, bold=None):
    _style_range(ws.used_range, font={"size": size, "bold": bold})


def _set_table_row_heights(ws, n_rows, height, header_row=_HEADER_ROW):
    ws.range(f"{header_row}:{header_row + n_rows}").row_height = height


def _apply_named_columns_font(ws, header, names, size=None, bold=None,
                              header_row=_HEADER_ROW, start_col=_START_COL,
                              include_header=True, include_data=True,
                              last_row=None):
    name_set = set(names)
    first = header_row if include_header else header_row + 1
    last = last_row if last_row is not None else (_last_row(ws) if include_data else header_row)
    if last < first:
        last = first
    for i, name in enumerate(header):
        if name not in name_set:
            continue
        col = start_col + i
        _style_range(ws.range((first, col), (last, col)), font={"size": size, "bold": bold})


def _apply_font_delta_to_columns(ws, header, names, delta,
                                 header_row=_HEADER_ROW, start_col=_START_COL):
    if _is_cpk_header(header):
        with _flow_prof("fill_cpk.font_delta"):
            return _apply_font_delta_to_columns_inner(
                ws, header, names, delta, header_row, start_col)
    return _apply_font_delta_to_columns_inner(ws, header, names, delta, header_row, start_col)


def _apply_font_delta_to_columns_inner(ws, header, names, delta,
                                       header_row=_HEADER_ROW, start_col=_START_COL):
    """지정 열의 폰트 크기를 헤더(_HDR_FONT)·데이터(_DATA_FONT) 기준 + delta 로 설정."""
    name_set = set(names)
    last = _last_row(ws)
    for i, name in enumerate(header):
        if name not in name_set:
            continue
        col = start_col + i
        _style_range(ws.range((header_row, col), (header_row, col)),
                     font={"size": _HDR_FONT["size"] + delta})
        if last > header_row:
            _style_range(ws.range((header_row + 1, col), (last, col)),
                         font={"size": _DATA_FONT["size"] + delta})


def _normalize_report_sheet_names(wb):
    canonical = {name.lower(): _report_sheet_display_name(name) for name in ALL_SHEETS}
    existing = {s.name.lower() for s in wb.sheets}
    for ws in list(wb.sheets):
        lower = ws.name.lower()
        if not lower.endswith("1"):
            continue
        base = lower[:-1]
        target = canonical.get(base)
        if target and target.lower() not in existing:
            existing.discard(lower)
            ws.name = target
            existing.add(target.lower())


def _center_used_cells(ws):
    """used_range 전체 중앙정렬(+wrap) — 범위 1회 적용."""
    _style_range(ws.used_range, halign=_XL_CENTER, valign=_XL_CENTER, wrap=True)


def _apply_sheet_title(ws, title=None):
    """1행에 제목 배너(연파랑 fill, 좌측정렬) + A1 제목값/폰트 적용."""
    title = title or ws.name
    row1 = ws.range((1, 1), (1, _TITLE_ROW_MAX_COL))
    try:
        row1.api.UnMerge()
    except Exception:
        pass
    ws.range("1:1").row_height = 30
    _style_range(row1, fill=_TITLE_FILL_RGB, halign=_XL_LEFT, valign=_XL_CENTER)
    a1 = ws.range("A1")
    a1.value = title
    _apply_font(a1.api, _TITLE_FONT)


def _finalize_sheet_layouts(wb, skip_title_titles=()):
    """시트 레이아웃 마무리(중앙정렬 + 제목 배너). distribution 시트는 생성 전이라
    여기 대상이 아니며, skip_title_titles(Raw Data) 는 모든 서식을 건너뛴다."""
    skip = {str(t).lower() for t in skip_title_titles}
    for ws in wb.sheets:
        if ws.name.lower() == "summary":
            continue
        if ws.name.lower() in skip:
            continue
        _center_used_cells(ws)
        _apply_sheet_title(ws)


def _apply_small_font_headers(ws, header, suffixes_or_names,
                               header_row=_HEADER_ROW, start_col=_START_COL, size=9):
    """지정 조건에 해당하는 헤더 셀 폰트 크기를 size 로 변경.

    suffixes_or_names 항목이 '_' 로 시작하면 endswith 검사, 그 외엔 == 검사.
    """
    for i, name in enumerate(header):
        match = any(
            (name.endswith(pat) if pat.startswith("_") else name == pat)
            for pat in suffixes_or_names
        )
        if match:
            _style_range(ws.range((header_row, start_col + i), (header_row, start_col + i)),
                         font={"size": size})


def _bin_label(value):
    """bin 표시값: 정수 문자열이면 int, 아니면 원본."""
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    try:
        f = float(s)
        if f.is_integer():
            return int(f)
    except (TypeError, ValueError):
        pass
    return value


def _sanitize_cell(v):
    """Excel 기록용 셀 정제 — NaN/inf 는 빈칸, numpy 스칼라는 native 로."""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return ""
        return v
    if isinstance(v, np.generic):
        try:
            return v.item()
        except Exception:
            return str(v)
    return v


def _report_sheet_display_name(name):
    text = str(name or "")
    return text[:1].upper() + text[1:] if text else text

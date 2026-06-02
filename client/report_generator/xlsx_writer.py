"""순수 openpyxl xlsx 리포트 생성.

- table 시트(summary / yield / cpk / fail_item / issue_table)는 openpyxl 로
  워크북을 직접 생성하고 스타일 상수(_HDR_FONT 등)를 적용한다.
- distribution 차트만 **xlwings**(Excel COM) 로 생성한다(차트 옵션 정밀 제어 목적).

스타일 변경은 모듈 상단 상수(_HDR_FONT, _HDR_FILL 등)만 수정하면 된다.
계산은 analyzer/_builders 에서 끝났고, 이 모듈은 출력만 담당한다.
"""
from __future__ import annotations

import contextlib
import math
import os
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np
from copy import copy
from openpyxl.utils import get_column_letter

# ── 차트 생성 병목 측정 프로파일러 (HONEY_CHART_PROFILE set 시에만 동작) ───────
# unset 이면 _prof 는 즉시 통과 → 평상시 동작·출력 불변. 측정 결과는 stderr 로.
_PROF_ON = bool(os.environ.get("HONEY_CHART_PROFILE"))
_PROF = defaultdict(float)
_PROF_CNT = defaultdict(int)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_EXPORT_MOVE_RETRIES = 2
_EXPORT_RETRY_SLEEP = 0.08


@contextlib.contextmanager
def _prof(bucket):
    if not _PROF_ON:
        yield
        return
    t = time.perf_counter()
    try:
        yield
    finally:
        _PROF[bucket] += time.perf_counter() - t
        _PROF_CNT[bucket] += 1


def _prof_count(bucket, n=1):
    """시간 측정 없이 카운터만 증가 (차트/시리즈/PNG 개수 등)."""
    if _PROF_ON:
        _PROF_CNT[bucket] += n


def _prof_report():
    if not _PROF_ON or not _PROF:
        return
    total = sum(_PROF.values())
    print("\n[chart-profile] phase breakdown (s):", file=sys.stderr)
    for k, v in sorted(_PROF.items(), key=lambda kv: -kv[1]):
        pct = (100 * v / total) if total else 0.0
        print(f"  {k:18s} {v:8.3f}  ({pct:5.1f}%)  x{_PROF_CNT[k]}", file=sys.stderr)
    print(f"  {'TOTAL':18s} {total:8.3f}", file=sys.stderr)
    extra = {k: _PROF_CNT[k] for k in ("charts", "series", "pngs") if k in _PROF_CNT}
    if extra:
        print(f"  counts: {extra}", file=sys.stderr)
    _PROF.clear()
    _PROF_CNT.clear()

_MAX_CDF_POINTS = 150
_CHARTS_PER_ROW = 5
# 차트 크기 — gap 없이 밀착 배치 (사용자 사양 324x198)
_CHART_W, _CHART_H = 324, 198
_PLOT_W, _PLOT_TOP, _PLOT_H = 280, 30, 167
# distribution 찾기(Ctrl+F)용 item 인덱스: 차트 그리드 오른쪽 열, 차트 한 행당 행 수
_INDEX_COL = 33  # AG열
_ROWS_PER_CHART = 12
# distribution 차트 그리드를 제목 배너 아래로 내리는 픽셀 오프셋
_DIST_TITLE_PX = 30

# Excel COM 상수 (distribution 차트 서식)
_XL_VALUE, _XL_CATEGORY, _XL_PRIMARY = 2, 1, 1
_XL_LOW = -4134               # xlLow (y축 TickLabelPosition)
_XL_MARKER_NONE = -4142       # xlMarkerStyleNone
_XL_MARKER_CIRCLE = 8         # xlMarkerStyleCircle (data 점)
_MARKER_SIZE = 6              # data 점 크기(pt) — plotly 기준(5) + 1
_MSO_FALSE = 0                # msoFalse (LineFormat.Visible — 점 사이 선 제거)
_MSO_LINE_SYSDASH = 10        # msoLineSysDash (limit line)
_RGB_RED = 255               # RGB(255,0,0)
_RGB_FAIL_BG = 255 + 255 * 256 + 204 * 65536  # RGB(255,255,204) 연노랑 (fail 차트 배경)
_XL_COLORINDEX_NONE = -4142   # xlColorIndexNone (ChartArea 채움 제거 — 템플릿 중립화)

ALL_SHEETS = ["summary", "yield", "cpk", "fail_item", "issue_table", "distribution"]

# ── openpyxl 셀 스타일 상수 (xlsx 파일 런타임 의존 없이 코드 직접 정의) ─────────
from openpyxl.styles import (  # noqa: E402  (module-level import after constants)
    Alignment as _Alignment, Border as _Border, Font as _Font,
    PatternFill as _PatternFill, Side as _SideStyle,
)
# 색상은 ARGB 8자리 (openpyxl 호환성). 스타일 변경 시 이 상수만 수정.
_HDR_FILL_RGB   = "FFD9E1F2"   # 헤더 연청색
_DATA_FILL_RGB  = "FFFFFFFF"   # 데이터 흰색
_TITLE_FILL_RGB = "FFBDD7EE"   # 제목 연파랑
_HDR_FONT    = _Font(name="Calibri", bold=True, size=11)
_HDR_ALIGN   = _Alignment(horizontal="center", vertical="center", wrap_text=True)
_DATA_FONT   = _Font(name="Calibri", size=10)
_DATA_ALIGN  = _Alignment(horizontal="center", vertical="center", wrap_text=True)
_TITLE_FONT  = _Font(name="Calibri", bold=True, size=20)
_TITLE_ALIGN = _Alignment(horizontal="center", vertical="center")
_TITLE_ROW_MAX_COL = 26
_SUMMARY_TITLE_FILL_RGB = "FFBFE3FF"
_SUMMARY_HDR_FILL_RGB = "FFE2E8F0"
_SUMMARY_TITLE_FONT = _Font(name="Tahoma", bold=True, size=22)
_SUMMARY_SECTION_FONT = _Font(name="Tahoma", bold=True, size=20)
_SUMMARY_HDR_FONT = _Font(name="Tahoma", bold=True, size=10)
_SUMMARY_DATA_FONT = _Font(name="Tahoma", size=10)
_SUMMARY_LEFT_ALIGN = _Alignment(horizontal="left", vertical="center")
_SUMMARY_CENTER_ALIGN = _Alignment(horizontal="center", vertical="center", wrap_text=True)


def _new_border():
    """매 호출마다 새 Border/Side 인스턴스 반환 (공유 객체 문제 방지)."""
    t = _SideStyle(style="thin")
    return _Border(left=t, right=t, top=t, bottom=t)


def _apply_hdr_style(cell):
    cell.font      = copy(_HDR_FONT)
    cell.fill      = _PatternFill(fill_type="solid", fgColor=_HDR_FILL_RGB)
    cell.alignment = copy(_HDR_ALIGN)
    cell.border    = _new_border()


def _apply_data_style(cell):
    cell.font      = copy(_DATA_FONT)
    cell.fill      = _PatternFill(fill_type="solid", fgColor=_DATA_FILL_RGB)
    cell.alignment = copy(_DATA_ALIGN)
    cell.border    = _new_border()

# ── table 시트의 표 시작 위치 (A열 비움, 제목 A1, 헤더 3행, 데이터 4행~)
_HEADER_ROW = 3
_START_COL = 2  # B열
_FAIL_ITEM_ROW_HEIGHT = 78  # fail_item / issue_table 데이터 행 높이(pt) — Distribution 차트 셀 맞춤
_YIELD_TABLE_ROW_HEIGHT = 22
_YIELD_HEADER_ROW_HEIGHT = 40
_NARROW_COL_WIDTH = 6.5    # bin / count / yield / avg / comment 등 짧은 데이터
_DIST_COL_WIDTH   = 27.1   # Distribution 열 (썸네일 이미지 크기 기준)
_ITEM_COL_WIDTH   = 20.0   # Item / Category 열 (긴 텍스트)
_CPK_TEST_NAME_COL_WIDTH = 30
_CPK_SERIES_COL_WIDTH = 15
_CPK_N_COL_WIDTH = _NARROW_COL_WIDTH * 0.7
_FAIL_VALUES_COLS  = ["DUT", "XCoord", "YCoord", "Bin", "Item", "Value"]
_FAIL_VALUES_NCOLS = 6     # source 블록당 열 수
_FAIL_VALUES_GAP   = 1     # source 블록 간 빈 열 수


# ── write ────────────────────────────────────────────────────────────────────

def write(result, out_path, sheets=None, colors=None, progress_cb=None,
          raw_sheets=None, dist_progress_cb=None, attach_progress_cb=None) -> str:
    """AnalysisResult 를 xlsx 로 저장. 반환: 저장 경로(str).

    sheets: 출력할 시트명 리스트/집합 (None 이면 전체). 알 수 없는 이름은 무시.
    colors: distribution Legend(소스)별 '#RRGGBB' 색 리스트 (None 이면 Excel 기본색).
    progress_cb: 시트 1개 생성 후 progress_cb(done, total, name) 호출 (선택).
    raw_sheets: [(sheet명, df_honey 포맷 DataFrame), ...]. 주어지면 source(input
        file)별로 df_honey 적재 포맷 그대로의 시트를 맨 앞에 추가한다.
    """
    import openpyxl

    out_path = str(Path(out_path).resolve())
    sel = [s for s in ALL_SHEETS if (sheets is None or s in set(sheets))]
    if not sel:
        sel = ["summary"]

    table_writers = {
        "summary": _fill_summary,
        "yield": _fill_yield,
        "cpk": _fill_cpk,
        "fail_item": _fill_fail_item,
        "issue_table": _fill_issue_table,
    }
    want_dist = "distribution" in sel and bool(result.distributions)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # 기본 Sheet 제거 후 필요한 시트만 생성
    for nm in ALL_SHEETS:
        if nm in table_writers and nm in sel:
            wb.create_sheet(_report_sheet_display_name(nm))
    # distribution 만 선택돼 table 시트가 없는 경우 빈 시트 1개 확보
    if want_dist and not wb.sheetnames:
        wb.create_sheet(_report_sheet_display_name("distribution"))

    total = len([s for s in sel if s in table_writers]) + (len(raw_sheets) if raw_sheets else 0) \
        + (1 if want_dist else 0)
    done = 0

    # table 시트 채움 (템플릿 순서 유지)
    for nm in ALL_SHEETS:
        sheet_name = _report_sheet_display_name(nm)
        if nm in table_writers and nm in sel and sheet_name in wb.sheetnames:
            table_writers[nm](wb[sheet_name], result)
            done += 1
            _progress(progress_cb, done, total, nm)

    # Raw Data — source(input file)별 df_honey 포맷 시트를 맨 앞에 순서대로 추가
    if raw_sheets:
        reserved_sheet_names = [_report_sheet_display_name("distribution")] if want_dist else []
        for idx, (name, df) in enumerate(raw_sheets):
            ws = wb.create_sheet(_unique_sheet_name(wb, name, reserved_sheet_names), idx)
            _fill_raw_data(ws, df)
            done += 1
            _progress(progress_cb, done, total, ws.title)

    # 모든 시트 눈금선 제거 (openpyxl 단계)
    for nm in wb.sheetnames:
        wb[nm].sheet_view.showGridLines = False

    _normalize_report_sheet_names(wb)
    _finalize_openpyxl_sheet_layouts(wb)

    wb.save(out_path)

    # Phase 2: distribution 차트 (xlwings / Excel COM) + fail_item PNG 썸네일
    if want_dist:
        try:
            _write_distribution_xlwings(out_path, result, colors,
                                        attach_fail_item=("fail_item" in sel),
                                        dist_progress_cb=dist_progress_cb,
                                        attach_progress_cb=attach_progress_cb)
            done += 1
            _progress(progress_cb, done, total, "distribution")
        except Exception as exc:
            # Excel/xlwings 미설치·실패 → distribution 만 생략(table 시트는 이미 저장됨)
            if _is_package_integrity_error(exc):
                raise
            print(f"[xlsx_writer] distribution 차트 생략: {exc}")

    return out_path


def _progress(cb, done, total, name):
    if cb is None:
        return
    try:
        cb(done, total, name)
    except Exception:
        pass


# ── openpyxl 채움 (table 시트) ───────────────────────────────────────────────

def _fill_summary_legacy_unused(ws, result):
    """Legacy summary layout kept unused."""
    meta = result.meta
    title = " ".join(x for x in [meta.product_type, meta.product, meta.lot_id] if x).strip()

    # 제목 A1 병합 + 스타일
    ws.merge_cells("A1:H1")
    cell = ws["A1"]
    cell.value     = title or "REPORT TITLE"
    cell.font      = copy(_TITLE_FONT)
    cell.fill      = _PatternFill(fill_type="solid", fgColor=_TITLE_FILL_RGB)
    cell.alignment = copy(_TITLE_ALIGN)

    # 1. Device Feature 라벨행(4행) 업데이트 + 값행(5행)
    _safe_set(ws, "B4", "DEVICE")
    _safe_set(ws, "D4", "customer")
    _safe_set(ws, "E4", "PKG Type")
    _safe_set(ws, "F4", "GrossDie")
    _safe_set(ws, "G4", "Process Line")
    _safe_set(ws, "H4", "EVT version")
    _safe_set(ws, "B5", "")                        # DEVICE — 미입력
    _safe_set(ws, "D5", "")                        # customer — 미입력
    _safe_set(ws, "E5", "")                        # PKG Type — 미입력
    _safe_set(ws, "F5", "")                        # GrossDie — 미입력
    _safe_set(ws, "G5", "")                        # Process Line — 미입력
    _safe_set(ws, "H5", meta.revision or "-")      # EVT version

    # 2. Yield — Lot NO / 전체 평균 yield (pass bin avg)
    _safe_set(ws, "B9", meta.lot_id or "-")
    pass_row = next((r for r in result.yield_rows if str(r.get("bin")) == "1"), None)
    pass_avg = pass_row.get("avg") if pass_row else result.pass_yield
    _safe_set(ws, "D9", pass_avg if pass_avg is not None else "-")

    # Major Fail Bins: avg 내림차순 상위 5개 fail bin (F=Main Fail subject, G=avg)
    majors = result.major_fail_bins(5)
    for i in range(5):
        r = 9 + i
        _safe_set(ws, f"F{r}", majors[i].get("Main Fail subject") if i < len(majors) else None)
        _safe_set(ws, f"G{r}", majors[i].get("avg") if i < len(majors) else None)
    # 3. Evaluation Summary 는 템플릿 플레이스홀더("-") 그대로 둔다.


def _fill_summary(ws, result):
    """Summary sheet layout implemented directly with openpyxl."""
    meta = result.meta
    title = " ".join(x for x in [meta.product_type, meta.product, meta.lot_id] if x).strip()

    _reset_summary_sheet(ws)
    _apply_summary_dimensions(ws)
    _apply_summary_layout_styles(ws)

    _safe_set(ws, "A1", f"{chr(0x25A0)} {title or 'REPORT TITLE'}")
    _safe_set(ws, "B3", "1. Device Feature")
    _safe_set(ws, "B7", "2. Yield")
    _safe_set(ws, "B15", "3. Evaluation Summary")

    _safe_set(ws, "B4", "DEVICE")
    _safe_set(ws, "C4", "Customer")
    _safe_set(ws, "D4", "PKG_Type")
    _safe_set(ws, "E4", "GrossDie")
    _safe_set(ws, "F4", "Process Line")
    _safe_set(ws, "G4", "EVT_Version")
    _safe_set(ws, "B5", meta.product or meta.product_type or "")
    _safe_set(ws, "C5", "")
    _safe_set(ws, "D5", meta.product_type or "")
    _safe_set(ws, "E5", "")
    _safe_set(ws, "F5", meta.process or "")
    _safe_set(ws, "G5", meta.revision or "")

    _safe_set(ws, "B8", "Lot NO")
    _safe_set(ws, "D8", "Yield")
    _safe_set(ws, "E8", "Major Fail Bins")
    _safe_set(ws, "H8", "Comment")
    _safe_set(ws, "B9", meta.lot_id or "-")
    pass_row = next((r for r in result.yield_rows if str(r.get("bin")) == "1"), None)
    pass_avg = pass_row.get("avg") if pass_row else result.pass_yield
    _safe_set(ws, "D9", pass_avg if pass_avg is not None else "-")

    majors = result.major_fail_subjects(5) or result.major_fail_bins(5)
    for i in range(5):
        r = 9 + i
        _safe_set(ws, f"E{r}", _ordinal_fail_label(i + 1))
        if i < len(majors):
            _safe_set(ws, f"F{r}", majors[i].get("subject") or majors[i].get("Main Fail subject"))
            _safe_set(ws, f"G{r}", _summary_fail_percent(majors[i]))

    _safe_set(ws, "B16", "Category")
    _safe_set(ws, "C16", "Condition & Judge Limit")
    _safe_set(ws, "D16", "Result")
    for r, category in enumerate(["Yield", "CPK", "Temp", "ETC"], start=17):
        _safe_set(ws, f"B{r}", category)
    _safe_set(ws, "C17", "-")
    _safe_set(ws, "D17", "-")


def _reset_summary_sheet(ws):
    for rng in list(ws.merged_cells.ranges):
        ws.unmerge_cells(str(rng))
    for row in ws.iter_rows(min_row=1, max_row=20, min_col=1, max_col=8):
        for cell in row:
            cell.value = None
            cell.fill = _PatternFill(fill_type="solid", fgColor=_DATA_FILL_RGB)
            cell.font = copy(_SUMMARY_DATA_FONT)
            cell.alignment = copy(_SUMMARY_CENTER_ALIGN)
            cell.border = _Border()


def _apply_summary_dimensions(ws):
    widths = {
        "A": 2.625, "B": 16, "C": 26.125, "D": 10.375,
        "E": 10.5, "F": 12.625, "G": 9, "H": 44.75,
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    row_heights = {
        1: 30, 3: 25.5, 4: 16.5, 5: 16.5, 7: 21.75, 8: 16.5,
        15: 27, 17: 48.75, 18: 48.75, 19: 48.75, 20: 48.75,
    }
    for row, height in row_heights.items():
        ws.row_dimensions[row].height = height


def _summary_style_range(ws, cell_range, font=None, fill_rgb=None, align=None, border=False):
    fill = _PatternFill(fill_type="solid", fgColor=fill_rgb or _DATA_FILL_RGB)
    for row in ws[cell_range]:
        for cell in row:
            cell.font = copy(font or _SUMMARY_DATA_FONT)
            cell.fill = copy(fill)
            cell.alignment = copy(align or _SUMMARY_CENTER_ALIGN)
            cell.border = _new_border() if border else _Border()


def _apply_summary_layout_styles(ws):
    _summary_style_range(
        ws, "A1:H1", _SUMMARY_TITLE_FONT, _SUMMARY_TITLE_FILL_RGB,
        _SUMMARY_LEFT_ALIGN, border=False
    )
    ws["A1"].border = _Border(bottom=_SideStyle(style="thin"))

    for cell_range in ("B3:C3", "B7:C7", "B15:C15"):
        _summary_style_range(
            ws, cell_range, _SUMMARY_SECTION_FONT, _DATA_FILL_RGB,
            _SUMMARY_LEFT_ALIGN, border=False
        )

    _summary_style_range(ws, "B4:H4", _SUMMARY_HDR_FONT, _SUMMARY_HDR_FILL_RGB, border=True)
    _summary_style_range(ws, "B5:H5", _SUMMARY_DATA_FONT, _DATA_FILL_RGB, border=True)
    _summary_style_range(ws, "B8:H13", _SUMMARY_DATA_FONT, _DATA_FILL_RGB, border=True)
    _summary_style_range(ws, "E8:H8", _SUMMARY_HDR_FONT, _SUMMARY_HDR_FILL_RGB, border=True)
    _summary_style_range(ws, "B16:H16", _SUMMARY_HDR_FONT, _SUMMARY_HDR_FILL_RGB, border=True)
    _summary_style_range(ws, "B17:H20", _SUMMARY_DATA_FONT, _DATA_FILL_RGB, border=True)

    ws["D9"].number_format = "0.00"
    for row in range(9, 14):
        ws[f"G{row}"].number_format = "0.00"

    for cell_range in (
        "A1:H1", "B3:C3", "B7:C7", "B8:C8", "B9:C13", "D9:D13",
        "E8:G8", "B15:C15", "D16:H16", "D17:H17", "D18:H18",
        "D19:H19", "D20:H20",
    ):
        ws.merge_cells(cell_range)


def _ordinal_fail_label(index):
    suffix = "th"
    if index == 1:
        suffix = "st"
    elif index == 2:
        suffix = "nd"
    elif index == 3:
        suffix = "rd"
    return f"{index}{suffix} Fail"


def _summary_fail_percent(row):
    if "ratio" in row and row.get("ratio") is not None:
        return row.get("ratio") * 100
    return row.get("avg")


def _yield_table(result):
    """yield / fail_item 공용 표 (bin | Item | {src}_count | {src}_yield | avg | comment)."""
    src = result.sources
    header = ["bin", "Item"]
    for s in src:
        header += [f"{s}_count", f"{s}_yield"]
    header += ["avg", "comment"]
    rows = []
    for r in result.yield_rows:
        row = [_bin_label(r.get("bin")), r.get("Main Fail subject", "")]
        for s in src:
            row += [r.get(f"{s}_count"), r.get(f"{s}_yield")]
        row += [r.get("avg"), r.get("comment", "")]
        rows.append(row)
    return header, rows


def _fill_yield(ws, result):
    if result.df_yield is not None and not result.df_yield.empty:
        header = list(result.df_yield.columns)
        rows = [list(r) for r in result.df_yield.itertuples(index=False)]
    else:
        header, rows = _yield_table(result)
    _fill_table(ws, header, rows)
    _apply_table_col_widths(ws, header, custom_widths={"comment": 50})
    _apply_table_font(ws, header, size=12)
    _apply_small_font_headers(ws, header, ["_count", "_yield"], size=10)
    _set_table_row_heights(ws, len(rows), height=_YIELD_TABLE_ROW_HEIGHT)
    ws.row_dimensions[_HEADER_ROW].height = _YIELD_HEADER_ROW_HEIGHT


def _fill_fail_item(ws, result):
    src = result.sources
    header = ["Bin", "Item"]
    for s in src:
        header += [f"{s}_count", f"{s}_yield"]
    header += ["Distribution"]
    rows = []
    for r in result.yield_rows:
        row = [_bin_label(r.get("bin")), r.get("Main Fail subject", "")]
        for s in src:
            row += [r.get(f"{s}_count"), r.get(f"{s}_yield")]
        row += [""]   # Distribution 열 — 차트는 xlwings 단계에서 삽입
        rows.append(row)
    _fill_table(ws, header, rows)
    for i in range(len(rows)):
        ws.row_dimensions[_HEADER_ROW + 1 + i].height = _FAIL_ITEM_ROW_HEIGHT
    _fill_fail_values_section(ws, result)
    _apply_table_col_widths(ws, header)
    _apply_used_cell_font(ws, size=15, bold=False)
    _apply_named_columns_font(ws, header, ["Bin", "Item"], size=15, bold=True,
                              last_row=_HEADER_ROW + len(rows))


def _fill_fail_values_section(ws, result):
    """Fail_item 시트 bin 테이블 아래 FAIL_VALUES 섹션 — source별 fail DUT 레코드를 수평 나열."""
    fvr = getattr(result, "fail_value_rows", {})
    if not fvr:
        return

    n_bin = len(result.yield_rows)
    title_row = _HEADER_ROW + n_bin + 3   # "FAIL_VALUES" 라벨 행 (2행 공백 후)
    src_row   = title_row + 1             # source 이름 행
    hdr_row   = title_row + 2            # 열 헤더 행
    data_row0 = title_row + 3           # 데이터 시작 행

    _apply_hdr_style(ws.cell(row=title_row, column=_START_COL, value="FAIL_VALUES"))

    for i, (src_name, rows) in enumerate(fvr.items()):
        col0 = _START_COL + i * (_FAIL_VALUES_NCOLS + _FAIL_VALUES_GAP)
        _apply_hdr_style(ws.cell(row=src_row, column=col0, value=src_name))
        for ci, h in enumerate(_FAIL_VALUES_COLS):
            _apply_hdr_style(ws.cell(row=hdr_row, column=col0 + ci, value=h))
        for ri, row in enumerate(rows):
            r = data_row0 + ri
            _apply_data_style(ws.cell(row=r, column=col0,     value=_sanitize_cell(row["dut"])))
            _apply_data_style(ws.cell(row=r, column=col0 + 1, value=_sanitize_cell(row["xcoord"])))
            _apply_data_style(ws.cell(row=r, column=col0 + 2, value=_sanitize_cell(row["ycoord"])))
            _apply_data_style(ws.cell(row=r, column=col0 + 3, value=_sanitize_cell(row["bin"])))
            _apply_data_style(ws.cell(row=r, column=col0 + 4, value=_sanitize_cell(row["item"])))
            _apply_data_style(ws.cell(row=r, column=col0 + 5, value=_sanitize_cell(row["value"])))

    # 소스 블록별 윤곽선 적용 (src_row 헤더~마지막 데이터행)
    for i, (src_name, rows) in enumerate(fvr.items()):
        col0 = _START_COL + i * (_FAIL_VALUES_NCOLS + _FAIL_VALUES_GAP)
        last_row = data_row0 + len(rows) - 1 if rows else hdr_row
        _apply_all_borders(ws, src_row, col0, last_row, col0 + _FAIL_VALUES_NCOLS - 1)


def _fill_cpk(ws, result):
    header = ["TEST NAME", "LOW SPEC", "HIGH SPEC", "SCALE", "계열", "n",
              "min", "median", "max", "average", "stdev",
              "cpl", "cpu", "cp", "cpk", "comment"]
    rows = []
    for r in result.cpk_rows:
        rows.append([
            r.get("subject"), r.get("lower_limit"), r.get("upper_limit"),
            r.get("units"), r.get("source"), r.get("n"), r.get("min"),
            r.get("median"), r.get("max"), r.get("average"), r.get("stdev"),
            r.get("cpl"), r.get("cpu"), r.get("cp"), r.get("cpk"), "",
        ])
    _fill_table(ws, header, rows)
    _apply_table_col_widths(ws, header, custom_widths={
        "TEST NAME": _CPK_TEST_NAME_COL_WIDTH,
        "계열": _CPK_SERIES_COL_WIDTH,
        "n": _CPK_N_COL_WIDTH,
        "comment": 30,
    })
    _apply_font_delta_to_columns(ws, header, ["TEST NAME", "LOW SPEC", "HIGH SPEC", "SCALE"], 2)
    _merge_cpk_subject(ws, len(rows))


def _merge_cpk_subject(ws, n_rows, header_row=_HEADER_ROW, start_col=_START_COL):
    """같은 subject 연속 행의 TEST NAME/LOW SPEC/HIGH SPEC/SCALE 열 병합 + 세로 중앙 정렬."""
    if n_rows <= 1:
        return
    from openpyxl.styles import Alignment
    merge_cols = [start_col + i for i in range(4)]  # TEST NAME, LOW SPEC, HIGH SPEC, SCALE
    data_start = header_row + 1

    groups = []
    cur_val = ws.cell(row=data_start, column=start_col).value
    grp_start = data_start
    for r in range(data_start + 1, data_start + n_rows):
        val = ws.cell(row=r, column=start_col).value
        if val != cur_val or val is None:
            groups.append((grp_start, r - 1))
            cur_val = val
            grp_start = r
    groups.append((grp_start, data_start + n_rows - 1))

    for r_start, r_end in groups:
        if r_start == r_end:
            continue
        for c in merge_cols:
            ws.merge_cells(start_row=r_start, start_column=c,
                           end_row=r_end, end_column=c)
            cell = ws.cell(row=r_start, column=c)
            al = cell.alignment
            cell.alignment = Alignment(
                horizontal=al.horizontal, vertical="center",
                wrap_text=al.wrap_text
            )


def _fill_issue_table(ws, result):
    """Category 그룹 레이아웃. Yield Category = yield 데이터 재사용, CPK/ETC 플레이스홀더."""
    src = result.sources
    header = ["Category", "Bin", "Item", "avg"]
    for s in src:
        header += [f"{s}_yield"]          # count 열 제거, yield 만 유지
    header += ["Distribution", "comment", "개발 1차 comment",
               "PTE 2차 comment", "개발 2차 comment"]
    pad = len(header) - (4 + len(src))    # Distribution + comment 열 수

    rows = []
    for r in result.yield_rows:
        row = ["Yield", _bin_label(r.get("bin")),
               r.get("Main Fail subject", ""), r.get("avg")]
        for s in src:
            row += [r.get(f"{s}_yield")]  # count 제거
        row += [""] * pad
        rows.append(row)
    # CPK / ETC Category 섹션 (플레이스홀더 행)
    rows.append(["CPK"] + [""] * (len(header) - 1))
    rows.append(["ETC"] + [""] * (len(header) - 1))
    _fill_table(ws, header, rows)
    n_yield = len(result.yield_rows)
    _merge_issue_category(ws, n_yield)
    for i in range(n_yield):
        ws.row_dimensions[_HEADER_ROW + 1 + i].height = _FAIL_ITEM_ROW_HEIGHT
    _apply_table_col_widths(ws, header, custom_widths={
        "Distribution": 17,
        "comment": 40,
        "개발 1차 comment": 40,
        "PTE 2차 comment": 40,
        "개발 2차 comment": 40,
    })
    _apply_used_cell_font(ws, size=15, bold=False)
    _apply_named_columns_font(ws, header, ["Bin", "Item"], size=15, bold=True,
                              last_row=_HEADER_ROW + len(rows))


def _merge_issue_category(ws, n_yield, header_row=_HEADER_ROW, start_col=_START_COL):
    """issue_table Category 열의 Yield 행 전체를 병합 + 세로 중앙 정렬."""
    if n_yield <= 1:
        return
    from openpyxl.styles import Alignment
    data_start = header_row + 1
    ws.merge_cells(start_row=data_start, start_column=start_col,
                   end_row=data_start + n_yield - 1, end_column=start_col)
    cell = ws.cell(row=data_start, column=start_col)
    al = cell.alignment
    cell.alignment = Alignment(
        horizontal=al.horizontal, vertical="center",
        wrap_text=al.wrap_text
    )


def _fill_raw_data(ws, df):
    """df_honey 포맷 DataFrame 을 제목행 아래 A2 부터 기록.

    행0=subject 헤더, 1=Units, 2~5=Lower/Upper/Lower/Upper limit, 6~=측정 데이터.
    제목·Source 열 없이 df_honey 적재 포맷과 동일. 헤더·라벨행(2~7행)만 bold.
    Serial 컬럼은 정규화 시 자동 삽입되는 내부 컬럼이므로 제거.
    """
    from openpyxl.styles import Font
    bold = Font(bold=True)
    serial_cols = [c for c, v in zip(df.columns, df.iloc[0]) if v == "Serial"]
    if serial_cols:
        df = df.drop(columns=serial_cols)
    for ri, row in enumerate(df.values.tolist(), start=2):
        for ci, val in enumerate(row, start=1):
            cell = ws.cell(row=ri, column=ci, value=_sanitize_cell(val))
            if ri <= 7:
                cell.font = bold
            cell.alignment = copy(_DATA_ALIGN)


def _unique_sheet_name(wb, name, reserved=()):
    """Excel 시트명 규칙(≤31자, []:*?/\\ 금지, 중복 불가)으로 정제."""
    import re
    base = re.sub(r"[\[\]:*?/\\]", "_", str(name or "Sheet")).strip()[:31] or "Sheet"
    cand, n = base, 2
    existing = {s.lower() for s in wb.sheetnames} | {str(s).lower() for s in reserved}
    while cand.lower() in existing:
        suffix = f"_{n}"
        cand = base[:31 - len(suffix)] + suffix
        n += 1
    return cand


# ── openpyxl 표 채움 헬퍼 (템플릿 스타일 복제) ───────────────────────────────

def _fill_table(ws, header, rows, header_row=_HEADER_ROW, start_col=_START_COL):
    """헤더·데이터 셀을 기입하고 정의된 스타일 상수를 직접 적용."""
    _unmerge_below(ws, header_row, start_col)
    # 기존 범위 값 클리어 (재호출 방어용)
    max_r = ws.max_row
    max_c = max(ws.max_column, start_col + len(header) - 1)
    for r in range(header_row, max_r + 1):
        for c in range(start_col, max_c + 1):
            ws.cell(row=r, column=c).value = None
    # 헤더 기입 + 스타일
    for i, h in enumerate(header):
        _apply_hdr_style(ws.cell(row=header_row, column=start_col + i, value=h))
    # 데이터 기입 + 스타일
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            _apply_data_style(ws.cell(row=header_row + 1 + ri, column=start_col + ci,
                                      value=_sanitize_cell(val)))


def _safe_set(ws, coord, value):
    """병합 셀(비-좌상단)이면 해당 병합을 해제한 뒤 값을 쓴다."""
    from openpyxl.cell.cell import MergedCell
    from openpyxl.utils import coordinate_to_tuple, range_boundaries
    cell = ws[coord]
    if isinstance(cell, MergedCell):
        rr, cc = coordinate_to_tuple(coord)
        for rng in list(ws.merged_cells.ranges):
            c0, r0, c1, r1 = range_boundaries(str(rng))
            if r0 <= rr <= r1 and c0 <= cc <= c1:
                ws.unmerge_cells(str(rng))
                break
        cell = ws[coord]
    cell.value = value


def _unmerge_below(ws, min_row, min_col):
    """min_row/min_col 이후 영역에 걸친 병합을 모두 해제 (좌상단 제목 병합은 유지)."""
    from openpyxl.utils import range_boundaries
    for rng in list(ws.merged_cells.ranges):
        c0, r0, c1, r1 = range_boundaries(str(rng))
        if r0 >= min_row and c0 >= min_col:
            ws.unmerge_cells(str(rng))



def _apply_table_col_widths(ws, header, start_col=_START_COL, custom_widths=None):
    """헤더 이름 기반 열너비 일괄 설정.

    Distribution → _DIST_COL_WIDTH, Item/Category → _ITEM_COL_WIDTH, 나머지 → _NARROW_COL_WIDTH.
    custom_widths dict 에 있는 열은 해당 너비로 우선 적용.
    """
    from openpyxl.utils import get_column_letter
    _WIDE = {"Item", "Category"}
    for i, name in enumerate(header):
        letter = get_column_letter(start_col + i)
        if custom_widths and name in custom_widths:
            ws.column_dimensions[letter].width = custom_widths[name]
        elif name == "Distribution":
            ws.column_dimensions[letter].width = _DIST_COL_WIDTH
        elif name in _WIDE:
            ws.column_dimensions[letter].width = _ITEM_COL_WIDTH
        else:
            ws.column_dimensions[letter].width = _NARROW_COL_WIDTH


def _apply_all_borders(ws, min_row, min_col, max_row, max_col):
    """지정 사각형 범위의 모든 셀에 thin 테두리 적용 (병합 상단 셀만 처리)."""
    from openpyxl.styles import Border, Side
    from openpyxl.cell.cell import MergedCell
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for r in range(min_row, max_row + 1):
        for c in range(min_col, max_col + 1):
            cell = ws.cell(row=r, column=c)
            if not isinstance(cell, MergedCell):
                cell.border = border


def _set_cell_font_size(cell, size):
    f = cell.font
    nf = copy(f)
    nf.size = size
    cell.font = nf


def _set_cell_font(cell, size=None, bold=None):
    f = copy(cell.font)
    if size is not None:
        f.size = size
    if bold is not None:
        f.bold = bold
    cell.font = f


def _apply_table_font(ws, header, size=None, bold=None,
                      header_row=_HEADER_ROW, start_col=_START_COL):
    max_row = header_row + max(0, ws.max_row - header_row)
    max_col = start_col + len(header) - 1
    for row in ws.iter_rows(min_row=header_row, max_row=max_row,
                            min_col=start_col, max_col=max_col):
        for cell in row:
            _set_cell_font(cell, size=size, bold=bold)


def _apply_used_cell_font(ws, size=None, bold=None):
    from openpyxl.cell.cell import MergedCell
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell, MergedCell) or cell.value is None:
                continue
            _set_cell_font(cell, size=size, bold=bold)


def _set_table_row_heights(ws, n_rows, height,
                           header_row=_HEADER_ROW):
    for row in range(header_row, header_row + n_rows + 1):
        ws.row_dimensions[row].height = height


def _apply_named_columns_font(ws, header, names, size=None, bold=None,
                              header_row=_HEADER_ROW, start_col=_START_COL,
                              include_header=True, include_data=True,
                              last_row=None):
    name_set = set(names)
    for i, name in enumerate(header):
        if name not in name_set:
            continue
        col = start_col + i
        first = header_row if include_header else header_row + 1
        last = last_row if last_row is not None else (ws.max_row if include_data else header_row)
        for r in range(first, last + 1):
            _set_cell_font(ws.cell(row=r, column=col), size=size, bold=bold)


def _apply_data_font_by_suffix(ws, header, suffixes_or_names, size,
                               header_row=_HEADER_ROW, start_col=_START_COL):
    for i, name in enumerate(header):
        match = any(
            (name.endswith(pat) if pat.startswith("_") else name == pat)
            for pat in suffixes_or_names
        )
        if not match:
            continue
        col = start_col + i
        for r in range(header_row + 1, ws.max_row + 1):
            _set_cell_font_size(ws.cell(row=r, column=col), size)


def _apply_font_delta_to_columns(ws, header, names, delta,
                                 header_row=_HEADER_ROW, start_col=_START_COL):
    name_set = set(names)
    for i, name in enumerate(header):
        if name not in name_set:
            continue
        col = start_col + i
        for r in range(header_row, ws.max_row + 1):
            cell = ws.cell(row=r, column=col)
            base_size = cell.font.size or (_HDR_FONT.size if r == header_row else _DATA_FONT.size)
            _set_cell_font_size(cell, base_size + delta)


def _normalize_report_sheet_names(wb):
    canonical = {name.lower(): _report_sheet_display_name(name) for name in ALL_SHEETS}
    existing = {name.lower() for name in wb.sheetnames}
    for ws in wb.worksheets:
        current = ws.title
        lower = current.lower()
        if not lower.endswith("1"):
            continue
        base = lower[:-1]
        target = canonical.get(base)
        if target and target.lower() not in existing:
            existing.discard(lower)
            ws.title = target
            existing.add(target.lower())


def _center_used_cells(ws):
    from openpyxl.cell.cell import MergedCell
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell, MergedCell) or cell.value is None:
                continue
            al = cell.alignment
            cell.alignment = _Alignment(
                horizontal="center",
                vertical="center",
                wrap_text=al.wrap_text if al.wrap_text is not None else True,
            )


def _apply_sheet_title(ws, title=None):
    from openpyxl.utils import range_boundaries
    title = title or ws.title
    for rng in list(ws.merged_cells.ranges):
        c0, r0, c1, r1 = range_boundaries(str(rng))
        if r0 <= 1 <= r1:
            ws.unmerge_cells(str(rng))
    ws.row_dimensions[1].height = 30
    for col in range(1, _TITLE_ROW_MAX_COL + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = _PatternFill(fill_type="solid", fgColor=_TITLE_FILL_RGB)
        cell.alignment = copy(_TITLE_ALIGN)
    cell = ws["A1"]
    cell.value = title
    cell.font = copy(_TITLE_FONT)


def _finalize_openpyxl_sheet_layouts(wb):
    for ws in wb.worksheets:
        if ws.title.lower() == "summary":
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
            _set_cell_font_size(ws.cell(row=header_row, column=start_col + i), size)


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


# ── distribution (xlwings / Excel COM) ───────────────────────────────────────

def _write_distribution_xlwings(out_path, result, colors=None, attach_fail_item=False,
                                dist_progress_cb=None, attach_progress_cb=None):
    """openpyxl 로 저장된 파일을 열어 distribution 시트 + 차트를 추가한다.

    attach_fail_item=True 면 distribution 차트를 PNG 로 export 해 fail_item 시트에
    불량율 높은 순으로 1/3 크기 썸네일로 부착한다 (차트 원본 재생성 없이 재활용).
    """
    import shutil
    import xlwings as xw

    out_path = str(Path(out_path).resolve())
    with _prof("app_launch"):
        app = xw.App(visible=False, add_book=False)
        app.display_alerts = False
        app.screen_updating = False
    wb = None
    tmpdirs = []
    try:
        with _prof("wb_open"):
            wb = app.books.open(out_path)
        # 변경마다 재계산·이벤트 억제 (전용 인스턴스라 사용자 Excel 무영향)
        try:
            app.api.Calculation = -4135   # xlCalculationManual
            app.api.EnableEvents = False
        except Exception:
            pass
        with _prof("clear"):
            names = [s.name for s in wb.sheets]
            dist_name = next((n for n in names if n.lower() == "distribution"), None)
            if dist_name:
                sh = wb.sheets[dist_name]
                for c in list(sh.charts):     # 템플릿/이전 차트 제거
                    try:
                        c.delete()
                    except Exception:
                        pass
                sh.clear()
            else:
                sh = wb.sheets.add(_report_sheet_display_name("distribution"),
                                   after=wb.sheets[len(wb.sheets) - 1])
        chart_map = _write_distribution(wb, sh, result, colors,
                                        dist_progress_cb=dist_progress_cb)
        if attach_fail_item and chart_map:
            tmpdir = _attach_fail_item_charts(
                wb, result, chart_map, attach_progress_cb=attach_progress_cb
            )
            if tmpdir:
                tmpdirs.append(tmpdir)
        if chart_map:
            tmpdir = _attach_issue_table_charts(
                wb, result, chart_map, attach_progress_cb=attach_progress_cb
            )
            if tmpdir:
                tmpdirs.append(tmpdir)
        # 모든 시트 눈금선 제거 (xlwings/Excel COM 단계 — distribution 포함)
        for s in wb.sheets:
            try:
                s.activate()
                app.api.ActiveWindow.DisplayGridlines = False
            except Exception:
                pass
        sh.activate()
        with _prof("wb_save"):
            wb.save()
    finally:
        try:
            with _prof("wb_close"):
                if wb is not None:
                    wb.close()
        finally:
            with _prof("wb_close"):
                app.quit()
            try:
                _validate_embedded_images(out_path)
            finally:
                for tmpdir in tmpdirs:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                _prof_report()


def _attach_fail_item_charts(wb, result, chart_map, attach_progress_cb=None):
    """fail_item 시트의 Distribution 열(각 데이터 행)에 해당 subject 차트 PNG 삽입.

    yield_row 1개 = 1행 = Distribution 열 1셀에 차트 1개 배치.
    """
    import os
    import tempfile

    names = [s.name for s in wb.sheets]
    fi_name = next((n for n in names if n.lower() == "fail_item"), None)
    if fi_name is None:
        return None
    fi = wb.sheets[fi_name]

    # Distribution 열 = B(2) + bin + Item + (count+yield) × sources
    dist_col = _START_COL + 2 + 2 * len(result.sources)

    tmpdir = tempfile.mkdtemp(prefix="honey_fi_")
    seq = 0

    for i, r in enumerate(result.yield_rows):
        subj = r.get("Main Fail subject")
        if not subj or subj not in chart_map:
            continue
        ch = chart_map[subj]   # COM Chart (Pass2 가 COM Chart 를 저장)
        row_excel = _HEADER_ROW + 1 + i
        try:
            cell = fi.range((row_excel, dist_col))
            left = cell.left
            top = cell.top
            w = cell.width
            h = cell.height
        except Exception:
            left, top = 700.0, 60.0 + i * _FAIL_ITEM_ROW_HEIGHT
            w, h = 200.0, float(_FAIL_ITEM_ROW_HEIGHT)
        png = os.path.join(tmpdir, f"fi_{seq}.png")
        seq += 1
        if _attach_chart_picture(fi, ch, png, f"fi_chart_{seq}", left, top, w, h,
                                 "fail_item", subj, attach_progress_cb):
            _prof_count("pngs")

    return tmpdir if seq > 0 else None


def _attach_issue_table_charts(wb, result, chart_map, attach_progress_cb=None):
    """issue_table 시트의 Distribution 열(각 데이터 행)에 해당 subject 차트 PNG 삽입.

    fail_item 과 동일한 COM Export 방식. dist_col 계산만 issue_table header 기준으로 다름.
    header: ["Category","bin","Item","avg", {src}_yield×N, "Distribution", ...]
    → dist_col = _START_COL + 4 + len(sources)
    """
    import os
    import tempfile

    names = [s.name for s in wb.sheets]
    it_name = next((n for n in names if n.lower() == "issue_table"), None)
    if it_name is None:
        return None
    it = wb.sheets[it_name]

    dist_col = _START_COL + 4 + len(result.sources)

    tmpdir = tempfile.mkdtemp(prefix="honey_it_")
    seq = 0

    for i, r in enumerate(result.yield_rows):
        subj = r.get("Main Fail subject")
        if not subj or subj not in chart_map:
            continue
        ch = chart_map[subj]
        row_excel = _HEADER_ROW + 1 + i
        try:
            cell = it.range((row_excel, dist_col))
            left = cell.left
            top = cell.top
            w = cell.width
            h = cell.height
        except Exception:
            left, top = 700.0, 60.0 + i * _FAIL_ITEM_ROW_HEIGHT
            w, h = 200.0, float(_FAIL_ITEM_ROW_HEIGHT)
        png = os.path.join(tmpdir, f"it_{seq}.png")
        seq += 1
        if _attach_chart_picture(it, ch, png, f"it_chart_{seq}", left, top, w, h,
                                 "issue_table", subj, attach_progress_cb):
            _prof_count("pngs")

    return tmpdir if seq > 0 else None


def _attach_chart_picture(sheet, chart, png_path, name, left, top, width, height,
                          sheet_name, subject, attach_progress_cb=None):
    """Export a COM chart as PNG, then embed it as a picture on target sheet."""
    try:
        with _prof(f"{sheet_name}.export"):
            method = _export_chart_png_stable(chart, png_path)
        if method:
            with _prof(f"{sheet_name}.picadd"):
                sheet.pictures.add(
                    png_path,
                    link_to_file=False,
                    save_with_document=True,
                    name=name,
                    left=left,
                    top=top,
                    width=width,
                    height=height,
                )
            return True
        _notify_attach_progress(attach_progress_cb, "copy_picture", sheet_name, subject)
        with _prof(f"{sheet_name}.copy_picture"):
            _copy_chart_picture_to_sheet(chart, sheet, name, left, top, width, height)
        _log_chart_attach(f"{sheet_name}:{subject} used CopyPicture fallback")
        return True
    except Exception as exc:
        _log_chart_attach(f"{sheet_name}:{subject} attach failed: {exc!r}")
        return False


def _export_chart_png_stable(chart, png_path):
    """Keep COM Chart.Export, but retry after moving off-screen charts into view."""
    if _export_chart_png_once(chart, png_path):
        return "direct"

    chart_object = _chart_object(chart)
    if chart_object is None:
        return None

    old_left = old_top = None
    try:
        old_left, old_top = chart_object.Left, chart_object.Top
        for attempt in range(1, _EXPORT_MOVE_RETRIES + 1):
            chart_object.Left = 0
            chart_object.Top = _DIST_TITLE_PX + (attempt - 1) * (_CHART_H + 6)
            time.sleep(_EXPORT_RETRY_SLEEP * attempt)
            if _export_chart_png_once(chart, png_path):
                return f"moved{attempt}"
    except Exception as exc:
        _log_chart_attach(f"Chart.Export move retry failed: {exc!r}")
    finally:
        if old_left is not None and old_top is not None:
            try:
                chart_object.Left = old_left
                chart_object.Top = old_top
            except Exception:
                pass
    return None


def _export_chart_png_once(chart, png_path):
    try:
        if os.path.exists(png_path):
            os.remove(png_path)
        chart.Export(png_path, "PNG")
    except Exception as exc:
        _log_chart_attach(f"Chart.Export failed: {exc!r}")
        return False
    return _is_valid_png(png_path)


def _is_valid_png(png_path):
    try:
        if os.path.getsize(png_path) <= len(_PNG_MAGIC):
            return False
        with open(png_path, "rb") as fh:
            return fh.read(len(_PNG_MAGIC)) == _PNG_MAGIC
    except OSError:
        return False


def _copy_chart_picture_to_sheet(chart, sheet, name, left, top, width, height):
    chart_object = _chart_object(chart)
    if chart_object is None:
        raise RuntimeError("chart object not found for CopyPicture fallback")
    before = int(sheet.api.Shapes.Count)
    try:
        sheet.api.Activate()
    except Exception:
        pass
    chart_object.CopyPicture(Appearance=1, Format=-4147)
    sheet.api.Paste()
    after = int(sheet.api.Shapes.Count)
    if after <= before:
        raise RuntimeError("CopyPicture paste did not create a shape")
    shape = sheet.api.Shapes.Item(after)
    shape.Name = name
    shape.Left = float(left)
    shape.Top = float(top)
    shape.Width = float(width)
    shape.Height = float(height)
    return shape


def _notify_attach_progress(cb, event, sheet_name, subject):
    if cb is None:
        return
    try:
        cb(event, sheet_name, subject)
    except Exception:
        pass


def _chart_object(chart):
    try:
        return chart.Parent
    except Exception:
        return None


def _log_chart_attach(message):
    print(f"[xlsx_writer] chart attach: {message}", file=sys.stderr)


def _validate_embedded_images(xlsx_path):
    try:
        with zipfile.ZipFile(xlsx_path) as zf:
            names = set(zf.namelist())
            rel_names = [n for n in names if n.startswith("xl/drawings/_rels/")
                         and n.endswith(".rels")]
            for rel_name in rel_names:
                rel_xml = zf.read(rel_name).decode("utf-8", errors="replace")
                if 'Target="NULL"' in rel_xml or "Target='NULL'" in rel_xml:
                    raise RuntimeError(f"broken image relationship in {rel_name}: Target=NULL")
                for target in _image_rel_targets(rel_xml):
                    part = _resolve_xlsx_part(rel_name, target)
                    if part not in names:
                        raise RuntimeError(
                            f"broken image relationship in {rel_name}: missing {part}"
                        )
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"invalid xlsx package: {xlsx_path}") from exc


def _is_package_integrity_error(exc):
    msg = str(exc)
    return ("broken image relationship" in msg
            or "invalid xlsx package" in msg)


def _image_rel_targets(rel_xml):
    import xml.etree.ElementTree as ET

    root = ET.fromstring(rel_xml)
    for rel in root:
        typ = rel.attrib.get("Type", "")
        if typ.endswith("/image"):
            yield rel.attrib.get("Target", "")


def _resolve_xlsx_part(rel_name, target):
    base = Path(rel_name).parent.parent
    part = (base / target).as_posix()
    while "/../" in part:
        left, right = part.split("/../", 1)
        part = left.rsplit("/", 1)[0] + "/" + right
    return part.lstrip("/")


def _hex_to_excel_rgb(hex_color):
    """'#RRGGBB' → Excel COM RGB 정수 (R + G*256 + B*65536). 실패 시 None."""
    try:
        s = str(hex_color).strip().lstrip("#")
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return r + (g << 8) + (b << 16)
    except Exception:
        return None


def _a1(r1, c1, r2, c2):
    """(r1,c1)~(r2,c2) → 'E1:E150' A1 주소. COM Range 1회 호출용(xlwings Range 우회)."""
    return f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}"


def _chart_pos(i):
    """차트 grid 좌상단 픽셀 (gap 없이 밀착, 제목 배너 아래)."""
    col = i % _CHARTS_PER_ROW
    grow = i // _CHARTS_PER_ROW
    return col * _CHART_W, _DIST_TITLE_PX + grow * _CHART_H


def _is_standard(spec, n_sources):
    """표준 레이아웃(LSL+USL 2개 + 전체 source) — 템플릿 클론 대상."""
    return (spec["lo_v"] is not None and spec["hi_v"] is not None
            and len(spec["series_list"]) == n_sources)


def _add_dist_series(sc, data_api, spec):
    """SeriesCollection 에 limit(1,2) + source series 추가. 반환 (limit, data) 리스트."""
    top_row = spec["top_row"]
    limit_series = []
    for lim_v, xcol, nm in ((spec["lo_v"], 1, "LSL"), (spec["hi_v"], 3, "USL")):
        if lim_v is None:
            continue
        s = sc.NewSeries()
        s.XValues = data_api.Range(_a1(top_row, xcol, top_row + 1, xcol))
        s.Values = data_api.Range(_a1(top_row, xcol + 1, top_row + 1, xcol + 1))
        s.Name = nm
        limit_series.append(s)
    data_series = []
    dcol = 5
    for name, xs, _ys in spec["series_list"]:
        n = len(xs)
        s = sc.NewSeries()
        s.XValues = data_api.Range(_a1(top_row, dcol, top_row + n - 1, dcol))
        s.Values = data_api.Range(_a1(top_row, dcol + 1, top_row + n - 1, dcol + 1))
        s.Name = str(name)
        data_series.append(s)
        dcol += 2
    return limit_series, data_series


def _style_series(limit_series, data_series, colors):
    """limit/data series 스타일 일괄 적용."""
    for s in limit_series:
        _style_limit_series(s)
    for k, s in enumerate(data_series):
        rgb = _hex_to_excel_rgb(colors[k % len(colors)]) if colors else None
        _style_data_series(s, rgb)


def _new_dist_chart(sh, spec, data_api, colors):
    """신규 차트 1개를 처음부터 생성+서식 (기존 경로). 반환: COM Chart."""
    left, top = _chart_pos(spec["i"])
    with _prof("dist.series_add"):
        ch = sh.charts.add(left, top, _CHART_W, _CHART_H)
        chart = _chart_com(ch)
        sc = chart.SeriesCollection()
        limit_series, data_series = _add_dist_series(sc, data_api, spec)
        ch.chart_type = "xy_scatter_lines_no_markers"
    _prof_count("series", len(limit_series) + len(data_series))
    with _prof("dist.style"):
        _style_series(limit_series, data_series, colors)
    with _prof("dist.format"):
        _format_dist_chart(chart, spec["d"], spec["x_min"], spec["x_max"],
                           len(limit_series), spec["is_fail"])
    return chart


def _repoint_series(chart, data_api, spec):
    """복제 차트의 기존 series(순서: LSL,USL,source…)를 spec 의 range 로 재참조.

    반환 (limit_series, data_series) — 서식 리셋 시 재스타일용.
    """
    sc = chart.SeriesCollection()
    top_row = spec["top_row"]
    limit_series = []
    for idx, xcol in ((1, 1), (2, 3)):
        s = sc.Item(idx)
        s.XValues = data_api.Range(_a1(top_row, xcol, top_row + 1, xcol))
        s.Values = data_api.Range(_a1(top_row, xcol + 1, top_row + 1, xcol + 1))
        limit_series.append(s)
    data_series = []
    dcol = 5
    for k, (name, xs, _ys) in enumerate(spec["series_list"]):
        s = sc.Item(3 + k)
        n = len(xs)
        s.XValues = data_api.Range(_a1(top_row, dcol, top_row + n - 1, dcol))
        s.Values = data_api.Range(_a1(top_row, dcol + 1, top_row + n - 1, dcol + 1))
        s.Name = str(name)
        data_series.append(s)
        dcol += 2
    return limit_series, data_series


def _apply_per_chart(chart, spec, legend_fix):
    """복제 차트에 subject별 가변 서식만 적용: x축 범위·제목·fail배경·(필요시)범례."""
    try:
        xax = chart.Axes(_XL_CATEGORY, _XL_PRIMARY)
        x_min, x_max = spec["x_min"], spec["x_max"]
        if x_min is not None and x_max is not None and x_min < x_max:
            xax.MinimumScale = x_min
            xax.MaximumScale = x_max
    except Exception:
        pass
    d = spec["d"]
    try:
        title = chart.ChartTitle
        cap = _limit_caption(d)
        title.Text = d.subject + "\n" + cap
        try:
            title.Characters(len(d.subject) + 2, len(cap)).Font.Size = 8
        except Exception:
            pass
        title.Top = 0
    except Exception:
        pass
    # 범례: 복제로 limit(1,2) entry 가 되살아난 경우에만 재삭제
    if legend_fix:
        try:
            leg = chart.Legend
            for li in (2, 1):
                try:
                    leg.LegendEntries(li).Delete()
                except Exception:
                    pass
        except Exception:
            pass
    if spec["is_fail"]:
        try:
            chart.ChartArea.Interior.Color = _RGB_FAIL_BG
        except Exception:
            pass


def _write_distribution(wb, sh, result, colors=None, dist_progress_cb=None):
    """각 subject 의 누적분포(CDF) 차트. x=value, y=0~100%(0~1 스케일).

    source(input file)별 series + LSL/USL 세로 한계선(series 1,2). 차트는 gap 없이
    밀착 배치. 서식은 _format_dist_chart 사양 따름.

    성능: _dist 데이터는 차트별로 쓰지 않고 한 번에 일괄기입(2-pass)하고, 시리즈
    참조는 COM Range(A1 문자열)로 직접 건다.
    """
    dists = result.distributions
    if not dists:
        sh.range("A1").value = "선택된 항목에 분포 데이터가 없습니다."
        return

    data = wb.sheets.add("_dist", after=sh)
    data_api = data.api  # COM Worksheet 캐시 (시리즈 참조용)

    _put_title(sh, 8, "Distribution")
    sh.range((1, _INDEX_COL)).value = "Item Index (Ctrl+F)"
    sh.range((1, _INDEX_COL)).column_width = 26

    # ── Pass 1: 차트별 레이아웃 계산 + _dist 전체를 하나의 2D 배열로 조립 ──────
    specs = []          # 차트 생성용 메타 (Pass 2)
    index_entries = []  # (idx_row, subject) — Item Index 열 일괄기입용
    cells = []          # (row0, col0, value) — grid 채울 좌표(0-based)
    total_rows = 0
    max_cols = 4        # 최소 limit 4열
    cur = 1             # _dist 행 커서(1-based)
    for i, d in enumerate(dists):
        # source별 (value, 누적 0~1) 준비
        series_list = []
        for tr in d.traces:
            xs = np.asarray(tr["xs"], dtype=float)
            ys = np.asarray(tr["ys"], dtype=float) / 100.0   # 0~100 → 0~1
            if xs.size == 0:
                continue
            xs, ys = _downsample(xs, ys)
            series_list.append((tr["source"], xs, ys))
        if not series_list:
            continue

        data_min = min(float(xs.min()) for _, xs, _ in series_list)
        data_max = max(float(xs.max()) for _, xs, _ in series_list)
        lo, hi = d.lower_limit, d.upper_limit
        is_fail = (_isnum(lo) and data_min < float(lo)) or (_isnum(hi) and data_max > float(hi))
        x_min, x_max = _x_axis_range(lo, hi, data_min, data_max, is_fail)
        lo_v = float(lo) if _isnum(lo) else None
        hi_v = float(hi) if _isnum(hi) else None

        # _dist 좌표 적재 (0-based): col1/2=LSL x/y, col3/4=USL x/y, col5~=source x/y
        top_row = cur
        max_len = 2
        if lo_v is not None:
            cells += [(top_row - 1, 0, lo_v), (top_row - 1, 1, 0.0),
                      (top_row, 0, lo_v), (top_row, 1, 1.0)]
        if hi_v is not None:
            cells += [(top_row - 1, 2, hi_v), (top_row - 1, 3, 0.0),
                      (top_row, 2, hi_v), (top_row, 3, 1.0)]
        dcol = 5
        for _name, xs, ys in series_list:
            for j in range(len(xs)):
                cells.append((top_row - 1 + j, dcol - 1, float(xs[j])))
                cells.append((top_row - 1 + j, dcol, float(ys[j])))
            max_len = max(max_len, len(xs))
            dcol += 2
        bot_row = top_row + max_len - 1
        max_cols = max(max_cols, 4 + 2 * len(series_list))
        total_rows = max(total_rows, bot_row)

        # 차트 배치 인덱스 (원본 enumerate i 기준 — 빈 series 스킵 시 격자 위치 보존)
        col = i % _CHARTS_PER_ROW
        grow = i // _CHARTS_PER_ROW
        index_entries.append((2 + grow * _ROWS_PER_CHART + col, d.subject))
        specs.append({
            "d": d, "i": i, "top_row": top_row, "series_list": series_list,
            "lo_v": lo_v, "hi_v": hi_v, "x_min": x_min, "x_max": x_max,
            "is_fail": is_fail,
        })
        cur = bot_row + 2

    chart_map = {}  # subject 이름 → xlwings Chart (fail_item PNG 부착에 재활용)
    if specs:
        # ── _dist + Item Index 일괄기입 (병목 #1: 차트당 쓰기 → 1~2회로) ──────
        with _prof("dist.data_write"):
            grid = [[None] * max_cols for _ in range(total_rows)]
            for r0, c0, v in cells:
                grid[r0][c0] = v
            data.range((1, 1), (total_rows, max_cols)).value = grid
            # Item Index 열: 흩어진 행을 None-pad 한 단일 열로 1회 기입
            max_idx = max(r for r, _ in index_entries)
            col_vals = [[None] for _ in range(2, max_idx + 1)]
            for r, subj in index_entries:
                col_vals[r - 2] = [subj]
            sh.range((2, _INDEX_COL), (max_idx, _INDEX_COL)).value = col_vals

    # ── Pass 2: 차트 생성 — 표준 레이아웃은 템플릿 1개 만들어 COM Duplicate 복제 ─
    # (format/style 반복 COM 호출 회피). 비표준(한계 누락 등)은 개별 빌드.
    n_sources = len(result.sources)
    standard = [s for s in specs if _is_standard(s, n_sources)]
    others = [s for s in specs if not _is_standard(s, n_sources)]
    n_dist_charts = len(specs)
    done_charts = 0

    if standard:
        tspec = standard[0]
        template = _new_dist_chart(sh, tspec, data_api, colors)  # 완전 서식
        chart_map[tspec["d"].subject] = template
        done_charts += 1
        if dist_progress_cb:
            dist_progress_cb(done_charts, n_dist_charts)
        template_co = template.Parent  # ChartObject (Duplicate 원본)
        # 클론이 fail 배경을 상속하지 않도록 중립화 (tspec 배경은 마지막에 재적용)
        try:
            template.ChartArea.Interior.ColorIndex = _XL_COLORINDEX_NONE
        except Exception:
            pass

        restyle = False       # 복제 시 series 마커/선 서식이 리셋되는가
        legend_fix = False    # 복제 시 limit 범례 entry 가 되살아나는가
        coll = sh.api.ChartObjects()   # live 컬렉션 — Duplicate 후 Count 증가
        for idx, spec in enumerate(standard[1:]):
            with _prof("dist.series_add"):
                template_co.Duplicate()
                new_co = coll.Item(coll.Count)   # 방금 복제된 것(최고 인덱스)
                # 크기는 Duplicate 가 복사 → 위치만 재설정
                new_co.Left, new_co.Top = _chart_pos(spec["i"])
                nchart = new_co.Chart
                limit_series, data_series = _repoint_series(nchart, data_api, spec)
            _prof_count("series", len(limit_series) + len(data_series))

            if idx == 0:  # 첫 복제로 게이트 판정 (이후 동일하게 적용)
                try:
                    marker_ok = (nchart.SeriesCollection().Item(3).MarkerStyle
                                 == _XL_MARKER_CIRCLE)
                except Exception:
                    marker_ok = False
                n_legend = 2 + len(spec["series_list"])
                try:
                    leg_count = nchart.Legend.LegendEntries().Count
                except Exception:
                    leg_count = n_legend
                restyle = not marker_ok
                legend_fix = (leg_count >= n_legend)
                if _PROF_ON:
                    print(f"[chart-profile] clone gate: marker_ok={marker_ok} "
                          f"legend={leg_count}/{n_legend} → restyle={restyle} "
                          f"legend_fix={legend_fix}", file=sys.stderr)

            if restyle:
                with _prof("dist.style"):
                    _style_series(limit_series, data_series, colors)
            with _prof("dist.format"):
                _apply_per_chart(nchart, spec, legend_fix)
            chart_map[spec["d"].subject] = nchart
            done_charts += 1
            if dist_progress_cb:
                dist_progress_cb(done_charts, n_dist_charts)

        # 템플릿 자신의 fail 배경 재적용 (중립화 되돌림)
        if tspec["is_fail"]:
            try:
                template.ChartArea.Interior.Color = _RGB_FAIL_BG
            except Exception:
                pass

    # 비표준 차트는 개별 빌드 (기존 경로)
    for spec in others:
        chart_map[spec["d"].subject] = _new_dist_chart(sh, spec, data_api, colors)
        done_charts += 1
        if dist_progress_cb:
            dist_progress_cb(done_charts, n_dist_charts)

    _prof_count("charts", len(chart_map))
    _finalize_title_row(sh)
    try:
        data.api.Visible = False  # 헬퍼 시트 숨김
    except Exception:
        pass
    return chart_map


def _chart_com(ch):
    """xlwings Chart → COM Chart 객체. ch.api 가 (ChartObject, Chart) 튜플일 수 있음."""
    api = ch.api
    if isinstance(api, tuple):
        return api[1]
    return getattr(api, "Chart", api)


def _style_limit_series(s):
    """limit line series: 빨강 system dash + 마커 제거 (선만)."""
    try:
        line = s.Format.Line
        line.DashStyle = _MSO_LINE_SYSDASH
        line.ForeColor.RGB = _RGB_RED
    except Exception:
        pass
    try:
        s.MarkerStyle = _XL_MARKER_NONE
    except Exception:
        pass


def _style_data_series(s, rgb=None):
    """data series: 점(마커)만 — 점 사이 선 제거, 마커 색 = source 색(rgb)."""
    try:
        s.Format.Line.Visible = _MSO_FALSE   # 점 사이 잇는 선 제거
    except Exception:
        pass
    try:
        s.MarkerStyle = _XL_MARKER_CIRCLE
        s.MarkerSize = _MARKER_SIZE
    except Exception:
        pass
    if rgb is not None:
        try:
            s.MarkerBackgroundColor = rgb
            s.MarkerForegroundColor = rgb
        except Exception:
            pass


def _format_dist_chart(chart, d, x_min, x_max, limit_count, is_fail):
    """xy_scatter CDF 차트 서식 (COM 객체 1회 할당 후 재사용)."""
    try:
        yax = chart.Axes(_XL_VALUE, _XL_PRIMARY)
        yax.MinimumScale = 0
        yax.MaximumScale = 1
        yax.MajorUnit = 0.2
        yax.HasMinorGridlines = True
        ytl = yax.TickLabels
        ytl.NumberFormatLocal = "0%"
        ytl.Font.Size = 8
        yax.TickLabelPosition = _XL_LOW
    except Exception:
        pass
    try:
        xax = chart.Axes(_XL_CATEGORY, _XL_PRIMARY)
        if x_min is not None and x_max is not None and x_min < x_max:
            xax.MinimumScale = x_min
            xax.MaximumScale = x_max
        xax.HasMinorGridlines = True
        xax.TickLabels.Font.Size = 8
    except Exception:
        pass
    try:
        chart.HasTitle = True
        title = chart.ChartTitle
        cap = _limit_caption(d)          # item 명 아래 줄: (LO ~ HI units)
        title.Text = d.subject + "\n" + cap
        tf = title.Font
        tf.Name = "Arial Black"
        tf.Size = 10
        try:                             # 둘째 줄(캡션)은 작게
            title.Characters(len(d.subject) + 2, len(cap)).Font.Size = 8
        except Exception:
            pass
        title.Top = 0
    except Exception:
        pass
    try:
        pa = chart.PlotArea
        pa.Width = _PLOT_W
        pa.Top = _PLOT_TOP
        pa.Height = _PLOT_H
    except Exception:
        pass
    # legend: limit series(1..limit_count) entry 삭제, 폰트 8
    try:
        chart.HasLegend = True
        leg = chart.Legend
        leg.Font.Size = 8
        for idx in range(limit_count, 0, -1):
            try:
                leg.LegendEntries(idx).Delete()
            except Exception:
                pass
    except Exception:
        pass
    if is_fail:
        try:
            chart.ChartArea.Interior.Color = _RGB_FAIL_BG
        except Exception:
            pass


# ── x축 범위 계산 헬퍼 ───────────────────────────────────────────────────────

def _isnum(v):
    if v is None:
        return False
    try:
        return not math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def _fmt_lim(v):
    """limit 표시값: nan/None → '-', 정수 → int, 그 외 → 간결한 실수."""
    if not _isnum(v):
        return "-"
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _limit_caption(d):
    """차트 item 명 아래 줄: '(LO ~ HI units)'."""
    unit = (d.unit or "").strip()
    body = f"{_fmt_lim(d.lower_limit)} ~ {_fmt_lim(d.upper_limit)}"
    return f"({body} {unit})" if unit else f"({body})"


def _decimals(v):
    """숫자의 유효 소수 자릿수 (정수면 0)."""
    if v is None:
        return 0
    s = repr(float(v))
    if "e" in s or "E" in s or "." not in s:
        return 0
    return len(s.split(".")[1].rstrip("0"))


def _floor_dec(x, dec):
    f = 10 ** dec
    return math.floor(x * f) / f


def _ceil_dec(x, dec):
    f = 10 ** dec
    return math.ceil(x * f) / f


def _x_axis_range(lo, hi, dmin, dmax, is_fail):
    """x축 [min,max]. Pass=LIM 그대로, Fail=±5% 가드밴드 후 LIM 자릿수로 floor/ceil.
    LIM None/nan 이면 data min/max 사용."""
    lo_n = float(lo) if _isnum(lo) else None
    hi_n = float(hi) if _isnum(hi) else None
    xmin = lo_n if lo_n is not None else dmin
    xmax = hi_n if hi_n is not None else dmax
    if not is_fail:
        return xmin, xmax
    if lo_n is not None and dmin < lo_n:
        xmin = dmin - (lo_n - dmin) * 0.05
    if hi_n is not None and dmax > hi_n:
        xmax = dmax + (dmax - hi_n) * 0.05
    dec = max(_decimals(lo_n), _decimals(hi_n))
    return _floor_dec(xmin, dec), _ceil_dec(xmax, dec)


def _downsample(xs, ys, max_points=_MAX_CDF_POINTS):
    if xs.size <= max_points:
        return xs, ys
    idx = np.unique(np.linspace(0, xs.size - 1, max_points).astype(int))
    return xs[idx], ys[idx]


# ── distribution 시트 제목 배너 (xlwings) ────────────────────────────────────

_XL_CENTER = -4108        # xlCenter
_TITLE_FILL = (191, 227, 255)
_TITLE_FONT_SIZE = 20
_TITLE_ROW_HEIGHT = 30


def _put_title(sh, ncols, text):
    """1행에 시트 제목 배너 — 하늘색 배경, 검은 bold, 큰 글씨, 가로 병합."""
    span = max(_TITLE_ROW_MAX_COL, ncols)
    try:
        sh.range((1, 1), (1, span)).merge()
    except Exception:
        pass
    c = sh.range((1, 1))
    c.value = text
    try:
        c.color = _TITLE_FILL
        f = c.api.Font
        f.Bold = True
        f.Size = _TITLE_FONT_SIZE
        f.Color = 0  # black
        c.api.HorizontalAlignment = _XL_CENTER
        c.api.VerticalAlignment = _XL_CENTER
        sh.range((1, 1), (1, span)).color = _TITLE_FILL
    except Exception:
        pass


def _finalize_title_row(sh):
    """제목 행 높이 보정."""
    try:
        sh.range((1, 1)).row_height = _TITLE_ROW_HEIGHT
    except Exception:
        pass

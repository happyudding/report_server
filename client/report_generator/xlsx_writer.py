"""하이브리드 xlsx 리포트 생성.

- table 시트(summary / yield / cpk / fail_item / issue_table)는 클라이언트에 동봉된
  templete.xlsx 를 **openpyxl** 로 열어 셀 값만 채운다(서식은 템플릿이 보유 → 빠르고
  Excel COM 불필요).
- distribution 차트만 **xlwings**(Excel COM) 로 생성한다(차트 옵션 정밀 제어 목적).

레이아웃은 client/data/templete.xlsx 기준. summary/yield/issue_table 은
server/xlsx_parser.py 의 anchor/header 규약과도 맞춘다.
계산은 analyzer/_builders 에서 끝났고, 이 모듈은 출력만 담당한다.
"""
from __future__ import annotations

import math
import sys
from copy import copy
from pathlib import Path

import numpy as np

_MAX_CDF_POINTS = 150
_CHARTS_PER_ROW = 5
_CHART_W, _CHART_H, _GAP = 320, 220, 16
# distribution 찾기(Ctrl+F)용 item 인덱스: 차트 그리드 오른쪽 열, 차트 한 행당 행 수
_INDEX_COL = 40
_ROWS_PER_CHART = 16
# distribution 차트 그리드를 제목 배너 아래로 내리는 픽셀 오프셋
_DIST_TITLE_PX = 30

ALL_SHEETS = ["summary", "yield", "cpk", "fail_item", "issue_table", "distribution"]

# 템플릿 table 시트의 표 시작 위치 (A열 비움, 제목 A1, 헤더 3행, 데이터 4행~)
_HEADER_ROW = 3
_START_COL = 2  # B열


# ── 템플릿 경로 ──────────────────────────────────────────────────────────────

def _template_path() -> str:
    """동봉된 templete.xlsx 경로. PyInstaller(frozen)·소스 실행 양쪽 지원."""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "data" / "templete.xlsx")
    here = Path(__file__).resolve()
    # client/report_generator/xlsx_writer.py → client/data/templete.xlsx
    candidates.append(here.parent.parent / "data" / "templete.xlsx")
    candidates.append(Path.cwd() / "data" / "templete.xlsx")
    for c in candidates:
        if c.exists():
            return str(c)
    raise RuntimeError(
        "templete.xlsx 를 찾을 수 없습니다: " + ", ".join(str(c) for c in candidates))


# ── write ────────────────────────────────────────────────────────────────────

def write(result, out_path, sheets=None, colors=None, progress_cb=None,
          raw_data=None) -> str:
    """AnalysisResult 를 xlsx 로 저장. 반환: 저장 경로(str).

    sheets: 출력할 시트명 리스트/집합 (None 이면 전체). 알 수 없는 이름은 무시.
    colors: distribution Legend(소스)별 '#RRGGBB' 색 리스트 (None 이면 Excel 기본색).
    progress_cb: 시트 1개 생성 후 progress_cb(done, total, name) 호출 (선택).
    raw_data: (header, rows) 튜플. 주어지면 'Raw Data' 시트를 맨 앞에 추가.
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

    wb = openpyxl.load_workbook(_template_path())

    # distribution/_dist 는 Phase2(xlwings)에서 재생성 → openpyxl 단계에서 제거
    for nm in ("distribution", "_dist"):
        if nm in wb.sheetnames:
            del wb[nm]
    # 선택되지 않은 table 시트 제거
    for nm in list(wb.sheetnames):
        if nm in table_writers and nm not in sel:
            del wb[nm]

    total = len([s for s in sel if s in table_writers]) + (1 if raw_data is not None else 0) \
        + (1 if want_dist else 0)
    done = 0

    # table 시트 채움 (템플릿 순서 유지)
    for nm in ALL_SHEETS:
        if nm in table_writers and nm in sel and nm in wb.sheetnames:
            table_writers[nm](wb[nm], result)
            done += 1
            _progress(progress_cb, done, total, nm)

    # Raw Data (맨 앞)
    if raw_data is not None:
        ws = wb.create_sheet("Raw Data", 0)
        _fill_raw_data(ws, raw_data)
        done += 1
        _progress(progress_cb, done, total, "Raw Data")

    wb.save(out_path)

    # Phase 2: distribution 차트 (xlwings / Excel COM)
    if want_dist:
        try:
            _write_distribution_xlwings(out_path, result, colors)
            done += 1
            _progress(progress_cb, done, total, "distribution")
        except Exception as exc:
            # Excel/xlwings 미설치·실패 → distribution 만 생략(table 시트는 이미 저장됨)
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

def _fill_summary(ws, result):
    """summary 시트 고정 셀 채움 (3개 번호 섹션, 템플릿 라벨 유지)."""
    meta = result.meta
    title = " ".join(x for x in [meta.product_type, meta.product, meta.lot_id] if x).strip()

    feat = result.summary_feature()
    # 1. Device Feature 값행 (5행) — 라벨행(4행)은 템플릿 유지
    _safe_set(ws, "A1", title or "REPORT TITLE")
    _safe_set(ws, "B5", feat["Total DUT"])
    _safe_set(ws, "D5", feat["Pass (Bin 1)"])
    _safe_set(ws, "E5", feat["Fail Types"])
    _safe_set(ws, "F5", feat["Sources"])
    _safe_set(ws, "G5", feat["Subjects"])
    _safe_set(ws, "H5", feat["EVT Version"])

    # 2. Yield — Lot NO / Yield 값 (라벨행 8행 유지)
    _safe_set(ws, "B9", meta.lot_id or "-")
    _safe_set(ws, "D9", result.pass_yield if result.pass_yield is not None else "-")

    # Major Fail Bins: E9~E13 라벨(1st~5th Fail)은 유지, F=subject / G=ratio 채움
    majors = result.major_fail_subjects(5)
    for i in range(5):
        r = 9 + i
        _safe_set(ws, f"F{r}", majors[i]["subject"] if i < len(majors) else None)
        _safe_set(ws, f"G{r}", majors[i]["ratio"] if i < len(majors) else None)
    # 3. Evaluation Summary 는 템플릿 플레이스홀더("-") 그대로 둔다.


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
    header, rows = _yield_table(result)
    _fill_table(ws, header, rows)


def _fill_fail_item(ws, result):
    # 템플릿상 fail_item 은 yield 시트와 동일
    header, rows = _yield_table(result)
    _fill_table(ws, header, rows)


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


def _fill_issue_table(ws, result):
    """Category 그룹 레이아웃. Yield Category = yield 데이터 재사용, CPK/ETC 플레이스홀더."""
    src = result.sources
    header = ["Category", "bin", "Item", "avg"]
    for s in src:
        header += [f"{s}_count", f"{s}_yield"]
    header += ["Distribution", "comment", "개발 1차 comment",
               "PTE 2차 comment", "개발 2차 comment"]
    pad = len(header) - (4 + 2 * len(src))  # Distribution + comment 열 수

    rows = []
    first = True
    for r in result.yield_rows:
        row = ["Yield" if first else "", _bin_label(r.get("bin")),
               r.get("Main Fail subject", ""), r.get("avg")]
        for s in src:
            row += [r.get(f"{s}_count"), r.get(f"{s}_yield")]
        row += [""] * pad
        rows.append(row)
        first = False
    # CPK / ETC Category 섹션 (플레이스홀더 행)
    rows.append(["CPK"] + [""] * (len(header) - 1))
    rows.append(["ETC"] + [""] * (len(header) - 1))
    _fill_table(ws, header, rows)


def _fill_raw_data(ws, raw_data):
    """raw_data=(header, rows) 를 표로 기록 (제목 + 헤더 bold)."""
    from openpyxl.styles import Font
    header, rows = raw_data
    ws.cell(row=1, column=1, value="Raw Data").font = Font(bold=True, size=14)
    for ci, h in enumerate(header, start=1):
        ws.cell(row=2, column=ci, value=h).font = Font(bold=True)
    for ri, row in enumerate(rows, start=3):
        for ci, val in enumerate(row, start=1):
            ws.cell(row=ri, column=ci, value=_sanitize_cell(val))


# ── openpyxl 표 채움 헬퍼 (템플릿 스타일 복제) ───────────────────────────────

def _fill_table(ws, header, rows, header_row=_HEADER_ROW, start_col=_START_COL):
    """템플릿의 헤더/데이터 셀 스타일을 스탬프로 보존하며 표 영역을 새 값으로 교체."""
    # 1) 스타일 스탬프 캡처 (클리어 전)
    hdr_style = _capture_style(ws.cell(row=header_row, column=start_col))
    col_styles = [_capture_style(ws.cell(row=header_row + 1, column=start_col + i))
                  for i in range(len(header))]
    # 2) 표 영역 병합 해제 (제목 A1 병합 등 상단/좌측은 유지) → MergedCell 쓰기 오류 방지
    _unmerge_below(ws, header_row, start_col)
    # 3) 기존 표 영역 값 클리어
    max_r = ws.max_row
    max_c = max(ws.max_column, start_col + len(header) - 1)
    for r in range(header_row, max_r + 1):
        for c in range(start_col, max_c + 1):
            ws.cell(row=r, column=c).value = None
    # 3) 헤더 기입 + 스타일
    for i, h in enumerate(header):
        _apply_style(ws.cell(row=header_row, column=start_col + i, value=h), hdr_style)
    # 4) 데이터 기입 + 열별 스타일
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            cell = ws.cell(row=header_row + 1 + ri, column=start_col + ci,
                           value=_sanitize_cell(val))
            _apply_style(cell, col_styles[ci] if ci < len(col_styles) else None)


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


def _capture_style(cell):
    if cell is None or not cell.has_style:
        return None
    return {
        "font": copy(cell.font),
        "border": copy(cell.border),
        "fill": copy(cell.fill),
        "number_format": cell.number_format,
        "alignment": copy(cell.alignment),
        "protection": copy(cell.protection),
    }


def _apply_style(cell, st):
    if not st:
        return
    cell.font = st["font"]
    cell.border = st["border"]
    cell.fill = st["fill"]
    cell.number_format = st["number_format"]
    cell.alignment = st["alignment"]
    cell.protection = st["protection"]


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


# ── distribution (xlwings / Excel COM) ───────────────────────────────────────

def _write_distribution_xlwings(out_path, result, colors=None):
    """openpyxl 로 저장된 파일을 열어 distribution 시트 + 차트를 추가한다."""
    import xlwings as xw

    app = xw.App(visible=False, add_book=False)
    app.display_alerts = False
    app.screen_updating = False
    wb = None
    try:
        wb = app.books.open(out_path)
        last = wb.sheets[len(wb.sheets) - 1]
        sh = wb.sheets.add("distribution", after=last)
        _write_distribution(wb, sh, result, colors)
        sh.activate()
        wb.save()
    finally:
        try:
            if wb is not None:
                wb.close()
        finally:
            app.quit()


def _hex_to_excel_rgb(hex_color):
    """'#RRGGBB' → Excel COM RGB 정수 (R + G*256 + B*65536). 실패 시 None."""
    try:
        s = str(hex_color).strip().lstrip("#")
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return r + (g << 8) + (b << 16)
    except Exception:
        return None


def _apply_series_colors(ch, n_sources, colors):
    """차트의 소스 series(처음 n_sources 개) 선색을 팔레트로 지정."""
    if not colors or n_sources <= 0:
        return
    try:
        chart_com = getattr(ch.api, "Chart", ch.api)
        sc = chart_com.SeriesCollection()
    except Exception:
        return
    for i in range(n_sources):
        rgb = _hex_to_excel_rgb(colors[i % len(colors)])
        if rgb is None:
            continue
        try:
            s = sc.Item(i + 1)
            s.Format.Line.ForeColor.RGB = rgb
            s.Format.Line.Weight = 1.5
        except Exception:
            pass


def _write_distribution(wb, sh, result, colors=None):
    if not result.distributions:
        sh.range("A1").value = "선택된 항목에 분포 데이터가 없습니다."
        return

    data = wb.sheets.add("_dist", after=sh)
    cur = 1  # 헬퍼 시트 행 커서

    # 시트 제목 배너 (차트는 셀 위에 부유 — 배너 아래로 내려 자리 확보)
    _put_title(sh, 8, "Distribution")

    # 찾기(Ctrl+F) index 헤더 — 차트 그리드 오른쪽 열
    sh.range((1, _INDEX_COL)).value = "Item Index (Ctrl+F)"
    sh.range((1, _INDEX_COL)).column_width = 26

    for i, d in enumerate(result.distributions):
        header, rows, sources = _aligned_cdf_table(d)
        if not rows:
            continue
        ncol = 1 + len(sources)
        block = [header] + rows
        top_row = cur
        data.range((top_row, 1)).value = block
        bot_row = top_row + len(block) - 1
        src_range = data.range((top_row, 1), (bot_row, ncol))

        col = i % _CHARTS_PER_ROW
        grow = i // _CHARTS_PER_ROW
        left = _GAP + col * (_CHART_W + _GAP)
        top = _GAP + _DIST_TITLE_PX + grow * (_CHART_H + _GAP)

        ch = sh.charts.add(left, top, _CHART_W, _CHART_H)
        ch.set_source_data(src_range)
        ch.chart_type = "xy_scatter_lines_no_markers"
        _apply_series_colors(ch, len(sources), colors)
        title = d.subject + (f" ({d.unit})" if d.unit else "")
        _set_chart_title(ch, title)
        _add_limit_lines(ch, data, d, top_row, bot_row, ncol)
        _fix_cdf_axes(ch)

        # 차트 그리드 오른쪽에 item 명을 세로 5개씩, 차트 행에 정렬해 기록 (찾기용)
        idx_row = 2 + grow * _ROWS_PER_CHART + col
        sh.range((idx_row, _INDEX_COL)).value = f"[{col + 1}] {d.subject}"

        cur = bot_row + 2

    _finalize_title_row(sh)
    try:
        data.api.Visible = False  # 헬퍼 시트 숨김
    except Exception:
        pass


def _aligned_cdf_table(d):
    """source 별 CDF 를 공통 x 축으로 정렬한 테이블.

    Returns (header, rows, sources):
      header = ["value", src1, src2, ...]
      rows   = [[x, y_src1, y_src2, ...], ...]   (각 source 의 step-CDF)
    """
    sources, src_xs, src_ys = [], [], []
    for tr in d.traces:
        xs = np.asarray(tr["xs"], dtype=float)
        ys = np.asarray(tr["ys"], dtype=float)
        if xs.size == 0:
            continue
        sources.append(tr["source"])
        src_xs.append(xs)
        src_ys.append(ys)
    if not sources:
        return [], [], []

    union = np.unique(np.concatenate(src_xs))
    union, _ = _downsample(union, union)  # 점 수 제한
    header = ["value"] + sources
    rows = []
    for x in union:
        row = [float(x)]
        for xs, ys in zip(src_xs, src_ys):
            idx = int(np.searchsorted(xs, x, side="right")) - 1
            row.append(float(ys[idx]) if idx >= 0 else 0.0)
        rows.append(row)
    return header, rows, sources


def _add_limit_lines(ch, data, d, top_row, bot_row, ncol):
    """LSL/USL 세로 한계선을 2-point series 로 추가 (best-effort)."""
    com = ch.api
    chart_com = getattr(com, "Chart", com)
    pad_col = ncol + 2
    pr = top_row
    for label, lim in (("LSL", d.lower_limit), ("USL", d.upper_limit)):
        if lim is None:
            continue
        try:
            data.range((pr, pad_col)).value = [[float(lim), 0.0], [float(lim), 100.0]]
            x_rng = data.range((pr, pad_col), (pr + 1, pad_col)).api
            y_rng = data.range((pr, pad_col + 1), (pr + 1, pad_col + 1)).api
            s = chart_com.SeriesCollection().NewSeries()
            s.Values = y_rng
            s.XValues = x_rng
            s.Name = label
            pr += 3
        except Exception:
            pass


def _set_chart_title(ch, title):
    try:
        com = ch.api
        chart_com = getattr(com, "Chart", com)
        chart_com.HasTitle = True
        chart_com.ChartTitle.Text = title
    except Exception:
        pass


# Excel 축 상수: xlCategory(x)=1, xlValue(y)=2 / xlPrimary=1
def _fix_cdf_axes(ch):
    """y축(누적%)을 0~100 으로 고정. x축(value)은 자동."""
    try:
        com = ch.api
        chart_com = getattr(com, "Chart", com)
        y_axis = chart_com.Axes(2, 1)  # xlValue, xlPrimary
        y_axis.MinimumScale = 0
        y_axis.MaximumScale = 100
        y_axis.HasTitle = True
        y_axis.AxisTitle.Text = "Cumulative (%)"
        x_axis = chart_com.Axes(1, 1)  # xlCategory(=value for XY)
        x_axis.HasTitle = True
        x_axis.AxisTitle.Text = "value"
    except Exception:
        pass


def _downsample(xs, ys, max_points=_MAX_CDF_POINTS):
    if xs.size <= max_points:
        return xs, ys
    idx = np.unique(np.linspace(0, xs.size - 1, max_points).astype(int))
    return xs[idx], ys[idx]


# ── distribution 시트 제목 배너 (xlwings) ────────────────────────────────────

_XL_CENTER = -4108        # xlCenter
_TITLE_FILL = (191, 227, 255)
_TITLE_FONT_SIZE = 14
_TITLE_ROW_HEIGHT = 26


def _put_title(sh, ncols, text):
    """1행에 시트 제목 배너 — 하늘색 배경, 검은 bold, 큰 글씨, 가로 병합."""
    span = max(1, ncols)
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
    except Exception:
        pass


def _finalize_title_row(sh):
    """제목 행 높이 보정."""
    try:
        sh.range((1, 1)).row_height = _TITLE_ROW_HEIGHT
    except Exception:
        pass

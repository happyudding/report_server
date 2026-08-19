"""Excel Download 기입 계층 (XlsxWriter) — Excel 설치·COM 없이 xlsx 를 직접 만든다.

기존 `_sheets.py`(xlwings/Excel COM)와 **같은 서식 상수**(report_generator._xlsx_style)를
써서 결과물이 같아 보이게 하되, 구현은 완전히 다르다:

  - COM 왕복이 없어 셀·서식·이미지 기입이 빠르고 시간이 일정하다(멈춤 없음).
  - Excel 이 없는 PC 에서도 동작하고, Excel 상태(열려 있는 문서·추가 기능)의 영향을 안 받는다.
  - AutoFit 이 없으므로 열 너비·행 높이를 **웹 CSS 에서 유도한 고정값**으로 지정한다.
    PC 마다 결과가 달라지던 COM AutoFit 보다 오히려 일관적이다.

이 엔진에만 있는 web_report 파리티 보강 (COM 경로는 동결이라 종전 출력 그대로):
  Summary 의 Issue Status·Engr Comment · Yield 상단 요약 3표 · fail 빨강 그라데이션 ·
  Status 셀 Open/Close 색 · Compare 시트.

값 계산은 전부 `_extra.py`(순수 빌더)가 하고 여기서는 **기입만** 한다.
"""
from __future__ import annotations

import os
import shutil
import zipfile

from report_generator._xlsx_style import (
    _CPK_SERIES_COL_WIDTH,
    _CPK_TEST_NAME_COL_WIDTH,
    _DATA_FILL_RGB,
    _DATA_FONT,
    _HDR_FILL_RGB,
    _HDR_FONT,
    _HEADER_ROW,
    _ITEM_COL_WIDTH,
    _NARROW_COL_WIDTH,
    _START_COL,
    _SUMMARY_DATA_FONT,
    _SUMMARY_HDR_FILL_RGB,
    _SUMMARY_HDR_FONT,
    _SUMMARY_SECTION_FONT,
    _SUMMARY_TITLE_FILL_RGB,
    _TITLE_FILL_RGB,
    _TITLE_ROW_MAX_COL,
    _YIELD_HEADER_ROW_HEIGHT,
    _YIELD_TABLE_ROW_HEIGHT,
)

from . import _extra

# ── 서식 상수 (COM 경로와 같은 값) ──────────────────────────────────────────
_BANNER_FONT = {"name": "Tahoma", "bold": True, "size": 22}
_SESSION_LINK_FONT = {"name": "Calibri", "bold": True, "size": 12, "color": "#0563C1"}
_CPK_WARN_FILL = "FFF3B0"
_MAP_LEGEND_HEADER = ["Bin", "Description", "Count", "비율 (%)"]

# 이미지 배치 (COM 경로 _sheets 와 같은 상수 — 결과물 배치가 같아 보이도록)
_MAP_COLS_PER_ROW = 3
_MAP_PIC_W_PT = 500.0
_MAP_PIC_H_PT = 500.0
_PIC_GAP_PT = 24.0
_PIC_MARGIN_PT = 10.0

# 좌표 환산 — Excel 화면 좌표계는 96 DPI 고정이다.
_SCREEN_DPI = 96.0
_PT_PER_PX = 72.0 / _SCREEN_DPI          # 1px = 0.75pt
# 기본 행 높이/열 너비 (우리가 만드는 파일이라 Excel 로캘 기본값에 의존하지 않는다)
_DEFAULT_ROW_H_PT = 15.0
_DEFAULT_COL_W_CHARS = 8.43

_ANCHOR_ROW0 = _HEADER_ROW - 1           # B3 (0-index)
_ANCHOR_COL0 = _START_COL - 1
_SESSION_LINK_CELL0 = (0, 7)             # H1
_MAX_CELL_CHARS = 32000                  # xlsx 셀 상한(32767)에 여유


def _pt_to_px(pt):
    return float(pt) / _PT_PER_PX


def _col_width_px(chars):
    """열 너비(문자) → 픽셀 — Excel 의 Calibri 11 기준 환산식."""
    w = float(chars)
    return (w * 7.0 + 5.0) if w >= 1 else (w * 12.0)


def _safe(value):
    """수식 오인('=' 시작)·과대 문자열·NaN 방지. 반환은 xlsxwriter 가 그대로 쓸 값."""
    if isinstance(value, str):
        if value.startswith("="):
            value = "'" + value
        if len(value) > _MAX_CELL_CHARS:
            value = value[:_MAX_CELL_CHARS - 1] + "…"
        return value
    if isinstance(value, float):
        # NaN/Inf 는 xlsxwriter 가 오류 셀로 쓴다 — 빈 칸이 사용자에게 덜 혼란스럽다.
        if value != value or value in (float("inf"), float("-inf")):
            return None
    return value


class XlsxBook:
    """xlsx 1개를 만드는 동안의 상태 — 시트·서식 캐시·이미지 배치 정보를 들고 있다.

    메서드 이름은 `_sheets.py`(COM) 의 함수와 1:1 대응해 오케스트레이터가 엔진만 바꿔
    같은 순서로 부를 수 있게 했다.
    """

    def __init__(self, out_path, session_url="", *, chart_dpi=None):
        import xlsxwriter

        self.out_path = str(out_path)
        self.session_url = session_url or ""
        self.tmp_path = self.out_path + ".part"
        # nan_inf_to_errors: 혹시 _safe 를 빠져나간 NaN 이 있어도 예외 대신 오류 셀로.
        # strings_to_urls: 코멘트에 든 URL 문자열이 하이퍼링크 상한(65530)을 건드리지 않게.
        self.wb = xlsxwriter.Workbook(self.tmp_path, {
            "constant_memory": False,           # 병합·후행 서식이 필요해 일반 모드
            "nan_inf_to_errors": True,
            "strings_to_urls": False,
            "default_date_format": "yyyy-mm-dd",
        })
        self.wb.set_size(1800, 1000)
        self._fmt_cache = {}
        self._sheets = {}
        self._meta = {}                  # 시트별 배치 정보(행 높이 등)
        self._col_px = {}                # 명시 지정한 열 폭(px) — 썸네일 칸 맞춤에 쓴다
        self._img_count = 0
        # 차트 PNG 를 만든 렌더 DPI. Excel 화면은 96DPI 고정이라 그 비율만큼 축소해야
        # 물리 크기(pt)가 의도대로 유지된다.
        self._chart_dpi = float(chart_dpi) if chart_dpi else None

    # ── 서식 ────────────────────────────────────────────────────────────────
    def _fmt(self, **opts):
        key = tuple(sorted((k, str(v)) for k, v in opts.items()))
        fmt = self._fmt_cache.get(key)
        if fmt is None:
            fmt = self.wb.add_format(opts)
            self._fmt_cache[key] = fmt
        return fmt

    @staticmethod
    def _font(font, **extra):
        out = {}
        if font.get("name"):
            out["font_name"] = font["name"]
        if font.get("size"):
            out["font_size"] = font["size"]
        if font.get("bold"):
            out["bold"] = True
        color = font.get("color")
        if color:
            out["font_color"] = "#" + str(color).lstrip("#")[-6:]
        out.update(extra)
        return out

    def _cell_fmt(self, font, *, fill=None, border=False, center=True, wrap=False):
        opts = self._font(font)
        if fill:
            opts["bg_color"] = "#" + str(fill).lstrip("#")[-6:]
        if border:
            opts["border"] = 1
            opts["border_color"] = "#BFBFBF"
        if center:
            opts["align"] = "center"
        else:
            opts["align"] = "left"
        opts["valign"] = "vcenter"
        if wrap:
            opts["text_wrap"] = True
        return self._fmt(**opts)

    # ── 시트 ────────────────────────────────────────────────────────────────
    def add_sheets(self, names):
        for name in names:
            self._sheets[name] = self.wb.add_worksheet(name[:31])

    def sheet(self, name):
        return self._sheets[name]

    def has(self, name):
        return name in self._sheets

    def write_sheet_title(self, name, text=None, *, fill=_TITLE_FILL_RGB):
        """A1 제목 배너 (Tahoma 22 Bold) — 모든 시트 공통."""
        ws = self._sheets[name]
        fmt = self._cell_fmt(_BANNER_FONT, fill=fill, center=False)
        ws.merge_range(0, 0, 0, _TITLE_ROW_MAX_COL - 1, _safe(text or name), fmt)
        ws.set_row(0, 30)

    def add_session_link(self, name):
        """H1 에 세션 웹뷰 하이퍼링크 — 배너 위에 겹쳐 쓴다(COM 경로와 같은 위치)."""
        if not self.session_url:
            return
        ws = self._sheets[name]
        text = "▶ 웹에서 이 세션 열기"
        fmt = self._cell_fmt(_SESSION_LINK_FONT, fill=_TITLE_FILL_RGB, center=False)
        try:
            ws.write_url(_SESSION_LINK_CELL0[0], _SESSION_LINK_CELL0[1],
                         self.session_url, fmt, text)
        except Exception:
            ws.write(_SESSION_LINK_CELL0[0], _SESSION_LINK_CELL0[1],
                     _safe(f"{text}: {self.session_url}"), fmt)

    def _section_label(self, ws, row, text, *, font=_SUMMARY_SECTION_FONT):
        ws.write(row - 1, _START_COL - 1, _safe(text), self._cell_fmt(font, center=False))

    def _caption(self, ws, row, text):
        if not text:
            return
        ws.write(row - 1, _START_COL - 1, _safe(text),
                 self._cell_fmt({"name": "Calibri", "size": 10, "color": "#6B7280"},
                                center=False))

    def _table(self, ws, header, rows, *, header_row=_HEADER_ROW, start_col=_START_COL,
               hdr_font=_HDR_FONT, hdr_fill=_HDR_FILL_RGB, data_font=_DATA_FONT,
               wrap_cols=()):
        """헤더+데이터 기입. 반환: 마지막 데이터 행(1-index, 데이터가 없으면 헤더 행)."""
        hdr_fmt = self._cell_fmt(hdr_font, fill=hdr_fill, border=True, wrap=True)
        for i, name in enumerate(header):
            ws.write(header_row - 1, start_col - 1 + i, _safe(name), hdr_fmt)
        base = self._cell_fmt(data_font, fill=_DATA_FILL_RGB, border=True)
        wrapped = self._cell_fmt(data_font, fill=_DATA_FILL_RGB, border=True,
                                 center=False, wrap=True)
        wrap_idx = {header.index(c) for c in wrap_cols if c in header}
        for r, row in enumerate(rows or []):
            for c, value in enumerate(row):
                fmt = wrapped if c in wrap_idx else base
                ws.write(header_row + r, start_col - 1 + c, _safe(value), fmt)
        return header_row + len(rows or [])

    def _set_col_widths(self, ws, header, widths, *, default=None, start_col=_START_COL):
        for i, name in enumerate(header):
            w = widths.get(name, default)
            if w is not None:
                ws.set_column(start_col - 1 + i, start_col - 1 + i, w)

    def _fill_cells(self, ws, cells, rgb, value_of=None, *, font=_DATA_FONT):
        """(row, col) 목록의 배경색을 바꾼다.

        xlsxwriter 는 셀을 다시 쓰는 것 말고 서식만 바꾸는 방법이 없다 — 그래서 **원래
        값을 다시 기입**한다. value_of 를 주지 않으면 값이 지워지므로 반드시 넘길 것
        (값 소실은 이 프로젝트에서 가장 비싼 종류의 버그다).
        """
        fmt = self._cell_fmt(font, fill=rgb, border=True)
        for row, col in cells:
            value = value_of(row, col) if value_of else None
            ws.write(row - 1, col - 1, _safe(value), fmt)

    # ── Summary ─────────────────────────────────────────────────────────────
    def write_summary_sheet(self, yield_summary, fail_bin_rows, *,
                            issue_rows=None, temp_rows=None, summary_engr=None,
                            mode="Normal"):
        """①Yield 요약 ②Major Fail Bin ③Issue Status ④Engr Comment.

        ③④ 는 웹 Summary 탭에는 있는데 Excel 에 없던 카드다. 블록마다 예외를 격리해
        신규 블록이 실패해도 기존 2표는 남는다.
        """
        ws = self._sheets["Summary"]
        self.write_sheet_title("Summary", fill=_SUMMARY_TITLE_FILL_RGB)

        def table(header_row, header, rows, **kw):
            return self._table(ws, header, rows, header_row=header_row,
                               hdr_font=_SUMMARY_HDR_FONT, hdr_fill=_SUMMARY_HDR_FILL_RGB,
                               data_font=_SUMMARY_DATA_FONT, **kw)

        self._section_label(ws, 3, "Yield Summary")
        ys = yield_summary or {}
        y_rows = [[s.get("source"), s.get("pass"), s.get("fail"), s.get("total"),
                   s.get("yield_pct")] for s in (ys.get("by_source") or [])]
        y_rows.append(["Total", ys.get("pass"), ys.get("fail"), ys.get("total"),
                       ys.get("yield_pct")])
        last = table(4, ["Source", "Pass", "Fail", "Total", "Yield (%)"], y_rows)

        self._section_label(ws, last + 2, "Major Fail Bin")
        fb_rows = [[r.get("bin"), r.get("item"), r.get("yield_pct")]
                   for r in (fail_bin_rows or [])[:5]]
        last = table(last + 3, ["Bin", "Item", "Yield (%)"], fb_rows)

        try:
            status_rows = _extra.build_issue_status_rows(issue_rows, temp_rows, mode)
            self._section_label(ws, last + 2, "Issue Status")
            last = table(last + 3, ["구분", "Open", "Close", "진행률"], status_rows)
        except Exception:
            pass

        try:
            engr_rows = _extra.build_engr_rows(summary_engr, mode)
            self._section_label(ws, last + 2, "Engr Comment")
            start = last + 3
            last = table(start, ["구분", "Comment"], engr_rows, wrap_cols=("Comment",))
            for i, (_label, text) in enumerate(engr_rows):
                lines = str(text or "").count("\n") + 1
                ws.set_row(start + i, min(15.0 * max(1, lines), 220.0))
        except Exception:
            pass

        for col, w in ((2, 28), (3, 40), (4, 10), (5, 10), (6, 10)):
            ws.set_column(col - 1, col - 1, w)

    # ── Yield ───────────────────────────────────────────────────────────────
    def write_yield_sheet(self, yield_rows, yield_bin_groups, source_names,
                          step_groups=None, step_summary=None, *,
                          yield_summary=None, yield_basis=None, pass_row_builder=None):
        """웹 Yield 탭 구성 — 상단 요약 3표(신규) + STEP 별 Bin 표 + fail 그라데이션."""
        from ._sheets import yield_header, _yield_row_values, _step_pass_row_values

        ws = self._sheets["Yield"]
        self.write_sheet_title("Yield")
        header = yield_header(source_names)
        row = _HEADER_ROW

        try:
            row = self._write_yield_overview(ws, yield_summary, yield_basis, row)
        except Exception:
            row = _HEADER_ROW

        def one_table(start_row, rows, label=None):
            if label:
                self._section_label(ws, start_row, label)
                start_row += 1
            last = self._table(ws, header, rows, header_row=start_row)
            ws.set_row(start_row - 1, _YIELD_HEADER_ROW_HEIGHT)
            for i in range(len(rows)):
                ws.set_row(start_row + i, _YIELD_TABLE_ROW_HEIGHT)
            grads = _extra.build_yield_grad_cells(rows, header, header_row=start_row)
            self._apply_gradient(ws, grads, _matrix_value_of(rows, start_row, _START_COL))
            return last

        if step_groups:
            for sg in step_groups:
                label = str(sg.get("step") or "").strip() or "(기타)"
                rows = []
                pass_row = _step_pass_row_values(sg.get("step"), step_summary, source_names)
                if pass_row:
                    rows.append(pass_row)
                for group in sg.get("groups") or []:
                    rows.append(_yield_row_values(group.get("rep") or {}, source_names))
                row = one_table(row, rows, label=f"STEP {label}") + 3
        else:
            rows = []
            if yield_rows and str(yield_rows[0].get("bin")) == _extra.PASS_BIN:
                rows.append(_yield_row_values(yield_rows[0], source_names))
            for group in yield_bin_groups or []:
                rows.append(_yield_row_values(group.get("rep") or {}, source_names))
            one_table(row, rows)

        self._set_col_widths(ws, header, {"Item": 36}, default=_NARROW_COL_WIDTH * 1.6)

    def _write_yield_overview(self, ws, yield_summary, yield_basis, row):
        """웹 Yield 탭 상단 요약(전체 / STEP×Source / Source별) — sheets.js yieldOverviewHtml."""
        ov = _extra.build_yield_overview(yield_summary, yield_basis)
        if not ov.get("overall"):
            return row

        def block(start, title, spec, *, merges=None):
            self._section_label(ws, start, title)
            last = self._table(ws, spec["header"], spec["rows"], header_row=start + 1,
                               hdr_font=_SUMMARY_HDR_FONT, hdr_fill=_SUMMARY_HDR_FILL_RGB,
                               data_font=_SUMMARY_DATA_FONT, wrap_cols=("Step",))
            for r1, r2 in (merges or []):
                fmt = self._cell_fmt(_SUMMARY_DATA_FONT, fill=_DATA_FILL_RGB, border=True,
                                     wrap=True)
                value = spec["rows"][r1][0]
                ws.merge_range(start + 1 + r1, _START_COL - 1, start + 1 + r2,
                               _START_COL - 1, _safe(value), fmt)
            return last

        overall = ov["overall"]
        last = block(row, "Yield 요약", overall)
        if overall.get("caption"):
            self._caption(ws, last + 1, overall["caption"])
            last += 1
        if ov.get("by_step"):
            last = block(last + 2, "STEP 별 수율", ov["by_step"],
                         merges=ov["by_step"].get("merges"))
        if ov.get("by_source"):
            last = block(last + 2, "Source 별 수율", ov["by_source"])
        return last + 3

    def _apply_gradient(self, ws, grad_cells, value_of, *, font=_DATA_FONT):
        """비율별로 셀을 묶어 배경색 — 색 수는 _extra.GRAD_LEVELS 로 상한이 걸려 있다."""
        by_ratio = {}
        for (row, col), ratio in (grad_cells or {}).items():
            by_ratio.setdefault(ratio, []).append((row, col))
        for ratio, cells in by_ratio.items():
            self._fill_cells(ws, cells, _extra.grad_fill_rgb(ratio), value_of, font=font)

    # ── CPK ─────────────────────────────────────────────────────────────────
    def write_cpk_sheet(self, cpk_rows):
        from ._sheets import _CPK_HEADER, _CPK_LABEL_NCOL, _blank_repeated_labels

        ws = self._sheets["CPK"]
        self.write_sheet_title("CPK")
        rows, warn_offsets = [], []
        for r in cpk_rows or []:
            cpk = r.get("cpk")
            try:
                if cpk is not None and float(cpk) < _extra.CPK_THRESHOLD:
                    warn_offsets.append(len(rows))
            except (TypeError, ValueError):
                pass
            rows.append([r.get("subject"), r.get("lower_limit"), r.get("upper_limit"),
                         r.get("units"), r.get("source"), r.get("n"), r.get("min"),
                         r.get("median"), r.get("max"), r.get("average"), r.get("stdev"),
                         r.get("cpl"), r.get("cpu"), r.get("cp"), r.get("cpk"), ""])
        runs = _blank_repeated_labels(rows)
        self._table(ws, _CPK_HEADER, rows)
        warn_fmt = self._cell_fmt(_DATA_FONT, fill=_CPK_WARN_FILL, border=True)
        for off in warn_offsets:
            for c in range(len(_CPK_HEADER)):
                ws.write(_HEADER_ROW + off, _START_COL - 1 + c,
                         _safe(rows[off][c]), warn_fmt)
        # 같은 항목을 여러 계열이 공유하면 라벨 4열을 세로 병합 (값은 첫 행에만 남아 있다)
        base = self._cell_fmt(_DATA_FONT, fill=_DATA_FILL_RGB, border=True)
        for start, length in runs:
            if length < 2:
                continue
            r1 = _HEADER_ROW + start
            fmt = warn_fmt if start in warn_offsets else base
            for c in range(_CPK_LABEL_NCOL):
                ws.merge_range(r1, _START_COL - 1 + c, r1 + length - 1, _START_COL - 1 + c,
                               _safe(rows[start][c]), fmt)
        self._set_col_widths(ws, _CPK_HEADER, {
            "TEST NAME": _CPK_TEST_NAME_COL_WIDTH,
            "계열": _CPK_SERIES_COL_WIDTH,
            "n": _NARROW_COL_WIDTH * 1.05,
            "comment": 30,
        }, default=9.5)

    # ── Issue Table ─────────────────────────────────────────────────────────
    def write_issue_sheet(self, name, issue_rows, source_names, *, title=None):
        """웹 Issue Table 파리티 — fail 빨강 그라데이션 + Status Open/Close 셀 색 포함.

        반환 layout 은 COM 경로(_sheets.write_issue_sheet)와 같은 키를 갖는다
        (rows/map_rows/temp_rows/dist_col/map_col) — 썸네일 부착 코드를 공유하기 위해서.
        """
        from ._charts import ISSUE_MAP_IN, issue_cdf_pt_size

        ws = self._sheets[name]
        self.write_sheet_title(name, title or name)
        m = _extra.build_issue_matrix(issue_rows, source_names)
        header = m["header"]
        self._table(ws, header, m["rows"], wrap_cols=tuple(_extra.COMMENT_COLS))

        hdr_fmt = self._cell_fmt(_HDR_FONT, fill=_HDR_FILL_RGB, border=True, wrap=True)
        for row in m["subhead_rows"]:
            for c, value in enumerate(m["rows"][row - _HEADER_ROW - 1]):
                ws.write(row - 1, _START_COL - 1 + c, _safe(value), hdr_fmt)

        value_of = _matrix_value_of(m["rows"], _HEADER_ROW, _START_COL)
        self._apply_gradient(ws, m["grad_cells"], value_of)
        self._fill_cells(ws, m["cpk_cells"], _CPK_WARN_FILL, value_of)
        for (row, col), kind in m["status_cells"].items():
            fmt = self._cell_fmt(_DATA_FONT, fill=_extra.STATUS_FILL[kind], border=True)
            ws.write(row - 1, col - 1, _safe(value_of(row, col)), fmt)

        base = self._cell_fmt(_DATA_FONT, fill=_DATA_FILL_RGB, border=True)
        for r1, r2 in m["merges"]:
            ws.merge_range(r1 - 1, _START_COL - 1, r2 - 1, _START_COL - 1,
                           _safe(m["rows"][r1 - _HEADER_ROW - 1][0]), base)

        widths = {"Item": _ITEM_COL_WIDTH * 1.8, "Category": 10, "Status": 10}
        for col in _extra.COMMENT_COLS:
            widths[col] = 40
        self._set_col_widths(ws, header, widths, default=_NARROW_COL_WIDTH * 1.6)
        # Map/Distribution 열은 썸네일 물리 크기에 맞춘다 — AutoFit 이 없으니 직접 환산한다.
        dist_w_pt, dist_h_pt = issue_cdf_pt_size()
        self._set_px_col_width(ws, m["map_col"], _pt_to_px(ISSUE_MAP_IN * 72.0) + 6)
        self._set_px_col_width(ws, m["dist_col"], _pt_to_px(dist_w_pt) + 6)
        # 썸네일이 들어갈 행 높이 — 이미지가 칸을 넘지 않게 미리 확보.
        row_h = max(dist_h_pt, ISSUE_MAP_IN * 72.0) + 4
        for _item, excel_row, _section in m["rows_meta"]:
            ws.set_row(excel_row - 1, row_h)
        self._sheet_meta(name)["row_h"] = row_h
        return {"rows": m["rows_meta"], "map_rows": m["map_rows"],
                "temp_rows": m["temp_rows"], "dist_col": m["dist_col"],
                "map_col": m["map_col"]}

    def _set_px_col_width(self, ws, col, px):
        chars = max(0.5, (float(px) - 5.0) / 7.0)
        ws.set_column(col - 1, col - 1, chars)
        self._col_px[col] = float(px)

    # ── Compare ─────────────────────────────────────────────────────────────
    _MARK_FILL = {"bad": "FAD4D4", "warn": "FFF3B0", "good": "DCFCE7"}

    def write_compare_sheet(self, name, tables):
        ws = self._sheets[name]
        self.write_sheet_title(name)
        row = _HEADER_ROW
        wrote = False
        for key in ("equivalence", "dist_shift", "goodlog"):
            spec = (tables or {}).get(key)
            if not spec:
                continue
            wrote = True
            self._section_label(ws, row, spec["title"])
            self._caption(ws, row + 1, spec.get("caption"))
            header_row = row + 2
            last = self._table(ws, spec["header"], spec["rows"], header_row=header_row)
            for (r, c), kind in (spec.get("marks") or {}).items():
                fill = self._MARK_FILL.get(kind)
                if not fill:
                    continue
                fmt = self._cell_fmt(_DATA_FONT, fill=fill, border=True)
                ws.write(header_row + r, _START_COL - 1 + c, _safe(spec["rows"][r][c]), fmt)
            self._set_col_widths(ws, spec["header"], {"Item": 34, "subject": 34,
                                                      "after_item_name": 30,
                                                      "Before_item_name": 30},
                                 default=13)
            row = last + 3
        if not wrote:
            self._caption(ws, 3, "이 세션에는 Compare 데이터가 없습니다.")

    def write_sheet_error(self, name, exc):
        """시트 하나가 실패해도 파일 전체는 만든다 — 그 시트에 사유만 남긴다."""
        try:
            ws = self._sheets[name]
            fmt = self._cell_fmt({"name": "Calibri", "size": 12, "color": "#B42318"},
                                 center=False, wrap=True)
            ws.write(_HEADER_ROW - 1, _START_COL - 1,
                     _safe(f"⚠ 이 시트를 만들지 못했습니다: {exc} — 다른 시트는 정상입니다."),
                     fmt)
            ws.set_column(_START_COL - 1, _START_COL - 1, 120)
        except Exception:
            pass

    # ── 차트 이미지 ─────────────────────────────────────────────────────────
    def _sheet_meta(self, name):
        return self._meta.setdefault(name, {})

    def write_source_legend(self, name, source_colors, *, row=2):
        ws = self._sheets[name]
        col = _START_COL
        for src, color in source_colors:
            fmt = self._cell_fmt({"name": "Calibri", "size": 11, "bold": True,
                                  "color": color}, center=False)
            ws.write(row - 1, col - 1, _safe(f"■ {src}"), fmt)
            col += 3

    def chart_anchor(self, name):
        """이미지 배치 기준점 — B3 셀 기준의 (left_px, top_px) 오프셋. COM 의 pt 좌표 대응."""
        return (_pt_to_px(_PIC_MARGIN_PT), 0.0)

    def picture_stack_tops(self, heights_px, top0=0.0):
        """세로 연속 배치의 각 이미지 top(px) — 렌더 완료 순서와 무관하게 선계산."""
        tops, top = [], float(top0)
        gap_px = _pt_to_px(_PIC_GAP_PT)
        for h_px in heights_px:
            tops.append(top)
            top += self._display_px(h_px) + gap_px
        return tops

    def _display_px(self, render_px):
        """렌더 DPI 로 만든 픽셀 → Excel 화면(96DPI) 픽셀 — 물리 크기를 고정한다."""
        if not self._chart_dpi or self._chart_dpi == _SCREEN_DPI:
            return float(render_px)
        return float(render_px) * _SCREEN_DPI / self._chart_dpi

    def _image_scale(self):
        if not self._chart_dpi or self._chart_dpi == _SCREEN_DPI:
            return 1.0
        return _SCREEN_DPI / self._chart_dpi

    def add_picture_at(self, name, path, *, top, width_px, height_px, left=None):
        ws = self._sheets[name]
        scale = self._image_scale()
        ws.insert_image(_ANCHOR_ROW0, _ANCHOR_COL0, str(path), {
            "x_offset": int(_pt_to_px(_PIC_MARGIN_PT) if left is None else left),
            "y_offset": int(top),
            "x_scale": scale, "y_scale": scale,
            "object_position": 3,          # 셀 크기 변화와 무관하게 고정
        })
        self._img_count += 1

    def add_picture_in_cell(self, name, path, row, col, w_pt, h_pt):
        """(row, col) 칸 안에 썸네일 — 목표 물리크기(pt)로 맞추고, 칸이 좁으면 비율 유지 축소.

        썸네일 PNG 는 고해상도(ISSUE_DPI)라 파일 픽셀과 표시 픽셀이 다르다. 파일에서 실제
        픽셀을 읽어 배율을 정하므로 렌더 DPI 가 바뀌어도 표시 크기는 그대로다.
        """
        ws = self._sheets[name]
        cell_w = self._col_px.get(col, _col_width_px(_DEFAULT_COL_W_CHARS))
        cell_h = _pt_to_px(self._meta.get(name, {}).get("row_h", _DEFAULT_ROW_H_PT))
        target_w, target_h = _pt_to_px(w_pt), _pt_to_px(h_pt)
        fit = max(min((cell_w - 2) / target_w, (cell_h - 2) / target_h, 1.0), 0.05)
        disp_w, disp_h = target_w * fit, target_h * fit
        try:
            img_w, img_h = _png_size(path)
        except Exception:
            img_w, img_h = target_w, target_h
        ws.insert_image(row - 1, col - 1, str(path), {
            "x_offset": max(0, int((cell_w - disp_w) / 2)),
            "y_offset": max(0, int((cell_h - disp_h) / 2)),
            "x_scale": disp_w / img_w if img_w else 1.0,
            "y_scale": disp_h / img_h if img_h else 1.0,
            "object_position": 1,
        })
        self._img_count += 1

    def write_hidden_item_index(self, name, entries, tops, *, left=None, top=None):
        """차트가 덮는 셀에 항목명을 흰 글씨로 — Ctrl+F 로 차트를 찾게 한다(COM 경로와 동일 의도)."""
        from ._charts import NCOLS, cell_pt_size

        if not entries:
            return
        ws = self._sheets[name]
        cell_w_px, cell_h_px = (_pt_to_px(v) for v in cell_pt_size())
        row_px = _pt_to_px(_DEFAULT_ROW_H_PT)
        col_px = _col_width_px(_DEFAULT_COL_W_CHARS)
        left_px = _pt_to_px(_PIC_MARGIN_PT) if left is None else float(left)
        fmt = self._cell_fmt({"name": "Calibri", "size": 8, "color": "#FFFFFF"},
                             center=False)
        placed = {}
        for chunk_idx, cell_idx, subject in entries:
            if chunk_idx >= len(tops):
                continue
            r, c = divmod(int(cell_idx), NCOLS)
            y_px = tops[chunk_idx] + (r + 0.45) * cell_h_px
            x_px = left_px + (c + 0.08) * cell_w_px
            row = max(_HEADER_ROW, _HEADER_ROW + int(y_px // row_px))
            col = int(x_px // col_px) + 1
            placed.setdefault((row, col), str(subject))
        for (row, col), subject in placed.items():
            ws.write(row - 1, col - 1, _safe(subject), fmt)

    def add_map_grid(self, name, labeled_pngs, *, left=None, top=None):
        """wafer map PNG 3열 그리드 (COM 경로와 같은 500x500pt 배치)."""
        ws = self._sheets[name]
        left_px = _pt_to_px(_PIC_MARGIN_PT) if left is None else float(left)
        top_px = 0.0 if top is None else float(top)
        w_px, h_px = _pt_to_px(_MAP_PIC_W_PT), _pt_to_px(_MAP_PIC_H_PT)
        gap_px = _pt_to_px(_PIC_GAP_PT)
        for idx, (_label, path) in enumerate(labeled_pngs):
            if not path or not os.path.exists(str(path)):
                continue
            c, r = idx % _MAP_COLS_PER_ROW, idx // _MAP_COLS_PER_ROW
            try:
                iw, ih = _png_size(path)
            except Exception:
                iw = ih = 0
            scale_x = (w_px / iw) if iw else 1.0
            scale_y = (h_px / ih) if ih else 1.0
            scale = min(scale_x, scale_y) or 1.0
            ws.insert_image(_ANCHOR_ROW0, _ANCHOR_COL0, str(path), {
                "x_offset": int(left_px + c * (w_px + gap_px)),
                "y_offset": int(top_px + r * (h_px + gap_px)),
                "x_scale": scale, "y_scale": scale,
                "object_position": 3,
            })
            self._img_count += 1

    def write_map_legend(self, name, legend_rows, desc_map, color_map, n_maps, *, left=None,
                         header_row=_HEADER_ROW):
        """맵 그리드 우측 Bin Legend 표 — 웹 binLegendHtml(Bin/Description/Count/비율) 파리티."""
        if not legend_rows:
            return
        ws = self._sheets[name]
        left_px = _pt_to_px(_PIC_MARGIN_PT) if left is None else float(left)
        used_cols = min(_MAP_COLS_PER_ROW, max(1, n_maps))
        right_px = left_px + used_cols * (_pt_to_px(_MAP_PIC_W_PT) + _pt_to_px(_PIC_GAP_PT))
        start_col = int(right_px // _col_width_px(_DEFAULT_COL_W_CHARS)) + 2
        desc = desc_map or {}
        rows = [[f"{r['bin']} (Pass)" if r.get("is_pass") else r["bin"],
                 "" if r.get("is_pass") else desc.get(str(r["bin"]), ""),
                 r.get("count"), r.get("pct")] for r in legend_rows]
        self._table(ws, _MAP_LEGEND_HEADER, rows, header_row=header_row,
                    start_col=start_col)
        for i, r in enumerate(legend_rows):
            color = (color_map or {}).get(str(r["bin"]))
            if color:
                fmt = self._cell_fmt(_DATA_FONT, fill=str(color).lstrip("#"), border=True)
                ws.write(header_row + i, start_col - 1, _safe(rows[i][0]), fmt)
        self._set_col_widths(ws, _MAP_LEGEND_HEADER,
                             {"Bin": 12, "Description": 34, "Count": 10, "비율 (%)": 10},
                             start_col=start_col)

    # ── 저장 ────────────────────────────────────────────────────────────────
    def close(self, *, activate="Summary"):
        """임시 파일로 저장 → zip/시트/이미지 검증 → 최종 경로로 원자 교체."""
        if activate in self._sheets:
            self._sheets[activate].activate()
        self.wb.close()
        verify_xlsx(self.tmp_path, expect_sheets=list(self._sheets),
                    expect_images=self._img_count)
        os.replace(self.tmp_path, self.out_path)
        return self.out_path

    def abort(self):
        try:
            self.wb.close()
        except Exception:
            pass
        for path in (self.tmp_path,):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


def _matrix_value_of(rows, header_row, start_col):
    """(excel_row, excel_col) → 이미 기입한 값. 색만 바꿔 다시 쓸 때 값 소실을 막는다."""
    def value_of(row, col):
        r, c = row - header_row - 1, col - start_col
        if 0 <= r < len(rows) and 0 <= c < len(rows[r]):
            return rows[r][c]
        return None
    return value_of


def _png_size(path):
    """PNG 헤더에서 (width, height) — Pillow 없이 stdlib 만으로."""
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 아님")
    return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))


def verify_xlsx(path, *, expect_sheets=None, expect_images=0):
    """만들어진 xlsx 가 열리는 파일인지 확인 — zip 구조 + 시트 수 + 이미지 수.

    Excel 없이 검증한다(서버·클라 어디서도 Excel/openpyxl 을 쓰지 않는다는 규칙과 같은 이유).
    깨진 파일을 사용자에게 건네는 것보다 여기서 실패해 폴백으로 넘기는 편이 낫다.
    """
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        raise RuntimeError("xlsx 파일이 생성되지 않았습니다")
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        if bad:
            raise RuntimeError(f"xlsx 압축이 손상됐습니다: {bad}")
        names = set(zf.namelist())
        for required in ("[Content_Types].xml", "xl/workbook.xml"):
            if required not in names:
                raise RuntimeError(f"xlsx 필수 파트 누락: {required}")
        n_sheets = sum(1 for n in names if n.startswith("xl/worksheets/sheet"))
        if expect_sheets and n_sheets < len(expect_sheets):
            raise RuntimeError(f"시트 수 불일치: {n_sheets} < {len(expect_sheets)}")
        n_media = sum(1 for n in names if n.startswith("xl/media/"))
        if expect_images and n_media == 0:
            raise RuntimeError("차트 이미지가 하나도 포함되지 않았습니다")
    return True

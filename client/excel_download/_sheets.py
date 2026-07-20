"""Excel Download 시트 작성기 — web_report payload dict 를 직접 소비 (xlwings).

서식(색·폰트·헤더행 위치·경고 하이라이트)은 honey excel 과 동일하게 보이도록
report_generator._xlsx_style 의 상수/헬퍼를 그대로 재사용한다. 기존 _fill_*
(AnalysisResult 강결합)는 호출하지 않는다. 셀 단위 COM 왕복을 피하기 위해 값은
2-D 배열 일괄 기입, 스타일은 Range 단위 1회 적용한다.
"""
from __future__ import annotations

from report_generator._xlsx_style import (
    _CPK_TEST_NAME_COL_WIDTH,
    _CPK_SERIES_COL_WIDTH,
    _HEADER_ROW,
    _ITEM_COL_WIDTH,
    _NARROW_COL_WIDTH,
    _START_COL,
    _SUMMARY_DATA_FONT,
    _SUMMARY_HDR_FILL_RGB,
    _SUMMARY_HDR_FONT,
    _SUMMARY_SECTION_FONT,
    _SUMMARY_TITLE_FILL_RGB,
    _SUMMARY_TITLE_FONT,
    _TITLE_FILL_RGB,
    _TITLE_FONT,
    _TITLE_ROW_MAX_COL,
    _XL_CENTER,
    _YIELD_HEADER_ROW_HEIGHT,
    _YIELD_TABLE_ROW_HEIGHT,
    _col_letter,
    _data_range,
    _hdr_range,
    _style_range,
)

_CPK_WARN_FILL_RGB = "FFFFF3B0"   # cpk < 1.33 경고(연노랑) — honey excel 과 동일 계열
_CPK_THRESHOLD = 1.33
_PASS_BIN = "1"
_COMMENT_COLS = ["PTE comment", "개발 comment"]
_ADDR_JOIN_MAXLEN = 200           # Range("A1:...,A2:...") 주소 문자열 상한

# PNG 부착 배치 (map_report.write_map_sheet 와 동일 상수)
_MAP_COLS_PER_ROW = 3
_MAP_PIC_W = 500
_MAP_PIC_H = 500
_PIC_GAP = 24
_PIC_MARGIN = 10
_PIC_TOP_START = _PIC_MARGIN + 30  # 상단 범례 행 아래부터

# msoFalse / msoTrue (Shapes.AddPicture 인자)
_MSO_FALSE = 0
_MSO_TRUE = -1


def _png_pt_per_px():
    from ._charts import DPI
    return 72.0 / DPI              # 렌더 DPI 기준 원본 물리 크기 유지


def _safe(v):
    """수식 오인('=' 시작 문자열) 방지."""
    if isinstance(v, str) and v.startswith("="):
        return "'" + v
    return v


def _title_banner(ws, text, *, font=_TITLE_FONT, fill=_TITLE_FILL_RGB):
    ws.range((1, 1)).value = _safe(text)
    _style_range(ws.range((1, 1), (1, _TITLE_ROW_MAX_COL)), fill=fill, font=font)


# 세션 웹뷰 링크 — 모든 시트 상단에 삽입. 제목 배너 있는 시트는 배너(연파랑) 안 빈 칸,
# 없는 시트(Distribution/Histogram/Map)는 흰 셀. H1 은 제목 텍스트 우측·화면에 보이는 위치.
_SESSION_LINK_CELL = (1, 8)   # H1
_SESSION_LINK_FONT = {"name": "Calibri", "bold": True, "size": 12, "color": "FF0563C1"}


def add_session_link(ws, url, *, text="▶ 웹에서 이 세션 열기"):
    """시트 상단(H1)에 세션 웹뷰 하이퍼링크 — 클릭 시 기본 브라우저로 /pe/report/view/<sid>."""
    if not url:
        return
    cell = ws.range(_SESSION_LINK_CELL)
    try:
        # 위치 인자(Anchor, Address, SubAddress, ScreenTip, TextToDisplay) — pywin32 호환.
        ws.api.Hyperlinks.Add(cell.api, str(url), "", str(text), str(text))
    except Exception:
        cell.value = _safe(f"{text}: {url}")
    _style_range(cell, font=_SESSION_LINK_FONT)


def _write_table(ws, header, rows, *, header_row=_HEADER_ROW, start_col=_START_COL):
    """헤더+데이터 일괄 기입 + honey 공통 스타일. 반환: 마지막 데이터 행 번호."""
    ncol = len(header)
    ws.range((header_row, start_col)).value = [list(header)]
    _hdr_range(ws, header_row, start_col, start_col + ncol - 1)
    if rows:
        data = [[_safe(v) for v in row] for row in rows]
        ws.range((header_row + 1, start_col)).value = data
        _data_range(ws, header_row + 1, start_col, header_row + len(rows), start_col + ncol - 1)
    return header_row + len(rows)


def _set_col_widths(ws, header, widths, *, default=None, start_col=_START_COL):
    for i, name in enumerate(header):
        w = widths.get(name, default)
        if w is not None:
            ws.range((1, start_col + i)).column_width = w


# ── Summary ──────────────────────────────────────────────────────────────────

def write_summary_sheet(ws, yield_summary, fail_bin_rows):
    """①Yield 요약(source 별 + Total) ②Major Fail Bin 랭킹 — 표 2개."""
    _title_banner(ws, "Summary", font=_SUMMARY_TITLE_FONT, fill=_SUMMARY_TITLE_FILL_RGB)

    def _section(row, text):
        ws.range((row, _START_COL)).value = _safe(text)
        _style_range(ws.range((row, _START_COL)), font=_SUMMARY_SECTION_FONT)

    def _table(header_row, header, rows):
        ncol = len(header)
        ws.range((header_row, _START_COL)).value = [list(header)]
        _style_range(ws.range((header_row, _START_COL), (header_row, _START_COL + ncol - 1)),
                     fill=_SUMMARY_HDR_FILL_RGB, font=_SUMMARY_HDR_FONT,
                     halign=_XL_CENTER, valign=_XL_CENTER, wrap=True, border=True)
        if rows:
            ws.range((header_row + 1, _START_COL)).value = [[_safe(v) for v in r] for r in rows]
            _style_range(ws.range((header_row + 1, _START_COL),
                                  (header_row + len(rows), _START_COL + ncol - 1)),
                         font=_SUMMARY_DATA_FONT, halign=_XL_CENTER, valign=_XL_CENTER,
                         border=True)
        return header_row + len(rows)

    _section(3, "Yield Summary")
    ys = yield_summary or {}
    y_rows = [[s.get("source"), s.get("pass"), s.get("fail"), s.get("total"), s.get("yield_pct")]
              for s in (ys.get("by_source") or [])]
    y_rows.append(["Total", ys.get("pass"), ys.get("fail"), ys.get("total"), ys.get("yield_pct")])
    last = _table(4, ["Source", "Pass", "Fail", "Total", "Yield (%)"], y_rows)

    _section(last + 2, "Major Fail Bin")
    # 상위 5개 bin 만 · Bin/Item/Yield (웹 summary yield 와 동일 — count desc 정렬 유지, Count 열 생략)
    fb_rows = [[r.get("bin"), r.get("item"), r.get("yield_pct")]
               for r in (fail_bin_rows or [])[:5]]
    _table(last + 3, ["Bin", "Item", "Yield (%)"], fb_rows)

    for col, w in ((2, 28), (3, 40), (4, 10), (5, 10), (6, 10)):
        ws.range((1, col)).column_width = w


# ── Yield (TNO 접힌 형태) ────────────────────────────────────────────────────

def yield_header(source_names):
    return (["Step", "Bin", "TNO", "Item", "avg (%)"]
            + [f"{s} (%)" for s in source_names]
            + [f"{s} count" for s in source_names])


def _yield_row_values(row, source_names):
    return ([row.get("step"), row.get("bin"), row.get("TNO"), row.get("Item"), row.get("avg")]
            + [row.get(f"{s}_yield") for s in source_names]
            + [row.get(f"{s}_count") for s in source_names])


def write_yield_sheet(ws, yield_rows, yield_bin_groups, source_names):
    """Pass 행 + Bin 별 대표(총합) 행만 — 웹 Yield 탭의 접힌 상태와 동일."""
    _title_banner(ws, "Yield")
    header = yield_header(source_names)
    rows = []
    if yield_rows and str(yield_rows[0].get("bin")) == _PASS_BIN:
        rows.append(_yield_row_values(yield_rows[0], source_names))
    for group in yield_bin_groups or []:
        rows.append(_yield_row_values(group.get("rep") or {}, source_names))
    _write_table(ws, header, rows)
    _set_col_widths(ws, header, {"Item": 36}, default=_NARROW_COL_WIDTH * 1.6)
    ws.range((_HEADER_ROW, 1)).row_height = _YIELD_HEADER_ROW_HEIGHT
    if rows:
        ws.range((_HEADER_ROW + 1, 1), (_HEADER_ROW + len(rows), 1)).row_height = \
            _YIELD_TABLE_ROW_HEIGHT


# ── CPK (전체 기준, honey excel 서식) ────────────────────────────────────────

_CPK_HEADER = ["TEST NAME", "LOW SPEC", "HIGH SPEC", "SCALE", "계열", "n",
               "min", "median", "max", "average", "stdev",
               "cpl", "cpu", "cp", "cpk", "comment"]


def write_cpk_sheet(ws, cpk_rows):
    """subject × source 행 그대로 (전체(all-die) 기준 컬럼, *_bin1 무시)."""
    _title_banner(ws, "CPK")
    rows = []
    warn_offsets = []
    for r in cpk_rows or []:
        cpk = r.get("cpk")
        try:
            if cpk is not None and float(cpk) < _CPK_THRESHOLD:
                warn_offsets.append(len(rows))
        except (TypeError, ValueError):
            pass
        rows.append([
            r.get("subject"), r.get("lower_limit"), r.get("upper_limit"),
            r.get("units"), r.get("source"), r.get("n"), r.get("min"),
            r.get("median"), r.get("max"), r.get("average"), r.get("stdev"),
            r.get("cpl"), r.get("cpu"), r.get("cp"), r.get("cpk"), "",
        ])
    _blank_repeated_labels(rows)
    _write_table(ws, _CPK_HEADER, rows)
    _apply_warn_fill(ws, warn_offsets, len(_CPK_HEADER))
    _set_col_widths(ws, _CPK_HEADER, {
        "TEST NAME": _CPK_TEST_NAME_COL_WIDTH,
        "계열": _CPK_SERIES_COL_WIDTH,
        "n": _NARROW_COL_WIDTH * 1.05,
        "comment": 30,
    }, default=9.5)


def _blank_repeated_labels(rows):
    """같은 subject 연속 행의 TEST NAME/SPEC/SCALE 반복 생략 (honey excel 과 동일)."""
    prev_key = None
    for row in rows:
        key = tuple(row[:4])
        if key == prev_key:
            row[0:4] = ["", "", "", ""]
        else:
            prev_key = key


def _apply_warn_fill(ws, row_offsets, ncol, *, header_row=_HEADER_ROW, start_col=_START_COL):
    """cpk < 1.33 행 전체 노란 하이라이트 — 연속 행은 병합, 주소 join 으로 COM 호출 최소화."""
    if not row_offsets:
        return
    c1 = _col_letter(start_col)
    c2 = _col_letter(start_col + ncol - 1)
    # 연속 offset 을 (start, end) 구간으로 병합
    spans = []
    s = e = row_offsets[0]
    for off in row_offsets[1:]:
        if off == e + 1:
            e = off
        else:
            spans.append((s, e))
            s = e = off
    spans.append((s, e))

    def _flush(addresses):
        if addresses:
            _style_range(ws.range(",".join(addresses)), fill=_CPK_WARN_FILL_RGB)

    addresses = []
    length = 0
    for s, e in spans:
        r1 = header_row + 1 + s
        r2 = header_row + 1 + e
        address = f"{c1}{r1}:{c2}{r2}"
        next_length = length + len(address) + (1 if addresses else 0)
        if addresses and next_length > _ADDR_JOIN_MAXLEN:
            _flush(addresses)
            addresses = []
            next_length = len(address)
        addresses.append(address)
        length = next_length
    _flush(addresses)


# ── Issue Table (접힌 형태 + 숨은 comment 를 bin 별로 묶어 나열) ─────────────

def write_issue_sheet(ws, issue_rows, source_names):
    """rep/서브헤더/ETC 행만 표시. 접힌 detail 행의 comment 는 같은 bin(rep) 행의
    comment 셀에 "<Item>: <comment>" 줄로 묶어 나열한다 (rep 자신의 comment 가 맨 위).

    source yields 뒤 · comment 앞에 'Distribution' 열을 추가한다(값은 비움). 행별 CDF PNG
    는 오케스트레이터가 add_picture_in_cell 로 부착한다.
    반환: {"rows": [(item, excel_row, section), ...], "dist_col": Distribution 열 인덱스}.
    section 은 "Yield"/"CPK"/"ETC" — CPK 섹션 썸네일만 규격창 재정규화(웹과 동일 기준).
    """
    _title_banner(ws, "Issue Table")
    header = (["Category", "Step", "Bin", "TNO", "Item", "avg"]
              + list(source_names) + ["Distribution"] + list(_COMMENT_COLS))

    # _grp 별 detail comment 수집
    detail_comments = {}
    for r in issue_rows or []:
        if not r.get("_detail"):
            continue
        grp = r.get("_grp")
        for col in _COMMENT_COLS:
            text = str(r.get(col) or "").strip()
            if text:
                detail_comments.setdefault((grp, col), []).append(
                    f"{r.get('Item')}: {text}")

    rows = []
    item_rows = []
    section = ""
    for r in issue_rows or []:
        if r.get("_detail"):
            continue
        # Category 는 섹션 개시행에만 채워진다 (build_issue_table_rows) — 이후 행은 승계.
        if r.get("Category") in ("Yield", "CPK", "ETC"):
            section = r["Category"]
        vals = [r.get("Category"), r.get("Step"), r.get("Bin"), r.get("TNO"),
                r.get("Item"), r.get("avg")]
        vals += [r.get(f"{s}_yield") for s in source_names]
        vals.append("")                      # Distribution PNG 자리(비움)
        for col in _COMMENT_COLS:
            parts = []
            own = str(r.get(col) or "").strip()
            if own:
                parts.append(own)
            parts.extend(detail_comments.get((r.get("_grp"), col), []))
            vals.append("\n".join(parts))
        item_rows.append((r.get("Item"), _HEADER_ROW + 1 + len(rows), section))
        rows.append(vals)

    _write_table(ws, header, rows)
    dist_col = _START_COL + 6 + len(source_names)
    widths = {"Item": _ITEM_COL_WIDTH * 1.8, "Category": 10, "Distribution": 28}
    for col in _COMMENT_COLS:
        widths[col] = 40
    _set_col_widths(ws, header, widths, default=_NARROW_COL_WIDTH * 1.6)
    return {"rows": item_rows, "dist_col": dist_col}


# ── PNG 부착 (Distribution / Histogram / Map Analysis) ──────────────────────

def write_source_legend(ws, source_colors, *, row=2):
    """시트 상단에 source ↔ 색 범례를 1행으로 (차트 셀 안 범례 생략을 보완)."""
    col = _START_COL
    for name, color in source_colors:
        ws.range((row, col)).value = _safe(f"■ {name}")
        _style_range(ws.range((row, col)),
                     font={"name": "Calibri", "size": 11, "bold": True,
                           "color": "FF" + color.lstrip("#").upper()})
        col += 3


def _add_picture(ws, path, left, top, w_pt, h_pt):
    """raw COM Shapes.AddPicture — xlwings pictures.add 대비 ~20% 빠르다(픽셀 비례 비용)."""
    ws.api.Shapes.AddPicture(str(path), _MSO_FALSE, _MSO_TRUE,
                             float(left), float(top), float(w_pt), float(h_pt))


def picture_stack_tops(heights_px):
    """세로 연속 배치의 각 PNG top(pt) 목록 — 완료 순서와 무관하게 부착 가능하도록 선계산."""
    ppp = _png_pt_per_px()
    tops = []
    top = float(_PIC_TOP_START)
    for h_px in heights_px:
        tops.append(top)
        top += h_px * ppp + _PIC_GAP
    return tops


def add_picture_at(ws, path, *, top, width_px, height_px):
    """선계산된 top 위치에 PNG 1장 부착 (as_completed 파이프라인용)."""
    ppp = _png_pt_per_px()
    _add_picture(ws, path, _PIC_MARGIN, top, width_px * ppp, height_px * ppp)


def add_picture_in_cell(ws, path, row, col, w_pt, h_pt):
    """지정 (row,col) 셀 좌상단에 PNG 를 지정 물리크기(pt)로 부착 (Issue Table 행별 CDF).

    썸네일은 렌더 DPI 가 시트 차트와 달라(고해상도) 크기를 호출부가 pt 로 넘긴다.
    행 높이가 PNG 보다 낮으면 PNG 높이에 맞춰 넓힌다(긴 comment 로 이미 높은 행은 유지).
    """
    row_rng = ws.range((row, 1))
    if row_rng.row_height < h_pt + 4:
        row_rng.row_height = h_pt + 4
    cell = ws.range((row, col))
    _add_picture(ws, path, cell.left, cell.top, w_pt, h_pt)


_HIDDEN_INDEX_FONT = {"name": "Calibri", "size": 8, "color": "FFFFFFFF"}  # 흰 글씨 = 화면에 안 보임
_HIDDEN_INDEX_MIN_ROW = 3          # 세션링크(H1)·source 범례(row2) 보호


def write_hidden_item_index(ws, entries, tops):
    """차트 이미지가 덮는 셀에 항목명을 흰 글씨로 기입 — Ctrl+F 로 차트를 찾게 한다.

    Distribution/Histogram 시트는 항목명이 PNG 픽셀 안에만 있어 검색이 안 된다. 그림 아래
    셀에 텍스트를 심으면 Ctrl+F 가 해당 차트 위치로 점프한다(흰 글씨 + 그림에 가려 비가시).

    entries: [(chunk_idx, cell_idx, subject)] — add_picture_at 과 동일한 tops 를 넘겨야
    좌표가 어긋나지 않는다. 이 시트들은 행높이·열너비를 바꾸지 않으므로(기본값·균일)
    pt→(row,col) 은 단순 나눗셈이다. 값·스타일 모두 범위 1회 적용(COM 왕복 최소).
    """
    from ._charts import NCOLS, cell_pt_size

    if not entries:
        return
    cell_w_pt, cell_h_pt = cell_pt_size()
    row_h = float(ws.api.StandardHeight)          # 한국어 Excel 기본 16.5pt 등 — 가정 금지
    col_w = float(ws.range((1, 1)).width)
    placed = {}
    for chunk_idx, cell_idx, subject in entries:
        r, c = divmod(int(cell_idx), NCOLS)
        y_pt = tops[chunk_idx] + (r + 0.45) * cell_h_pt   # 셀 중앙 부근 = 점프 시 차트가 화면에 걸림
        x_pt = _PIC_MARGIN + (c + 0.08) * cell_w_pt
        row = max(_HIDDEN_INDEX_MIN_ROW, int(y_pt // row_h) + 1)
        col = int(x_pt // col_w) + 1
        placed.setdefault((row, col), str(subject))       # 충돌 시 먼저 온 항목 유지
    rows = [r for r, _ in placed]
    cols = [c for _, c in placed]
    r_min, r_max, c_max = min(rows), max(rows), max(cols)
    block = [[None] * c_max for _ in range(r_max - r_min + 1)]
    for (row, col), subject in placed.items():
        block[row - r_min][col - 1] = _safe(subject)
    rng = ws.range((r_min, 1), (r_max, c_max))
    rng.value = block
    _style_range(rng, font=_HIDDEN_INDEX_FONT)


def add_map_grid(ws, labeled_pngs):
    """wafer map PNG 들을 가로 3개 그리드로 부착 (honey excel Map 시트와 동일 배치)."""
    for idx, (label, path) in enumerate(labeled_pngs):
        col = idx % _MAP_COLS_PER_ROW
        row = idx // _MAP_COLS_PER_ROW
        left = _PIC_MARGIN + col * (_MAP_PIC_W + _PIC_GAP)
        top = _PIC_MARGIN + row * (_MAP_PIC_H + _PIC_GAP)
        _add_picture(ws, path, left, top, _MAP_PIC_W, _MAP_PIC_H)


_MAP_LEGEND_HEADER = ["Bin", "Description", "Count", "비율 (%)"]
_MAP_LEGEND_ROW = 2                # 맵 그리드 상단과 나란히


def write_map_legend(ws, legend_rows, desc_map, color_map, n_maps):
    """맵 그리드 우측에 Bin Legend 표 — 웹 binLegendHtml(Bin/Description/Count/비율) 파리티.

    맵 PNG 는 셀이 아니라 pt 좌표로 부착되므로, 표는 그리드 오른쪽 끝을 열폭으로 환산한
    위치에 놓는다. 색 스와치는 Bin 셀 배경(웹 .bin-swatch 대응).
    """
    if not legend_rows:
        return
    used_cols = min(_MAP_COLS_PER_ROW, max(1, n_maps))
    left_pt = _PIC_MARGIN + used_cols * (_MAP_PIC_W + _PIC_GAP)
    start_col = int(left_pt // float(ws.range((1, 1)).width)) + 2
    desc = desc_map or {}

    rows = [[f"{r['bin']} (Pass)" if r.get("is_pass") else r["bin"],
             "" if r.get("is_pass") else desc.get(str(r["bin"]), ""),
             r.get("count"), r.get("pct")]
            for r in legend_rows]
    _write_table(ws, _MAP_LEGEND_HEADER, rows,
                 header_row=_MAP_LEGEND_ROW, start_col=start_col)
    for i, r in enumerate(legend_rows):     # bin 색 스와치 (bin 수는 수십 이하 — COM 부담 없음)
        color = color_map.get(str(r["bin"]))
        if color:
            _style_range(ws.range((_MAP_LEGEND_ROW + 1 + i, start_col)),
                         fill="FF" + color.lstrip("#").upper())
    _set_col_widths(ws, _MAP_LEGEND_HEADER,
                    {"Bin": 12, "Description": 34, "Count": 10, "비율 (%)": 10},
                    start_col=start_col)

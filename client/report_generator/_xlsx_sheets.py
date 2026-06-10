"""table 시트 채움 — summary / yield / fail_item / cpk / issue_table.

계산은 analyzer/_builders 에서 끝났고, 이 모듈은 AnalysisResult 를 받아 각 시트에
고정/동적 레이아웃으로 출력만 담당한다. 공통 표 헬퍼는 _xlsx_table_helpers,
스타일 상수는 _xlsx_style 참조.

`_cpk_fail_subjects` / `_excel_safe_sheet_name` / `_unique_sheet_name` 은 distribution
phase·write 오케스트레이터에서도 재사용하므로 모듈 외부로 노출한다.
"""
from __future__ import annotations

import re

import pandas as pd

from ._xlsx_profile import _flow_prof
from ._xlsx_style import (
    _CPK_N_COL_WIDTH,
    _CPK_SERIES_COL_WIDTH,
    _CPK_TEST_NAME_COL_WIDTH,
    _DATA_FILL_RGB,
    _FAIL_ITEM_ROW_HEIGHT,
    _FAIL_VALUES_GAP,
    _FAIL_VALUES_NCOLS,
    _HEADER_ROW,
    _ISSUE_TABLE_ROW_HEIGHT,
    _ITEM_COL_WIDTH,
    _START_COL,
    _SUMMARY_DATA_FONT,
    _SUMMARY_HDR_FILL_RGB,
    _SUMMARY_HDR_FONT,
    _SUMMARY_SECTION_FONT,
    _SUMMARY_TITLE_FILL_RGB,
    _SUMMARY_TITLE_FONT,
    _XL_CENTER,
    _XL_CONTINUOUS,
    _XL_LEFT,
    _XL_THIN,
    _YIELD_HEADER_ROW_HEIGHT,
    _YIELD_TABLE_ROW_HEIGHT,
    _data_range,
    _hdr_range,
    _style_range,
)
from ._xlsx_table_helpers import (
    _apply_font_delta_to_columns,
    _apply_named_columns_font,
    _apply_small_font_headers,
    _apply_table_col_widths,
    _apply_table_font,
    _apply_used_cell_font,
    _bin_label,
    _fill_table,
    _hdr_cell,
    _safe_set,
    _sanitize_cell,
    _set_table_row_heights,
)

# ── CPK 시트 하이라이트 상수 ──────────────────────────────────────────────────
_CPK_THRESHOLD = 1.33
_CPK_WARN_FILL_RGB = "FFFFFF00"  # 노란색 ARGB — CPK < 1.33 행 하이라이트
_CPK_WARN_FONT_RGB = "FF000000"
_CPK_TOTAL_FILL_RGB = "FFDDEBF7"
_CPK_TOTAL_FONT_RGB = "FF000000"
_CPK_TOTAL_ADDR_MAXLEN = 250  # Excel Range 주소 255자 한계 대비 마진


# ── summary ──────────────────────────────────────────────────────────────────

def _fill_summary(ws, result):
    """Summary 시트 — 고정 좌표 레이아웃 (xlwings)."""
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
    rng = ws.range("A1:H20")
    try:
        rng.api.UnMerge()
    except Exception:
        pass
    rng.clear_contents()
    _style_range(rng, fill=_DATA_FILL_RGB, font=_SUMMARY_DATA_FONT,
                 halign=_XL_CENTER, valign=_XL_CENTER, wrap=True)


def _apply_summary_dimensions(ws):
    widths = {
        "A": 2.625, "B": 16, "C": 26.125, "D": 10.375,
        "E": 10.5, "F": 12.625, "G": 9, "H": 44.75,
    }
    for col, width in widths.items():
        ws.range(f"{col}:{col}").column_width = width
    row_heights = {
        1: 30, 3: 25.5, 4: 16.5, 5: 16.5, 7: 21.75, 8: 16.5,
        15: 27, 17: 48.75, 18: 48.75, 19: 48.75, 20: 48.75,
    }
    for row, height in row_heights.items():
        ws.range(f"{row}:{row}").row_height = height


def _summary_style_range(ws, cell_range, font=None, fill_rgb=None, halign=None, border=False):
    """summary 전용 범위 스타일. halign=None 이면 center+wrap, _XL_LEFT 면 left(no wrap)."""
    center = halign is None
    _style_range(ws.range(cell_range), fill=fill_rgb or _DATA_FILL_RGB,
                 font=font or _SUMMARY_DATA_FONT,
                 halign=_XL_CENTER if center else halign,
                 valign=_XL_CENTER, wrap=center, border=border)


def _apply_summary_layout_styles(ws):
    _summary_style_range(ws, "A1:H1", _SUMMARY_TITLE_FONT, _SUMMARY_TITLE_FILL_RGB, _XL_LEFT)
    b = ws.range("A1:H1").api.Borders(9)   # xlEdgeBottom
    b.LineStyle = _XL_CONTINUOUS
    b.Weight = _XL_THIN

    for cell_range in ("B3:C3", "B7:C7", "B15:C15"):
        _summary_style_range(ws, cell_range, _SUMMARY_SECTION_FONT, _DATA_FILL_RGB, _XL_LEFT)

    _summary_style_range(ws, "B4:H4", _SUMMARY_HDR_FONT, _SUMMARY_HDR_FILL_RGB, border=True)
    _summary_style_range(ws, "B5:H5", _SUMMARY_DATA_FONT, _DATA_FILL_RGB, border=True)
    _summary_style_range(ws, "B8:H13", _SUMMARY_DATA_FONT, _DATA_FILL_RGB, border=True)
    _summary_style_range(ws, "E8:H8", _SUMMARY_HDR_FONT, _SUMMARY_HDR_FILL_RGB, border=True)
    _summary_style_range(ws, "B16:H16", _SUMMARY_HDR_FONT, _SUMMARY_HDR_FILL_RGB, border=True)
    _summary_style_range(ws, "B17:H20", _SUMMARY_DATA_FONT, _DATA_FILL_RGB, border=True)

    ws.range("D9").number_format = "0.00"
    for row in range(9, 14):
        ws.range(f"G{row}").number_format = "0.00"

    for cell_range in (
        "A1:H1", "B3:C3", "B7:C7", "B8:C8", "B9:C13", "D9:D13",
        "E8:G8", "B15:C15", "D16:H16", "D17:H17", "D18:H18",
        "D19:H19", "D20:H20",
    ):
        ws.range(cell_range).merge()


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


# ── yield ────────────────────────────────────────────────────────────────────

def _yield_table(result):
    """yield 표 (step | bin | TNO | Item | {src}_count | {src}_yield | avg | comment)."""
    src = result.sources
    header = ["step", "bin", "TNO", "Item"]
    header += [f"{s}_count" for s in src]
    header += [f"{s}_yield" for s in src]
    header += ["avg", "comment"]
    rows = []
    for r in result.yield_rows:
        items = r.get("items") or [{"tno": "", "item": r.get("Main Fail subject", "")}]
        for it in items:
            row = [r.get("step", ""), _bin_label(r.get("bin")), it.get("tno", ""), it.get("item", "")]
            row += [r.get(f"{s}_count") for s in src]
            row += [r.get(f"{s}_yield") for s in src]
            row += [r.get("avg"), r.get("comment", "")]
            rows.append(row)
    return header, rows


def _fill_yield_by_step(ws, df_yield):
    """df_yield 를 step 별 섹션으로 출력. 각 섹션 상단에 동일 헤더, 섹션 간 1행 공백.

    반환: (header, section_header_rows) — 각 섹션 헤더 행 번호 목록.
    """
    def _step_sort_key(s):
        parts = re.split(r'(\d+)', str(s))
        return [int(p) if p.isdigit() else p.lower() for p in parts]

    header = list(df_yield.columns)
    add_comment = "comment" not in header
    if add_comment:
        header = header + ["comment"]

    ncol = len(header)
    c2 = _START_COL + ncol - 1

    steps = (sorted(df_yield["Step"].unique().tolist(), key=_step_sort_key)
             if "Step" in df_yield.columns else [None])

    section_header_rows = []
    cur = _HEADER_ROW

    for i, step in enumerate(steps):
        sub = df_yield[df_yield["Step"] == step] if step is not None else df_yield
        rows = [list(r) for r in sub.itertuples(index=False)]
        if add_comment:
            rows = [row + [""] for row in rows]

        # 섹션 헤더
        ws.range((cur, _START_COL), (cur, c2)).value = header
        _hdr_range(ws, cur, _START_COL, c2)
        ws.range(f"{cur}:{cur}").row_height = _YIELD_HEADER_ROW_HEIGHT
        section_header_rows.append(cur)
        cur += 1

        # 데이터 행
        if rows:
            data = [[_sanitize_cell(v) for v in row] for row in rows]
            ws.range((cur, _START_COL), (cur + len(rows) - 1, c2)).value = data
            _data_range(ws, cur, _START_COL, cur + len(rows) - 1, c2)
            ws.range(f"{cur}:{cur + len(rows) - 1}").row_height = _YIELD_TABLE_ROW_HEIGHT
            cur += len(rows)

        # 합계 행 — 모든 수치 컬럼(yield%/_cnt/avg) sum. key/comment 는 제외.
        _fixed_keys = {"Step", "Bin", "TNO", "Item"}
        sum_row = []
        for j, col in enumerate(header):
            if j == 0:
                sum_row.append("Sum")
            elif col in _fixed_keys or col == "comment":
                sum_row.append("")
            else:
                total = pd.to_numeric(sub[col], errors="coerce").sum()
                sum_row.append(int(total) if str(col).endswith("_cnt")
                               else round(float(total), 2))
        ws.range((cur, _START_COL), (cur, c2)).value = [_sanitize_cell(v) for v in sum_row]
        _data_range(ws, cur, _START_COL, cur, c2)
        ws.range(f"{cur}:{cur}").row_height = _YIELD_TABLE_ROW_HEIGHT
        cur += 1

        # 섹션 간 공백 1행 (마지막 섹션 제외)
        if i < len(steps) - 1:
            cur += 1

    return header, section_header_rows


def _fill_yield(ws, result):
    if result.df_yield is not None and not result.df_yield.empty:
        header, section_header_rows = _fill_yield_by_step(ws, result.df_yield)
    else:
        header, rows = _yield_table(result)
        _fill_table(ws, header, rows)
        section_header_rows = [_HEADER_ROW]
        _set_table_row_heights(ws, len(rows), height=_YIELD_TABLE_ROW_HEIGHT)
        ws.range(f"{_HEADER_ROW}:{_HEADER_ROW}").row_height = _YIELD_HEADER_ROW_HEIGHT

    _apply_table_col_widths(ws, header, custom_widths={"comment": 50, "Item": _ITEM_COL_WIDTH * 2})
    _apply_table_font(ws, header, size=12)
    for hr in section_header_rows:
        _apply_small_font_headers(ws, header, ["_count", "_yield"], header_row=hr, size=10)


# ── fail_item ─────────────────────────────────────────────────────────────────

def _fill_fail_item(ws, result):
    src = result.sources
    header = ["Step", "Bin", "Item"]
    header += [f"{s}_count" for s in src]
    header += [f"{s}_yield" for s in src]
    header += ["Distribution"]
    rows = []
    for r in result.yield_rows:
        row = [r.get("step", ""), _bin_label(r.get("bin")), r.get("Main Fail subject", "")]
        row += [r.get(f"{s}_count") for s in src]
        row += [r.get(f"{s}_yield") for s in src]
        row += [""]   # Distribution 열 — 차트는 xlwings 단계에서 삽입
        rows.append(row)
    with _flow_prof("fill_fail_item.top_table"):
        _fill_table(ws, header, rows)
        if rows:
            ws.range(f"{_HEADER_ROW + 1}:{_HEADER_ROW + len(rows)}").row_height = _FAIL_ITEM_ROW_HEIGHT
    with _flow_prof("fill_fail_item.fail_values"):
        _fill_fail_values_section(ws, result)
    with _flow_prof("fill_fail_item.style"):
        _apply_table_col_widths(ws, header, col_multiplier=1.3)
        _apply_used_cell_font(ws, size=15, bold=False)
        _apply_named_columns_font(ws, header, ["Step", "Bin", "Item"], size=15, bold=False,
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

    with _flow_prof("fail_values.title"):
        _hdr_cell(ws, title_row, _START_COL, "FAIL_VALUES")

    for i, (src_name, df) in enumerate(fvr.items()):
        with _flow_prof(f"fail_values.write_source[{src_name}]"):
            ncol = df.shape[1]
            col0 = _START_COL + i * (_FAIL_VALUES_NCOLS + _FAIL_VALUES_GAP)
            c1 = col0 + ncol - 1
            # source 이름 셀 + 열 헤더행(df.columns) 일괄
            _hdr_cell(ws, src_row, col0, src_name)
            ws.range((hdr_row, col0), (hdr_row, c1)).value = list(df.columns)
            _hdr_range(ws, hdr_row, col0, c1)
            # 분석단계 산출 DataFrame 을 블록 단위 1회 write (재계산·행단위 setter 없음)
            if len(df):
                rlast = data_row0 + len(df) - 1
                # DUT/XCoord/YCoord/Bin(식별자 텍스트)은 Excel 숫자 자동변환 방지로 text 유지
                ws.range((data_row0, col0), (rlast, col0 + 3)).number_format = "@"
                ws.range((data_row0, col0), (rlast, c1)).value = df.values.tolist()
                _data_range(ws, data_row0, col0, rlast, c1)

    # 소스 블록별 윤곽선 적용 (src_row 헤더~마지막 데이터행) — 블록 Range 1회
    with _flow_prof("fail_values.borders"):
        for i, (src_name, df) in enumerate(fvr.items()):
            ncol = df.shape[1]
            col0 = _START_COL + i * (_FAIL_VALUES_NCOLS + _FAIL_VALUES_GAP)
            last_row = data_row0 + len(df) - 1 if len(df) else hdr_row
            _style_range(ws.range((src_row, col0), (last_row, col0 + ncol - 1)),
                         border=True)


# ── cpk ───────────────────────────────────────────────────────────────────────

def _fill_cpk(ws, result):
    _fill_cpk_rows(ws, result.cpk_rows)


def _fill_cpk_rows(ws, cpk_rows):
    header = ["TEST NAME", "LOW SPEC", "HIGH SPEC", "SCALE", "계열", "n",
              "min", "median", "max", "average", "stdev",
              "cpl", "cpu", "cp", "cpk", "comment"]
    rows = []
    total_row_offsets = []
    for r in cpk_rows:
        if str(r.get("source") or "").strip().lower() == "total":
            total_row_offsets.append(len(rows))
        rows.append([
            r.get("subject"), r.get("lower_limit"), r.get("upper_limit"),
            r.get("units"), r.get("source"), r.get("n"), r.get("min"),
            r.get("median"), r.get("max"), r.get("average"), r.get("stdev"),
            r.get("cpl"), r.get("cpu"), r.get("cp"), r.get("cpk"), "",
        ])
    _blank_repeated_cpk_labels(rows)
    _fill_cpk_table(ws, header, rows)
    _apply_cpk_total_fill(ws, total_row_offsets)
    _apply_cpk_warn_fill(ws, header, rows)   # CPK < 1.33 행 노란 하이라이트 (병합 전)
    _apply_table_col_widths(ws, header, custom_widths={
        "TEST NAME": _CPK_TEST_NAME_COL_WIDTH,
        "계열": _CPK_SERIES_COL_WIDTH,
        "n": _CPK_N_COL_WIDTH,
        "comment": 30,
    })
    _apply_font_delta_to_columns(ws, header, ["TEST NAME", "LOW SPEC", "HIGH SPEC", "SCALE"], 2)


def _blank_repeated_cpk_labels(rows):
    prev_key = None
    for row in rows:
        key = tuple(row[:4])
        if key == prev_key:
            row[0:4] = ["", "", "", ""]
        else:
            prev_key = key


def _fill_cpk_table(ws, header, rows):
    with _flow_prof(f"fill_cpk.fill_table[{len(rows)}x{len(header)}]"):
        _fill_table(ws, header, rows)


def _apply_cpk_total_fill(ws, row_offsets, header_row=_HEADER_ROW):
    if not row_offsets:
        return
    with _flow_prof("fill_cpk.total_fill"):
        excel_rows = [header_row + 1 + offset for offset in row_offsets]

        def _flush(addresses):
            if not addresses:
                return
            _style_range(ws.range(",".join(addresses)),
                         fill=_CPK_TOTAL_FILL_RGB,
                         font={"color": _CPK_TOTAL_FONT_RGB})

        addresses = []
        length = 0
        for row in excel_rows:
            address = f"B{row}:P{row}"
            next_length = length + len(address) + (1 if addresses else 0)
            if addresses and next_length > _CPK_TOTAL_ADDR_MAXLEN:
                _flush(addresses)
                addresses = []
                length = 0
                next_length = len(address)
            addresses.append(address)
            length = next_length
        _flush(addresses)


def _apply_cpk_warn_fill(ws, header, rows, header_row=_HEADER_ROW, start_col=_START_COL):
    with _flow_prof("fill_cpk.warn_fill"):
        return _apply_cpk_warn_fill_inner(ws, header, rows, header_row, start_col)


def _apply_cpk_warn_fill_inner(ws, header, rows, header_row=_HEADER_ROW, start_col=_START_COL):
    """CPK 열 값이 _CPK_THRESHOLD 미만인 행 전체에 노란 배경 적용 (병합 전 호출)."""
    cpk_idx = next((i for i, h in enumerate(header) if h == "cpk"), None)
    if cpk_idx is None:
        return
    source_idx = 4
    ncol = len(header)
    for ri, row in enumerate(rows):
        val = row[cpk_idx] if cpk_idx < len(row) else None
        try:
            f = float(val) if val is not None else None
        except (TypeError, ValueError):
            f = None
        if f is not None and f < _CPK_THRESHOLD:
            excel_row = header_row + 1 + ri
            if len(row) > source_idx and str(row[source_idx]).strip().lower() == "total":
                cpk_col = start_col + cpk_idx
                _style_range(ws.range((excel_row, cpk_col)),
                             fill=_CPK_WARN_FILL_RGB,
                             font={"color": _CPK_WARN_FONT_RGB})
            else:
                _style_range(ws.range((excel_row, start_col), (excel_row, start_col + ncol - 1)),
                             fill=_CPK_WARN_FILL_RGB)


def _cpk_fail_subjects(result):
    """source=='total' 이고 CPK < _CPK_THRESHOLD 인 (subject, cpk_val) 목록. 순서 보존."""
    out = []
    seen = set()
    for r in (getattr(result, "cpk_rows", None) or []):
        if str(r.get("source") or "").strip().lower() != "total":
            continue
        cpk_val = r.get("cpk")
        try:
            cpk_f = float(cpk_val) if cpk_val is not None else None
        except (TypeError, ValueError):
            cpk_f = None
        if cpk_f is not None and cpk_f < _CPK_THRESHOLD:
            subj = r.get("subject")
            if subj and subj not in seen:
                seen.add(subj)
                out.append((subj, cpk_f))
    return out


# ── issue_table ───────────────────────────────────────────────────────────────

def _fill_issue_table(ws, result, include_cpk=True):
    """Category 그룹 레이아웃. Yield = yield 데이터, CPK = CPK < 1.33 아이템, ETC = 플레이스홀더."""
    src = result.sources
    header = ["Category", "Step", "Bin", "TNO", "Item", "avg"]
    for s in src:
        header += [f"{s}_yield"]          # count 열 제거, yield 만 유지
    header += ["Distribution", "comment", "개발 1차 comment",
               "PTE 2차 comment", "개발 2차 comment"]
    pad = len(header) - (6 + len(src))    # Distribution + comment 열 수

    rows = []
    for r in result.issue_yield_rows:
        row = ["Yield", r.get("step", ""), _bin_label(r.get("bin")), r.get("tno", ""),
               r.get("item", ""), r.get("avg")]
        for s in src:
            row += [r.get(f"{s}_yield")]  # count 제거
        row += [""] * pad
        rows.append(row)

    # CPK Category: CPK < 1.33 아이템 (source='total' 기준).
    # 카테고리 시작 행에 "item name" / "cpk" 서브헤더를 넣어 'avg' 헤더와의 혼동 방지.
    cpk_fails = _cpk_fail_subjects(result) if include_cpk else []
    n_cpk = max(1, len(cpk_fails))
    cpk_subheader = ["item name", "cpk"] if include_cpk else ["", ""]
    rows.append(["CPK", "", "", "", cpk_subheader[0], cpk_subheader[1]] + [""] * len(src) + [""] * pad)
    if cpk_fails:
        for subj, cpk_val in cpk_fails:
            row = ["", "", "", "", subj, _sanitize_cell(cpk_val)]
            row += [""] * len(src)       # _yield 열: CPK 에 해당 없음
            row += [""] * pad
            rows.append(row)
    else:
        rows.append([""] * len(header))  # CPK 없으면 빈 데이터행 유지

    rows.append(["ETC"] + [""] * (len(header) - 1))

    _fill_table(ws, header, rows)
    n_yield = len(result.issue_yield_rows)

    _merge_issue_category(ws, n_yield)  # Yield 병합 (기존)

    # CPK Category 병합: 서브헤더 + 데이터 행 전체에 걸쳐 B열 "CPK" 세로 표시
    cpk_start = _HEADER_ROW + 1 + n_yield   # 서브헤더 행
    cpk_block = 1 + n_cpk                    # 서브헤더 + 데이터 행 수
    rng = ws.range((cpk_start, _START_COL), (cpk_start + cpk_block - 1, _START_COL))
    rng.merge()
    rng.api.VerticalAlignment = _XL_CENTER

    if n_yield:
        ws.range(f"{_HEADER_ROW + 1}:{_HEADER_ROW + n_yield}").row_height = _ISSUE_TABLE_ROW_HEIGHT
    if n_cpk:
        ws.range(f"{cpk_start + 1}:{cpk_start + n_cpk}").row_height = _ISSUE_TABLE_ROW_HEIGHT

    _apply_table_col_widths(ws, header, custom_widths={
        "Item": _ITEM_COL_WIDTH * 2,
        "Distribution": 22.1,
        "comment": 40,
        "개발 1차 comment": 40,
        "PTE 2차 comment": 40,
        "개발 2차 comment": 40,
    })
    _apply_used_cell_font(ws, size=15, bold=False)
    _apply_named_columns_font(ws, header, ["Step", "Bin", "TNO", "Item"], size=15, bold=False,
                              last_row=_HEADER_ROW + len(rows))
    # CPK 서브헤더(item name / cpk)는 폰트 패스 이후 헤더 스타일 재적용 — 굵게/음영 유지
    if include_cpk:
        _hdr_range(ws, cpk_start, _START_COL + 4, _START_COL + 4)  # Item 열 (+2 for Step,TNO col)
        _hdr_range(ws, cpk_start, _START_COL + 5, _START_COL + 5)  # avg 열 (+2 for Step,TNO col)


def _merge_issue_category(ws, n_yield, header_row=_HEADER_ROW, start_col=_START_COL):
    """issue_table Category 열의 Yield 행 전체를 병합 + 세로 중앙 정렬."""
    if n_yield <= 1:
        return
    data_start = header_row + 1
    rng = ws.range((data_start, start_col), (data_start + n_yield - 1, start_col))
    rng.merge()
    rng.api.VerticalAlignment = _XL_CENTER


# ── 시트명 유틸 (write 오케스트레이터·distribution phase 공용) ────────────────


def _unique_sheet_name(wb, name, reserved=()):
    """Excel 시트명 규칙(≤31자, []:*?/\\ 금지, 중복 불가)으로 정제."""
    existing = {s.name.lower() for s in wb.sheets} | {str(s).lower() for s in reserved}
    return _excel_safe_sheet_name(name, existing)


def _excel_safe_sheet_name(name, existing_lower):
    """Excel 시트명 규칙(≤31자, []:*?/\\ 금지, 중복 불가)으로 정제.

    existing_lower: 이미 사용 중인 시트명(소문자) 집합. 충돌 시 _n 접미사로 회피.
    """
    base = re.sub(r"[\[\]:*?/\\]", "_", str(name or "Sheet")).strip()[:31] or "Sheet"
    cand, n = base, 2
    existing = {str(s).lower() for s in existing_lower}
    while cand.lower() in existing:
        suffix = f"_{n}"
        cand = base[:31 - len(suffix)] + suffix
        n += 1
    return cand

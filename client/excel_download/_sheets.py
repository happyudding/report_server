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
    _TITLE_FILL_RGB,
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

# 모든 시트 A1 제목 배너 폰트 (2026-07-23 요청 — 배경색은 시트별 기존 값 유지).
_BANNER_FONT = {"name": "Tahoma", "bold": True, "size": 22}
# Issue Table Yield/ETC 섹션에서 fail yield > 0 인 셀 (웹 빨강 강조 대응)
_ISSUE_FAIL_FILL_RGB = "FFFAD4D4"

# PNG 부착 배치 (map_report.write_map_sheet 와 동일 상수)
_MAP_COLS_PER_ROW = 3
_MAP_PIC_W = 500
_MAP_PIC_H = 500
_PIC_GAP = 24
_PIC_MARGIN = 10
_PIC_TOP_START = _PIC_MARGIN + 30  # 상단 범례 행 아래부터 (chart_anchor 미사용 시 폴백)
# 차트/이미지 부착 기준 셀 = B3 (표 시작 위치와 동일 — 2026-07-23 요청)
_ANCHOR_ROW = _HEADER_ROW
_ANCHOR_COL = _START_COL

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


def _title_banner(ws, text, *, font=_BANNER_FONT, fill=_TITLE_FILL_RGB):
    ws.range((1, 1)).value = _safe(text)
    _style_range(ws.range((1, 1), (1, _TITLE_ROW_MAX_COL)), fill=fill, font=font)


def write_sheet_title(ws, text, *, fill=_TITLE_FILL_RGB):
    """시트 제목 배너 — 모든 시트가 시트명과 같은 제목을 A1 에 갖는다(Tahoma 22 Bold).

    표가 있는 시트는 각 write_*_sheet 가 직접 부르고, 차트만 있는 시트
    (Distribution/Histogram/Map Analysis)는 오케스트레이터가 부른다.
    """
    _title_banner(ws, text, fill=fill)


def _section_label(ws, row, text, *, font=_SUMMARY_SECTION_FONT):
    """표 위 섹션 제목 1줄 (Summary 의 'Yield Summary', Yield 의 'STEP P1' 등)."""
    ws.range((row, _START_COL)).value = _safe(text)
    _style_range(ws.range((row, _START_COL)), font=font)


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


def _fit_col_width_pt(ws, col, target_pt, *, iters=3):
    """열 너비를 지정 물리폭(pt)에 맞춘다 — column_width 단위가 문자폭이라 비례 보정 반복.

    width_pt = a*column_width + b(여백) 관계라 한 번에 정확히 맞지 않아 2~3회 수렴시킨다.
    Issue Table 의 Map/Distribution 열처럼 이미지 크기에 칸을 맞춰야 할 때 쓴다.
    """
    rng = ws.range((1, col))
    for _ in range(iters):
        cur_pt = float(rng.width)
        cur_cw = float(rng.column_width)
        if cur_pt <= 0 or cur_cw <= 0 or abs(cur_pt - target_pt) < 0.5:
            break
        rng.column_width = max(cur_cw * target_pt / cur_pt, 0.5)


# ── Summary ──────────────────────────────────────────────────────────────────

def write_summary_sheet(ws, yield_summary, fail_bin_rows):
    """①Yield 요약(source 별 + Total) ②Major Fail Bin 랭킹 — 표 2개."""
    _title_banner(ws, "Summary", fill=_SUMMARY_TITLE_FILL_RGB)

    def _section(row, text):
        _section_label(ws, row, text)

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


def _step_pass_row_values(step, step_summary, source_names):
    """STEP 표 최상단 Bin1(Pass) 행 — 웹 yieldStepPassRow 미러.

    값은 yield_summary.by_step(서버 yield_step_summary)의 **누적** 수율:
    (전체 die − 그 STEP 까지의 누적 fail) / 전체 die. 없으면 None(Pass 행 생략).
    """
    for s in step_summary or []:
        if str(s.get("step") or "") != str(step or ""):
            continue
        by_src = {x.get("source"): x for x in (s.get("sources") or [])}
        return (["", _PASS_BIN, "", "Pass", s.get("avg_yield_pct")]
                + [(by_src.get(name) or {}).get("yield_pct") for name in source_names]
                + [(by_src.get(name) or {}).get("survivor") for name in source_names])
    return None


def _yield_row_heights(ws, header_row, nrows):
    ws.range((header_row, 1)).row_height = _YIELD_HEADER_ROW_HEIGHT
    if nrows:
        ws.range((header_row + 1, 1), (header_row + nrows, 1)).row_height = \
            _YIELD_TABLE_ROW_HEIGHT


def write_yield_sheet(ws, yield_rows, yield_bin_groups, source_names,
                      step_groups=None, step_summary=None):
    """STEP(P1/P2/P3) 별로 표를 나눠 쓴다 — 웹 Yield 탭과 동일 구성.

    각 STEP 표 = 최상단 Pass 행(그 STEP 까지의 누적 수율) + Bin 대표(총합) 행(접힌 상태).
    bin fail % 는 STEP 별로 재계산하지 않는다(서버 build_yield_rows 의 전체 die 기준 값).
    step_groups 가 없으면(구 payload) 종전처럼 전체 Bin 표 1개만 쓴다.
    """
    _title_banner(ws, "Yield")
    header = yield_header(source_names)
    widths = {"Item": 36}

    if step_groups:
        row = _HEADER_ROW                      # 첫 섹션 제목 = B3
        for sg in step_groups:
            label = str(sg.get("step") or "").strip() or "(기타)"
            _section_label(ws, row, f"STEP {label}")
            rows = []
            pass_row = _step_pass_row_values(sg.get("step"), step_summary, source_names)
            if pass_row:
                rows.append(pass_row)
            for group in sg.get("groups") or []:
                rows.append(_yield_row_values(group.get("rep") or {}, source_names))
            last = _write_table(ws, header, rows, header_row=row + 1)
            _yield_row_heights(ws, row + 1, len(rows))
            row = last + 3                     # 표 사이 2행 비우고 다음 STEP 제목
        _set_col_widths(ws, header, widths, default=_NARROW_COL_WIDTH * 1.6)
        return

    rows = []
    if yield_rows and str(yield_rows[0].get("bin")) == _PASS_BIN:
        rows.append(_yield_row_values(yield_rows[0], source_names))
    for group in yield_bin_groups or []:
        rows.append(_yield_row_values(group.get("rep") or {}, source_names))
    _write_table(ws, header, rows)
    _set_col_widths(ws, header, widths, default=_NARROW_COL_WIDTH * 1.6)
    _yield_row_heights(ws, _HEADER_ROW, len(rows))


# ── CPK (Bin1 기준, honey excel 서식) ────────────────────────────────────────

_CPK_HEADER = ["TEST NAME", "LOW SPEC", "HIGH SPEC", "SCALE", "계열", "n",
               "min", "median", "max", "average", "stdev",
               "cpl", "cpu", "cp", "cpk", "comment"]
_CPK_LABEL_NCOL = 4    # TEST NAME/LOW SPEC/HIGH SPEC/SCALE — 계열끼리 공통이라 병합 대상


def write_cpk_sheet(ws, cpk_rows):
    """subject × source 행 그대로 (통계는 서버가 통일한 Bin1(양품) 기준 단일 값)."""
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
    runs = _blank_repeated_labels(rows)
    _write_table(ws, _CPK_HEADER, rows)
    # 여러 계열(source)이 한 항목을 공유하면 TEST NAME/LOW·HIGH SPEC/SCALE 를 세로 병합.
    _merge_label_runs(ws, runs, _CPK_LABEL_NCOL)
    _apply_warn_fill(ws, warn_offsets, len(_CPK_HEADER))
    _set_col_widths(ws, _CPK_HEADER, {
        "TEST NAME": _CPK_TEST_NAME_COL_WIDTH,
        "계열": _CPK_SERIES_COL_WIDTH,
        "n": _NARROW_COL_WIDTH * 1.05,
        "comment": 30,
    }, default=9.5)


def _blank_repeated_labels(rows):
    """같은 subject 연속 행의 TEST NAME/SPEC/SCALE 반복 생략 (honey excel 과 동일).

    반환: 같은 라벨을 공유하는 연속 구간 [(첫 행 offset, 행 수), ...] — 셀 병합에 쓴다.
    """
    prev_key = None
    runs = []
    for i, row in enumerate(rows):
        key = tuple(row[:_CPK_LABEL_NCOL])
        if key == prev_key:
            row[0:_CPK_LABEL_NCOL] = [""] * _CPK_LABEL_NCOL
            runs[-1][1] += 1
        else:
            prev_key = key
            runs.append([i, 1])
    return [(start, length) for start, length in runs]


def _merge_label_runs(ws, runs, ncol, *, header_row=_HEADER_ROW, start_col=_START_COL):
    """연속 동일 라벨 구간을 열별로 세로 병합 (값은 첫 행에만 남아 있음)."""
    for start, length in runs:
        if length < 2:
            continue
        r1 = header_row + 1 + start
        r2 = r1 + length - 1
        for col in range(start_col, start_col + ncol):
            ws.range((r1, col), (r2, col)).api.Merge()


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

_ISSUE_ID_COLS = ["Category", "Step", "Bin", "TNO", "Item"]


def issue_header(source_names):
    """웹 Issue Table 과 같은 컬럼 순서 — 식별 → Map → Distribution → avg → source → Status."""
    return (list(_ISSUE_ID_COLS) + ["Map", "Distribution", "avg"]
            + list(source_names) + ["Status"] + list(_COMMENT_COLS))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fill_cells(ws, cells, rgb):
    """(row, col) 목록에 같은 배경색 — 주소 join 으로 COM 호출 최소화(_apply_warn_fill 과 동일 방식)."""
    if not cells:
        return
    batch = []
    length = 0
    for row, col in cells:
        address = f"{_col_letter(col)}{row}"
        next_length = length + len(address) + (1 if batch else 0)
        if batch and next_length > _ADDR_JOIN_MAXLEN:
            _style_range(ws.range(",".join(batch)), fill=rgb)
            batch = []
            next_length = len(address)
        batch.append(address)
        length = next_length
    if batch:
        _style_range(ws.range(",".join(batch)), fill=rgb)


def write_issue_sheet(ws, issue_rows, source_names):
    """rep/서브헤더/ETC 행만 표시. 접힌 detail 행의 comment 는 같은 bin(rep) 행의
    comment 셀에 "<Item>: <comment>" 줄로 묶어 나열한다 (rep 자신의 comment 가 맨 위).

    컬럼 순서·구성은 웹 Issue Table 과 같다: Map/Distribution 은 PNG 자리(값 비움)로
    오케스트레이터가 add_picture_in_cell 로 채우고, Status(Open/Close)는 서버 값 그대로 쓴다.
    CPK 섹션 서브헤더는 소스 컬럼을 "CPK" 반복 대신 **입력 시트명**으로 채워 Yield 헤더와
    같은 형태로 만들고 헤더 서식을 준다.
    강조: Yield/ETC 섹션은 fail yield > 0, CPK 섹션은 cpk < 1.33 셀에 배경색.
    Category 열은 섹션 범위만큼 세로 병합한다.

    반환: {"rows": [(item, excel_row, section), ...], "map_rows": [(bin, excel_row), ...],
           "dist_col": 열 인덱스, "map_col": 열 인덱스}.
    """
    from ._charts import ISSUE_MAP_IN, issue_cdf_pt_size

    _title_banner(ws, "Issue Table")
    header = issue_header(source_names)
    c1 = _START_COL
    map_col = c1 + 5
    dist_col = c1 + 6
    avg_col = c1 + 7            # avg 뒤로 source 컬럼이 이어진다(색상 표시 대상 구간)

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
    map_rows = []
    fail_cells = []
    cpk_cells = []
    subhead_rows = []
    spans = []                  # Category 병합 구간 [[section, 첫 행, 마지막 행], ...]
    section = ""
    for r in issue_rows or []:
        if r.get("_detail"):
            continue
        # Category 는 섹션 개시행에만 채워진다 (build_issue_table_rows) — 이후 행은 승계.
        if r.get("Category") in ("Yield", "CPK", "ETC"):
            section = r["Category"]
        excel_row = _HEADER_ROW + 1 + len(rows)
        # CPK 섹션 서브헤더 = avg 칸이 "cpk" 인 행 (프런트 isCpkSubheadRow 와 같은 판정)
        subhead = section == "CPK" and str(r.get("avg") or "").strip().lower() == "cpk"
        bin_text = str(r.get("Bin") or "").strip()
        is_pass = bin_text == _PASS_BIN

        src_vals = list(source_names) if subhead else \
            [r.get(f"{s}_yield") for s in source_names]
        vals = [r.get("Category"), r.get("Step"), r.get("Bin"), r.get("TNO"), r.get("Item"),
                "", "",                      # Map / Distribution PNG 자리(비움)
                r.get("avg")] + src_vals + [r.get("Status") or ""]
        for col in _COMMENT_COLS:
            parts = []
            own = str(r.get(col) or "").strip()
            if own:
                parts.append(own)
            parts.extend(detail_comments.get((r.get("_grp"), col), []))
            vals.append("\n".join(parts))

        if subhead:
            subhead_rows.append(excel_row)
        else:
            if not is_pass:
                # avg + source 컬럼 색상 표시 (Yield/ETC: fail>0 / CPK: 임계 미만)
                for off, v in enumerate([r.get("avg")] + [r.get(f"{s}_yield")
                                                          for s in source_names]):
                    num = _num(v)
                    if num is None:
                        continue
                    if section in ("Yield", "ETC") and num > 0:
                        fail_cells.append((excel_row, avg_col + off))
                    elif section == "CPK" and num < _CPK_THRESHOLD:
                        cpk_cells.append((excel_row, avg_col + off))
            # Map 썸네일 대상 — 웹과 동일하게 Bin 이 있는 Yield/ETC 행(Pass 제외)
            if not is_pass and bin_text and section in ("Yield", "ETC"):
                map_rows.append((bin_text, excel_row))

        item_rows.append((r.get("Item"), excel_row, section))
        if spans and spans[-1][0] == section:
            spans[-1][2] = excel_row
        else:
            spans.append([section, excel_row, excel_row])
        rows.append(vals)

    _write_table(ws, header, rows)
    # CPK 서브헤더 행은 헤더 서식(연청색 fill) — Yield 섹션 헤더와 같은 형태.
    for row in subhead_rows:
        _hdr_range(ws, row, c1, c1 + len(header) - 1)
    _fill_cells(ws, fail_cells, _ISSUE_FAIL_FILL_RGB)
    _fill_cells(ws, cpk_cells, _CPK_WARN_FILL_RGB)
    for _section, r1, r2 in spans:
        if r2 > r1:
            ws.range((r1, c1), (r2, c1)).api.Merge()

    widths = {"Item": _ITEM_COL_WIDTH * 1.8, "Category": 10, "Status": 10}
    for col in _COMMENT_COLS:
        widths[col] = 40
    _set_col_widths(ws, header, widths, default=_NARROW_COL_WIDTH * 1.6)
    # Map/Distribution 열은 썸네일 물리 크기에 칸을 맞춘다(이미지가 칸을 넘지 않게).
    dist_w_pt, _dist_h_pt = issue_cdf_pt_size()
    _fit_col_width_pt(ws, map_col, ISSUE_MAP_IN * 72.0)
    _fit_col_width_pt(ws, dist_col, dist_w_pt)
    return {"rows": item_rows, "map_rows": map_rows,
            "dist_col": dist_col, "map_col": map_col}


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


def chart_anchor(ws):
    """차트/이미지 부착 기준점(pt) = B3 셀 좌상단 — 표 시작 위치와 동일하게 맞춘다.

    제목 배너(1행)·범례(2행)를 쓴 뒤에 불러야 실제 행 높이가 반영된다.
    """
    cell = ws.range((_ANCHOR_ROW, _ANCHOR_COL))
    return float(cell.left), float(cell.top)


def picture_stack_tops(heights_px, top0=_PIC_TOP_START):
    """세로 연속 배치의 각 PNG top(pt) 목록 — 완료 순서와 무관하게 부착 가능하도록 선계산."""
    ppp = _png_pt_per_px()
    tops = []
    top = float(top0)
    for h_px in heights_px:
        tops.append(top)
        top += h_px * ppp + _PIC_GAP
    return tops


def add_picture_at(ws, path, *, top, width_px, height_px, left=_PIC_MARGIN):
    """선계산된 top 위치에 PNG 1장 부착 (as_completed 파이프라인용)."""
    ppp = _png_pt_per_px()
    _add_picture(ws, path, left, top, width_px * ppp, height_px * ppp)


def add_picture_in_cell(ws, path, row, col, w_pt, h_pt):
    """지정 (row,col) 셀에 PNG 를 **칸 크기에 맞춰** 부착 (Issue Table Map/Distribution).

    썸네일은 렌더 DPI 가 시트 차트와 달라(고해상도) 원본 물리크기를 호출부가 pt 로 넘긴다.
    행 높이가 PNG 보다 낮으면 PNG 높이에 맞춰 넓히고(긴 comment 로 이미 높은 행은 유지),
    열너비는 write_issue_sheet 가 이미 이미지 폭에 맞춰 뒀다. 칸이 이미지보다 좁으면
    가로세로 비율을 유지한 채 축소해 칸 안에 넣고, 남는 여백만큼 가운데 정렬한다.
    """
    row_rng = ws.range((row, 1))
    if row_rng.row_height < h_pt + 4:
        row_rng.row_height = h_pt + 4
    cell = ws.range((row, col))
    cw, ch = float(cell.width), float(cell.height)
    scale = min((cw - 2.0) / w_pt, (ch - 2.0) / h_pt, 1.0)
    scale = max(scale, 0.05)
    w, h = w_pt * scale, h_pt * scale
    _add_picture(ws, path, cell.left + (cw - w) / 2.0, cell.top + (ch - h) / 2.0, w, h)


_HIDDEN_INDEX_FONT = {"name": "Calibri", "size": 8, "color": "FFFFFFFF"}  # 흰 글씨 = 화면에 안 보임
_HIDDEN_INDEX_MIN_ROW = 3          # 세션링크(H1)·source 범례(row2) 보호


def write_hidden_item_index(ws, entries, tops, *, left=_PIC_MARGIN, top=None):
    """차트 이미지가 덮는 셀에 항목명을 흰 글씨로 기입 — Ctrl+F 로 차트를 찾게 한다.

    Distribution/Histogram 시트는 항목명이 PNG 픽셀 안에만 있어 검색이 안 된다. 그림 아래
    셀에 텍스트를 심으면 Ctrl+F 가 해당 차트 위치로 점프한다(흰 글씨 + 그림에 가려 비가시).

    entries: [(chunk_idx, cell_idx, subject)] — add_picture_at 과 동일한 tops/left/top 을
    넘겨야 좌표가 어긋나지 않는다. 기준행(B3) 아래는 행높이·열너비가 기본값·균일하므로
    pt→(row,col) 은 기준점에서의 단순 나눗셈이다(제목 배너로 1행만 높아진 것은 top 으로
    흡수). 값·스타일 모두 범위 1회 적용(COM 왕복 최소).
    """
    from ._charts import NCOLS, cell_pt_size

    if not entries:
        return
    cell_w_pt, cell_h_pt = cell_pt_size()
    row_h = float(ws.api.StandardHeight)          # 한국어 Excel 기본 16.5pt 등 — 가정 금지
    col_w = float(ws.range((1, 1)).width)
    # 기준점(B3)이 주어지면 그 행부터 세고, 없으면 시트 최상단부터 센다(구 동작).
    top0 = 0.0 if top is None else float(top)
    row0 = 1 if top is None else _ANCHOR_ROW
    placed = {}
    for chunk_idx, cell_idx, subject in entries:
        r, c = divmod(int(cell_idx), NCOLS)
        y_pt = tops[chunk_idx] + (r + 0.45) * cell_h_pt   # 셀 중앙 부근 = 점프 시 차트가 화면에 걸림
        x_pt = left + (c + 0.08) * cell_w_pt
        row = max(_HIDDEN_INDEX_MIN_ROW,
                  row0 + int(max(y_pt - top0, 0.0) // row_h))
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


def add_map_grid(ws, labeled_pngs, *, left=_PIC_MARGIN, top=_PIC_MARGIN):
    """wafer map PNG 들을 가로 3개 그리드로 부착 (좌상단 = B3 셀, 정사각 500x500pt)."""
    for idx, (label, path) in enumerate(labeled_pngs):
        col = idx % _MAP_COLS_PER_ROW
        row = idx // _MAP_COLS_PER_ROW
        _add_picture(ws, path,
                     left + col * (_MAP_PIC_W + _PIC_GAP),
                     top + row * (_MAP_PIC_H + _PIC_GAP),
                     _MAP_PIC_W, _MAP_PIC_H)


_MAP_LEGEND_HEADER = ["Bin", "Description", "Count", "비율 (%)"]


def write_map_legend(ws, legend_rows, desc_map, color_map, n_maps, *, left=_PIC_MARGIN,
                     header_row=_HEADER_ROW):
    """맵 그리드 우측에 Bin Legend 표 — 웹 binLegendHtml(Bin/Description/Count/비율) 파리티.

    맵 PNG 는 셀이 아니라 pt 좌표로 부착되므로, 표는 그리드 오른쪽 끝을 열폭으로 환산한
    위치에 놓는다(세로는 맵 그리드와 같은 B3 행). 색 스와치는 Bin 셀 배경(웹 .bin-swatch 대응).
    """
    if not legend_rows:
        return
    used_cols = min(_MAP_COLS_PER_ROW, max(1, n_maps))
    left_pt = left + used_cols * (_MAP_PIC_W + _PIC_GAP)
    start_col = int(left_pt // float(ws.range((1, 1)).width)) + 2
    desc = desc_map or {}

    rows = [[f"{r['bin']} (Pass)" if r.get("is_pass") else r["bin"],
             "" if r.get("is_pass") else desc.get(str(r["bin"]), ""),
             r.get("count"), r.get("pct")]
            for r in legend_rows]
    _write_table(ws, _MAP_LEGEND_HEADER, rows,
                 header_row=header_row, start_col=start_col)
    for i, r in enumerate(legend_rows):     # bin 색 스와치 (bin 수는 수십 이하 — COM 부담 없음)
        color = color_map.get(str(r["bin"]))
        if color:
            _style_range(ws.range((header_row + 1 + i, start_col)),
                         fill="FF" + color.lstrip("#").upper())
    _set_col_widths(ws, _MAP_LEGEND_HEADER,
                    {"Bin": 12, "Description": 34, "Count": 10, "비율 (%)": 10},
                    start_col=start_col)

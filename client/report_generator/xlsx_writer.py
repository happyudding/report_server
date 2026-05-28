"""xlwings 기반 xlsx 리포트 생성 (Excel COM 필요).

AnalysisResult → summary / yield / cpk / fail_item / issue_table / distribution 시트.
summary/yield/issue_table 는 server/xlsx_parser.py 의 anchor/header 규약에 맞춰
서버 업로드 후에도 그대로 파싱되도록 한다. distribution 은 네이티브 Excel 산점
(CDF) 차트로 출력 → 기존 chart_export.py 가 PNG 로 렌더 가능.

계산은 analyzer/_builders 에서 끝났고, 이 모듈은 출력만 담당한다.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

_MAX_CDF_POINTS = 150
_CHARTS_PER_ROW = 5
_CHART_W, _CHART_H, _GAP = 320, 220, 16

ALL_SHEETS = ["summary", "yield", "cpk", "fail_item", "issue_table", "distribution"]


def write(result, out_path, sheets=None) -> str:
    """AnalysisResult 를 xlsx 로 저장. 반환: 저장 경로(str). Excel 미설치 시 RuntimeError.

    sheets: 출력할 시트명 리스트/집합 (None 이면 전체). 알 수 없는 이름은 무시.
    """
    try:
        import xlwings as xw
    except ImportError as exc:
        raise RuntimeError(
            "xlwings 가 설치되어 있지 않습니다. 로컬 리포트 생성에는 MS Excel + xlwings 가 필요합니다."
        ) from exc

    sel = [s for s in ALL_SHEETS if (sheets is None or s in set(sheets))]
    if not sel:
        sel = ["summary"]

    writers = {
        "summary": lambda sh: _write_summary(sh, result),
        "yield": lambda sh: _write_yield(sh, result),
        "cpk": lambda sh: _write_cpk(sh, result),
        "fail_item": lambda sh: _write_fail_item(sh, result),
        "issue_table": lambda sh: _write_issue_table(sh, result),
        "distribution": lambda sh: _write_distribution(wb, sh, result),
    }

    out_path = str(Path(out_path).resolve())
    app = xw.App(visible=False, add_book=False)
    app.display_alerts = False
    app.screen_updating = False
    wb = None
    try:
        wb = app.books.add()
        # 첫 시트를 첫 선택 시트로 재사용, 나머지는 순서대로 추가
        first = wb.sheets[0]
        first.name = sel[0]
        sheet_objs = {sel[0]: first}
        prev = first
        for name in sel[1:]:
            sh = wb.sheets.add(name, after=prev)
            sheet_objs[name] = sh
            prev = sh

        for name in sel:
            writers[name](sheet_objs[name])

        sheet_objs[sel[0]].activate()
        wb.save(out_path)
        return out_path
    finally:
        try:
            if wb is not None:
                wb.close()
        finally:
            app.quit()


# ---------------------------------------------------------------------------
# summary (server parser anchor 규약 준수)

def _write_summary(sh, result):
    meta = result.meta
    title = f"Honey Local Report — {meta.product_type} {meta.product} {meta.lot_id}".strip()
    sh.range("A1").value = title

    feature = result.summary_feature()
    feat_keys = list(feature.keys())

    r = 3
    sh.range((r, 1)).value = "Feature"
    sh.range((r + 1, 1)).value = feat_keys
    sh.range((r + 2, 1)).value = [feature[k] for k in feat_keys]

    r = 7
    sh.range((r, 1)).value = "Yield Summary"
    pass_y = result.pass_yield if result.pass_yield is not None else "N/A"
    sh.range((r + 1, 1)).value = f"Overall Pass Yield (Bin 1): {pass_y}%"

    r = 10
    sh.range((r, 1)).value = "Major Fail Bins"
    sh.range((r + 1, 1)).value = ["Rank", "Fail Type", "Main Fail Subject", "Fail Ratio(%)", "Comment"]
    rr = r + 2
    for i, b in enumerate(result.major_fail_bins(), start=1):
        sh.range((rr, 1)).value = [i, str(b.get("bin")), b.get("Main Fail subject", ""),
                                   b.get("avg", 0.0), ""]
        rr += 1

    rr += 1
    sh.range((rr, 1)).value = "Evaluation Summary"
    # _read_section: header(anchor+1) + value(anchor+2) → dict. category 를 헤더로.
    # 값 행은 빈 문자열이면 xlwings 가 셀을 만들지 않아 파서가 못 읽음 → 플레이스홀더 "-".
    sh.range((rr + 1, 1)).value = ["Yield", "CPK", "Temp", "ETC"]
    sh.range((rr + 2, 1)).value = ["-", "-", "-", "-"]

    sh.range("A1").column_width = 18


# ---------------------------------------------------------------------------
# yield

def _write_yield(sh, result):
    cols = ["bin", "count", "portion (%)"]
    cols += [f"portion_{s}" for s in result.sources]
    cols += ["avg", "Main Fail subject", "comment"]
    _write_table(sh, cols, result.yield_rows)


# ---------------------------------------------------------------------------
# cpk

def _write_cpk(sh, result):
    cols = ["subject", "lower_limit", "upper_limit", "units", "source",
            "n", "min", "median", "max", "average", "stdev",
            "cpl", "cpu", "cp", "cpk"]
    _write_table(sh, cols, result.cpk_rows, extra_cols=["comment"])


# ---------------------------------------------------------------------------
# fail_item (bin별 fail subject 랭킹 평탄화)

def _write_fail_item(sh, result):
    header = ["bin", "bin_count", "bin_portion (%)", "subject", "fail_count", "fail_portion (%)"]
    sh.range((1, 1)).value = header
    rr = 2
    for row in result.fail_item_rows:
        if str(row.get("bin")) == "1":
            continue
        subs = row.get("fail_subjects") or []
        if not subs:
            sh.range((rr, 1)).value = [str(row.get("bin")), row.get("count"),
                                       row.get("portion (%)"), "N/A", "", ""]
            rr += 1
            continue
        for fs in subs:
            sh.range((rr, 1)).value = [str(row.get("bin")), row.get("count"),
                                       row.get("portion (%)"), fs.get("subject"),
                                       fs.get("count"), fs.get("portion (%)")]
            rr += 1
    _freeze_header(sh)


# ---------------------------------------------------------------------------
# issue_table (yield/fail_items 기반 bin별 most-fail item, Distribution 열은 서버가 drop)

def _write_issue_table(sh, result):
    src_cols = [f"portion_{s}" for s in result.sources]
    header = (["Bin", "Most Fail Subject", "Avg(%)"] + src_cols
              + ["Distribution", "Issue Point", "Comment"])
    sh.range((1, 1)).value = header
    rr = 2
    for row in result.issue_rows:
        vals = [str(row.get("bin")), row.get("subject"), row.get("avg")]
        vals += [row.get(c) for c in src_cols]
        vals += ["", "", ""]
        sh.range((rr, 1)).value = vals
        rr += 1
    _freeze_header(sh)


# ---------------------------------------------------------------------------
# distribution (네이티브 Excel 산점 CDF 차트)

def _write_distribution(wb, sh, result):
    if not result.distributions:
        sh.range("A1").value = "선택된 항목에 분포 데이터가 없습니다."
        return

    data = wb.sheets.add("_dist", after=sh)
    cur = 1  # 헬퍼 시트 행 커서

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
        top = _GAP + grow * (_CHART_H + _GAP)

        ch = sh.charts.add(left, top, _CHART_W, _CHART_H)
        ch.set_source_data(src_range)
        ch.chart_type = "xy_scatter_lines_no_markers"
        title = d.subject + (f" ({d.unit})" if d.unit else "")
        _set_chart_title(ch, title)
        _add_limit_lines(ch, data, d, top_row, bot_row, ncol)
        _fix_cdf_axes(ch)

        cur = bot_row + 2

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


# ---------------------------------------------------------------------------
# 공용 헬퍼

def _write_table(sh, cols, rows, extra_cols=None):
    header = list(cols) + list(extra_cols or [])
    sh.range((1, 1)).value = header
    rr = 2
    for row in rows:
        vals = [row.get(c) for c in cols] + ["" for _ in (extra_cols or [])]
        sh.range((rr, 1)).value = vals
        rr += 1
    _freeze_header(sh)


def _freeze_header(sh):
    try:
        sh.activate()
        win = sh.api.Application.ActiveWindow
        win.SplitColumn = 0
        win.SplitRow = 1
        win.FreezePanes = True
    except Exception:
        pass


def _downsample(xs, ys, max_points=_MAX_CDF_POINTS):
    if xs.size <= max_points:
        return xs, ys
    idx = np.unique(np.linspace(0, xs.size - 1, max_points).astype(int))
    return xs[idx], ys[idx]

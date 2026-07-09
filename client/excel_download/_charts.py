"""Excel Download 용 matplotlib PNG 렌더러 — Qt/xlwings 비의존.

ProcessPoolExecutor 자식 프로세스에서 실행되므로 모든 렌더 함수는 모듈 최상위에 있고
(피클링), 인자는 순수 데이터(job dict)만 받는다. numpy 배열로 넘기면 리스트보다
피클 전송이 훨씬 빠르다 (호출자가 변환).

렌더 방식: 수천 셀 규모에서 matplotlib Axes/틱 기계(axes 생성 + 틱 객체 + 텍스트
레이아웃)가 렌더 시간을 지배하므로(실측 청크당 ~2s), Axes 를 쓰지 않고 **figure
좌표계에 직접** 그린다 — 셀당 테두리/격자/step 라인/limit 선 + 텍스트 몇 개뿐이라
동일 결과를 수 배 빠르게 만든다. 좌표 변환(data→figure fraction)은 numpy 로 직접 계산.

- render_chunk_pair / render_grid_chunk: CDF(ECDF step)/Histogram 4열 그리드 청크 PNG.
  ECDF 는 서버가 내려준 전 고유값 포인트를 그대로 그린다 (다운샘플링 금지 — 불변규칙 6).
  Histogram 은 ECDF 에서 빈도를 수학적으로 복원한다(고유값 x_i 의 개수 = diff(y)/100 × n)
  — 원본 측정값 기준과 동일한 집계이며 다운샘플이 아니다.
- render_map_png_job: web_report Map Analysis 행(dies) → wafer map PNG
  (report_generator.map_analyze.render_map_png 재사용 — honey excel Map 과 동일 모양).
"""
from __future__ import annotations

import numpy as np

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure    # noqa: E402
from matplotlib.lines import Line2D     # noqa: E402
from matplotlib.patches import Polygon, Rectangle  # noqa: E402

# 웹 report_view.html 의 DIST_PALETTE 와 동일 — source i 색이 웹과 일치하도록 유지.
DIST_PALETTE = ["#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
                "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52"]

# 그리드 레이아웃 (시트 부착 시 크기 계산도 이 상수를 쓴다)
NCOLS = 4
ROWS_PER_CHUNK = 16           # 청크당 4열 x 16행 = 64 차트 (PNG 수 = pictures.add COM 왕복 최소화)
# 셀 크기/해상도: Excel 의 그림 삽입(AddPicture) 시간이 픽셀 수에 비례(실측 ~36ms/Mpx)
# 하므로 가독성을 해치지 않는 선에서 픽셀을 줄인다.
CELL_W_IN = 3.5               # 셀 크기(inch)
CELL_H_IN = 2.2
DPI = 96

_LIMIT_COLOR = "#d62728"
_STATUS_TITLE_COLOR = {"fail": "#d62728", "cpk_low": "#e67700", "ok": "#111111"}
_BORDER_COLOR = "#bbbbbb"
_GRID_COLOR = "#dddddd"
_TEXT_COLOR = "#444444"
_HIST_BINS = 40
_TITLE_MAX_CHARS = 46

# 셀 내부 플롯 영역 여백 (셀 크기에 대한 비율)
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 0.09, 0.03, 0.17, 0.13


def chunk_px_size(n_cells):
    """청크(n_cells 개 셀) PNG 의 (width_px, height_px) — 시트 배치용."""
    nrows = max(1, (int(n_cells) + NCOLS - 1) // NCOLS)
    return (int(NCOLS * CELL_W_IN * DPI), int(nrows * CELL_H_IN * DPI))


def _fmt_title(cell):
    title = str(cell.get("title") or "")
    if len(title) > _TITLE_MAX_CHARS:
        title = title[:_TITLE_MAX_CHARS - 1] + "…"
    tno = cell.get("test_num")
    out = f"{title} ({tno})" if tno else title
    units = cell.get("units")
    if units:
        out += f" [{units}]"
    return out


def _fmt_num(v):
    return f"{float(v):.4g}"


def _cell_box(idx, nrows):
    """셀 idx 의 플롯 영역 (x0, y0, w, h) — figure fraction. 행 0 이 맨 위."""
    r, c = divmod(idx, NCOLS)
    cw = 1.0 / NCOLS
    ch = 1.0 / nrows
    x0 = (c + _PAD_L) * cw
    y0 = (nrows - 1 - r + _PAD_B) * ch
    return x0, y0, cw * (1.0 - _PAD_L - _PAD_R), ch * (1.0 - _PAD_T - _PAD_B)


def _x_range(cell):
    """셀 x 데이터 범위 (limit 선 포함 — 항상 보이도록). ECDF x 는 정렬돼 있음."""
    xs = [s[2] for s in cell["sources"] if len(s[2])]
    if not xs:
        return None
    xmin = min(float(x[0]) for x in xs)
    xmax = max(float(x[-1]) for x in xs)
    for v in (cell.get("lo"), cell.get("hi")):
        if v is not None:
            xmin = min(xmin, float(v))
            xmax = max(xmax, float(v))
    if xmax <= xmin:
        pad = abs(xmin) * 1e-6 or 0.5
        return xmin - pad, xmax + pad
    return xmin, xmax


def _add_line(fig, xs, ys, color, lw=0.9, ls="-", alpha=1.0):
    fig.add_artist(Line2D(xs, ys, transform=fig.transFigure, color=color,
                          linewidth=lw, linestyle=ls, alpha=alpha,
                          solid_joinstyle="miter"))


def _cell_frame(fig, cell, box, xr, y_labels):
    """테두리 + 가로 격자 + 제목 + x/y 라벨 텍스트 (Axes/틱 기계 대체)."""
    x0, y0, w, h = box
    fig.add_artist(Rectangle((x0, y0), w, h, transform=fig.transFigure,
                             fill=False, edgecolor=_BORDER_COLOR, linewidth=0.6))
    fig.text(x0, y0 + h + 0.004, _fmt_title(cell), fontsize=7,
             color=_STATUS_TITLE_COLOR.get(cell.get("status"), "#111111"),
             ha="left", va="bottom")
    # 가로 격자(y_labels 위치) + y 라벨
    n = len(y_labels)
    for i, lab in enumerate(y_labels):
        gy = y0 + h * (i / (n - 1) if n > 1 else 0)
        if 0 < i < n - 1:
            _add_line(fig, (x0, x0 + w), (gy, gy), _GRID_COLOR, lw=0.4)
        if lab is not None:
            fig.text(x0 - 0.002, gy, lab, fontsize=5, color=_TEXT_COLOR,
                     ha="right", va="center")
    # x 라벨: min / mid / max
    if xr is not None:
        xmin, xmax = xr
        for frac, v in ((0.0, xmin), (0.5, (xmin + xmax) / 2), (1.0, xmax)):
            fig.text(x0 + w * frac, y0 - 0.003, _fmt_num(v), fontsize=5,
                     color=_TEXT_COLOR, ha="center", va="top")


def _limit_lines(fig, cell, box, xr):
    x0, y0, w, h = box
    xmin, xmax = xr
    span = xmax - xmin
    for v in (cell.get("lo"), cell.get("hi")):
        if v is None:
            continue
        fx = x0 + (float(v) - xmin) / span * w
        _add_line(fig, (fx, fx), (y0, y0 + h), _LIMIT_COLOR, lw=0.8, ls="--")


def _draw_cdf_cell(fig, cell, box):
    xr = _x_range(cell)
    _cell_frame(fig, cell, box, xr, y_labels=("0", "50", "100"))
    if xr is None:
        return
    x0, y0, w, h = box
    xmin, xmax = xr
    span = xmax - xmin
    for name, color, x, y, _n in cell["sources"]:
        if len(x) == 0:
            continue
        # step(where="post") 경로를 직접 구성 — 전 고유값 포인트 유지(다운샘플 아님)
        if len(x) == 1:
            fx = x0 + (float(x[0]) - xmin) / span * w
            _add_line(fig, (fx, fx), (y0, y0 + float(y[0]) / 100.0 * h), color)
            continue
        sx = np.repeat(x, 2)[1:]
        sy = np.repeat(y, 2)[:-1]
        _add_line(fig, x0 + (sx - xmin) / span * w, y0 + sy / 100.0 * h, color)
    _limit_lines(fig, cell, box, xr)


def _hist_counts(x, y, n):
    """ECDF(고유값 x, 누적% y) → 고유값별 개수. n(측정 수) 없으면 % 비율."""
    frac = np.diff(np.concatenate(([0.0], np.asarray(y, dtype="float64")))) / 100.0
    if n:
        return np.rint(frac * float(n))
    return frac * 100.0  # % 단위


def _draw_hist_cell(fig, cell, box):
    xr = _x_range(cell)
    if xr is None:
        _cell_frame(fig, cell, box, None, y_labels=(None,))
        return
    xmin, xmax = xr
    edges = np.linspace(xmin, xmax, _HIST_BINS + 1)
    percent_axis = any(not s[4] for s in cell["sources"])
    binned_all = []
    for name, color, x, y, n in cell["sources"]:
        if len(x) == 0:
            continue
        counts = _hist_counts(x, y, None if percent_axis else n)
        binned, _ = np.histogram(np.asarray(x, dtype="float64"), bins=edges, weights=counts)
        binned_all.append((color, binned))
    ymax = max((float(b.max()) for _, b in binned_all), default=0.0) or 1.0
    unit = "%" if percent_axis else ""
    _cell_frame(fig, cell, box, xr,
                y_labels=("0", None, _fmt_num(ymax) + unit))
    x0, y0, w, h = box
    span = xmax - xmin
    ex = x0 + (np.repeat(edges, 2)[1:-1] - xmin) / span * w   # 계단 외곽선 x
    for color, binned in binned_all:
        ey = y0 + np.repeat(binned, 2) / ymax * h
        verts = np.column_stack((
            np.concatenate(([ex[0]], ex, [ex[-1]])),
            np.concatenate(([y0], ey, [y0])),
        ))
        fig.add_artist(Polygon(verts, closed=True, transform=fig.transFigure,
                               facecolor=color, edgecolor="none", alpha=0.55))
    _limit_lines(fig, cell, box, xr)


def _render_cells(cells, kind, out_path, nrows):
    fig = Figure(figsize=(NCOLS * CELL_W_IN, nrows * CELL_H_IN), dpi=DPI)
    draw = _draw_cdf_cell if kind == "cdf" else _draw_hist_cell
    for idx, cell in enumerate(cells):
        draw(fig, cell, _cell_box(idx, nrows))
    fig.savefig(out_path, format="png", facecolor="white")


def render_grid_chunk(job) -> str:
    """CDF/Histogram 그리드 청크 1장 렌더. job:

    {"kind": "cdf"|"hist", "out_path": str,
     "cells": [{"title","test_num","units","lo","hi","status",
                "sources": [(name, color, x(np.ndarray), y(np.ndarray), n|None), ...]}, ...]}
    반환: out_path. cells 는 최대 NCOLS*ROWS_PER_CHUNK 개.
    """
    cells = job["cells"]
    nrows = max(1, (len(cells) + NCOLS - 1) // NCOLS)
    _render_cells(cells, job["kind"], job["out_path"], nrows)
    return job["out_path"]


def render_chunk_pair(job) -> tuple:
    """같은 cells 로 CDF·Histogram 청크 PNG 를 한 번에 렌더 — 프로세스 간 데이터
    피클 전송을 절반으로 줄인다. job:

    {"cells": [...], "cdf_path": str, "hist_path": str}
    반환: (cdf_path, hist_path).
    """
    cells = job["cells"]
    nrows = max(1, (len(cells) + NCOLS - 1) // NCOLS)
    _render_cells(cells, "cdf", job["cdf_path"], nrows)
    _render_cells(cells, "hist", job["hist_path"], nrows)
    return job["cdf_path"], job["hist_path"]


def render_map_png_job(job) -> str:
    """Map Analysis 행 1개 → wafer map PNG. job:

    {"out_path": str, "title": str, "xs": list, "ys": list, "bins": list}
    honey excel 의 render_map_png 를 그대로 재사용해 동일한 모양(bin1 파랑 고정,
    격자, 범례)을 만든다. 반환: out_path. 좌표가 비어 있으면 ValueError.
    """
    from report_generator.map_analyze import render_map_png

    if not job["xs"]:
        raise ValueError(f"{job['title']}: 좌표가 없습니다.")
    render_map_png(job["xs"], job["ys"], job["bins"],
                   title=job["title"], out_path=job["out_path"])
    return job["out_path"]

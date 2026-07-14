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
  (_map.render_wafer_map_png — 웹 Plotly heatmap 과 색/방향/라벨/프레임 일치).
"""
from __future__ import annotations

import numpy as np

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure    # noqa: E402
from matplotlib.lines import Line2D     # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

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
# Issue Table 행별 단일 CDF 썸네일 크기(inch) — 행 높이에 맞춰 작게.
ISSUE_CELL_W_IN = 2.6
ISSUE_CELL_H_IN = 1.15

_LIMIT_COLOR = "#d62728"
_STATUS_TITLE_COLOR = {"fail": "#d62728", "cpk_low": "#e67700", "ok": "#111111"}
_BORDER_COLOR = "#bbbbbb"
_GRID_COLOR = "#dddddd"
_TEXT_COLOR = "#444444"
_TITLE_MAX_CHARS = 46
_MARKER_SIZE = 1.6            # CDF 점(마커) 크기(pt)

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


def _fmt_limit_caption(cell):
    """limit 값 캡션 '(lo ~ hi)' — 둘 다 없으면 빈 문자열."""
    lo, hi = cell.get("lo"), cell.get("hi")
    if lo is None and hi is None:
        return ""
    return f"({_fmt_num(lo) if lo is not None else '-'} ~ {_fmt_num(hi) if hi is not None else '-'})"


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


def _add_markers(fig, xs, ys, color, size=_MARKER_SIZE):
    """산포 점(마커) — 선 없이 각 포인트를 작은 원으로. 전 포인트 유지(다운샘플 금지)."""
    fig.add_artist(Line2D(xs, ys, transform=fig.transFigure, color=color,
                          linestyle="none", marker="o", markersize=size,
                          markeredgewidth=0))


def _dist_fill_vertical(xs, ys, step_y=0.8):
    """ECDF riser(동일 x 의 세로 구간)를 step_y(%p) 간격 세로 점으로 채운다.

    웹 distribution.js distFillVertical 포팅 — 이산/코드값 항목이 세로 점기둥으로
    촘촘해 보이도록(선 없이 점만). 연속값(Δy<step_y)은 내부 루프 0회라 원본 포인트와
    동일. 다운샘플이 아니라 표시용 포인트 추가(불변규칙 #5 의 sanctioned 세로채움).
    """
    n = len(xs)
    if n == 0:
        return xs, ys
    ox, oy = [], []
    prev_y = 0.0                        # ECDF 는 0 에서 첫 riser 시작
    for i in range(n):
        x = xs[i]
        y = ys[i]
        yy = prev_y + step_y
        while yy < y - 1e-9:
            ox.append(x)
            oy.append(yy)
            yy += step_y
        ox.append(x)
        oy.append(y)
        prev_y = y
    return np.asarray(ox, dtype="float64"), np.asarray(oy, dtype="float64")


def _cell_frame(fig, cell, box, xr, y_labels):
    """테두리 + 가로 격자 + 제목 + x/y 라벨 텍스트 (Axes/틱 기계 대체)."""
    x0, y0, w, h = box
    fig.add_artist(Rectangle((x0, y0), w, h, transform=fig.transFigure,
                             fill=False, edgecolor=_BORDER_COLOR, linewidth=0.6))
    fig.text(x0, y0 + h + 0.004, _fmt_title(cell), fontsize=7,
             color=_STATUS_TITLE_COLOR.get(cell.get("status"), "#111111"),
             ha="left", va="bottom")
    cap = _fmt_limit_caption(cell)      # 제목줄 우측에 limit 값 캡션
    if cap:
        fig.text(x0 + w, y0 + h + 0.004, cap, fontsize=5, color=_LIMIT_COLOR,
                 ha="right", va="bottom")
    # 가로 격자(y_labels 위치) + y 라벨
    n = len(y_labels)
    for i, lab in enumerate(y_labels):
        gy = y0 + h * (i / (n - 1) if n > 1 else 0)
        if 0 < i < n - 1:
            _add_line(fig, (x0, x0 + w), (gy, gy), _GRID_COLOR, lw=0.4)
        if lab is not None:
            fig.text(x0 - 0.002, gy, lab, fontsize=5, color=_TEXT_COLOR,
                     ha="right", va="center")
    # x축: 4분할 세로 격자선(눈금선) + min/mid/max 라벨
    if xr is not None:
        xmin, xmax = xr
        for frac in (0.25, 0.5, 0.75):
            gx = x0 + w * frac
            _add_line(fig, (gx, gx), (y0, y0 + h), _GRID_COLOR, lw=0.4)
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
    # ECDF 를 선이 아닌 점(마커)으로 — 전 고유값 포인트를 그대로 찍는다(다운샘플 아님).
    # 그리기 전 세로채움(웹과 동일)으로 이산/코드값 riser 를 세로 점기둥으로 메운다.
    for src in cell["sources"]:
        color, x, y = src[1], src[2], src[3]
        if len(x) == 0:
            continue
        x, y = _dist_fill_vertical(x, y)
        px = x0 + (np.asarray(x, dtype="float64") - xmin) / span * w
        py = y0 + np.asarray(y, dtype="float64") / 100.0 * h
        _add_markers(fig, px, py, color)
    _limit_lines(fig, cell, box, xr)


def _normal_x_range(cell):
    """정규분포 곡선용 x 범위 — 각 source μ±4σ + limit 포함, ±5% 마진. 없으면 None."""
    los, his = [], []
    for src in cell["sources"]:
        n = src[4]
        avg = src[5] if len(src) > 5 else None
        std = src[6] if len(src) > 6 else None
        if avg is None:
            continue
        if std is not None and float(std) > 0 and not (n is not None and n < 2):
            los.append(float(avg) - 4.0 * float(std))
            his.append(float(avg) + 4.0 * float(std))
        else:
            los.append(float(avg))
            his.append(float(avg))
    for v in (cell.get("lo"), cell.get("hi")):
        if v is not None:
            los.append(float(v))
            his.append(float(v))
    if not los:
        return None
    xmin, xmax = min(los), max(his)
    if xmax <= xmin:
        pad = abs(xmin) * 1e-6 or 0.5
        return xmin - pad, xmax + pad
    span = xmax - xmin
    return xmin - span * 0.05, xmax + span * 0.05


def _draw_hist_cell(fig, cell, box):
    """웹 Report 모드(distRenderNormal)와 동일한 정규분포(가우시안 PDF) 곡선.

    source 별 μ/σ 로 매끄러운 곡선(μ±4σ 256점). 축퇴(n<2 또는 σ≤0)는 x=μ 세로 스파이크.
    y축은 PDF 라 라벨 숨김, limit 세로선 유지.
    """
    xr = _normal_x_range(cell)
    if xr is None:
        _cell_frame(fig, cell, box, None, y_labels=(None,))
        return
    xmin, xmax = xr
    span = xmax - xmin
    curves, spikes, ymax = [], [], 0.0
    for src in cell["sources"]:
        color, n = src[1], src[4]
        avg = src[5] if len(src) > 5 else None
        std = src[6] if len(src) > 6 else None
        if avg is None:
            continue
        mu = float(avg)
        if std is None or float(std) <= 0 or (n is not None and n < 2):
            spikes.append((color, mu))       # 축퇴 → x=μ 세로 스파이크
            continue
        sd = float(std)
        xs = np.linspace(mu - 4.0 * sd, mu + 4.0 * sd, 256)
        coef = 1.0 / (sd * np.sqrt(2.0 * np.pi))
        ys = coef * np.exp(-0.5 * ((xs - mu) / sd) ** 2)
        curves.append((color, xs, ys))
        ymax = max(ymax, coef)
    _cell_frame(fig, cell, box, xr, y_labels=(None,))
    x0, y0, w, h = box
    ytop = ymax if ymax > 0 else 1.0
    for color, xs, ys in curves:
        px = x0 + (xs - xmin) / span * w
        py = y0 + ys / ytop * h
        _add_line(fig, px, py, color, lw=0.9)
    for color, mu in spikes:
        fx = x0 + (mu - xmin) / span * w
        _add_line(fig, (fx, fx), (y0, y0 + h), color, lw=0.9)
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
                "sources": [(name, color, x(np.ndarray), y(np.ndarray),
                             n|None, avg|None, std|None), ...]}, ...]}
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


def issue_cdf_px_size():
    """Issue Table 행별 단일 CDF PNG 의 (width_px, height_px)."""
    return (int(ISSUE_CELL_W_IN * DPI), int(ISSUE_CELL_H_IN * DPI))


def render_single_cdf(job) -> str:
    """Issue Table 행 1개용 단일 CDF PNG. job: {"cell": {...}, "out_path": str}.

    figure 전체를 셀 1칸으로 써서 Distribution 셀과 동일 스타일(점+눈금+limit)로 렌더.
    반환: out_path.
    """
    fig = Figure(figsize=(ISSUE_CELL_W_IN, ISSUE_CELL_H_IN), dpi=DPI)
    box = (_PAD_L, _PAD_B, 1.0 - _PAD_L - _PAD_R, 1.0 - _PAD_T - _PAD_B)
    _draw_cdf_cell(fig, job["cell"], box)
    fig.savefig(job["out_path"], format="png", facecolor="white")
    return job["out_path"]


def render_map_png_job(job) -> str:
    """Map Analysis 행 1개 → 웹-파리티 wafer map PNG. job:

    {"out_path","title","dies","frame","color_map","bin_order","product_type"}
    _map.render_wafer_map_png 으로 웹(Plotly heatmap)과 색/방향/라벨/프레임을 맞춘다
    (데스크톱 map_report 와 독립). 반환: out_path. 좌표(dies)가 비어 있으면 ValueError.
    """
    from ._map import render_wafer_map_png

    if not job.get("dies"):
        raise ValueError(f"{job['title']}: 좌표가 없습니다.")
    render_wafer_map_png(
        job["dies"], job["frame"], job["color_map"], job.get("bin_order") or [],
        product_type=job.get("product_type", ""),
        title=job["title"], out_path=job["out_path"],
    )
    return job["out_path"]

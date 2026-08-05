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
ISSUE_DPI = 192               # 썸네일만 2배 해상도(물리 크기는 pt 로 고정 — 확대 시 선명)
ISSUE_MAP_IN = ISSUE_CELL_H_IN   # Map 썸네일은 정사각(행 높이 = CDF 썸네일 높이)

# 웹 파리티 스타일 (distribution.js distSpecShapes/DIST_STATUS_BG + report_view.html .distg-*)
_LIMIT_COLOR = "#DC2626"      # 웹 spec line 색
_BORDER_COLOR = "#e2e4e8"     # 웹 .distg-card border
_GRID_COLOR = "#eeeeee"       # 웹 gridcolor #eee
_TEXT_COLOR = "#444444"
_TITLE_MAX_CHARS = 46
_MARKER_SIZE = 2.25           # CDF 점(마커) 크기(pt) — 웹 marker.size 3px = 3*72/96 pt
_STATUS_BG = {"fail": "#FDECEC", "cpk_low": "#FEF9E7", "ok": "#FFFFFF"}  # 웹 DIST_STATUS_BG
_TNO_COLOR = "#999999"        # 웹 .distg-tno
_NAME_COLOR = "#1A1A1F"       # 웹 .distg-name
_LIM_RANGE_COLOR = "#1d4ed8"  # 웹 .dist-lim-range
_LIM_UNIT_COLOR = "#15803d"   # 웹 .dist-lim-unit
_CPK_COLOR = "#555555"        # 웹 .distg-cpk
# Map Analysis 선택 좌표 마커 (웹 map_select.js chipMarkersFor 미러 — 7px 점 + 흰 테두리 1px).
# 단일 선택일 때만 점선 크로스헤어를 더한다(다중은 점 색으로 구분) — 웹과 동일.
_CHIP_MARKER_SIZE = 5.25      # 웹 marker.size 7px = 7*72/96 pt
_CHIP_EDGE_PT = 0.75          # 웹 marker.line.width 1px

# 셀 내부 플롯 영역 여백 (셀 크기에 대한 비율)
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 0.09, 0.03, 0.17, 0.13


def chunk_px_size(n_cells):
    """청크(n_cells 개 셀) PNG 의 (width_px, height_px) — 시트 배치용."""
    nrows = max(1, (int(n_cells) + NCOLS - 1) // NCOLS)
    return (int(NCOLS * CELL_W_IN * DPI), int(nrows * CELL_H_IN * DPI))


def cell_pt_size():
    """차트 셀 1칸의 부착 물리 크기 (width_pt, height_pt) — 숨김 항목 인덱스 좌표 계산용."""
    return (CELL_W_IN * 72.0, CELL_H_IN * 72.0)


def _fmt_name(cell):
    title = str(cell.get("title") or "")
    if len(title) > _TITLE_MAX_CHARS:
        title = title[:_TITLE_MAX_CHARS - 1] + "…"
    return title


def _fmt_limit_range(cell):
    """웹 카드 헤더 2줄째의 limit 범위 'lo ~ hi' — 둘 다 없으면 빈 문자열."""
    lo, hi = cell.get("lo"), cell.get("hi")
    if lo is None and hi is None:
        return ""
    return f"{_fmt_num(lo) if lo is not None else '-'} ~ {_fmt_num(hi) if hi is not None else '-'}"


def _text_w_frac(fig, text, fs, *, bold=False):
    """텍스트 폭 추정(figure fraction) — 헤더 조각을 이어 붙일 때만 쓴다.

    렌더러 측정(canvas draw)은 셀 수천 개 규모에서 비싸므로 문자폭 근사로 대체한다.
    조금 어긋나도 겹침만 없으면 되는 용도(웹 카드 헤더의 tno→이름 순서 재현).
    """
    per_char = fs * (0.66 if bold else 0.58)
    return len(str(text)) * per_char / (fig.get_size_inches()[0] * 72.0)


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


def _cell_outer_box(idx, nrows):
    """셀 idx 의 전체 영역(헤더 포함) — 상태 배경 fill 용 (웹 카드 배경 대응)."""
    r, c = divmod(idx, NCOLS)
    cw = 1.0 / NCOLS
    ch = 1.0 / nrows
    return c * cw, (nrows - 1 - r) * ch, cw, ch


def _x_range(cell):
    """셀 x 데이터 범위 (limit 선 + 선택 좌표 값 포함 — 항상 보이도록). ECDF x 는 정렬됨."""
    xs = [s[2] for s in cell["sources"] if len(s[2])]
    if not xs:
        return None
    xmin = min(float(x[0]) for x in xs)
    xmax = max(float(x[-1]) for x in xs)
    # 선택 좌표 값도 범위에 넣는다 — 웹은 autorange 라 마커가 축을 넓히므로 값이 데이터
    # 바깥(측정 이상치)이어도 잘리지 않는다. 넣지 않으면 그 점만 사라져 화면과 어긋난다.
    for v in (cell.get("lo"), cell.get("hi")) + tuple(
            c["value"] for c in (cell.get("chips") or [])):
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


_FILL_MAX_POINTS = 3000     # 세로 채움점 총량 상한 — stepY 하한 100/이값 (웹 DIST.FILL_MAX_POINTS 대칭)
_FILL_VISUAL_MAX_DY = 0.3   # 시각 연속성 캡(%) — 웹 DIST.FILL_VISUAL_MAX_DY 와 동일


def _dist_step_y(ys):
    """세로 채움 간격 = "단일 데이터 점 1개의 ECDF 증가량"(최소 양의 Δy, 첫 riser 0→ys[0] 포함).

    웹 distribution.js distStepY 포팅. 값이 전부 다른 진짜 희소 데이터는 모든 Δy 가 이 값과
    같아 채움 0(업샘플링 없음)이고, 동일값이 축약된 riser 만 개수에 비례해 채운다. 표본이
    매우 커 간격이 잘면 100/_FILL_MAX_POINTS 하한으로 채움점 폭증을 막는다. 반대로 표본이
    작아 단일점 증가량이 _FILL_VISUAL_MAX_DY 를 넘으면 그 값으로 캡해 누적 0~100% 에 marker
    빈 구간이 없게 한다(조밀한 데이터는 캡이 no-op — 기존과 픽셀 동일).
    """
    step = float("inf")
    prev = 0.0
    for v in ys:
        d = float(v) - prev
        if 1e-9 < d < step:
            step = d
        prev = float(v)
    if step == float("inf"):
        step = 0.8                      # 유효 riser 없음 — 폴백
    return min(max(step, 100.0 / _FILL_MAX_POINTS), _FILL_VISUAL_MAX_DY)


def _dist_fill_vertical(xs, ys, step_y=None):
    """ECDF riser(동일 x 의 세로 구간)를 세로 점으로 채운다. step_y 미지정 시 데이터에서
    유도(_dist_step_y) — "단일 점 1개의 증가량".

    웹 distribution.js distFillVertical 포팅 — 이산/코드값 항목이 세로 점기둥으로
    촘촘해 보이도록(선 없이 점만). 값이 전부 다른 진짜 희소 데이터는 각 riser Δy==step_y 라
    내부 루프 0회 → 채움 없이 원본 포인트만(업샘플링 없음). 다운샘플이 아니라 표시용 포인트
    추가(불변규칙 #5 의 sanctioned 세로채움).
    """
    n = len(xs)
    if n == 0:
        return xs, ys
    if step_y is None:
        step_y = _dist_step_y(ys)
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


def _cell_bg(fig, cell, outer):
    """셀 전체를 status 배경색으로 — 웹 카드(.distg-card + plot_bgcolor)와 동일."""
    if outer is None:
        return
    ox0, oy0, ow, oh = outer
    fig.add_artist(Rectangle((ox0, oy0), ow, oh, transform=fig.transFigure,
                             facecolor=_STATUS_BG.get(cell.get("status"), "#FFFFFF"),
                             edgecolor="none", zorder=0))


def _cell_header(fig, cell, box, outer):
    """웹 갤러리 카드 헤더 2줄 — ①tno + 항목명 ②limit 범위[unit] + cpk.

    report_view.html .distg-tno/.distg-name/.distg-lim/.distg-cpk 미러.
    """
    x0, y0, w, h = box
    oh = outer[3] if outer else (y0 + h)
    top = (outer[1] + outer[3]) if outer else (y0 + h)

    # 1줄: test_num(회색) → 항목명(bold)
    x = x0
    tno = cell.get("test_num")
    y1 = top - 0.02 * oh
    if tno not in (None, ""):
        fig.text(x, y1, str(tno), fontsize=5.5, color=_TNO_COLOR, ha="left", va="top")
        x += _text_w_frac(fig, tno, 5.5) + 0.004 * (1.0 / NCOLS)
    fig.text(x, y1, _fmt_name(cell), fontsize=7, color=_NAME_COLOR,
             ha="left", va="top", fontweight="bold")

    # 2줄: limit 범위(진한 파랑) + [unit](진한 초록) 좌측, cpk 우측
    y2 = top - 0.105 * oh
    x = x0
    lim = _fmt_limit_range(cell)
    if lim:
        fig.text(x, y2, lim, fontsize=5.5, color=_LIM_RANGE_COLOR,
                 ha="left", va="top", fontweight="bold")
        x += _text_w_frac(fig, lim, 5.5, bold=True) + 0.004 * (1.0 / NCOLS)
    units = cell.get("units")
    if units:
        fig.text(x, y2, f"[{units}]", fontsize=5.5, color=_LIM_UNIT_COLOR,
                 ha="left", va="top")
    cpk = cell.get("cpk")
    if cpk is not None:
        try:
            fig.text(x0 + w, y2, f"cpk {float(cpk):.2f}", fontsize=5.5,
                     color=_CPK_COLOR, ha="right", va="top")
        except (TypeError, ValueError):
            pass


def _cell_frame(fig, cell, box, xr, y_labels, outer=None):
    """상태 배경 + 테두리 + 가로 격자 + 웹 카드 헤더 + x/y 라벨 (Axes/틱 기계 대체)."""
    x0, y0, w, h = box
    _cell_bg(fig, cell, outer)
    fig.add_artist(Rectangle((x0, y0), w, h, transform=fig.transFigure,
                             fill=False, edgecolor=_BORDER_COLOR, linewidth=0.6))
    _cell_header(fig, cell, box, outer)
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


def _limit_lines(fig, cell, box, xr, *, labels=True, label_fs=5.5):
    """LSL/USL 세로 점선 + 세로(-90도) 라벨 — 웹 distSpecShapes/distSpecAnnos 미러."""
    x0, y0, w, h = box
    xmin, xmax = xr
    span = xmax - xmin
    for key, tag in (("hi", "USL"), ("lo", "LSL")):
        v = cell.get(key)
        if v is None:
            continue
        fx = x0 + (float(v) - xmin) / span * w
        _add_line(fig, (fx, fx), (y0, y0 + h), _LIMIT_COLOR, lw=0.9, ls="--")
        if labels:
            fig.text(fx, y0 + h, f"{tag} {_fmt_num(v)}", rotation=-90,
                     fontsize=label_fs, color=_LIMIT_COLOR, ha="left", va="top",
                     bbox=dict(facecolor="white", alpha=0.72, pad=0.6,
                               edgecolor="none"))


def _chip_markers(fig, cell, box, xr):
    """Map Analysis 선택 좌표의 이 항목 값을 (값, 누적%) 점으로 — 웹 chipMarkersFor 미러.

    값이 없는 chip(그 항목 측정 없음)은 건너뛴다. 단일 선택이면 그 색 점선 크로스헤어를
    더해 포커싱한다(다중은 점 색으로 구분 — 웹과 동일 규칙).
    """
    chips = [c for c in (cell.get("chips") or [])
             if c.get("value") is not None and c.get("cum_pct") is not None]
    if not chips:
        return
    x0, y0, w, h = box
    xmin, xmax = xr
    span = xmax - xmin
    for c in chips:
        px = x0 + (float(c["value"]) - xmin) / span * w
        py = y0 + float(c["cum_pct"]) / 100.0 * h
        if len(chips) == 1:                  # 단일: 점선 크로스헤어로 포커싱
            # lw 는 limit 선과 같은 0.9 — 웹 width:1px(=0.75pt)보다 가늘면 셀 폭에서
            # 안티에일리어싱에 묻혀 선이 사실상 사라진다(0.6 에서 확인).
            _add_line(fig, (px, px), (y0, y0 + h), c["color"], lw=0.9, ls=":")
            _add_line(fig, (x0, x0 + w), (py, py), c["color"], lw=0.9, ls=":")
        fig.add_artist(Line2D([px], [py], transform=fig.transFigure,
                              linestyle="none", marker="o", color=c["color"],
                              markersize=_CHIP_MARKER_SIZE,
                              markeredgecolor="#ffffff",
                              markeredgewidth=_CHIP_EDGE_PT, zorder=6))


def _draw_cdf_cell(fig, cell, box, outer=None):
    xr = _x_range(cell)
    _cell_frame(fig, cell, box, xr, y_labels=("0", "50", "100"), outer=outer)
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
    _chip_markers(fig, cell, box, xr)


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


def _draw_hist_cell(fig, cell, box, outer=None):
    """웹 Report 모드(distRenderNormal)와 동일한 정규분포(가우시안 PDF) 곡선.

    source 별 μ/σ 로 매끄러운 곡선(μ±4σ 256점). 축퇴(n<2 또는 σ≤0)는 x=μ 세로 스파이크.
    y축은 PDF 라 라벨 숨김, limit 세로선 유지.
    """
    xr = _normal_x_range(cell)
    if xr is None:
        _cell_frame(fig, cell, box, None, y_labels=(None,), outer=outer)
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
    _cell_frame(fig, cell, box, xr, y_labels=(None,), outer=outer)
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
        draw(fig, cell, _cell_box(idx, nrows), _cell_outer_box(idx, nrows))
    fig.savefig(out_path, format="png", facecolor="white")


def render_grid_chunk(job) -> str:
    """CDF/Histogram 그리드 청크 1장 렌더. job:

    {"kind": "cdf"|"hist", "out_path": str,
     "cells": [{"title","test_num","units","lo","hi","status","cpk",
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


def issue_cdf_pt_size():
    """Issue Table 썸네일의 부착 물리 크기 (width_pt, height_pt) — DPI 와 무관."""
    return (ISSUE_CELL_W_IN * 72.0, ISSUE_CELL_H_IN * 72.0)


def _mini_x_range(pts, cell):
    """미니셀 x 범위 — 데이터 ∪ limit 에 ±5% 가드밴드 (웹 renderMiniDistCell 과 동일)."""
    xmin, xmax = float("inf"), float("-inf")
    for x, _ in pts:
        if len(x):
            xmin = min(xmin, float(x[0]))
            xmax = max(xmax, float(x[-1]))
    for v in (cell.get("lo"), cell.get("hi")):
        if v is not None:
            xmin = min(xmin, float(v))
            xmax = max(xmax, float(v))
    if xmin == float("inf"):
        return None
    gb = (xmax - xmin) * 0.05 if xmax > xmin else (abs(xmin) * 0.05 or 1.0)
    return xmin - gb, xmax + gb


def _draw_mini_cdf_cell(fig, cell, box):
    """웹 Issue Table 미니셀(renderMiniDistCell) 미러 — 축·격자·제목 없이 점 + spec 선만.

    셀에 담긴 ECDF 를 그대로 그린다 — CPK 섹션 행은 호출부가 이미 Bin1(양품) ECDF 로
    갈아끼운 셀을 넘긴다(웹 data-bin1 미러).
    """
    pts = []
    for src in cell["sources"]:
        color, x, y = src[1], src[2], src[3]
        if len(x) == 0:
            continue
        pts.append((np.asarray(x, dtype="float64"),
                    np.asarray(y, dtype="float64"), color))
    xr = _mini_x_range([(x, y) for x, y, _ in pts], cell)
    if xr is None:
        return
    x0, y0, w, h = box
    xmin, xmax = xr
    span = xmax - xmin
    for x, y, color in pts:
        fx, fy = _dist_fill_vertical(x, y)
        px = x0 + (fx - xmin) / span * w
        py = y0 + fy / 100.0 * h
        _add_markers(fig, px, py, color)
    _limit_lines(fig, cell, box, xr, labels=False)   # 웹 미니셀은 라벨 없이 선만


def render_single_cdf(job) -> str:
    """Issue Table 행 1개용 단일 CDF PNG. job:

    {"cell": {...}, "out_path": str}
    웹 Issue Table 미니셀과 같은 포맷(축 숨김·최소 여백·spec 선). 반환: out_path.
    """
    fig = Figure(figsize=(ISSUE_CELL_W_IN, ISSUE_CELL_H_IN), dpi=ISSUE_DPI)
    m = 0.01                                   # 웹 margin 1px 대응(마커 반경만큼만 여유)
    _draw_mini_cdf_cell(fig, job["cell"], (m, m, 1.0 - 2 * m, 1.0 - 2 * m))
    fig.savefig(job["out_path"], format="png", facecolor="white")
    return job["out_path"]


def render_map_png_job(job) -> str:
    """Map Analysis 행 1개 → 웹-파리티 wafer map PNG. job:

    {"out_path","title","dies","color_map","chips"}
    _map.render_wafer_map_png 으로 웹(canvas 썸네일)과 색/방향/격자를 맞춘다
    (데스크톱 map_report 와 독립). 반환: out_path. 좌표(dies)가 비어 있으면 ValueError.
    """
    from ._map import render_wafer_map_png

    if not job.get("dies"):
        raise ValueError(f"{job['title']}: 좌표가 없습니다.")
    render_wafer_map_png(job["dies"], job["color_map"],
                         title=job["title"], out_path=job["out_path"],
                         chips=job.get("chips"))
    return job["out_path"]


def render_issue_maps_job(job) -> dict:
    """Issue Table Map 셀 썸네일을 한 맵의 dies 로 여러 bin 만큼 렌더. job:

    {"dies","color_map","targets": [(bin, out_path), ...]}
    같은 맵을 쓰는 행끼리 묶어 한 잡으로 보낸다 — die 목록을 bin 마다 자식 프로세스로
    피클 전송하지 않기 위함. 반환: {bin: out_path} (좌표 없으면 빈 dict).
    """
    from ._map import render_issue_map_png

    dies = job.get("dies") or []
    out = {}
    if not dies:
        return out
    for bin_value, out_path in job.get("targets") or []:
        render_issue_map_png(dies, job["color_map"], bin_value, out_path=out_path,
                             size_in=ISSUE_MAP_IN)
        out[str(bin_value)] = out_path
    return out


def render_temp_maps_job(job) -> dict:
    """Issue Table Temp 행 Map 썸네일 — 한 소스 dies 로 여러 항목만큼 렌더. job:

    {"dies", "targets": [(item, idx, out_path), ...]}
    render_issue_maps_job 과 같은 이유로 소스 단위로 묶는다(die 목록 피클 1회).
    반환: {item: out_path}.
    """
    from ._map import render_temp_map_png

    dies = job.get("dies") or []
    out = {}
    if not dies:
        return out
    for item, idx, out_path in job.get("targets") or []:
        render_temp_map_png(dies, idx, out_path=out_path, size_in=ISSUE_MAP_IN)
        out[str(item)] = out_path
    return out


def issue_map_pt_size():
    """Issue Table Map 썸네일의 부착 물리 크기 (width_pt, height_pt) — 정사각."""
    return (ISSUE_MAP_IN * 72.0, ISSUE_MAP_IN * 72.0)

"""Excel Download 전용 web_report 파리티 wafer bin map 렌더러 (matplotlib → PNG).

server/report/static/webreport/wafer_charts.js 의 Plotly heatmap 을 정적 PNG 로 재현한다.
데스크톱 honey_main 의 map_report.render_map_png 와는 독립(그쪽은 현행 유지) — 이 렌더러는
Excel Download 경로에서만 쓰며 웹과 색/방향/라벨/프레임을 맞춘다:

- Pass(bin "1") = 초록 고정, fail = 전 소스 합산 count 내림차순 전역 팔레트(세션 전체 공통).
- Y 는 아래로 증가(웨이퍼 관례, 웹 autorange:"reversed" 대응) — matplotlib origin="upper".
- fail 셀에 bin 번호 표시(웹 texttemplate), Pass 셀은 빈칸.
- 고정 웨이퍼 프레임(x_min..x_max/y_min..y_max) 크기로 그리고 빈 셀은 흰색.
- 정사각 figure 로 저장 → 시트 부착 시 500x500 박스에 넣어도 왜곡 없음.
- MDDI/PDDI 는 세로로 긴 chip 을 반영해 Y 셀을 늘림(웹 waferCellYScale=W/H).

ProcessPoolExecutor 자식에서 실행되므로 함수는 모듈 최상위·순수 데이터 인자만 받는다.
"""
from __future__ import annotations

import numpy as np

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure          # noqa: E402
from matplotlib.patches import Patch          # noqa: E402
from matplotlib.ticker import MultipleLocator  # noqa: E402

# wafer_charts.js 와 동일 값 — 세션 전체 차트(Summary/Fail Bin/Map)와 색을 일치시킨다.
PASS_COLOR = "#0ca30c"
PASS_BIN = "1"
FAIL_PALETTE = ["#2a78d6", "#eda100", "#e34948", "#4a3aa7", "#eb6834", "#e87ba4", "#1baf7a"]

_EMPTY_RGB = (1.0, 1.0, 1.0)        # 빈 셀(die 없음) = 흰색
_NA_COLOR = "#000000"               # 색맵에 없는 bin
_CELL_GAP_COLOR = "#ffffff"         # 셀 경계 흰 간격(웹 xgap/ygap 느낌)
_LABEL_COLOR = "#0b0b0b"
_MAJOR_TICK = 5
_MINOR_TICK = 1


def build_bin_color_map(map_rows):
    """전 map 행 bin_counts 합산 → 전역 bin 순서(Pass 먼저, fail count 내림차순) → 색맵.

    wafer_charts.js buildGlobalBinLegend + makeBinColorMap 미러. map 에 등장하는 fail bin 의
    팔레트 인덱스는 이 순서만으로 결정되므로(웹이 뒤에 붙이는 Fail Bin 시트 bin 은 map 색에
    영향 없음), map 색상이 웹과 동일해진다. Pass count 는 색/순서에 무관해 step 중복 합산해도
    무방(fail bin 은 칩당 한 step 맵에만 나와 단순 합산이 정확).
    반환: (color_map: {bin_str: hex}, order: [bin_str, ...]).
    """
    totals = {}
    is_pass = {}
    for row in map_rows:
        for bc in (row.get("bin_counts") or []):
            b = str(bc.get("bin"))
            totals[b] = totals.get(b, 0) + int(bc.get("count") or 0)
            is_pass[b] = bool(bc.get("is_pass")) or b == PASS_BIN
    order = sorted(totals.keys(),
                   key=lambda b: (0, 0) if is_pass.get(b) else (1, -totals[b]))
    color_map = {}
    fi = 0
    for b in order:
        if is_pass.get(b):
            color_map[b] = PASS_COLOR
        else:
            color_map[b] = FAIL_PALETTE[fi % len(FAIL_PALETTE)]
            fi += 1
    return color_map, order


def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _y_scale(product_type, nx, ny):
    """MDDI/PDDI 는 세로로 긴 chip 이라 Y 셀을 W/H 배 늘려 원형에 가깝게(웹 waferCellYScale)."""
    pt = (product_type or "").strip().upper()
    if pt not in ("MDDI", "PDDI"):
        return 1.0
    return (nx / ny) if (nx > 0 and ny > 0) else 1.0


def _frame_bounds(frame, dies):
    """frame 값 우선, None 이면 dies 좌표 min/max 폴백."""
    xs = [d.get("x") for d in dies if d.get("x") is not None]
    ys = [d.get("y") for d in dies if d.get("y") is not None]
    x_min = frame.get("x_min"); x_max = frame.get("x_max")
    y_min = frame.get("y_min"); y_max = frame.get("y_max")
    if x_min is None: x_min = min(xs)
    if x_max is None: x_max = max(xs)
    if y_min is None: y_min = min(ys)
    if y_max is None: y_max = max(ys)
    return int(x_min), int(x_max), int(y_min), int(y_max)


def render_wafer_map_png(dies, frame, color_map, bin_order, *,
                         product_type="", title="", out_path) -> None:
    """웹-파리티 wafer bin map 을 out_path(PNG, 정사각)로 저장.

    dies: [{"x","y","bin"(문자열)}], frame: {"x_min","x_max","y_min","y_max"},
    color_map/bin_order: build_bin_color_map 산출(전역).
    """
    x_min, x_max, y_min, y_max = _frame_bounds(frame, dies)
    nx = x_max - x_min + 1
    ny = y_max - y_min + 1

    # RGB 이미지 합성 (빈 셀 흰색). row 0 = y_min → origin="upper" 로 위쪽 배치 = Y 아래로 증가.
    img = np.empty((ny, nx, 3), dtype="float64")
    img[:] = _EMPTY_RGB
    labels = []              # (x, y, bin) — fail 셀 번호
    for d in dies:
        x = d.get("x"); y = d.get("y")
        if x is None or y is None:
            continue
        c = int(x) - x_min
        r = int(y) - y_min
        if not (0 <= c < nx and 0 <= r < ny):
            continue
        b = str(d.get("bin"))
        img[r, c] = _hex_to_rgb(color_map.get(b, _NA_COLOR))
        if b != PASS_BIN:
            labels.append((int(x), int(y), b))

    fig = Figure(figsize=(6.0, 6.0), dpi=110)      # 정사각 → 500x500 부착 왜곡 없음
    ax = fig.add_subplot(111)
    ax.imshow(
        img,
        origin="upper",
        extent=(x_min - 0.5, x_max + 0.5, y_max + 0.5, y_min - 0.5),   # y 아래로 증가
        interpolation="nearest",
        zorder=0,
    )
    ax.set_aspect(_y_scale(product_type, nx, ny))

    # 셀 경계 흰 간격 — 색 셀 사이에만 보인다(빈 셀 흰 배경엔 안 보임 = 웹 gap 과 동일 인상).
    x_bounds = np.arange(x_min - 0.5, x_max + 1.0, 1.0)
    y_bounds = np.arange(y_min - 0.5, y_max + 1.0, 1.0)
    ax.vlines(x_bounds, y_min - 0.5, y_max + 0.5, colors=_CELL_GAP_COLOR, linewidth=0.5, zorder=2)
    ax.hlines(y_bounds, x_min - 0.5, x_max + 0.5, colors=_CELL_GAP_COLOR, linewidth=0.5, zorder=2)

    # fail 셀 bin 번호 (웹 texttemplate). 셀 크기에 맞춰 폰트 축소.
    if labels:
        fs = max(2.0, min(8.0, 300.0 / max(nx, ny)))
        for x, y, b in labels:
            ax.text(x, y, b, ha="center", va="center", fontsize=fs,
                    color=_LABEL_COLOR, zorder=3)

    ax.xaxis.set_major_locator(MultipleLocator(_MAJOR_TICK))
    ax.xaxis.set_minor_locator(MultipleLocator(_MINOR_TICK))
    ax.yaxis.set_major_locator(MultipleLocator(_MAJOR_TICK))
    ax.yaxis.set_minor_locator(MultipleLocator(_MINOR_TICK))
    ax.set_xlim(x_min - 0.5, x_max + 0.5)
    ax.set_ylim(y_max + 0.5, y_min - 0.5)          # 반전(위=y_min, 아래=y_max)
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_title(title, fontsize=9)
    ax.tick_params(which="major", labelsize=8)
    ax.tick_params(which="minor", length=2)

    # 이 맵에 등장하는 bin 을 전역 순서로 범례(하단 가로) — figure 정사각 유지 위해 축 밖 하단.
    order = bin_order or list(color_map.keys())
    handles = [
        Patch(facecolor=color_map.get(b, _NA_COLOR), edgecolor="#888888",
              label=(f"{b} (Pass)" if b == PASS_BIN else b))
        for b in order
    ]
    fig.subplots_adjust(left=0.11, right=0.97, top=0.92, bottom=0.16)
    if handles:
        fig.legend(handles=handles, loc="lower center", ncol=min(len(handles), 8),
                   fontsize=7, frameon=False, title="Bin", title_fontsize=7,
                   bbox_to_anchor=(0.5, 0.0))
    fig.savefig(str(out_path), format="png", facecolor="white")

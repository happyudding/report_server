"""MAP 분석 — 입력 파일(source)별 2-D 웨이퍼 Bin 맵 생성 (matplotlib → PNG → xlsx).

각 die 의 XCoord/YCoord 가 좌표, Bin 이 색(value)인 채워진 격자 맵을 그린다.
- bin 1 = 파란색 고정, 나머지 bin 은 구분되는 색 자동 배정.
- x·y 축에 정수 좌표 스케일 표시.
- 좌표가 비어있는 source 는 건너뛰고 log_cb 로 안내 문구 표시.

PyQt/xlwings 비의존(렌더). xlwings 의존은 write_map_sheet(wb, ...) 한 곳뿐 —
xlsx_writer 의 단일 Excel 세션 안에서 PNG 만 시트로 부착한다(별도 Excel 재오픈 없음).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure          # noqa: E402
from matplotlib.patches import Patch          # noqa: E402
from matplotlib.ticker import MultipleLocator  # noqa: E402

MAP_COORD_ERROR_MSG = "X,Y 좌표가 맞지 않아 map 을 확인할수 없습니다."

# bin 1(Pass) 고정 파랑.
_BIN1_COLOR = "#1f77b4"
# bin 1 외 bin 코드에 순서대로 배정할 구분색 팔레트(파랑 계열 제외).
_OTHER_COLORS = [
    "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b",
    "#e377c2", "#bcbd22", "#17becf", "#7f7f7f", "#393b79",
    "#637939", "#8c6d31", "#843c39", "#7b4173", "#3182bd",
    "#31a354", "#756bb1", "#636363", "#e6550d", "#fd8d3c",
]
_EMPTY_RGB = (1.0, 1.0, 1.0)        # 빈 셀(die 없음) 배경 = 흰색
_NA_BIN_COLOR = "#000000"           # bin 값이 비수치인 die
_MAJOR_TICK = 5                     # 축 주눈금 단위
_MINOR_TICK = 1                     # 축 보조눈금 단위
_CHIP_GRID_COLOR = "#9a9a9a"        # 칩(셀) 격자선 색


def _color_for_bin(bin_code, palette_map):
    """bin 코드 → hex 색. 1 은 파랑 고정, 그 외는 등장 순서대로 팔레트 배정."""
    if bin_code == 1:
        return _BIN1_COLOR
    if bin_code is None:
        return _NA_BIN_COLOR
    if bin_code not in palette_map:
        idx = len(palette_map)
        palette_map[bin_code] = _OTHER_COLORS[idx % len(_OTHER_COLORS)]
    return palette_map[bin_code]


def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def render_map_png(xs, ys, bins, title, out_path) -> None:
    """채워진 격자 wafer bin map 을 out_path(PNG)로 저장.

    xs/ys/bins: 길이가 같은 1-D 배열(좌표 유효 행만, 개별 NaN 좌표는 호출자가 제거).
    bins 의 비수치 값은 'N/A' 색으로 표시한다.
    """
    xi = np.asarray(xs, dtype="int64")
    yi = np.asarray(ys, dtype="int64")
    bins_num = pd.to_numeric(pd.Series(bins), errors="coerce")

    xmin, xmax = int(xi.min()), int(xi.max())
    ymin, ymax = int(yi.min()), int(yi.max())
    nx = xmax - xmin + 1
    ny = ymax - ymin + 1

    # RGB 이미지 직접 합성 (배경 = 흰색). origin='lower' 로 y 위로 증가.
    img = np.empty((ny, nx, 3), dtype="float64")
    img[:] = _EMPTY_RGB

    palette_map = {}
    present_bins = []          # 범례용 (bin_code, hex) — 등장 순서
    seen = set()
    for r in range(len(xi)):
        b = bins_num.iat[r]
        bin_code = int(b) if pd.notna(b) else None
        hexc = _color_for_bin(bin_code, palette_map)
        img[yi[r] - ymin, xi[r] - xmin] = _hex_to_rgb(hexc)
        key = bin_code if bin_code is not None else "N/A"
        if key not in seen:
            seen.add(key)
            present_bins.append((key, hexc))

    fig = Figure(figsize=(6.2, 6.2), dpi=110)
    ax = fig.add_subplot(111)
    ax.imshow(
        img,
        origin="lower",
        extent=(xmin - 0.5, xmax + 0.5, ymin - 0.5, ymax + 0.5),
        interpolation="nearest",
        aspect="equal",
        zorder=0,
    )
    # 칩(셀) 격자선 — 셀 경계(반정수 위치)에 그림
    x_bounds = np.arange(xmin - 0.5, xmax + 1.0, 1.0)
    y_bounds = np.arange(ymin - 0.5, ymax + 1.0, 1.0)
    ax.vlines(x_bounds, ymin - 0.5, ymax + 0.5,
              colors=_CHIP_GRID_COLOR, linewidth=0.5, zorder=2)
    ax.hlines(y_bounds, xmin - 0.5, xmax + 0.5,
              colors=_CHIP_GRID_COLOR, linewidth=0.5, zorder=2)

    # 축 눈금: 주눈금 5단위 / 보조눈금 1단위
    ax.xaxis.set_major_locator(MultipleLocator(_MAJOR_TICK))
    ax.xaxis.set_minor_locator(MultipleLocator(_MINOR_TICK))
    ax.yaxis.set_major_locator(MultipleLocator(_MAJOR_TICK))
    ax.yaxis.set_minor_locator(MultipleLocator(_MINOR_TICK))
    ax.set_xlim(xmin - 0.5, xmax + 0.5)
    ax.set_ylim(ymin - 0.5, ymax + 0.5)
    ax.set_xlabel("XCoord")
    ax.set_ylabel("YCoord")
    ax.set_title(title)
    ax.tick_params(which="major", labelsize=8)
    ax.tick_params(which="minor", length=2)

    # bin ↔ 색 범례 (Pass=bin1 먼저, 그 외 코드 오름차순)
    def _legend_key(item):
        k = item[0]
        return (0, k) if isinstance(k, int) else (1, 0)

    handles = [
        Patch(facecolor=hexc, edgecolor="#888888",
              label=("Pass (bin 1)" if k == 1 else f"bin {k}"))
        for k, hexc in sorted(present_bins, key=_legend_key)
    ]
    if handles:
        ax.legend(handles=handles, loc="center left", bbox_to_anchor=(1.02, 0.5),
                  fontsize=8, framealpha=0.9, title="Bin")

    fig.tight_layout()
    fig.savefig(str(out_path), format="png", bbox_inches="tight")


def build_map_pngs(mass_data_map, log_cb=None):
    """각 source 별로 맵 PNG 생성.

    반환: (list[(label, png_path)], tmpdir). 호출자가 tmpdir 정리 책임(shutil.rmtree).
    좌표(XCoord/YCoord)가 전부 비어있는 source 는 건너뛰고
    log_cb(f"{name}: {MAP_COORD_ERROR_MSG}") 를 호출한다.
    """
    tmpdir = tempfile.mkdtemp(prefix="honey_map_")
    out = []
    for i, (name, md) in enumerate(mass_data_map.items()):
        meta = md.meta
        xs = pd.to_numeric(meta["XCoord"], errors="coerce")
        ys = pd.to_numeric(meta["YCoord"], errors="coerce")
        valid = xs.notna() & ys.notna()
        if not valid.any():
            if log_cb is not None:
                log_cb(f"{name}: {MAP_COORD_ERROR_MSG}")
            continue
        xs_v = xs[valid].to_numpy()
        ys_v = ys[valid].to_numpy()
        bins_v = meta["Bin"][valid].to_numpy()
        png_path = str(Path(tmpdir) / f"map_{i}.png")
        render_map_png(xs_v, ys_v, bins_v, title=str(name), out_path=png_path)
        out.append((str(name), png_path))
    return out, tmpdir


# ── xlsx 부착 (xlwings 의존은 여기뿐) ──────────────────────────────────────────

_COLS_PER_ROW = 3
_PIC_W = 500
_PIC_H = 500
_GAP = 24
_MARGIN = 10


def _unique_map_sheet_name(wb):
    existing = {s.name for s in wb.sheets}
    name = "Map"
    n = 2
    while name in existing:
        name = f"Map_{n}"
        n += 1
    return name


def _map_sheet_anchor(wb):
    """Map 시트를 yield 와 cpk 사이에 두기 위한 add() 앵커(kwargs) 결정.

    cpk 시트가 있으면 그 앞, 없으면 yield 뒤, 그것도 없으면 summary 뒤, 모두 없으면 맨 끝.
    """
    from ._xlsx_table_helpers import _report_sheet_display_name
    names = {s.name: s for s in wb.sheets}
    cpk = names.get(_report_sheet_display_name("cpk"))
    if cpk is not None:
        return {"before": cpk}
    yld = names.get(_report_sheet_display_name("yield"))
    if yld is not None:
        return {"after": yld}
    summ = names.get(_report_sheet_display_name("summary"))
    if summ is not None:
        return {"after": summ}
    return {"after": wb.sheets[wb.sheets.count - 1]}


def write_map_sheet(wb, map_pngs) -> None:
    """xlwings wb 에 'Map' 시트 추가 후 PNG 를 가로 3개·아래로 크게 배치.

    시트 위치는 yield 와 cpk 사이.
    """
    if not map_pngs:
        return
    ws = wb.sheets.add(_unique_map_sheet_name(wb), **_map_sheet_anchor(wb))
    for idx, (label, png_path) in enumerate(map_pngs):
        col = idx % _COLS_PER_ROW
        row = idx // _COLS_PER_ROW
        left = _MARGIN + col * (_PIC_W + _GAP)
        top = _MARGIN + row * (_PIC_H + _GAP)
        ws.pictures.add(
            png_path,
            link_to_file=False,
            save_with_document=True,
            name=f"map_pic_{idx}",
            left=left,
            top=top,
            width=_PIC_W,
            height=_PIC_H,
        )

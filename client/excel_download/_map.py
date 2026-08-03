"""Excel Download 전용 web_report 파리티 wafer bin map 렌더러 (matplotlib → PNG).

server/report/static/webreport/wafer_charts.js 의 canvas 썸네일(drawWaferThumb)을 정적
PNG 로 재현한다. 데스크톱 honey_main 의 map_report.render_map_png 와는 독립(그쪽은 현행
유지) — 이 렌더러는 Excel Download 경로에서만 쓰며 웹과 색/방향/격자를 맞춘다:

- Pass(bin "1") = 초록 고정, fail = 전 맵 합산 count 내림차순 전역 팔레트(세션 전체 공통).
- 앞 step 에서 이미 fail 난 die(payload 에 bin 없음, g=1) = 회색.
- 압축 격자(waferCompactGrid): die 가 실제 존재하는 x/y 만 남겨 빈 행/열을 제거한다.
- die 당 cell px 블록 + 셀 사이 1px 흰 격자선(각 chip 구분·윤곽선) — 웹과 동일 시각.
- Y 는 아래로 증가(웨이퍼 관례, 웹 autorange:"reversed").
- 셀에 bin 번호를 쓰지 않는다(웹과 동일 — Bin 은 시트의 Bin Legend 표로 읽는다).
- 축 눈금은 compact index → 실제 좌표 라벨(웹 Detail _compactTicks).

ProcessPoolExecutor 자식에서 실행되므로 함수는 모듈 최상위·순수 데이터 인자만 받는다.
"""
from __future__ import annotations

import numpy as np

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure          # noqa: E402

# wafer_charts.js 와 동일 값 — 세션 전체 차트(Summary/Fail Bin/Map)와 색을 일치시킨다.
PASS_COLOR = "#0ca30c"
PASS_BIN = "1"
FAIL_PALETTE = ["#2a78d6", "#eda100", "#e34948", "#4a3aa7", "#eb6834", "#e87ba4", "#1baf7a"]
GRAY_COLOR = "#c8ccd0"              # 앞 step fail die (웹 MAP_GRAY_HEX)

_EMPTY_RGB = (255, 255, 255)        # 빈 셀(die 없음) = 흰색
_NA_COLOR = "#9aa0a6"               # 색맵에 없는 bin (웹 TNO_OTHER_COLOR 계열 중립 회색)
DIM_COLOR = "#d9d9d9"               # 선택 bin 외 dim (웹 MAP_BIN_DIM_COLOR)
_TARGET_PX = 1000                   # 목표 한 변 픽셀 (웹은 표시 폭 기준 — PNG 는 고정)
_MAX_PX = 4096                      # 축당 픽셀 상한 (웹 cellFor 와 동일 메모리 보호)
_MAX_TICKS = 8                      # 웹 _compactTicks

# Map Analysis 선택 좌표 마커 (웹 .wafer-sel-marker 미러 — 15px 원 + 3px 테두리 + 흰 halo).
# 웹은 썸네일 표시 폭과 무관한 고정 px 라 die 수가 많으면 여러 die 를 덮는다 — 같은 사상으로
# **격자 크기와 무관한 고정 pt** 를 쓴다(플롯 6in 기준 웹 비율 ≈ 5%).
_SEL_MARKER_PT = 18.0
_SEL_EDGE_PT = 3.0
_SEL_HALO_PT = 1.5                  # 흰 외곽(웹 box-shadow 1.5px)


def build_global_bin_legend(map_rows):
    """전 맵 bin_counts 합산 → 범례 행 [{bin,count,is_pass,pct}] (Pass 먼저, fail count desc).

    wafer_charts.js buildGlobalBinLegend 포팅. step 분리 맵은 Pass 칩이 step 수만큼 중복
    등장하므로 Pass 는 소스별 step 맵 중 최솟값(= 마지막 step 의 Pass = 전체 Pass)만 반영하고,
    fail bin 은 칩당 한 step 맵에만 나오므로 그대로 합산한다.
    """
    totals = {}          # bin → {"count", "is_pass"}
    order = []
    step_pass_by_source = {}
    for m in map_rows or []:
        step_pass = 0
        for bc in (m.get("bin_counts") or []):
            b = str(bc.get("bin"))
            count = int(bc.get("count") or 0)
            is_pass = bool(bc.get("is_pass")) or b == PASS_BIN
            if b not in totals:
                order.append(b)
                totals[b] = {"count": 0, "is_pass": is_pass}
            if is_pass and m.get("step") is not None:
                step_pass += count
            else:
                totals[b]["count"] += count
        if m.get("step") is not None:
            step_pass_by_source.setdefault(m.get("source"), []).append(step_pass)
    pass_bin = next((b for b in order if totals[b]["is_pass"]), None)
    if pass_bin is not None:
        for arr in step_pass_by_source.values():
            totals[pass_bin]["count"] += min(arr)
    order.sort(key=lambda b: (0, 0) if totals[b]["is_pass"] else (1, -totals[b]["count"]))
    grand = sum(totals[b]["count"] for b in order)
    return [{"bin": b, "count": totals[b]["count"], "is_pass": totals[b]["is_pass"],
             "pct": (round(totals[b]["count"] / grand * 10000) / 100) if grand else 0}
            for b in order]


def build_bin_color_map(map_rows):
    """범례 순서 → 색맵. Pass=초록 고정, fail=count 내림차순 팔레트 순환 (웹 makeBinColorMap).

    범례(build_global_bin_legend)와 같은 순서에서 색을 유도하므로 Excel 의 맵·범례·웹 화면이
    모두 같은 bin=같은 색이 된다. 반환: (color_map {bin_str: hex}, order [bin_str, ...]).
    """
    legend = build_global_bin_legend(map_rows)
    color_map = {}
    fi = 0
    for row in legend:
        if row["is_pass"]:
            color_map[row["bin"]] = PASS_COLOR
        else:
            color_map[row["bin"]] = FAIL_PALETTE[fi % len(FAIL_PALETTE)]
            fi += 1
    return color_map, [row["bin"] for row in legend]


def build_bin_desc_map(yield_rows):
    """bin → 대표 fail item(avg 최대) — 웹 buildBinDescMap 포팅 (Bin Legend Description)."""
    best = {}
    for r in yield_rows or []:
        b = str(r.get("bin") or "")
        item = r.get("Item")
        if not b or b == PASS_BIN or not item:
            continue
        try:
            avg = float(r.get("avg") or 0)
        except (TypeError, ValueError):
            avg = 0.0
        if b not in best or avg > best[b][1]:
            best[b] = (item, avg)
    return {b: v[0] for b, v in best.items()}


def _hex_to_rgb255(hex_color):
    h = str(hex_color).lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _compact_grid(dies):
    """die 가 실제 존재하는 x/y 만 남긴 압축 격자 — 웹 waferCompactGrid 포팅.

    XPOS/YPOS 가 stride(띄엄띄엄)여도 빈 행/열을 제거해 빈 스트라이프 없이 그린다.
    반환: (x_idx{좌표:index}, y_idx, xs, ys).
    """
    xs = sorted({int(d["x"]) for d in dies if d.get("x") is not None})
    ys = sorted({int(d["y"]) for d in dies if d.get("y") is not None})
    return ({v: i for i, v in enumerate(xs)}, {v: i for i, v in enumerate(ys)}, xs, ys)


def _cell_for(n):
    """축 die 수 n → die 당 픽셀 (웹 cellFor 미러: 상한 4096/n, 하한 2)."""
    c = _TARGET_PX // n
    cap = _MAX_PX // n
    if c > cap:
        c = cap
    return max(c, 2)                 # 최소 2 (1px 격자선 확보)


def _compact_ticks(vals):
    """compact index → 실제 좌표 라벨, 양끝 포함 최대 8개 균등 (웹 _compactTicks)."""
    n = len(vals)
    if not n:
        return [], []
    cnt = min(n, _MAX_TICKS)
    ticks, labels, seen = [], [], set()
    for i in range(cnt):
        idx = 0 if cnt == 1 else round(i * (n - 1) / (cnt - 1))
        if idx in seen:
            continue
        seen.add(idx)
        ticks.append(idx + 0.5)      # 셀 중앙
        labels.append(str(vals[idx]))
    return ticks, labels


def _die_color(die, color_map):
    """die → hex 색. 앞 step fail(g=1, bin 키 없음)은 회색 — 웹 rgbFor 규칙."""
    if die.get("g") or "bin" not in die:
        return GRAY_COLOR
    return color_map.get(str(die.get("bin")), _NA_COLOR)


def _map_image(dies, color_of):
    """die 목록 → (RGB 블록 이미지, xs, ys). color_of(die) 가 die 색(hex)을 돌려준다.

    xs/ys 는 압축 격자의 실제 좌표값(축 눈금 라벨용). 격자 압축·die 당 픽셀 블록·
    1px 흰 격자선은 웹 drawWaferThumb 와 동일.
    """
    x_idx, y_idx, xs, ys = _compact_grid(dies)
    W, H = len(xs), len(ys)
    if W == 0 or H == 0:
        return None, xs, ys

    # 색 → 팔레트 인덱스(0=빈 셀 흰색)로 격자를 채운 뒤 블록 확대는 numpy 벡터 연산으로.
    codes = np.zeros((H, W), dtype="int32")
    palette = [_EMPTY_RGB]
    code_of = {}
    for d in dies:
        cx, cy = x_idx.get(d.get("x")), y_idx.get(d.get("y"))
        if cx is None or cy is None:
            continue
        hex_color = color_of(d)
        code = code_of.get(hex_color)
        if code is None:
            code = len(palette)
            code_of[hex_color] = code
            palette.append(_hex_to_rgb255(hex_color))
        codes[cy, cx] = code

    cell_x, cell_y = _cell_for(W), _cell_for(H)
    gap_x = 1 if cell_x >= 3 else 0        # cell 이 너무 작으면 격자선 생략(웹과 동일)
    gap_y = 1 if cell_y >= 3 else 0
    img = np.asarray(palette, dtype="uint8")[codes]
    img = np.repeat(np.repeat(img, cell_y, axis=0), cell_x, axis=1)
    # 각 die 블록의 오른쪽/아래 끝 1px 을 흰색으로 — 웹은 w=cellX-gapX 만 칠해 같은 자리가 빈다.
    if gap_x:
        img[:, cell_x - 1::cell_x, :] = 255
    if gap_y:
        img[cell_y - 1::cell_y, :, :] = 255
    return img, xs, ys


def _draw_sel_markers(ax, chips, xs, ys):
    """선택 좌표(chip)를 압축 격자 index 위치에 chip 색 원으로 — 웹 renderThumbMarkers 미러.

    호출부가 이미 source 로 걸러 넘긴다(웹도 `c.source !== m.source` 로 건너뛴다).
    좌표가 이 맵의 격자에 없으면(다른 웨이퍼의 die) 그 chip 만 조용히 건너뛴다.
    """
    x_idx = {v: i for i, v in enumerate(xs)}
    y_idx = {v: i for i, v in enumerate(ys)}
    for c in chips or []:
        try:
            cx = x_idx.get(int(c.get("x")))
            cy = y_idx.get(int(c.get("y")))
        except (TypeError, ValueError):
            continue
        if cx is None or cy is None:
            continue
        px, py = cx + 0.5, cy + 0.5          # extent 가 index 공간이라 셀 중앙
        color = c.get("color") or "#111111"
        # 흰 halo 를 먼저 깔아 어떤 bin 색 위에서도 원이 보이게 한다(웹 box-shadow 대응).
        ax.plot([px], [py], marker="o", markerfacecolor="none", markeredgecolor="#ffffff",
                markersize=_SEL_MARKER_PT, markeredgewidth=_SEL_EDGE_PT + 2 * _SEL_HALO_PT,
                linestyle="none", clip_on=False, zorder=4)
        ax.plot([px], [py], marker="o", markerfacecolor="none", markeredgecolor=color,
                markersize=_SEL_MARKER_PT, markeredgewidth=_SEL_EDGE_PT,
                linestyle="none", clip_on=False, zorder=5)


def render_wafer_map_png(dies, color_map, *, title="", out_path, chips=None) -> None:
    """웹-파리티 wafer bin map 을 out_path(PNG, 정사각)로 저장.

    dies: [{"x","y","bin"} | {"x","y","g":1}], color_map: build_bin_color_map 산출(전역).
    chips: Map Analysis 에서 선택한 좌표(이 맵의 source 것만) — 웹과 같은 색 원 마커.
    """
    img, xs, ys = _map_image(dies, lambda d: _die_color(d, color_map))
    if img is None:
        raise ValueError(f"{title}: 좌표가 없습니다.")
    W, H = len(xs), len(ys)

    fig = Figure(figsize=(6.0, 6.0), dpi=110)      # 정사각 → 500x500 부착 왜곡 없음
    ax = fig.add_subplot(111)
    # extent 로 index 공간(0..W, 0..H) 매핑 + y 반전(위=작은 y, 웨이퍼 관례).
    # 격자를 축 영역 비율대로 늘리지 않도록(웨이퍼가 타원으로 보임) 플롯 박스를 정사각으로
    # 고정한다(2026-07-23 요청). aspect="auto" 는 그 정사각 박스를 die 격자로 채우는 용도.
    ax.imshow(img, extent=(0, W, H, 0), aspect="auto", interpolation="nearest")
    ax.set_box_aspect(1)
    _draw_sel_markers(ax, chips, xs, ys)
    xt, xl = _compact_ticks(xs)
    yt, yl = _compact_ticks(ys)
    ax.set_xticks(xt)
    ax.set_xticklabels(xl)
    ax.set_yticks(yt)
    ax.set_yticklabels(yl)
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_title(title, fontsize=9)
    ax.tick_params(labelsize=8)
    fig.subplots_adjust(left=0.10, right=0.97, top=0.93, bottom=0.09)
    fig.savefig(str(out_path), format="png", facecolor="white")


def dim_color_map(color_map, bin_value):
    """선택 bin 만 원색, 나머지는 dim 회색인 색맵 — 웹 dimColorMap 포팅."""
    return {b: (c if str(b) == str(bin_value) else DIM_COLOR)
            for b, c in (color_map or {}).items()}


def render_issue_map_png(dies, color_map, bin_value, *, out_path,
                         size_in=1.15, dpi=192) -> None:
    """Issue Table Map 셀용 미니 웨이퍼 맵 — 해당 bin 만 원색, 나머지 dim (웹 미니셀 미러).

    축·제목·bin 번호 없이 격자만 정사각 PNG 로 그린다(웹 map-cell-mini 와 동일).
    앞 step 에서 이미 fail 난 die(g=1)는 회색 그대로.
    """
    dim = dim_color_map(color_map, bin_value)
    img, xs, ys = _map_image(dies, lambda d: _die_color(d, dim))
    if img is None:
        raise ValueError(f"bin {bin_value}: 좌표가 없습니다.")
    fig = Figure(figsize=(size_in, size_in), dpi=dpi)
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.imshow(img, extent=(0, len(xs), len(ys), 0), aspect="auto",
              interpolation="nearest")
    ax.set_axis_off()
    fig.savefig(str(out_path), format="png", facecolor="white")

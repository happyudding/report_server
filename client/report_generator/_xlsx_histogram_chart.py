"""histogram 차트 그리기 — distribution(ECDF) 패턴 재사용, y축만 빈도(count).

distribution 과 동일한 차트 그리드/헬퍼시트/COM 패턴을 재사용하되, y축이 누적분포
(0~1) 가 아니라 자동 bin 빈도(count) 곡선이다. subject 별로 source 마다 곡선 1개
(파란/팔레트색) + USL/LSL 빨강 세로선. 제목은 'Item[Unit], <USL>, <LSL>'.

차트 배치/제목/Item Index 는 _xlsx_distribution_chart(J) 의 헬퍼를, x축 범위/제목 배너는
_xlsx_chart_common(K) 을 재사용한다.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from ._xlsx_chart_common import (
    _finalize_title_row,
    _fmt_lim,
    _isnum,
    _put_title,
    _x_axis_range,
)
from ._xlsx_distribution_chart import (
    _CHART_H,
    _CHART_TITLE_ITEM_FONT,
    _CHART_W,
    _CHARTS_PER_ROW,
    _HISTOGRAM_BINS,
    _INDEX_COL,
    _MSO_TRUE,
    _PLOT_H,
    _PLOT_TOP,
    _PLOT_W,
    _RGB_BLUE,
    _ROWS_PER_CHART,
    _XL_CATEGORY,
    _XL_LOW,
    _XL_MARKER_NONE,
    _XL_PRIMARY,
    _XL_VALUE,
    _chart_com,
    _chart_pos,
    _hex_to_excel_rgb,
    _style_limit_series,
    _unique_helper_name,
)
from ._xlsx_profile import _prof_count
from ._xlsx_style import _col_letter
from ._xlsx_table_helpers import _report_sheet_display_name


def _write_histogram_phase(wb, result, colors=None, dist_progress_cb=None):
    """이미 열린 app/wb 에 "Histogram" 시트 + subject 별 빈도 히스토그램 차트를 추가한다.

    distribution 과 독립된 시트다. PNG 부착(fail_item/issue_table) 은 없다.
    반환: 정리할 임시 디렉토리 리스트 (histogram 은 PNG 미사용 → 항상 []).
    """
    names = [s.name for s in wb.sheets]
    hist_name = next((n for n in names if n.lower() == "histogram"), None)
    if hist_name:
        sh = wb.sheets[hist_name]
        for c in list(sh.charts):
            try:
                c.delete()
            except Exception:
                pass
        sh.clear()
    else:
        sh = wb.sheets.add(_report_sheet_display_name("histogram"),
                           after=wb.sheets[len(wb.sheets) - 1])
    _write_histogram(wb, sh, result, colors, dist_progress_cb=dist_progress_cb)
    try:
        sh.activate()
    except Exception:
        pass
    return []


def _write_histogram(wb, sh, result, colors=None, dist_progress_cb=None,
                     title="Histogram"):
    """각 subject 의 자동 bin 빈도 히스토그램 차트. source 별 곡선 + USL/LSL 세로선.

    데이터는 정리_H(X=bin 중심)/정리_HY(Y=빈도) 두 숨김 시트에 통째 1회 bulk write 하고,
    차트 series 는 그 시트의 (subject×source) 열을 bin 행구간으로 참조한다.
    차트 배치/제목/Item Index 는 distribution(_write_distribution) 패턴 재사용.
    """
    dists = result.distributions
    sources = result.sources
    source_data = result.dist_source_data
    if not dists or not source_data:
        sh.range("A1").value = "선택된 항목에 분포 데이터가 없습니다."
        return {}

    subj_names = [d.subject for d in dists]
    sd_map = dict(source_data)
    src_dfs, src_names = [], []
    for s in sources:
        df = sd_map.get(s)
        if df is None:
            continue
        src_dfs.append(df.reindex(columns=subj_names))   # 없는 subject 열 → NaN
        src_names.append(s)
    if not src_dfs:
        sh.range("A1").value = "선택된 항목에 분포 데이터가 없습니다."
        return {}

    df_hx, df_hy, nbins, ymax = _build_histogram_data(dists, src_dfs)

    # 숨김 헬퍼시트 (X=bin 중심, Y=빈도). 컬럼 = subject×source, 행 = bin
    existing = {s.name for s in wb.sheets}
    hx_name = _unique_helper_name("정리_H", existing)
    hy_name = _unique_helper_name("정리_HY", existing | {hx_name})
    ws_hx = wb.sheets.add(hx_name, after=sh)
    ws_hy = wb.sheets.add(hy_name, after=ws_hx)
    ws_hx.range("A1").options(index=True, header=True).value = df_hx
    ws_hy.range("A1").options(index=True, header=True).value = df_hy
    for w in (ws_hx, ws_hy):
        try:
            w.api.Visible = False
        except Exception:
            pass

    _put_title(sh, 8, title)
    sh.range((1, _INDEX_COL)).value = "Item Index (Ctrl+F)"
    sh.range((1, _INDEX_COL)).column_width = 26

    n_src = len(src_names)
    chart_map, index_entries = _draw_all_histogram(
        sh, dists, src_dfs, hx_name, hy_name, n_src, src_names, colors,
        nbins, ymax, dist_progress_cb)

    # Item Index 열 일괄 기입 (distribution 과 동일)
    if index_entries:
        max_idx = max(r for r, _ in index_entries)
        col_vals = [[None] for _ in range(2, max_idx + 1)]
        for r, subj in index_entries:
            col_vals[r - 2] = [subj]
        sh.range((2, _INDEX_COL), (max_idx, _INDEX_COL)).value = col_vals

    _prof_count("charts", len(chart_map))
    _finalize_title_row(sh)
    return chart_map


def _build_histogram_data(dists, src_dfs):
    """subject × source 별 자동 bin 히스토그램(중심, 빈도) 산출.

    각 (subject i, source k) 유한값에 numpy histogram(bins='auto') 적용 →
    bin 중심 = (edges[:-1]+edges[1:])/2, 빈도 = counts. 컬럼 인덱스 i*n_src+k 로
    X(중심)/Y(빈도) 두 DataFrame 에 NaN 패딩 적재. 반환:
    (df_hx, df_hy, nbins[i][k], ymax[i]=subject i 의 source 간 최대 빈도).
    """
    n_subj = len(dists)
    n_src = len(src_dfs)
    arrs = [df.to_numpy(dtype=float) for df in src_dfs]   # [k] (N, n_subj)
    centers_cols, counts_cols = [], []                    # index = i*n_src+k
    nbins = [[0] * n_src for _ in range(n_subj)]
    ymax = [0.0] * n_subj
    max_bins = 0
    for i in range(n_subj):
        for k in range(n_src):
            col = arrs[k][:, i] if arrs[k].size else np.empty(0)
            finite = col[np.isfinite(col)]
            if finite.size:
                counts, edges = np.histogram(finite, bins=_HISTOGRAM_BINS)
                centers = (edges[:-1] + edges[1:]) / 2.0
                counts = counts.astype(float)
            else:
                counts, centers = np.empty(0), np.empty(0)
            centers_cols.append(centers)
            counts_cols.append(counts)
            nbins[i][k] = int(centers.size)
            if counts.size:
                ymax[i] = max(ymax[i], float(counts.max()))
            max_bins = max(max_bins, int(centers.size))

    ncols = n_subj * n_src
    hx = np.full((max_bins, ncols), np.nan, dtype=float)
    hy = np.full((max_bins, ncols), np.nan, dtype=float)
    for idx in range(ncols):
        c, y = centers_cols[idx], counts_cols[idx]
        if c.size:
            hx[:c.size, idx] = c
            hy[:y.size, idx] = y
    cols = [f"c{idx}" for idx in range(ncols)]
    return (pd.DataFrame(hx, columns=cols), pd.DataFrame(hy, columns=cols), nbins, ymax)


def _draw_all_histogram(sh, dists, src_dfs, hx_name, hy_name, n_src, src_names,
                        colors, nbins, ymax, dist_progress_cb):
    """전체 subject 를 순회하며 histogram 차트를 1개씩 생성 (_draw_all_chart 미러).

    유한 데이터가 없는 subject 는 건너뛰되 grid 인덱스 i 는 유지해 칸 gap 을 보존한다.
    반환: (chart_map{subject: COM Chart}, index_entries[(row, subject)]).
    """
    arrs = [df.to_numpy(dtype=float) for df in src_dfs]
    chart_map, index_entries = {}, []
    done = 0
    n_charts = len(dists)
    for i, d in enumerate(dists):
        cols = [arr[:, i] for arr in arrs] if arrs else []
        finite = np.concatenate([c[np.isfinite(c)] for c in cols]) if cols else np.empty(0)
        if finite.size == 0:
            done += 1
            if dist_progress_cb:
                dist_progress_cb(done, n_charts)
            continue
        data_min, data_max = float(finite.min()), float(finite.max())
        data_med = float(np.median(finite))
        lo, hi = d.lower_limit, d.upper_limit
        is_fail = (_isnum(lo) and data_min < float(lo)) or (
            _isnum(hi) and data_max > float(hi))
        x_min, x_max = _x_axis_range(lo, hi, data_min, data_max, is_fail, data_med)
        y_top = max(1.0, ymax[i])
        try:
            chart_map[d.subject] = _histogram_draw_at_position(
                sh, i, d, hx_name, hy_name, n_src, src_names, colors,
                x_min, x_max, y_top, nbins[i])
        except Exception as _e:
            print(f"[hist-chart] skip subject={d.subject!r}: {_e}", file=sys.stderr)
        col = i % _CHARTS_PER_ROW
        grow = i // _CHARTS_PER_ROW
        index_entries.append((2 + grow * _ROWS_PER_CHART + col, d.subject))
        done += 1
        if dist_progress_cb:
            dist_progress_cb(done, n_charts)
    return chart_map, index_entries


def _histogram_draw_at_position(sh, i, d, hx_name, hy_name, n_src, src_names, colors,
                                x_min, x_max, y_top, nbins_row):
    """histogram 차트 1개를 새로 생성·서식 적용. 반환: COM Chart."""
    left, top = _chart_pos(i)
    ch = sh.charts.add(left, top, _CHART_W, _CHART_H)
    chart_api = _chart_com(ch)
    ch.chart_type = "xy_scatter_lines_no_markers"
    _histogram_title_set(chart_api, d)
    limit_count = _histogram_data_set(
        chart_api, d, hx_name, hy_name, i, n_src, src_names, colors, nbins_row,
        y_top, x_min)
    _histogram_layout_setting(chart_api, x_min, x_max, y_top, limit_count)
    return chart_api


def _histogram_title_set(chart_api, d):
    """차트 제목: 'Item[Unit], <USL>, <LSL>' (값만, USL→LSL 순; 값 없으면 '-')."""
    try:
        chart_api.HasTitle = True
        title = chart_api.ChartTitle
        unit = (d.unit or "").strip()
        title.Text = (f"{d.subject}[{unit}], "
                      f"{_fmt_lim(d.upper_limit)}, {_fmt_lim(d.lower_limit)}")
        tf = title.Font
        tf.Name = "Arial Black"
        tf.Size = _CHART_TITLE_ITEM_FONT
        title.Top = 0
    except Exception:
        pass


def _histogram_data_set(chart_api, d, hx_name, hy_name, i, n_src, src_names, colors,
                        nbins_row, y_top, x_min):
    """LSL/USL 세로선 + source 별 빈도 곡선 series 생성·스타일 (_chart_data_set 미러).

    series 1=LSL, 2=USL(없으면 차트 밖), 3+=source 곡선. 세로선 Y 는 0~y_top(빈도 축).
    반환: limit series 개수(범례 삭제 수).
    """
    sc = chart_api.SeriesCollection()
    lo = float(d.lower_limit) if _isnum(d.lower_limit) else None
    hi = float(d.upper_limit) if _isnum(d.upper_limit) else None
    xv0 = x_min if x_min is not None else 0.0
    limit_count = 0
    for lim, nm in ((lo, "LSL"), (hi, "USL")):
        s = sc.NewSeries()
        if lim is not None:
            s.XValues = (lim, lim)
            s.Values = (0.0, y_top)              # x=lim 세로선(빈도 0~y_top)
        else:
            s.XValues = (xv0, xv0)
            s.Values = (-2.0, -2.0)              # 차트 밖(series 인덱스 안정용)
        s.Name = nm
        _style_limit_series(s)
        limit_count += 1
    for k, name in enumerate(src_names):
        nb = nbins_row[k] if k < len(nbins_row) else 0
        col = _col_letter(2 + i * n_src + k)
        s = sc.NewSeries()
        if nb > 0:
            r1, r2 = 2, 1 + nb
            s.XValues = f"='{hx_name}'!${col}${r1}:${col}${r2}"
            s.Values = f"='{hy_name}'!${col}${r1}:${col}${r2}"
        else:
            s.XValues = (xv0, xv0)
            s.Values = (-2.0, -2.0)
        s.Name = str(name)
        rgb = _hex_to_excel_rgb(colors[k % len(colors)]) if colors else _RGB_BLUE
        _style_hist_series(s, rgb)
    return limit_count


def _style_hist_series(s, rgb=None):
    """Histogram 곡선 series: 선 보이기 + 마커 없음(빈도 폴리곤). 색 = source 팔레트/파랑."""
    try:
        line = s.Format.Line
        line.Visible = _MSO_TRUE
        if rgb is not None:
            line.ForeColor.RGB = rgb
    except Exception:
        pass
    try:
        s.MarkerStyle = _XL_MARKER_NONE
    except Exception:
        pass
    try:
        s.Smooth = True
    except Exception:
        pass


def _histogram_layout_setting(chart_api, x_min, x_max, y_top, limit_count):
    """축·plotarea·범례 (distribution _chart_layout_setting 미러, y축만 빈도(정수)).

    y축은 0~y_top 정수 눈금(분포의 % 대신). 범례에서 앞 limit_count(LSL/USL) 삭제.
    """
    try:
        yax = chart_api.Axes(_XL_VALUE, _XL_PRIMARY)
        yax.MinimumScale = 0
        yax.MaximumScale = y_top
        ytl = yax.TickLabels
        ytl.NumberFormatLocal = "0"
        ytl.Font.Size = 8
        yax.TickLabelPosition = _XL_LOW
    except Exception:
        pass
    try:
        xax = chart_api.Axes(_XL_CATEGORY, _XL_PRIMARY)
        if x_min is not None and x_max is not None and x_min < x_max:
            xax.MinimumScale = x_min
            xax.MaximumScale = x_max
        xax.HasMinorGridlines = False
        xax.TickLabels.Font.Size = 8
    except Exception:
        pass
    try:
        pa = chart_api.PlotArea
        pa.Width = _PLOT_W
        pa.Top = _PLOT_TOP
        pa.Height = _PLOT_H
    except Exception:
        pass
    try:
        chart_api.HasLegend = True
        leg = chart_api.Legend
        leg.Font.Size = 8
        for _ in range(limit_count):
            try:
                leg.LegendEntries(1).Delete()
            except Exception:
                pass
    except Exception:
        pass

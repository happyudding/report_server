"""distribution(ECDF) 차트 그리기 — subject별 순회 + 매번 새로 생성.

HONEY 원본 구조 참조: draw_all_chart → chart_draw_at_position →
  {chart_title_limit_set, chart_data_set, chart_layout_setting}.
template/duplicate 미사용. 차트당 COM 객체(api[1]/SeriesCollection/Axes/Legend 등)는
한 번만 바인딩해 호출 수를 최소화한다.

데이터는 정리(X)/정리_Y(Y) 두 숨김 시트에 통째 1회 bulk write 하고, 차트 series 는 그
시트의 subject 열을 source별 행구간으로 참조한다(compact-Y). 차트 그리기 함수의
line-by-line 디버그 트레이서(_trace_chart_lines)도 이 모듈에 둔다 — 추적 대상
(_chart_draw_at_position 호출 스택)이 모두 이 파일에 속해야 줄 단위 추적이 가능하다.
"""
from __future__ import annotations

import contextlib
import linecache
import os
import sys
import time

import numpy as np
import pandas as pd

from . import DEBUG_CHART_LINE_TRACE
from ._xlsx_chart_common import (
    _finalize_title_row,
    _isnum,
    _limit_caption,
    _put_title,
    _x_axis_range,
)
from ._xlsx_profile import (
    _dist_time,
    _emit_profile_info,
    _prof,
    _prof_count,
    _profile_info_time,
)
from ._xlsx_style import _col_letter

# ── 차트 그리드/크기 상수 ─────────────────────────────────────────────────────
_CHARTS_PER_ROW = 5
_HISTOGRAM_BINS = 20
# 차트 크기 — gap 없이 밀착 배치 (사용자 사양 324x198)
_CHART_W, _CHART_H = 324, 198
_PLOT_W, _PLOT_TOP, _PLOT_H = 280, 30, 167
# distribution 찾기(Ctrl+F)용 item 인덱스: 차트 그리드 오른쪽 열, 차트 한 행당 행 수
_INDEX_COL = 33  # AG열
_ROWS_PER_CHART = 12
# distribution 차트 그리드를 제목 배너 아래로 내리는 픽셀 오프셋
_DIST_TITLE_PX = 30

# Excel COM 상수 (distribution 차트 서식)
_XL_VALUE, _XL_CATEGORY, _XL_PRIMARY = 2, 1, 1
_XL_LOW = -4134               # xlLow (y축 TickLabelPosition)
_XL_MARKER_NONE   = -4142       # xlMarkerStyleNone
_XL_MARKER_CIRCLE = 8           # xlMarkerStyleCircle (dot 방식 series)
_MARKER_SIZE = 4             # data 점 크기(pt)
# distribution 차트 제목: item 명(subject) / 둘째줄 limit 캡션(Lo~Hi)
_CHART_TITLE_ITEM_FONT = 11   # item 명
_CHART_TITLE_CAP_FONT = 9     # Lo~Hi limit 캡션
_MSO_TRUE  = -1               # msoTrue  (LineFormat.Visible — 선 활성화)
_MSO_FALSE = 0                # msoFalse (LineFormat.Visible — 선 숨김)
_MSO_LINE_SYSDASH = 10        # msoLineSysDash (limit line)
_RGB_RED = 255               # RGB(255,0,0)
_RGB_BLUE = 255 * 65536      # RGB(0,0,255) — histogram 기본 곡선색(팔레트 없을 때)
_RGB_FAIL_BG = 255 + 255 * 256 + 204 * 65536  # RGB(255,255,204) 연노랑 (fail 차트 배경)


def _parse_int_set(raw):
    """\"100,101\" → {100, 101} (콤마 split + int, 잘못된 토큰은 무시)."""
    out = set()
    for tok in str(raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            pass
    return out


# Line-by-line chart debug tracer. Toggle DEBUG_CHART_LINE_TRACE in __init__.py.
# When off, sys.settrace is never called, so normal runtime is unchanged.
# 지정한 차트 순번(_LINE_TRACE_TARGETS, 1-based, skip 제외)에서 _chart_draw_at_position
# 호출 1건의 모든 실행 줄을 줄 단위 소요시간과 함께 stderr 로 출력한다.
_LINE_TRACE_ON = bool(DEBUG_CHART_LINE_TRACE)
_LINE_TRACE_TARGETS = _parse_int_set(os.environ.get("HONEY_CHART_LINE_TRACE_TARGETS", "10,11"))
_LINE_TRACE_FILE = os.path.abspath(__file__)
_LINE_TRACE_FILE_KEY = os.path.normcase(_LINE_TRACE_FILE)


def _hex_to_excel_rgb(hex_color):
    """'#RRGGBB' → Excel COM RGB 정수 (R + G*256 + B*65536). 실패 시 None."""
    try:
        s = str(hex_color).strip().lstrip("#")
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return r + (g << 8) + (b << 16)
    except Exception:
        return None


def _chart_pos(i):
    """차트 grid 좌상단 픽셀 (gap 없이 밀착, 제목 배너 아래)."""
    col = i % _CHARTS_PER_ROW
    grow = i // _CHARTS_PER_ROW
    return col * _CHART_W, _DIST_TITLE_PX + grow * _CHART_H


def sort_alldata(df, ascending=True):
    """각 열을 독립적으로 오름차순 정렬(NaN 말미). 행=정렬된 값, 열=subject."""
    out = df.copy()
    for c in df.columns:
        out[c] = df[c].sort_values(ascending=ascending, na_position="last").to_numpy()
    return out


def sort_data_to_percent(df):
    """열별 ECDF(rank/count). y = 행순위/notna개수, >1 또는 count=0 → NaN (모든 DUT 1점)."""
    inds = np.arange(df.shape[0]).reshape(-1, 1) + 1            # (N,1)
    counts = df.notna().sum().to_numpy()                        # (n_cols,)
    vals = np.divide(inds, counts, out=np.full(df.shape, np.nan, dtype=float),
                     where=counts != 0)
    vals[vals > 1] = np.nan
    return pd.DataFrame(vals, columns=df.columns)


def _build_compact_dist_y(df_x_list):
    """Build ECDF Y helper columns keyed by non-NaN count.

    X data still uses one column per subject. Y only depends on the number of
    valid points, so subjects with the same count can share one Y column.
    """
    if not df_x_list:
        return pd.DataFrame(), [], [], {
            "full_y_cells": 0, "compact_y_cells": 0, "unique_counts": 0,
            "unique_count_cases": 0, "valid_refs": 0, "trimmed_nan_tail": 0,
        }

    count_arrs = [df.notna().sum().astype(int).to_numpy() for df in df_x_list]
    unique_counts_by_source = []
    for counts in count_arrs:
        unique_counts = sorted({int(c) for c in counts})
        unique_counts_by_source.append(unique_counts or [0])
    max_unique_counts = max(len(counts) for counts in unique_counts_by_source)

    columns = [f"count_slot_{i + 1}" for i in range(max_unique_counts)]
    y_blocks = []
    y_cols_by_source = []
    for df, unique_counts, counts in zip(df_x_list, unique_counts_by_source, count_arrs):
        n_rows = df.shape[0]
        block = np.full((n_rows, max_unique_counts), np.nan, dtype=float)
        count_to_pos = {count: pos for pos, count in enumerate(unique_counts)}
        for count, pos in count_to_pos.items():
            if count <= 0:
                continue
            upto = min(count, n_rows)
            block[:upto, pos] = np.arange(1, upto + 1, dtype=float) / float(count)
        y_blocks.append(pd.DataFrame(block, columns=columns))
        y_cols_by_source.append([
            _col_letter(count_to_pos[int(count)] + 2) for count in counts
        ])

    df_y = pd.concat(y_blocks, ignore_index=True) if y_blocks else pd.DataFrame(columns=columns)
    full_y_cells = sum(df.shape[0] * df.shape[1] for df in df_x_list)
    compact_y_cells = df_y.shape[0] * df_y.shape[1]
    valid_refs = sum(int(counts.sum()) for counts in count_arrs)
    return df_y, y_cols_by_source, count_arrs, {
        "full_y_cells": full_y_cells,
        "compact_y_cells": compact_y_cells,
        "unique_counts": max_unique_counts,
        "unique_count_cases": sum(len(counts) for counts in unique_counts_by_source),
        "valid_refs": valid_refs,
        "trimmed_nan_tail": full_y_cells - valid_refs,
    }


def _unique_helper_name(base, existing):
    """존재 시트명과 충돌 회피한 헬퍼 시트명(정리/정리_Y)."""
    name, n = base, 2
    while name in existing:
        name = f"{base}{n}"
        n += 1
    return name


# ── distribution 차트 그리기 (subject별 순회 + 매번 새로 생성) ────────────────

def _draw_all_chart(sh, dists, x_arrs, x_name, y_name, cnt_list, src_names, colors,
                    y_cols_by_source, count_matrix, n_charts, dist_progress_cb):
    """전체 subject 를 순회하며 차트를 1개씩 새로 생성.

    유한 데이터가 없는 subject 는 건너뛰되 grid 인덱스 i 는 유지해 칸 gap 을 보존한다.
    반환: (chart_map{subject: COM Chart}, index_entries[(row, subject)]).
    """
    chart_map = {}
    index_entries = []
    done = 0
    chart_seq = 0   # skip 제외, 실제 생성 시도한 차트의 1-based 순번 (line-trace 타깃)
    for i, d in enumerate(dists):
        with _dist_time("dist.loop.finite_scan"):
            cols = [arr[:, i] for arr in x_arrs]
            finite = np.concatenate([c[np.isfinite(c)] for c in cols]) if cols else np.empty(0)
            if finite.size:
                data_min, data_max = float(finite.min()), float(finite.max())
                data_med = float(np.median(finite))
        if finite.size == 0:
            done += 1
            if dist_progress_cb:
                dist_progress_cb(done, n_charts)
            continue
        with _dist_time("dist.loop.axis_range"):
            lo, hi = d.lower_limit, d.upper_limit
            is_fail = (_isnum(lo) and data_min < float(lo)) or (
                _isnum(hi) and data_max > float(hi))
            x_min, x_max = _x_axis_range(lo, hi, data_min, data_max, is_fail, data_med)

        chart_seq += 1
        try:
            with _trace_chart_lines(chart_seq, d.subject):
                chart_map[d.subject] = _chart_draw_at_position(
                    sh, i, d, x_name, y_name, i, cnt_list, src_names, colors,
                    x_min, x_max, is_fail, y_cols_by_source, count_matrix)
        except Exception as _e:
            print(f"[dist-chart] skip subject={d.subject!r}: {_e}", file=sys.stderr)
        col = i % _CHARTS_PER_ROW
        grow = i // _CHARTS_PER_ROW
        index_entries.append((2 + grow * _ROWS_PER_CHART + col, d.subject))
        done += 1
        if dist_progress_cb:
            dist_progress_cb(done, n_charts)
    return chart_map, index_entries


def _chart_draw_at_position(sh, i, d, x_sheet, y_sheet, col_idx, cnt_list, src_names,
                            colors, x_min, x_max, is_fail, y_cols_by_source, count_matrix):
    """차트 1개를 새로 생성·서식 적용 (정리/정리_Y range 참조). 반환: COM Chart."""
    left, top = _chart_pos(i)
    with _prof("dist.series_add"):
        with _dist_time("dist.loop.chart_add"):
            ch = sh.charts.add(left, top, _CHART_W, _CHART_H)
            chart_api = _chart_com(ch)   # api[1] 1회 바인딩, 이하 하위 함수에 전달
        with _dist_time("dist.loop.chart_type"):
            ch.chart_type = "xy_scatter_lines_no_markers"
    _chart_title_limit_set(chart_api, d)
    with _prof("dist.style"):
        limit_count = _chart_data_set(
            chart_api, d, x_sheet, y_sheet, col_idx, cnt_list, src_names, x_min,
            colors, y_cols_by_source, count_matrix)
    with _prof("dist.format"):
        _chart_layout_setting(chart_api, x_min, x_max, is_fail, limit_count)
    return chart_api


def _chart_title_limit_set(chart_api, d):
    """차트 제목: item 명 + 둘째 줄 (LO~HI unit) 캡션 (현재 서식 유지)."""
    with _dist_time("dist.loop.title_format"):
        try:
            chart_api.HasTitle = True
            title = chart_api.ChartTitle
            cap = _limit_caption(d)          # item 명 아래 줄: (LO ~ HI units)
            title.Text = d.subject + "\n" + cap
            tf = title.Font
            tf.Name = "Arial Black"
            tf.Size = _CHART_TITLE_ITEM_FONT
            try:                             # 둘째 줄(캡션)은 작게
                title.Characters(len(d.subject) + 2, len(cap)).Font.Size = _CHART_TITLE_CAP_FONT
            except Exception:
                pass
            title.Top = 0
        except Exception:
            pass


def _chart_data_set(chart_api, d, x_sheet, y_sheet, col_idx, cnt_list, src_names, x_min,
                    colors, y_cols_by_source, count_matrix):
    """LSL/USL + source series 를 생성하고 같은 객체에 스타일까지 한 번에 적용.

    series 1=LSL, 2=USL(없으면 차트 밖 -2,-2), 3+=source. 정리/정리_Y 시트의 subject
    열을 source별 행구간으로 참조(compact-Y). SeriesCollection 은 1회 바인딩, 각 series
    는 NewSeries 직후 같은 객체에 스타일 적용 — COM 재조회 없음. limit line 스타일도
    series 객체를 보유한 여기서 적용(COM 최소화). 반환: limit series 개수(범례 삭제 수).
    """
    col = _col_letter(col_idx + 2)   # A=index, B=subject0
    lo = float(d.lower_limit) if _isnum(d.lower_limit) else None
    hi = float(d.upper_limit) if _isnum(d.upper_limit) else None
    xv0 = x_min if x_min is not None else 0.0
    sc = chart_api.SeriesCollection()
    limit_count = 0
    with _dist_time("dist.loop.series_limits"):
        for lim, nm in ((lo, "LSL"), (hi, "USL")):
            s = sc.NewSeries()
            idx = sc.Count
            if lim is not None:
                x_val = f"{lim:.10g}"
                s.Formula = f'=SERIES("{nm}",{{{x_val},{x_val}}},{{-1,1}},{idx})'
            else:
                x_val = f"{xv0:.10g}"
                s.Formula = f'=SERIES("{nm}",{{{x_val},{x_val}}},{{-2,2}},{idx})'
            _style_limit_series(s)
            limit_count += 1
    y = 0
    with _dist_time("dist.loop.series_sources"):
        for k, name in enumerate(src_names):
            n = cnt_list[k]
            valid_count = min(int(count_matrix[k][col_idx]), n)
            r1 = y + 2
            y += n
            s = sc.NewSeries()
            if valid_count > 0:
                r2 = r1 + valid_count - 1
                x_ref = f"='{x_sheet}'!${col}${r1}:${col}${r2}"
                y_col = y_cols_by_source[k][col_idx]
                y_ref = f"='{y_sheet}'!${y_col}${r1}:${y_col}${r2}"
                s.XValues = x_ref
                s.Values = y_ref
            else:
                s.XValues = (x_min if x_min is not None else 0.0,
                             x_min if x_min is not None else 0.0)
                s.Values = (-2.0, -2.0)
            s.Name = str(name)
            rgb = _hex_to_excel_rgb(colors[k % len(colors)]) if colors else None
            _style_data_series(s, rgb)
    return limit_count


def _chart_layout_setting(chart_api, x_min, x_max, is_fail, limit_count):
    """축·gridline·plotarea·fail 배경·범례 삭제 (COM 객체별 1회 바인딩, 현재 서식 유지).

    range 재바인딩이 없는 신규 차트라 fail 이 아니면 ChartArea 는 기본(흰색) 그대로 둔다.
    범례는 맨 마지막에 앞 limit_count(=LSL/USL)개를 인덱스로 삭제.
    """
    with _dist_time("dist.loop.axis_format"):
        try:
            yax = chart_api.Axes(_XL_VALUE, _XL_PRIMARY)
            yax.MinimumScale = 0
            yax.MaximumScale = 1
            yax.MajorUnit = 0.2
            yax.HasMinorGridlines = True
            ytl = yax.TickLabels
            ytl.NumberFormatLocal = "0%"
            ytl.Font.Size = 8
            yax.TickLabelPosition = _XL_LOW
        except Exception:
            pass
        try:
            xax = chart_api.Axes(_XL_CATEGORY, _XL_PRIMARY)
            if x_min is not None and x_max is not None and x_min < x_max:
                xax.MinimumScale = x_min
                xax.MaximumScale = x_max
            xax.HasMinorGridlines = True
            xax.TickLabels.Font.Size = 8
        except Exception:
            pass
    with _dist_time("dist.loop.plot_format"):
        try:
            pa = chart_api.PlotArea
            pa.Width = _PLOT_W
            pa.Top = _PLOT_TOP
            pa.Height = _PLOT_H
        except Exception:
            pass
    if is_fail:
        with _dist_time("dist.loop.fail_bg"):
            try:
                chart_api.ChartArea.Interior.Color = _RGB_FAIL_BG
            except Exception:
                pass
    with _dist_time("dist.loop.legend_format"):
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


# ── 차트 생성 함수 line-by-line 디버그 트레이서 ──────────────────────────────
# _chart_draw_at_position 호출 1건(그 호출 스택 전체 — title/data/layout 등 하위 함수
# + COM 조작 줄 포함)의 모든 실행 줄을 줄 단위 소요시간과 함께 stderr 로 출력한다.
# DEBUG_CHART_LINE_TRACE is off by default, so sys.settrace is not called in normal runs.
# 동작·성능에 영향 없음.

def _chart_line_tracer(state):
    """sys.settrace 콜백. 이 모듈 파일에 속한 프레임의 'line' 이벤트만 기록한다.

    다른 파일(numpy/pandas 등 외부 라이브러리)로 진입하는 프레임은
    None 을 반환해 추적을 차단 — 소스가 없어 줄 단위 추적이 불가능한 컴파일 확장
    (pywin32 COM 등) 및 무관한 노이즈를 거른다. 활성 구간 자체가
    _chart_draw_at_position 호출 1건으로 한정되므로 실질적으로는 그 호출 스택만
    보게 된다.

    'line' 이벤트마다 "직전 줄"의 (filename, lineno, func, elapsed) 를
    state['entries'] 에 push 하고 현재 줄을 state['last'] 로 갱신한다 — 호출 스택을
    가로지른 단일 시간순 로그(실제 실행 순서 그대로).
    """
    def tracer(frame, event, _arg):
        if os.path.normcase(frame.f_code.co_filename) != _LINE_TRACE_FILE_KEY:
            return None
        if event == "call":
            return tracer
        if event == "line":
            now = time.perf_counter()
            last = state["last"]
            if last is not None:
                state["entries"].append((last[0], last[1], now - last[2]))
            state["last"] = (frame.f_lineno, frame.f_code.co_name, now)
            return tracer
        return tracer
    return tracer


def _dump_chart_line_trace(seq, subject, entries, total_elapsed):
    sep = "=" * 88
    print(sep, file=sys.stderr)
    print(f"[chart-line-trace] chart_seq={seq} subject={subject!r} "
          f"lines={len(entries)} total={total_elapsed * 1000:.3f}ms", file=sys.stderr)
    print(sep, file=sys.stderr)
    for lineno, func, elapsed in entries:
        src = linecache.getline(_LINE_TRACE_FILE, lineno).rstrip()
        print(f"  {func}:{lineno} | {elapsed * 1000:8.3f} ms | {src}", file=sys.stderr)
    print(sep, file=sys.stderr, flush=True)


@contextlib.contextmanager
def _trace_chart_lines(seq, subject):
    """대상 차트(seq)일 때만 _chart_draw_at_position 호출 1건을 줄 단위로 추적.

    대상이 아니면 sys.settrace 호출 없이 즉시 통과 → 다른 모든 차트·코드 경로는
    평소와 100% 동일하게 실행된다.
    """
    if not _LINE_TRACE_ON or seq not in _LINE_TRACE_TARGETS:
        yield
        return
    state = {"entries": [], "last": None}
    old_trace = sys.gettrace()
    sys.settrace(_chart_line_tracer(state))
    t0 = time.perf_counter()
    try:
        yield
    finally:
        sys.settrace(old_trace)
        last = state["last"]
        if last is not None:
            state["entries"].append((last[0], last[1], time.perf_counter() - last[2]))
        _dump_chart_line_trace(seq, subject, state["entries"], time.perf_counter() - t0)


def _write_distribution(wb, sh, result, colors=None, dist_progress_cb=None,
                        dists=None, sources=None, source_data=None, title="Distribution",
                        profile_label="main"):
    """각 subject 의 누적분포(ECDF) 차트 — 모든 DUT 가 1점(중복 제거 없음).

    source(input file)별 데이터 series + LSL/USL 세로 한계선(series 1,2, COM 배열리터럴).
    데이터는 정리(X)/정리_Y(Y) 두 시트에 통째 1회 bulk write, 차트 series 는 그 시트의
    subject 열을 source별 행구간으로 참조한다. 차트는 gap 없이 밀착 배치·독립 생성.

    dists/sources/source_data: diff compare 의 a_only/b_only 시트용 override (None 이면
    result.distributions / result.sources / result.dist_source_data 사용).
    """
    dists = result.distributions if dists is None else dists
    sources = result.sources if sources is None else sources
    source_data = result.dist_source_data if source_data is None else source_data
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

    # ── 데이터 변환: 열별 정렬(X) + rank/count(Y), source별 블록 concat ──────────
    with _profile_info_time("distribution.data_write"):
        with _prof("dist.data_write"):
            with _dist_time("dist.data_prepare"):
                df_x_list = [sort_alldata(df, True).reset_index(drop=True) for df in src_dfs]
                df_x = pd.concat(df_x_list, ignore_index=True)
                df_y, y_cols_by_source, count_matrix, y_stats = _build_compact_dist_y(df_x_list)
                cnt_list = [df.shape[0] for df in df_x_list]   # source별 DUT 수(모든 subject 균일)
                reduction = 0.0
                if y_stats["full_y_cells"]:
                    reduction = 100.0 * (1.0 - (
                        y_stats["compact_y_cells"] / y_stats["full_y_cells"]))
                _emit_profile_info(
                    "Dist helper Y mode: sheet count_cases "
                    f"x_cells={df_x.shape[0] * df_x.shape[1]:,} "
                    f"full_y_cells={y_stats['full_y_cells']:,} "
                    f"compact_y_cells={y_stats['compact_y_cells']:,} "
                    f"unique_counts={y_stats['unique_counts']} "
                    f"unique_count_cases={y_stats['unique_count_cases']} "
                    f"valid_refs={y_stats['valid_refs']:,} "
                    f"trimmed_nan_tail={y_stats['trimmed_nan_tail']:,} "
                    f"reduction={reduction:.1f}%"
                )

            with _dist_time("dist.helper_sheet_write"):
                existing = {s.name for s in wb.sheets}
                x_name = _unique_helper_name("정리", existing)
                y_name = _unique_helper_name("정리_Y", existing | {x_name})
                ws_x = wb.sheets.add(x_name, after=sh)
                ws_y = wb.sheets.add(y_name, after=ws_x)
                ws_x.range("A1").options(index=True, header=True).value = df_x
                ws_y.range("A1").options(index=True, header=True).value = df_y
                for w in (ws_x, ws_y):
                    try:
                        w.api.Visible = False
                    except Exception:
                        pass

    _put_title(sh, 8, title)
    sh.range((1, _INDEX_COL)).value = "Item Index (Ctrl+F)"
    sh.range((1, _INDEX_COL)).column_width = 26

    # 정렬값 numpy 캐시(데이터 min/max·step 판정용) — 셀 읽기 없이
    x_arrs = [df.to_numpy(dtype=float) for df in df_x_list]   # [source](N, n_subj)

    n_charts = len(dists)
    with _profile_info_time(f"distribution.chart_create[{profile_label}]"):
        chart_map, index_entries = _draw_all_chart(
            sh, dists, x_arrs, x_name, y_name, cnt_list, src_names, colors,
            y_cols_by_source, count_matrix, n_charts, dist_progress_cb)

    # Item Index 열 일괄 기입
    if index_entries:
        max_idx = max(r for r, _ in index_entries)
        col_vals = [[None] for _ in range(2, max_idx + 1)]
        for r, subj in index_entries:
            col_vals[r - 2] = [subj]
        sh.range((2, _INDEX_COL), (max_idx, _INDEX_COL)).value = col_vals

    _prof_count("charts", len(chart_map))
    _finalize_title_row(sh)
    return chart_map


def _chart_com(ch):
    """xlwings Chart → COM Chart 객체. ch.api 가 (ChartObject, Chart) 튜플일 수 있음."""
    api = ch.api
    if isinstance(api, tuple):
        return api[1]
    return getattr(api, "Chart", api)


def _style_limit_series(s):
    """limit line series: 빨강 system dash + 마커 제거 (선만)."""
    try:
        line = s.Format.Line
        line.DashStyle = _MSO_LINE_SYSDASH
        line.ForeColor.RGB = _RGB_RED
    except Exception:
        pass
    try:
        s.MarkerStyle = _XL_MARKER_NONE
    except Exception:
        pass


def _style_data_series(s, rgb=None):
    """Source data series: always show all DUT values as dot markers."""
    try:
        s.Format.Line.Visible = _MSO_FALSE
    except Exception:
        pass
    try:
        s.MarkerStyle = _XL_MARKER_CIRCLE
        s.MarkerSize = _MARKER_SIZE
    except Exception:
        pass
    if rgb is not None:
        try:
            s.MarkerBackgroundColor = rgb
            s.MarkerForegroundColor = rgb
        except Exception:
            pass

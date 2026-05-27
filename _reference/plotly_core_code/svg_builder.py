import math
from html import escape

import pandas as pd

from config import LIMIT_COLOR, LIMIT_LINE_WIDTH, MARKER_SIZE

SVG_WIDTH = 400
SVG_HEIGHT = 275
DEFAULT_MARGIN = {"l": 45, "r": 20, "t": 65, "b": 40}


def _is_num(v):
    return v is not None and not pd.isna(v)


def _fmt(v):
    if not _is_num(v):
        return "?"
    return f"{float(v):g}"


def _clean_unit(unit):
    return "" if unit is None or pd.isna(unit) else str(unit)


def _ticks(vmin, vmax, max_ticks=6):
    if not math.isfinite(vmin) or not math.isfinite(vmax):
        return []
    if vmin == vmax:
        return [vmin]
    span = abs(vmax - vmin)
    raw = span / max(1, max_ticks - 1)
    mag = 10 ** math.floor(math.log10(raw))
    norm = raw / mag
    if norm <= 1:
        step = mag
    elif norm <= 2:
        step = 2 * mag
    elif norm <= 5:
        step = 5 * mag
    else:
        step = 10 * mag
    start = math.ceil(vmin / step) * step
    vals = []
    cur = start
    guard = 0
    while cur <= vmax + step * 0.5 and guard < 20:
        vals.append(0.0 if abs(cur) < step * 1e-9 else cur)
        cur += step
        guard += 1
    return vals


def _path_for_points(xs, ys, x_to_px, y_to_px):
    parts = []
    for x, y in zip(xs, ys):
        if not (math.isfinite(float(x)) and math.isfinite(float(y))):
            continue
        px = x_to_px(float(x))
        py = y_to_px(float(y))
        parts.append(f"M{px:.2f},{py:.2f}l0.01,0")
    return "".join(parts)


def build_subject_svg(subject_id, name, unit, lo, hi, traces, layout):
    margin = {**DEFAULT_MARGIN, **(layout.get("margin") or {})}
    left, right = float(margin["l"]), float(margin["r"])
    top, bottom = float(margin["t"]), float(margin["b"])
    plot_w = SVG_WIDTH - left - right
    plot_h = SVG_HEIGHT - top - bottom
    x_range = ((layout.get("xaxis") or {}).get("range")) or [0, 1]
    y_range = ((layout.get("yaxis") or {}).get("range")) or [0, 100]
    xmin, xmax = float(x_range[0]), float(x_range[1])
    ymin, ymax = float(y_range[0]), float(y_range[1])
    if xmin == xmax:
        xmin, xmax = xmin - 0.5, xmax + 0.5
    if ymin == ymax:
        ymin, ymax = ymin - 0.5, ymax + 0.5

    def x_to_px(x):
        return left + (x - xmin) / (xmax - xmin) * plot_w

    def y_to_px(y):
        return top + plot_h - (y - ymin) / (ymax - ymin) * plot_h

    subtitle = f"({_fmt(lo)} ~ {_fmt(hi)} {_clean_unit(unit)})"
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" '
        f'width="{SVG_WIDTH}" height="{SVG_HEIGHT}" role="img" aria-label="{escape(str(name), quote=True)}">',
        "<style>",
        ".axis{stroke:#666;stroke-width:1;fill:none}.grid{stroke:#eee;stroke-width:1}.tick{fill:#666;font:10px Arial,sans-serif}.title{fill:#111;font:700 16px Arial,sans-serif}.subtitle{fill:#333;font:11px Arial,sans-serif}.points{fill:none;stroke-linecap:round}.limit{stroke-dasharray:5 5}",
        "</style>",
        '<rect x="0" y="0" width="400" height="275" fill="white"/>',
        f'<text class="title" x="{SVG_WIDTH / 2:.1f}" y="23" text-anchor="middle">{escape(str(name))}</text>',
        f'<text class="subtitle" x="{SVG_WIDTH / 2:.1f}" y="42" text-anchor="middle">{escape(subtitle)}</text>',
    ]

    for tick in _ticks(xmin, xmax):
        px = x_to_px(tick)
        body.append(f'<line class="grid" x1="{px:.2f}" y1="{top:.2f}" x2="{px:.2f}" y2="{top + plot_h:.2f}"/>')
        body.append(f'<text class="tick" x="{px:.2f}" y="{top + plot_h + 18:.2f}" text-anchor="middle">{tick:g}</text>')
    for tick in [0, 25, 50, 75, 100]:
        py = y_to_px(tick)
        body.append(f'<line class="grid" x1="{left:.2f}" y1="{py:.2f}" x2="{left + plot_w:.2f}" y2="{py:.2f}"/>')
        body.append(f'<text class="tick" x="{left - 8:.2f}" y="{py + 3:.2f}" text-anchor="end">{tick:g}%</text>')

    for limit in (lo, hi):
        if _is_num(limit):
            px = x_to_px(float(limit))
            body.append(
                f'<line class="limit" x1="{px:.2f}" y1="{top:.2f}" x2="{px:.2f}" y2="{top + plot_h:.2f}" '
                f'stroke="{LIMIT_COLOR}" stroke-width="{LIMIT_LINE_WIDTH}"/>'
            )

    body.append(f'<path class="axis" d="M{left:.2f},{top:.2f}V{top + plot_h:.2f}H{left + plot_w:.2f}"/>')
    body.append(f'<text class="tick" x="{left + plot_w / 2:.2f}" y="{SVG_HEIGHT - 8}" text-anchor="middle">score</text>')

    clip_id = f"clip-{int(subject_id)}"
    body.append(f'<clipPath id="{clip_id}"><rect x="{left:.2f}" y="{top:.2f}" width="{plot_w:.2f}" height="{plot_h:.2f}"/></clipPath>')
    marker_size = max(1.0, float(MARKER_SIZE))
    for trace in traces:
        d = _path_for_points(trace["xs"], trace["ys"], x_to_px, y_to_px)
        if not d:
            continue
        school = escape(str(trace["school"]), quote=True)
        color = escape(str(trace["color"]), quote=True)
        body.append(
            f'<g class="school-points" data-school="{school}" clip-path="url(#{clip_id})">'
            f'<path class="points" d="{d}" stroke="{color}" stroke-width="{marker_size}"/></g>'
        )
    body.append("</svg>")
    return "\n".join(body)

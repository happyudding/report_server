"""Per-subject KDE-only Plotly payload + SVG thumbnail builder.

KDE 계산은 numpy 기반 (scipy 비의존). 두 결과 모두 로컬 캐시:
  - Plotly JSON : output/datasets/<id>/histograms/<sid>.json
  - SVG thumb   : output/datasets/<id>/histogram_thumbs/<sid>.svg
"""
import json
import math
from html import escape

import numpy as np

from config import DATASETS_DIR, LIMIT_COLOR, LIMIT_LINE_WIDTH
from analysis.data_loader import load_table
from analysis.preprocess import to_numeric_clean

_COLORS = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]
_KDE_POINTS = 400
_KDE_MAX_SAMPLES = 5000

_SVG_WIDTH = 400
_SVG_HEIGHT = 275
_SVG_MARGIN = {"l": 38, "r": 14, "t": 50, "b": 30}


def _gaussian_kde(values, x_points):
    """Scott's rule Gaussian KDE evaluated at `x_points`. Pure numpy."""
    n = len(values)
    if n < 2:
        return np.zeros(len(x_points))
    std = float(np.std(values, ddof=1))
    if std == 0:
        return np.zeros(len(x_points))
    h = std * (n ** -0.2)
    if n > _KDE_MAX_SAMPLES:
        rng = np.random.default_rng(42)
        values = values[rng.choice(n, _KDE_MAX_SAMPLES, replace=False)]
    diff = (x_points[np.newaxis, :] - values[:, np.newaxis]) / h
    kernel = np.exp(-0.5 * diff * diff) / (np.sqrt(2 * np.pi) * h)
    return kernel.mean(axis=0)


def _safe_float(seq, i):
    try:
        v = seq[i]
        return float(v) if v is not None else None
    except (IndexError, TypeError, ValueError):
        return None


def _hex_to_rgba(hex_color, alpha):
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return f"rgba(100,100,100,{alpha})"
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _load_schools(dataset_id):
    input_dir = DATASETS_DIR / dataset_id / "input"
    csvs = sorted(input_dir.glob("*.csv"))
    return {p.stem: load_table(p) for p in csvs}


def _compute_subject_data(dataset_id, subject_id):
    """Compute KDE curves + meta for one subject. Shared by JSON and SVG builders."""
    schools = _load_schools(dataset_id)
    if not schools:
        return None
    first = next(iter(schools.values()))
    idx = int(subject_id)
    if idx >= len(first.subjects):
        return None

    subject_name = first.subjects[idx]
    unit = (first.units[idx] if idx < len(first.units) else "") or ""
    lo = _safe_float(first.lower_limits, idx)
    hi = _safe_float(first.upper_limits, idx)

    school_vals = {}
    for name, school in schools.items():
        vals = to_numeric_clean(school.scores.iloc[:, idx])
        if vals.size > 0:
            school_vals[name] = vals
    if not school_vals:
        return None

    combined = np.concatenate(list(school_vals.values()))
    xmin, xmax = float(combined.min()), float(combined.max())
    if lo is not None:
        xmin = min(xmin, lo)
    if hi is not None:
        xmax = max(xmax, hi)
    span = xmax - xmin if xmax > xmin else max(abs(xmax), 1.0)
    pad = span * 0.08
    x0, x1 = xmin - pad, xmax + pad
    x_kde = np.linspace(x0, x1, _KDE_POINTS)

    curves = []
    ymax = 0.0
    for i, (name, vals) in enumerate(school_vals.items()):
        density = _gaussian_kde(vals, x_kde)
        ymax = max(ymax, float(density.max()))
        curves.append({
            "name": name,
            "color": _COLORS[i % len(_COLORS)],
            "xs": x_kde,
            "ys": density,
        })

    return {
        "id": idx,
        "name": subject_name,
        "unit": unit,
        "lo": lo,
        "hi": hi,
        "xrange": [x0, x1],
        "yrange": [0.0, ymax * 1.08 if ymax > 0 else 1.0],
        "curves": curves,
    }


# ── Plotly JSON ────────────────────────────────────────────────────────────────

def build_histogram_payload(dataset_id, subject_id, use_cache=True):
    """Plotly JSON figure dict for one subject (cached)."""
    cache_path = DATASETS_DIR / dataset_id / "histograms" / f"{int(subject_id)}.json"
    if use_cache and cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    data = _compute_subject_data(dataset_id, subject_id)
    if data is None:
        return {"id": int(subject_id), "name": "", "data": [], "layout": {}}

    traces = []
    for c in data["curves"]:
        traces.append({
            "type": "scatter",
            "x": c["xs"].tolist(),
            "y": c["ys"].tolist(),
            "mode": "lines",
            "name": c["name"],
            "line": {"color": c["color"], "width": 2.0, "shape": "spline"},
            "hovertemplate": f"<b>{c['name']}</b><br>x: %{{x:.4g}}<br>density: %{{y:.4g}}<extra></extra>",
        })

    # Limit vertical lines only (no text labels per user request)
    shapes = []
    for val in (data["lo"], data["hi"]):
        if val is not None:
            shapes.append({
                "type": "line", "x0": val, "x1": val, "y0": 0, "y1": 1,
                "xref": "x", "yref": "paper",
                "line": {"dash": "dash", "color": LIMIT_COLOR, "width": LIMIT_LINE_WIDTH},
            })

    lo_s = f"{data['lo']:g}" if data["lo"] is not None else "?"
    hi_s = f"{data['hi']:g}" if data["hi"] is not None else "?"
    title = (
        f"<span style='font-size:13px'><b>{data['name']}</b></span><br>"
        f"<span style='font-size:9px'>({lo_s} ~ {hi_s} {data['unit']})</span>"
    )
    # Axes: tickmode=auto + 큰 nticks 로 ~2배 촘촘하게. tickvals 미고정 → 줌 시 자동 재계산.
    layout = {
        "title": {"text": title, "x": 0.5, "xanchor": "center"},
        "xaxis": {
            "range": data["xrange"],
            "showgrid": True, "gridcolor": "#eee",
            "zeroline": False, "ticks": "outside",
            "tickmode": "auto", "nticks": 12,
            "automargin": True,
        },
        "yaxis": {
            "range": data["yrange"],
            "showgrid": True, "gridcolor": "#eee",
            "zeroline": False, "ticks": "outside",
            "tickmode": "auto", "nticks": 10,
            "automargin": True,
        },
        "shapes": shapes,
        "paper_bgcolor": "white", "plot_bgcolor": "white",
        "showlegend": False,
        "margin": {"l": 38, "r": 12, "t": 52, "b": 28},
        "font": {"size": 9},
        "template": "none",
        "dragmode": "zoom",
    }
    payload = {"id": data["id"], "name": data["name"], "data": traces, "layout": layout}

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError:
        pass
    return payload


# ── SVG thumbnail ──────────────────────────────────────────────────────────────

def _nice_ticks(vmin, vmax, max_ticks=5):
    if not math.isfinite(vmin) or not math.isfinite(vmax) or vmin == vmax:
        return []
    span = abs(vmax - vmin)
    raw = span / max(1, max_ticks - 1)
    if raw <= 0:
        return []
    mag = 10 ** math.floor(math.log10(raw))
    norm = raw / mag
    if norm <= 1: step = mag
    elif norm <= 2: step = 2 * mag
    elif norm <= 5: step = 5 * mag
    else: step = 10 * mag
    start = math.ceil(vmin / step) * step
    vals, cur, guard = [], start, 0
    while cur <= vmax + step * 0.5 and guard < 25:
        vals.append(0.0 if abs(cur) < step * 1e-9 else cur)
        cur += step
        guard += 1
    return vals


def _denser_ticks(vmin, vmax, max_ticks=5):
    """nice ticks 사이에 중점을 끼워서 2배 촘촘하게.
    예: [-5,0,5] → [-5,-2.5,0,2.5,5]"""
    base = _nice_ticks(vmin, vmax, max_ticks)
    if len(base) < 2:
        return base
    out = []
    for i, t in enumerate(base):
        out.append(t)
        if i < len(base) - 1:
            out.append((t + base[i + 1]) / 2.0)
    return out


def _fmt_tick(v):
    if v == 0: return "0"
    a = abs(v)
    if a >= 1000 or a < 0.01: return f"{v:.2g}"
    return f"{v:g}"


def build_histogram_svg(dataset_id, subject_id, use_cache=True):
    """SVG thumbnail for one subject (cached). KDE curves + limit lines, no labels."""
    cache_path = DATASETS_DIR / dataset_id / "histogram_thumbs" / f"{int(subject_id)}.svg"
    if use_cache and cache_path.exists():
        try:
            return cache_path.read_text(encoding="utf-8")
        except OSError:
            pass

    data = _compute_subject_data(dataset_id, subject_id)
    if data is None:
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" '
            f'width="{_SVG_WIDTH}" height="{_SVG_HEIGHT}">'
            f'<rect width="{_SVG_WIDTH}" height="{_SVG_HEIGHT}" fill="white"/>'
            f'<text x="50%" y="50%" text-anchor="middle" fill="#ccc" '
            f'font-family="Arial">no data</text></svg>'
        )
        return svg

    margin = _SVG_MARGIN
    left, right = float(margin["l"]), float(margin["r"])
    top, bottom = float(margin["t"]), float(margin["b"])
    plot_w = _SVG_WIDTH - left - right
    plot_h = _SVG_HEIGHT - top - bottom

    xmin, xmax = data["xrange"]
    ymin, ymax = data["yrange"]
    if xmin == xmax: xmin, xmax = xmin - 0.5, xmax + 0.5
    if ymin == ymax: ymin, ymax = ymin - 0.5, ymax + 0.5

    def x2px(x): return left + (x - xmin) / (xmax - xmin) * plot_w
    def y2px(y): return top + plot_h - (y - ymin) / (ymax - ymin) * plot_h

    lo_s = f"{data['lo']:g}" if data["lo"] is not None else "?"
    hi_s = f"{data['hi']:g}" if data["hi"] is not None else "?"
    subtitle = f"({lo_s} ~ {hi_s} {data['unit']})".strip()

    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" '
        f'width="{_SVG_WIDTH}" height="{_SVG_HEIGHT}" role="img" '
        f'aria-label="{escape(str(data["name"]), quote=True)}">',
        '<style>'
        '.axis{stroke:#666;stroke-width:1;fill:none}'
        '.grid{stroke:#eee;stroke-width:1}'
        '.tick{fill:#666;font:10px Arial,sans-serif}'
        '.title{fill:#111;font:700 14px Arial,sans-serif}'
        '.subtitle{fill:#333;font:10px Arial,sans-serif}'
        '.kde{fill:none;stroke-width:2;stroke-linejoin:round}'
        '.limit{stroke-dasharray:5 5}'
        '</style>',
        f'<rect width="{_SVG_WIDTH}" height="{_SVG_HEIGHT}" fill="white"/>',
        f'<text class="title" x="{_SVG_WIDTH / 2:.1f}" y="22" text-anchor="middle">'
        f'{escape(str(data["name"]))}</text>',
        f'<text class="subtitle" x="{_SVG_WIDTH / 2:.1f}" y="40" text-anchor="middle">'
        f'{escape(subtitle)}</text>',
    ]

    for t in _denser_ticks(xmin, xmax):
        px = x2px(t)
        body.append(f'<line class="grid" x1="{px:.2f}" y1="{top:.2f}" x2="{px:.2f}" y2="{top + plot_h:.2f}"/>')
        body.append(f'<text class="tick" x="{px:.2f}" y="{top + plot_h + 14:.2f}" text-anchor="middle">{_fmt_tick(t)}</text>')

    for t in _denser_ticks(ymin, ymax):
        py = y2px(t)
        body.append(f'<line class="grid" x1="{left:.2f}" y1="{py:.2f}" x2="{left + plot_w:.2f}" y2="{py:.2f}"/>')
        body.append(f'<text class="tick" x="{left - 4:.2f}" y="{py + 3:.2f}" text-anchor="end">{_fmt_tick(t)}</text>')

    # Limit vertical lines (no text labels)
    for limit in (data["lo"], data["hi"]):
        if limit is None: continue
        px = x2px(float(limit))
        body.append(
            f'<line class="limit" x1="{px:.2f}" y1="{top:.2f}" x2="{px:.2f}" y2="{top + plot_h:.2f}" '
            f'stroke="{LIMIT_COLOR}" stroke-width="{LIMIT_LINE_WIDTH}"/>'
        )

    body.append(f'<path class="axis" d="M{left:.2f},{top:.2f}V{top + plot_h:.2f}H{left + plot_w:.2f}"/>')

    clip_id = f"hclip-{int(subject_id)}"
    body.append(
        f'<clipPath id="{clip_id}"><rect x="{left:.2f}" y="{top:.2f}" '
        f'width="{plot_w:.2f}" height="{plot_h:.2f}"/></clipPath>'
    )

    for c in data["curves"]:
        xs, ys = c["xs"], c["ys"]
        pts = []
        for x, y in zip(xs, ys):
            x_f, y_f = float(x), float(y)
            if math.isfinite(x_f) and math.isfinite(y_f):
                pts.append((x2px(x_f), y2px(y_f)))
        if len(pts) < 2:
            continue
        line_d = "M" + " L".join(f"{p[0]:.2f},{p[1]:.2f}" for p in pts)
        color = escape(str(c["color"]), quote=True)
        name = escape(str(c["name"]), quote=True)
        body.append(
            f'<g class="school-kde" data-school="{name}" clip-path="url(#{clip_id})">'
            f'<path class="kde" d="{line_d}" stroke="{color}"/>'
            f'</g>'
        )

    body.append('</svg>')
    svg = "\n".join(body)

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(svg, encoding="utf-8")
    except OSError:
        pass
    return svg

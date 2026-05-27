import pandas as pd

from config import LIMIT_COLOR, LIMIT_LINE_WIDTH, MARKER_SIZE, TITLE_FONT_SIZE, X_RANGE_PADDING_RATIO


def _fmt(v):
    return "?" if v is None or pd.isna(v) else f"{v:g}"


def _is_num(v):
    return v is not None and not pd.isna(v)


def _vline(x):
    return dict(type="line", x0=float(x), x1=float(x), y0=0, y1=100,
                line=dict(dash="dash", color=LIMIT_COLOR, width=LIMIT_LINE_WIDTH))


def _axis(**extra):
    base = dict(ticks="outside", tickcolor="#666", ticklen=6,
                showgrid=True, gridcolor="#eee", zeroline=False,
                minor=dict(ticks="outside", ticklen=3, tickcolor="#bbb", showgrid=False))
    base.update(extra)
    return base


def _xrange(traces, lo, hi):
    vals = []
    for t in traces:
        xs = t["xs"]
        if xs.size > 0:
            vals += [float(xs.min()), float(xs.max())]
    if _is_num(lo): vals.append(float(lo))
    if _is_num(hi): vals.append(float(hi))
    if not vals:
        return None
    dmin, dmax = min(vals), max(vals)
    span = dmax - dmin if dmax > dmin else max(abs(dmax), 1.0)
    pad = span * X_RANGE_PADDING_RATIO
    return [dmin - pad, dmax + pad]


def _trace(t):
    xs, ys = t["xs"], t["ys"]
    return dict(
        type="scattergl",
        x=xs.tolist(),
        y=ys.tolist(),
        mode="markers",
        name=t["school"],
        showlegend=True,
        marker=dict(color=t["color"], size=MARKER_SIZE),
        hovertemplate=f"<b>{t['school']}</b><br>score: %{{x}}<br>cum%: %{{y:.2f}}<extra></extra>",
    )


def build_subject_payload_parts(traces, lo, hi, name, unit):
    shapes = [_vline(v) for v in (lo, hi) if _is_num(v)]
    title = (f"<span style='font-size:16px'><b>{name}</b></span><br>"
             f"<span style='font-size:11px'>({_fmt(lo)} ~ {_fmt(hi)} {unit})</span>")
    layout = dict(
        font=dict(family="Open Sans, verdana, arial, sans-serif", size=TITLE_FONT_SIZE),
        title=dict(text=title, font=dict(size=TITLE_FONT_SIZE), x=0.5, xanchor="center"),
        xaxis=_axis(range=_xrange(traces, lo, hi), title="score"),
        yaxis=_axis(range=[0, 100], title="", ticksuffix="%"),
        shapes=shapes, margin=dict(l=45, r=20, t=65, b=40),
        paper_bgcolor="white", plot_bgcolor="white", showlegend=False,
        template="none",
    )
    return [_trace(t) for t in traces], layout

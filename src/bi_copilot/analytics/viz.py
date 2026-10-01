"""Automatic visualisation.

Chart choice follows the shape of the analytical question:
  trend over time        -> line
  category comparison    -> bar (horizontal when labels are long)
  period comparison      -> grouped bar
  contribution / bridge  -> waterfall
  distribution           -> histogram
  two measures           -> scatter
  forecast               -> line with interval band
Figures are returned as plotly JSON-able dicts so answers stay serialisable.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

PALETTE = ["#2F5D8A", "#E07A3F", "#3A9E77", "#C44E52", "#8172B2", "#937860", "#DA8BC3", "#8C8C8C", "#CCB974", "#64B5CD"]
LAYOUT = dict(template="plotly_white", margin=dict(l=10, r=10, t=56, b=10), height=420,
              font=dict(family="Public Sans, Segoe UI, sans-serif", size=13), colorway=PALETTE,
              legend=dict(orientation="h", yanchor="top", y=-0.12, x=0))


def _layout(fig, title, **extra):
    fig.update_layout(**LAYOUT)
    fig.update_layout(title=dict(text=title, x=0, font=dict(size=15)), **extra)


def _tickformat(fmt: str) -> dict:
    if fmt == "percent":
        return dict(tickformat=".1%")
    if fmt == "currency":
        return dict(tickprefix="$", tickformat="~s")
    return dict(tickformat="~s")


def choose_chart(df: pd.DataFrame, dims: list[str], metrics: list[str], grain: str | None,
                 comparison: bool = False, question: str = "") -> str:
    q = question.lower()
    if "distribution" in q or "histogram" in q:
        return "histogram"
    if grain:
        return "line"
    if df is None or len(df) <= 1:
        return "kpi"
    if comparison:
        return "grouped_bar"
    if len(metrics) >= 2 and dims and len(df) >= 5 and ("vs" in q or "relationship" in q or "correlat" in q or "scatter" in q):
        return "scatter"
    if dims:
        return "bar"
    return "table"


def line(df: pd.DataFrame, metric: str, label: str, fmt: str, color_dim: str | None = None, title: str = "") -> dict:
    fig = go.Figure()
    if color_dim and color_dim in df.columns:
        for i, (k, g) in enumerate(df.groupby(color_dim, sort=False)):
            fig.add_trace(go.Scatter(x=g["period"], y=g[metric], mode="lines+markers", name=str(k),
                                     line=dict(width=2.2)))
    else:
        fig.add_trace(go.Scatter(x=df["period"], y=df[metric], mode="lines+markers", name=label, line=dict(width=2.6)))
    _layout(fig, title or label)
    fig.update_yaxes(**_tickformat(fmt), rangemode="tozero" if fmt != "percent" else "normal")
    return fig.to_plotly_json()


def bar(df: pd.DataFrame, dim: str, metric: str, label: str, fmt: str, title: str = "", top: int = 20) -> dict:
    d = df.dropna(subset=[metric]).head(top)
    horizontal = d[dim].astype(str).str.len().max() > 12 or len(d) > 8
    d = d.iloc[::-1] if horizontal else d
    trace = go.Bar(x=d[metric], y=d[dim].astype(str), orientation="h") if horizontal else go.Bar(x=d[dim].astype(str), y=d[metric])
    fig = go.Figure(trace)
    fig.update_traces(marker_color=PALETTE[0])
    _layout(fig, title or f"{label} by {dim.replace('_', ' ')}")
    fig.update_layout(height=max(360, 28 * len(d) + 120) if horizontal else 420)
    (fig.update_xaxes if horizontal else fig.update_yaxes)(**_tickformat(fmt))
    return fig.to_plotly_json()


def grouped_bar(df: pd.DataFrame, dim: str | None, cur_col: str, prev_col: str, cur_label: str, prev_label: str,
                fmt: str, title: str) -> dict:
    x = df[dim].astype(str) if dim else [title]
    fig = go.Figure([go.Bar(x=x, y=df[prev_col], name=prev_label, marker_color="#A9B8C9"),
                     go.Bar(x=x, y=df[cur_col], name=cur_label, marker_color=PALETTE[0])])
    _layout(fig, title, barmode="group")
    fig.update_yaxes(**_tickformat(fmt))
    return fig.to_plotly_json()


def waterfall(start_label: str, start: float, steps: list[tuple[str, float]], end_label: str, end: float,
              fmt: str, title: str) -> dict:
    labels = [start_label] + [s[0] for s in steps] + [end_label]
    values = [start] + [s[1] for s in steps] + [end]
    measure = ["absolute"] + ["relative"] * len(steps) + ["total"]
    fig = go.Figure(go.Waterfall(x=labels, y=values, measure=measure,
                                 increasing=dict(marker=dict(color="#3A9E77")),
                                 decreasing=dict(marker=dict(color="#C44E52")),
                                 totals=dict(marker=dict(color=PALETTE[0])),
                                 connector=dict(line=dict(color="#BBBBBB"))))
    _layout(fig, title, showlegend=False)
    fig.update_yaxes(**_tickformat(fmt))
    # zoom to the moving part: running totals, not zero
    run, levels = start, [start, end]
    for _, v in steps:
        run += v
        levels.append(run)
    lo, hi = min(levels), max(levels)
    pad = (hi - lo) * 0.25 or abs(hi) * 0.05
    if lo - pad > 0 or fmt == "percent":
        fig.update_yaxes(range=[lo - pad, hi + pad])
    return fig.to_plotly_json()


def forecast_chart(hist: pd.DataFrame, fc: pd.DataFrame, label: str, fmt: str) -> dict:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=hist["period"], y=hist["value"], mode="lines", name="Actual", line=dict(color=PALETTE[0], width=2.4)))
    fig.add_trace(go.Scatter(x=list(fc["period"]) + list(fc["period"])[::-1], y=list(fc["hi"]) + list(fc["lo"])[::-1],
                             fill="toself", fillcolor="rgba(224,122,63,0.18)", line=dict(width=0), name="80% interval",
                             hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=fc["period"], y=fc["yhat"], mode="lines+markers", name="Forecast",
                             line=dict(color=PALETTE[1], width=2.4, dash="dash")))
    _layout(fig, f"{label}: history and forecast")
    fig.update_yaxes(**_tickformat(fmt))
    return fig.to_plotly_json()


def histogram(values: pd.Series, label: str, fmt: str, title: str) -> dict:
    fig = go.Figure(go.Histogram(x=values, nbinsx=40, marker_color=PALETTE[0]))
    _layout(fig, title)
    fig.update_xaxes(**_tickformat(fmt))
    return fig.to_plotly_json()


def scatter(df: pd.DataFrame, dim: str, x: str, y: str, xlab: str, ylab: str, xfmt: str, yfmt: str) -> dict:
    fig = go.Figure(go.Scatter(x=df[x], y=df[y], mode="markers", text=df[dim].astype(str),
                               marker=dict(size=10, color=PALETTE[0], opacity=0.75)))
    _layout(fig, f"{ylab} vs {xlab} by {dim.replace('_', ' ')}")
    fig.update_xaxes(title=xlab, **_tickformat(xfmt))
    fig.update_yaxes(title=ylab, **_tickformat(yfmt))
    return fig.to_plotly_json()


def anomaly_chart(series: pd.DataFrame, flagged: list, label: str, fmt: str, title: str) -> dict:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=series["period"], y=series["value"], mode="lines", name=label, line=dict(color=PALETTE[0])))
    if flagged:
        fig.add_trace(go.Scatter(x=[f[0] for f in flagged], y=[f[1] for f in flagged], mode="markers",
                                 name="Flagged", marker=dict(color="#C44E52", size=12, symbol="circle-open", line=dict(width=3))))
    _layout(fig, title)
    fig.update_yaxes(**_tickformat(fmt))
    return fig.to_plotly_json()

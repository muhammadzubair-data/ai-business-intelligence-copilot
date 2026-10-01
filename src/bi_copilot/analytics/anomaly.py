"""Proactive anomaly scan across KPIs and slices.

A week is flagged only when it is unusual against BOTH
  (a) its recent history: median and MAD of the previous 8 weeks, and
  (b) its seasonal expectation: the same ISO week last year, scaled by the
      year-on-year trend of the preceding 8 weeks.
Requiring both avoids flagging normal seasonality (Black Friday, the January
dip) while still catching real breaks. A volume KPI that drops to zero when its
history is healthy is reported as a probable data-quality issue, not a
business anomaly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from ..db import Database
from ..plan import SQLRecord, TimeRange
from ..semantic.catalog import Catalog
from ..semantic.compiler import Compiler, MetricRequest

# KPI -> dimensions to scan
SCAN = {
    "net_revenue": ["region", "segment", "category"],
    "orders": ["region", "segment"],
    "return_rate": ["category", "region"],
    "discount_rate": ["segment", "region"],
    "gross_margin_pct": ["category"],
    "sessions": ["marketing_channel"],
    "conversion_rate": ["marketing_channel", "segment"],
    "support_tickets": ["ticket_category"],
    "defect_tickets": ["category"],
    "stockout_rate": ["warehouse"],
    "marketing_spend": ["marketing_channel", "region"],
    "avg_csat": [],
}
VOLUME_KPIS = {"net_revenue", "orders", "sessions", "support_tickets", "marketing_spend"}
MIN_DENOMINATOR = {"return_rate": 20000.0, "discount_rate": 20000.0, "gross_margin_pct": 20000.0,
                   "conversion_rate": 3000.0, "stockout_rate": 200.0, "avg_csat": 15.0}


@dataclass
class Anomaly:
    metric: str
    label: str
    dimension: str | None
    member: str | None
    week: pd.Timestamp
    value: float
    expected: float
    z_recent: float
    z_seasonal: float
    kind: str = "business"            # business | data_quality
    direction: str = "up"
    fmt: str = "number"
    good: bool = False
    weeks: int = 1
    gap_days: list = field(default_factory=list)
    run_start: object = None

    @property
    def severity(self) -> float:
        return min(abs(self.z_recent), abs(self.z_seasonal))

    @property
    def where(self) -> str:
        return f"{self.member}" if self.member else "overall"


@dataclass
class AnomalyResult:
    period: TimeRange
    anomalies: list[Anomaly] = field(default_factory=list)
    scanned: int = 0
    sql: list[SQLRecord] = field(default_factory=list)


class AnomalyDetector:
    def __init__(self, db: Database, catalog: Catalog, compiler: Compiler, z: float = 3.5):
        self.db, self.cat, self.comp, self.z = db, catalog, compiler, z

    def _weekly(self, metric: str, start, end, dim: str | None, rec: list) -> pd.DataFrame:
        met = self.cat.metrics[metric]
        mets = [met.numerator, met.denominator] if met.type == "ratio" else [metric]
        tr = TimeRange(start=start, end=end, label="scan")
        df, r = self.db.run(self.comp.compile(MetricRequest(mets, tr, [dim] if dim else [], {}, grain="week")),
                            purpose=f"weekly {metric}" + (f" by {dim}" if dim else ""))
        rec.append(r)
        if met.type == "ratio":
            df["value"] = df[met.numerator] / df[met.denominator].replace(0, np.nan)
            df["weight"] = df[met.denominator]
        else:
            df["value"] = df[metric].astype(float)
            df["weight"] = np.inf
        df["period"] = pd.to_datetime(df["period"])
        return df

    def scan(self, period: TimeRange, metrics: list[str] | None = None) -> AnomalyResult:
        res = AnomalyResult(period=period)
        # whole weeks inside the period
        wk_start = period.start - timedelta(days=period.start.weekday())
        if period.start.weekday() >= 4:          # week mostly outside the period
            wk_start += timedelta(days=7)
        hist_start = wk_start - timedelta(days=7 * 62)
        end = period.end
        for metric in (metrics or list(SCAN)):
            if metric not in self.cat.metrics:
                continue
            met = self.cat.metrics[metric]
            for dim in [None] + [d for d in SCAN.get(metric, []) if d in self.cat.allowed_dimensions(metric)]:
                try:
                    df = self._weekly(metric, hist_start, end, dim, res.sql)
                except Exception:  # noqa: BLE001
                    continue
                groups = df.groupby(dim) if dim else [(None, df)]
                for member, g in groups:
                    g = g.set_index("period").sort_index()
                    # drop partial weeks at either edge
                    g = g[(g.index >= pd.Timestamp(hist_start)) & (g.index + pd.Timedelta(days=6) <= pd.Timestamp(end))]
                    res.scanned += 1
                    for wk in g.index[g.index >= pd.Timestamp(wk_start)]:
                        a = self._score(g, wk, metric, met, dim, member)
                        if a is not None:
                            res.anomalies.append(a)
        gaps = self._daily_gaps(period, res.sql)
        business = self._consolidate(res.anomalies)
        # weeks touched by a data gap: drop business anomalies on the affected KPIs
        affected = {"sessions": {"sessions", "conversion_rate"}, "orders": {"orders", "net_revenue"},
                    "support_tickets": {"support_tickets", "defect_tickets", "avg_csat"}}
        for g in gaps:
            wks = {pd.Timestamp(d - timedelta(days=d.weekday())) for d in g.gap_days}
            business = [a for a in business if not (a.metric in affected.get(g.metric, set()) and a.week in wks)]
        # the same series reached through two routes (e.g. defect tickets overall vs ticket_category=Product Defect)
        seen, uniq = set(), []
        for a in business:
            key = (a.week, round(a.value, 6), round(a.expected, 4))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(a)
        res.anomalies = gaps + uniq
        return res

    def _daily_gaps(self, period: TimeRange, rec: list) -> list[Anomaly]:
        """Days where a normally busy volume series records nothing: almost always a data problem."""
        out = []
        for metric in ("sessions", "orders", "support_tickets"):
            tr = TimeRange(start=period.start - timedelta(days=28), end=period.end, label="daily")
            df, r = self.db.run(self.comp.compile(MetricRequest([metric], tr, [], {}, grain="day")),
                                purpose=f"daily {metric} completeness check")
            rec.append(r)
            s = df.set_index(pd.to_datetime(df["period"]))[metric].astype(float)
            s = s.reindex(pd.date_range(tr.start, tr.end, freq="D"), fill_value=0.0)
            base = s.rolling(28, min_periods=14).median().shift(1)
            gaps = s[(s == 0) & (base > 5) & (s.index >= pd.Timestamp(period.start))]
            if len(gaps):
                met = self.cat.metrics[metric]
                out.append(Anomaly(metric, met.label, None, None, gaps.index[0], 0.0, float(base[gaps.index[0]]),
                                   -99, -99, kind="data_quality", direction=met.direction, fmt=met.format,
                                   weeks=len(gaps)))
                out[-1].gap_days = [d.date() for d in gaps.index]
        return out

    def _score(self, g: pd.DataFrame, wk, metric, met, dim, member) -> Anomaly | None:
        v = g.at[wk, "value"]
        w = g.at[wk, "weight"]
        prior = g[(g.index < wk) & (g.index >= wk - pd.Timedelta(weeks=8))]
        if len(prior) < 6 or pd.isna(v):
            if metric in VOLUME_KPIS and v == 0 and len(prior) >= 6 and prior["value"].median() > 0:
                pass
            else:
                return None
        base = prior["value"].median()
        mad = (prior["value"] - base).abs().median() * 1.4826
        scale = max(mad, 0.04 * abs(base), 1e-9)
        if met.type == "ratio" and np.isfinite(w) and w < MIN_DENOMINATOR.get(metric, 0):
            return None
        # data-quality: a healthy volume series collapsing to zero
        if metric in VOLUME_KPIS and v == 0 and base > 0:
            return Anomaly(metric, met.label, dim, member, wk, 0.0, base, -99, -99, kind="data_quality",
                           direction=met.direction, fmt=met.format)
        z1 = (v - base) / scale
        ly = wk - pd.Timedelta(weeks=52)
        if ly not in g.index:
            return None
        prior_ly = g[(g.index < ly) & (g.index >= ly - pd.Timedelta(weeks=8))]
        if len(prior_ly) < 6 or prior_ly["value"].median() == 0:
            return None
        if met.type == "ratio":
            expected = max(0.0, g.at[ly, "value"] + (base - prior_ly["value"].median()))
        else:
            expected = g.at[ly, "value"] * (base / prior_ly["value"].median())
        ly_resid = (prior_ly["value"] - prior_ly["value"].median()).abs().median() * 1.4826
        scale2 = max(mad, ly_resid, 0.05 * abs(expected), 1e-9)
        z2 = (v - expected) / scale2
        if abs(z1) < self.z or abs(z2) < self.z or np.sign(z1) != np.sign(z2):
            return None
        good = (z1 > 0) == (met.direction == "up")
        return Anomaly(metric, met.label, dim, member, wk, float(v), float(expected), float(z1), float(z2),
                       direction=met.direction, fmt=met.format, good=good)

    @staticmethod
    def _consolidate(items: list[Anomaly]) -> list[Anomaly]:
        """Merge consecutive weeks of the same KPI/slice; keep the most severe week."""
        items.sort(key=lambda a: (a.metric, a.dimension or "", a.member or "", a.week))
        out: list[Anomaly] = []
        for a in items:
            last = out[-1] if out else None
            if last and (last.metric, last.dimension, last.member, last.kind) == (a.metric, a.dimension, a.member, a.kind) \
                    and (a.week - last.run_start).days <= 7 * last.weeks:
                last.weeks += 1
                if abs(a.value - a.expected) > abs(last.value - last.expected):   # report the peak week of the run
                    a.weeks, a.run_start = last.weeks, last.run_start
                    out[-1] = a
                continue
            a.run_start = a.week
            out.append(a)
        # a slice-level anomaly is redundant if the overall KPI flags the same week in the same direction,
        # unless the slice is more severe
        out.sort(key=lambda a: (a.kind != "data_quality", -a.severity))
        return out

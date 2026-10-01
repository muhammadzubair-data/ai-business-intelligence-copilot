"""Automated root-cause analysis.

Given a metric and two periods, it:
  1. measures the total change;
  2. splits the change across candidate dimensions and ranks them by how
     concentrated the change is (a change spread evenly across regions says
     nothing about regions);
  3. drills down: fixes the top member of the best dimension and repeats
     inside that slice (bounded depth);
  4. for revenue, decomposes the change into price, volume, mix and refund
     effects at product level; for cost/profit metrics, isolates the supplier
     cost-rate effect;
  5. lists customers that stopped ordering when customer concentration matters.

Additive metrics use member deltas. Ratio metrics use an exact decomposition:
contribution_i = w1_i * r1_i - w0_i * r0_i where w is the member's share of the
denominator, so contributions sum to the change in the overall ratio.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from ..db import Database
from ..plan import SQLRecord, TimeRange
from ..semantic.catalog import Catalog
from ..semantic.compiler import Compiler, MetricRequest, q

REVENUE_METRICS = {"net_revenue", "sales_after_discounts", "gross_sales", "booked_revenue"}
COST_METRICS = {"cogs", "gross_profit", "gross_margin_pct", "contribution_profit", "contribution_margin_pct"}

LEVEL1 = {
    "sales": ["region", "segment", "channel", "category"],
    "marketing": ["marketing_channel", "region", "segment"],
    "acquisition": ["marketing_channel", "segment", "region"],
    "web": ["marketing_channel", "segment", "region"],
    "support": ["ticket_category", "category", "region", "segment"],
    "inventory": ["warehouse", "category", "supplier"],
    "purchasing": ["supplier", "category", "warehouse"],
    "finance": ["department", "expense_type"],
}
DEEPER = {
    "sales": ["sales_team", "product", "supplier", "country", "customer"],
    "support": ["product", "priority"],
    "inventory": ["product"],
    "purchasing": ["product"],
}


# natural parent -> child drill paths
HIERARCHY = {"category": "product", "business_unit": "category", "region": "country", "channel": "sales_team",
             "supplier": "product", "ticket_category": "product", "warehouse": "product"}


def _eligible(dim: str, f: dict) -> bool:
    if dim == "country":
        return "region" in f
    if dim == "sales_team":
        return f.get("channel") == ["Direct Sales"]
    return True


@dataclass
class DimResult:
    dimension: str
    table: pd.DataFrame          # member, current, previous, delta, share
    score: float


@dataclass
class DrillStep:
    dimension: str
    member: str
    delta: float
    share_of_total: float
    filters: dict


@dataclass
class RCAResult:
    metric: str
    current: TimeRange
    previous: TimeRange
    cur_value: float
    prev_value: float
    delta: float
    dims: list[DimResult] = field(default_factory=list)
    path: list[DrillStep] = field(default_factory=list)
    pvm: Optional[dict] = None
    cost: Optional[pd.DataFrame] = None
    lost_customers: Optional[pd.DataFrame] = None
    supply: Optional[dict] = None
    mix_driven: bool = False
    sql: list[SQLRecord] = field(default_factory=list)
    additive: bool = True

    @property
    def pct(self) -> Optional[float]:
        if not self.prev_value:
            return None
        return (self.cur_value / self.prev_value - 1) * 100


class RootCauseAnalyzer:
    def __init__(self, db: Database, catalog: Catalog, compiler: Compiler):
        self.db, self.cat, self.comp = db, catalog, compiler

    # ------------------------------------------------------------------ primitives
    def value(self, metric: str, tr: TimeRange, filters: dict, rec: list) -> float:
        sql = self.comp.compile(MetricRequest([metric], tr, [], filters))
        df, r = self.db.run(sql, purpose=f"{metric} for {tr.label}")
        rec.append(r)
        v = df.iloc[0, -1] if len(df) else None
        return float(v) if v is not None and not pd.isna(v) else 0.0

    def by_dim(self, metric: str, cur: TimeRange, prev: TimeRange, dim: str, filters: dict, rec: list) -> pd.DataFrame:
        met = self.cat.metrics[metric]
        if met.type == "ratio":
            mets = [met.numerator, met.denominator]
        else:
            mets = [metric]
        frames = {}
        for tag, tr in (("current", cur), ("previous", prev)):
            sql = self.comp.compile(MetricRequest(mets, tr, [dim], filters))
            df, r = self.db.run(sql, purpose=f"{metric} by {dim}, {tr.label}")
            rec.append(r)
            frames[tag] = df.set_index(dim)
        c, p = frames["current"], frames["previous"]
        idx = c.index.union(p.index)
        c, p = c.reindex(idx).fillna(0.0), p.reindex(idx).fillna(0.0)
        out = pd.DataFrame(index=idx)
        if met.type == "ratio":
            n, d = met.numerator, met.denominator
            D1, D0 = c[d].sum(), p[d].sum()
            r1 = (c[n] / c[d].replace(0, np.nan)).fillna(0.0)
            r0 = (p[n] / p[d].replace(0, np.nan)).fillna(0.0)
            out["current"], out["previous"] = r1, r0
            w1 = c[d] / D1 if D1 else 0 * c[d]
            w0 = p[d] / D0 if D0 else 0 * p[d]
            out["delta"] = w1 * r1 - w0 * r0
            out["weight"], out["weight_prev"] = w1, w0
            # split each member's contribution into rate (its own ratio moved) and mix (its share moved)
            R0 = (p[n].sum() / D0) if D0 else 0.0
            out["rate_effect"] = w1 * (r1 - r0)
            out["mix_effect"] = (w1 - w0) * (r0 - R0)
        else:
            out["current"], out["previous"] = c[metric], p[metric]
            out["delta"] = out["current"] - out["previous"]
        out.index = out.index.map(lambda v: "(none)" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v))
        out.index.name = "member"
        tot = out["delta"].sum()
        out["share"] = out["delta"] / tot if tot else 0.0
        return out.reset_index().sort_values("delta", key=lambda s: s * (1 if tot >= 0 else -1), ascending=False)

    @staticmethod
    def score(table: pd.DataFrame, total: float) -> float:
        if "weight" in table:   # ratio metric: members with a tiny denominator are noise, not drivers
            table = table[(table["weight"] >= 0.02) | (table["weight_prev"] >= 0.02)]
            if not len(table):
                return 0.0
        d = table["delta"].to_numpy()
        absum = np.abs(d).sum()
        n = max(1, (np.abs(d) > 1e-9 * max(1.0, absum)).sum())
        if absum == 0 or n < 2:
            return 0.0
        same = d * np.sign(total) if total else np.abs(d)
        top = same.max()
        return float(top / absum - 1.0 / n)

    # ------------------------------------------------------------------ main
    def analyze(self, metric: str, cur: TimeRange, prev: TimeRange, filters: dict | None = None,
                focus_dims: list[str] | None = None, max_depth: int = 3) -> RCAResult:
        filters = dict(filters or {})
        rec: list[SQLRecord] = []
        met = self.cat.metrics[metric]
        cv, pv = self.value(metric, cur, filters, rec), self.value(metric, prev, filters, rec)
        res = RCAResult(metric, cur, prev, cv, pv, cv - pv, sql=rec, additive=met.type != "ratio")
        if met.type in ("custom", "derived"):
            for d in (focus_dims or [])[:1] or (met.allowed_dimensions or [])[:2]:
                t = self._plain_by_dim(metric, cur, prev, d, filters, rec)
                res.dims.append(DimResult(d, t, 0.0))
            return res
        model = met.model
        allowed = self.cat.allowed_dimensions(metric)
        cands = [d for d in (focus_dims or []) if d in allowed]
        cands += [d for d in LEVEL1.get(model, []) if d in allowed and d not in cands and d not in filters]
        if metric in COST_METRICS and "supplier" not in cands and "supplier" in allowed and "supplier" not in filters:
            cands.append("supplier")
        for d in cands:
            t = self.by_dim(metric, cur, prev, d, filters, rec)
            res.dims.append(DimResult(d, t, self.score(t, res.delta)))
        # focus dimensions stay first; the rest by score
        focus = [r for r in res.dims if r.dimension in (focus_dims or [])]
        rest = sorted([r for r in res.dims if r.dimension not in (focus_dims or [])], key=lambda r: -r.score)
        res.dims = focus + rest
        # ratio metrics: a dimension whose mix shift explains most of the change is the story
        if met.type == "ratio" and res.delta and not focus:
            mixes = [(r, float(r.table["mix_effect"].sum()) / res.delta) for r in res.dims if "mix_effect" in r.table]
            mixes = [(r, m) for r, m in mixes if m >= 0.6]
            if mixes:
                top_mix = max(mixes, key=lambda x: x[1])[0]
                res.dims = [top_mix] + [r for r in res.dims if r is not top_mix]
                res.mix_driven = True
                return self._extras(res, metric, model, cur, prev, filters, filters, rec)

        # drill-down path
        f = dict(filters)
        used = set(filters)
        total = res.delta
        pool = [r for r in res.dims]
        for depth in range(max_depth):
            if not pool:
                break
            best = focus[0] if (depth == 0 and focus) else pool[0]
            if best.score <= 0.02 and depth > 0:
                break
            tbl = best.table[best.table.member != "(none)"]
            if "weight" in tbl:
                tbl = tbl[(tbl["weight"] >= 0.02) | (tbl["weight_prev"] >= 0.02)]
            if tbl.empty:
                break
            top = tbl.iloc[0]
            slice_total = best.table["delta"].sum()
            if slice_total and np.sign(top["delta"]) != np.sign(slice_total):
                break
            share = top["delta"] / slice_total if slice_total else 0.0
            if depth > 0 and abs(share) < 0.25:
                break
            res.path.append(DrillStep(best.dimension, str(top["member"]), float(top["delta"]), float(share), dict(f)))
            f = {**f, best.dimension: [str(top["member"])]}
            used.add(best.dimension)
            nxt = [d for d in LEVEL1.get(model, []) + DEEPER.get(model, [])
                   if d in allowed and d not in used and _eligible(d, f)]
            if metric in COST_METRICS and "supplier" in allowed and "supplier" not in used and "supplier" not in nxt:
                nxt.insert(0, "supplier")
            pool = []
            for d in nxt:
                t = self.by_dim(metric, cur, prev, d, f, rec)
                pool.append(DimResult(d, t, self.score(t, t["delta"].sum())))
            pool.sort(key=lambda r: -r.score)
            # prefer the natural child level when one member of it explains most of the slice
            child = HIERARCHY.get(best.dimension)
            for i, r in enumerate(pool):
                if r.dimension == child:
                    ct = r.table[r.table.member != "(none)"]
                    tot = r.table["delta"].sum()
                    if len(ct) and tot and ct.iloc[0]["delta"] / tot >= 0.4:
                        pool.insert(0, pool.pop(i))
                    break

        return self._extras(res, metric, model, cur, prev, filters, f, rec)

    def _extras(self, res, metric, model, cur, prev, filters, f, rec):
        total = res.delta
        if model == "sales":
            if metric in REVENUE_METRICS:
                res.pvm = self.pvm(metric, cur, prev, filters, rec)
            if metric in COST_METRICS:
                res.cost = self.cost_effect(cur, prev, filters, rec)
            slice_f = dict(f if res.path else filters)
            if metric in REVENUE_METRICS | {"gross_profit", "orders"} and total < 0:
                res.lost_customers = self.lost_customers(cur, prev, slice_f, rec)
            if metric in REVENUE_METRICS and total < 0:
                region = (filters.get("region") or next(([s.member] for s in res.path if s.dimension == "region"), None))
                res.supply = self.supply_check(cur, prev, region, rec)
        return res

    # ------------------------------------------------------------------ supply
    def supply_check(self, cur: TimeRange, prev: TimeRange, region: list[str] | None, rec: list) -> dict:
        """Did availability (stockouts, late supplier deliveries) change in the affected region?"""
        where_r = f" AND region IN ({', '.join(q(r) for r in region)})" if region else ""
        sql = f"""
SELECT
  AVG(is_stockout::DOUBLE) FILTER (WHERE snapshot_date BETWEEN DATE {q(cur.start)} AND DATE {q(cur.end)}) AS stockout_rate_current,
  AVG(is_stockout::DOUBLE) FILTER (WHERE snapshot_date BETWEEN DATE {q(prev.start)} AND DATE {q(prev.end)}) AS stockout_rate_previous
FROM v_inventory WHERE TRUE{where_r}"""
        df, r = self.db.run(sql, purpose="stockout rate in the affected region")
        rec.append(r)
        out = {"region": region[0] if region else "all regions",
               "stockout_rate_current": float(df.iloc[0, 0] or 0), "stockout_rate_previous": float(df.iloc[0, 1] or 0)}
        sql2 = f"""
WITH so AS (
  SELECT product, COUNT(*) FILTER (WHERE is_stockout) AS weeks_out
  FROM v_inventory WHERE snapshot_date BETWEEN DATE {q(cur.start)} AND DATE {q(cur.end)}{where_r}
  GROUP BY product HAVING COUNT(*) FILTER (WHERE is_stockout) > 0),
rev AS (
  SELECT product,
    SUM(net_line_amount) FILTER (WHERE ship_date BETWEEN DATE {q(cur.start)} AND DATE {q(cur.end)}) AS current,
    SUM(net_line_amount) FILTER (WHERE ship_date BETWEEN DATE {q(prev.start)} AND DATE {q(prev.end)}) AS previous
  FROM v_sales_lines WHERE order_status = 'Shipped'{where_r} GROUP BY product)
SELECT so.product, so.weeks_out, COALESCE(rev.current, 0) AS current, COALESCE(rev.previous, 0) AS previous,
       COALESCE(rev.current, 0) - COALESCE(rev.previous, 0) AS delta
FROM so JOIN rev USING (product) ORDER BY delta LIMIT 10"""
        d2, r2 = self.db.run(sql2, purpose="revenue change for products that were out of stock")
        rec.append(r2)
        out["stocked_out_products"] = d2
        out["stocked_out_revenue_delta"] = float(d2["delta"].sum()) if len(d2) else 0.0
        return out

    def _plain_by_dim(self, metric, cur, prev, dim, filters, rec) -> pd.DataFrame:
        out = []
        for tag, tr in (("current", cur), ("previous", prev)):
            df, r = self.db.run(self.comp.compile(MetricRequest([metric], tr, [dim], filters)), purpose=f"{metric} by {dim}")
            rec.append(r)
            out.append(df.set_index(dim)[metric].rename(tag))
        t = pd.concat(out, axis=1).fillna(0.0)
        t["delta"] = t["current"] - t["previous"]
        t["share"] = np.nan
        t.index.name = "member"
        return t.reset_index().sort_values("delta")

    # ------------------------------------------------------------------ PVM
    def _where(self, tr: TimeRange, filters: dict, flt: str, tcol: str = "ship_date") -> str:
        cols = self.cat.models["sales"].dimensions
        parts = [f"({flt})", f"{tcol} BETWEEN DATE {q(tr.start)} AND DATE {q(tr.end)}"]
        for d, vals in filters.items():
            parts.append(Compiler._filter_sql(cols[d], vals))
        return " AND ".join(parts)

    def pvm(self, metric: str, cur: TimeRange, prev: TimeRange, filters: dict, rec: list) -> dict:
        """Price / volume / mix at product level on sales after discounts, plus refund effect for net revenue."""
        met = self.cat.metrics[metric]
        tcol = met.time_column
        amount = "gross_amount" if metric == "gross_sales" else "net_line_amount"
        sql = f"""
WITH p AS (
  SELECT product_key,
         SUM(quantity) FILTER (WHERE {self._where(prev, filters, met.filter, tcol)}) AS q0,
         SUM({amount}) FILTER (WHERE {self._where(prev, filters, met.filter, tcol)}) AS r0,
         SUM(refund_amount) FILTER (WHERE {self._where(prev, filters, met.filter, tcol)}) AS f0,
         SUM(quantity) FILTER (WHERE {self._where(cur, filters, met.filter, tcol)}) AS q1,
         SUM({amount}) FILTER (WHERE {self._where(cur, filters, met.filter, tcol)}) AS r1,
         SUM(refund_amount) FILTER (WHERE {self._where(cur, filters, met.filter, tcol)}) AS f1
  FROM v_sales_lines GROUP BY product_key)
SELECT * FROM p WHERE COALESCE(q0, 0) > 0 OR COALESCE(q1, 0) > 0"""
        df, r = self.db.run(sql, purpose="price-volume-mix by product")
        rec.append(r)
        df = df.fillna(0.0)
        p0 = np.where(df.q0 > 0, df.r0 / df.q0.replace(0, np.nan), df.r1 / df.q1.replace(0, np.nan))
        p1 = np.where(df.q1 > 0, df.r1 / df.q1.replace(0, np.nan), 0.0)
        p0 = np.nan_to_num(p0)
        Q0, Q1, R0, R1 = df.q0.sum(), df.q1.sum(), df.r0.sum(), df.r1.sum()
        P0 = R0 / Q0 if Q0 else 0.0
        price = float(np.sum(np.where(df.q1 > 0, (p1 - p0) * df.q1, 0.0)))
        volume = float((Q1 - Q0) * P0)
        mix = float(np.sum(df.q1 * p0) - Q1 * P0)
        out = {"price_effect": price, "volume_effect": volume, "mix_effect": mix,
               "units_current": float(Q1), "units_previous": float(Q0)}
        if metric == "net_revenue":
            out["refund_effect"] = float(-(df.f1.sum() - df.f0.sum()))
        out["total"] = sum(v for k, v in out.items() if k.endswith("_effect"))
        return out

    # ------------------------------------------------------------------ cost effect
    def cost_effect(self, cur: TimeRange, prev: TimeRange, filters: dict, rec: list) -> pd.DataFrame:
        """COGS change from supplier unit-cost changes (like-for-like, current volumes), by supplier."""
        flt = "order_status = 'Shipped'"
        sql = f"""
WITH p AS (
  SELECT supplier, category, product_key,
         SUM(quantity) FILTER (WHERE {self._where(prev, filters, flt)}) AS q0,
         SUM(cogs_amount) FILTER (WHERE {self._where(prev, filters, flt)}) AS c0,
         SUM(quantity) FILTER (WHERE {self._where(cur, filters, flt)}) AS q1,
         SUM(cogs_amount) FILTER (WHERE {self._where(cur, filters, flt)}) AS c1
  FROM v_sales_lines GROUP BY ALL)
SELECT supplier, category,
       SUM(CASE WHEN q0 > 0 AND q1 > 0 THEN (c1 / q1 - c0 / q0) * q1 ELSE 0 END) AS cost_rate_effect,
       SUM(COALESCE(c1, 0)) AS cogs_current, SUM(COALESCE(c0, 0)) AS cogs_previous
FROM p GROUP BY ALL ORDER BY cost_rate_effect DESC"""
        df, r = self.db.run(sql, purpose="supplier cost-rate effect on COGS")
        rec.append(r)
        df["cost_change_pct"] = np.where(df.cogs_previous > 0,
                                         df.cost_rate_effect / (df.cogs_current - df.cost_rate_effect).replace(0, np.nan) * 100, np.nan)
        return df

    # ------------------------------------------------------------------ lost customers
    def lost_customers(self, cur: TimeRange, prev: TimeRange, filters: dict, rec: list, limit: int = 10) -> pd.DataFrame:
        cols = self.cat.models["sales"].dimensions
        extra = "".join(" AND " + Compiler._filter_sql(cols[d], v) for d, v in filters.items())
        sql = f"""
SELECT customer_name AS customer, ANY_VALUE(segment) AS segment, ANY_VALUE(region) AS region,
       BOOL_OR(is_key_account) AS key_account,
       SUM(net_line_amount - refund_amount) FILTER (WHERE ship_date BETWEEN DATE {q(prev.start)} AND DATE {q(prev.end)}) AS previous,
       COALESCE(SUM(net_line_amount - refund_amount) FILTER (WHERE ship_date BETWEEN DATE {q(cur.start)} AND DATE {q(cur.end)}), 0) AS current,
       MAX(order_date) AS last_order
FROM v_sales_lines
WHERE order_status = 'Shipped'{extra}
GROUP BY customer_name
HAVING previous > 0 AND MAX(order_date) < DATE {q(cur.end)} - 90 AND current < previous
ORDER BY previous DESC LIMIT {limit}"""
        df, r = self.db.run(sql, purpose="customers with no orders in the last 90 days of the period")
        rec.append(r)
        return df

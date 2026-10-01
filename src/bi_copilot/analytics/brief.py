"""Executive brief generation.

Structure (every number comes from a query result, never from a model):
  1. KPI table: current vs previous period and vs same period last year
  2. Main concern: the largest adverse movement, with its main driver (RCA)
  3. Opportunity: the strongest favourable movement, plus repeat-purchase
     trends by category for monthly-or-longer periods
  4. Unusual movements from the anomaly scan
  5. Suggested next steps, chosen by rules from the findings
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..plan import KPI, SQLRecord, TimeRange
from ..semantic.compiler import MetricRequest
from ..semantic.timeparse import previous_period, shift_years
from .grounding import fmt_delta, fmt_money, fmt_value

BRIEF_KPIS = ["net_revenue", "orders", "aov", "gross_margin_pct", "return_rate", "discount_rate",
              "conversion_rate", "support_tickets", "avg_csat", "stockout_rate"]
MONTHLY_KPIS = ["new_customers", "cac"]
THRESHOLDS = {"currency": 5.0, "number": 5.0, "percent": 0.5, "decimal": 0.1}   # % change, pp, or absolute


@dataclass
class Brief:
    period: TimeRange
    previous: TimeRange
    last_year: TimeRange
    kpis: list[KPI]
    table: pd.DataFrame
    concern: dict | None = None
    opportunity: dict | None = None
    repeat: dict | None = None
    anomalies: list = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    facts: dict[str, float] = field(default_factory=dict)
    sql: list[SQLRecord] = field(default_factory=list)
    narrative: str = ""
    headline: str = ""
    immature: bool = False


class BriefBuilder:
    def __init__(self, copilot):
        self.c = copilot

    def _values(self, metrics, tr, rec) -> dict:
        df, r = self.c.db.run(self.c.comp.compile(MetricRequest(metrics, tr)), purpose=f"KPI snapshot {tr.label}")
        rec.append(r)
        return {m: (None if pd.isna(df.iloc[0][m]) else float(df.iloc[0][m])) for m in metrics}

    def build(self, period: TimeRange) -> Brief:
        cat = self.c.cat
        rec: list[SQLRecord] = []
        prev, ly = previous_period(period), shift_years(period, -1)
        kpis = BRIEF_KPIS + (MONTHLY_KPIS if period.days() >= 28 else [])
        cur_v, prev_v, ly_v = (self._values(kpis, tr, rec) for tr in (period, prev, ly))
        rows, cards, facts = [], [], {}
        for k in kpis:
            met = cat.metrics[k]
            c, p, y = cur_v[k], prev_v[k], ly_v[k]
            if c is None:
                continue
            if met.format in ("percent", "decimal"):
                d_prev = c - p if p is not None else None
                d_ly = c - y if y is not None else None
            else:
                d_prev = (c / p - 1) * 100 if p else None
                d_ly = (c / y - 1) * 100 if y else None
            rows.append(dict(metric=met.label, key=k, fmt=met.format, direction=met.direction, current=c, previous=p,
                             last_year=y, change_vs_previous=d_prev, change_vs_last_year=d_ly))
            facts.update({f"{k}_current": c, f"{k}_previous": p, f"{k}_last_year": y,
                          f"{k}_chg_prev": d_prev, f"{k}_chg_ly": d_ly})
            if met.format in ("percent", "decimal"):
                facts[f"{k}_chg_prev_pp"] = d_prev * 100 if d_prev is not None and met.format == "percent" else d_prev
                facts[f"{k}_chg_ly_pp"] = d_ly * 100 if d_ly is not None and met.format == "percent" else d_ly
            cards.append(KPI(label=met.label, value=c, fmt=met.format, direction=met.direction,
                             delta_pct=d_ly if met.format not in ("percent", "decimal") else None,
                             delta_abs=d_ly if met.format in ("percent", "decimal") else None))
        table = pd.DataFrame(rows)
        b = Brief(period, prev, ly, cards, table, facts=facts, sql=rec)

        # ---------------- concern & opportunity (vs last year, to avoid seasonality)
        def adverse_score(r):
            ch = r["change_vs_last_year"]
            if ch is None or pd.isna(ch):
                return 0.0
            mag = ch * 100 if r["fmt"] == "percent" else ch
            signed = mag if r["direction"] == "up" else -mag
            return -signed / THRESHOLDS[r["fmt"]]

        table["adverse"] = table.apply(adverse_score, axis=1) if len(table) else []
        # refunds land up to ~6 weeks after the sale, so recent return rates are not final yet
        from ..config import DATA_END
        if (DATA_END - period.end).days < 45 and len(table):
            table.loc[table.key == "return_rate", "adverse"] = 0.0
            b.immature = True
        if len(table):
            worst = table.sort_values("adverse", ascending=False).iloc[0]
            if worst["adverse"] >= 1:
                b.concern = worst.to_dict()
                try:
                    rca = self.c.rca.analyze(worst["key"], period, ly, {}, max_depth=2)
                    b.sql += rca.sql
                    if rca.path:
                        s = rca.path[0]
                        b.concern["driver"] = dict(dimension=s.dimension, member=s.member, delta=s.delta, share=s.share_of_total)
                        facts["concern_driver_delta"] = s.delta
                        facts["concern_driver_share"] = s.share_of_total * 100
                        if len(rca.path) > 1:
                            s2 = rca.path[1]
                            b.concern["driver2"] = dict(dimension=s2.dimension, member=s2.member, delta=s2.delta)
                            facts["concern_driver2_delta"] = s2.delta
                except Exception:  # noqa: BLE001 - the brief still stands without a driver
                    pass
            best = table.sort_values("adverse").iloc[0]
            if best["adverse"] <= -1:
                b.opportunity = best.to_dict()

        # ---------------- repeat purchase by category (monthly+ periods)
        if period.days() >= 28:
            try:
                rr = {}
                for tag, tr in (("cur", period), ("ly", ly)):
                    df, r = self.c.db.run(self.c.comp.compile(MetricRequest(["repeat_purchase_rate"], tr, ["category"])),
                                          purpose=f"repeat purchase rate by category, {tr.label}")
                    rec.append(r)
                    rr[tag] = df.set_index("category")["repeat_purchase_rate"]
                d = (rr["cur"] - rr["ly"]).dropna().sort_values(ascending=False)
                if len(d) and d.iloc[0] >= 0.05:
                    cat_name = d.index[0]
                    b.repeat = dict(category=cat_name, current=float(rr["cur"][cat_name]), last_year=float(rr["ly"][cat_name]))
                    facts.update(repeat_current=b.repeat["current"], repeat_last_year=b.repeat["last_year"],
                                 repeat_change_pp=(b.repeat["current"] - b.repeat["last_year"]) * 100)
            except Exception:  # noqa: BLE001
                pass

        # ---------------- anomalies
        try:
            scan = self.c.anomaly.scan(period)
            b.sql += scan.sql
            b.anomalies = scan.anomalies[:4]
            for i, a in enumerate(b.anomalies):
                facts[f"anomaly_{i}_value"] = a.value
                facts[f"anomaly_{i}_expected"] = a.expected
        except Exception:  # noqa: BLE001
            pass

        b.actions = self._actions(b)
        b.headline, b.narrative = self._write(b)
        return b

    # ------------------------------------------------------------------ writing
    def _move(self, r: dict, vs: str = "last_year") -> str:
        ch = r[f"change_vs_{vs}"]
        if r["fmt"] == "percent":
            return f"{fmt_value(r['current'], 'percent')} ({fmt_delta(ch, 'percent')} vs {'last year' if vs == 'last_year' else 'the previous period'})"
        if r["fmt"] == "decimal":
            return f"{r['current']:.2f} ({ch:+.2f} vs last year)"
        return f"{fmt_value(r['current'], r['fmt'])} ({ch:+.1f}% vs {'last year' if vs == 'last_year' else 'the previous period'})"

    def _write(self, b: Brief) -> tuple[str, str]:
        t = b.table.set_index("key") if len(b.table) else pd.DataFrame()
        lines = []
        if "net_revenue" in t.index:
            r = t.loc["net_revenue"].to_dict()
            p_ch, y_ch = r["change_vs_previous"], r["change_vs_last_year"]
            head = (f"Net revenue for {b.period.label} was {fmt_money(r['current'])}, "
                    f"{'up' if y_ch >= 0 else 'down'} {abs(y_ch):.1f}% on the same period last year")
            if p_ch is not None:
                head += f" and {'up' if p_ch >= 0 else 'down'} {abs(p_ch):.1f}% on {b.previous.label}"
            head += "."
        else:
            head = f"Business summary for {b.period.label}."
        parts = []
        gm = t.loc["gross_margin_pct"].to_dict() if "gross_margin_pct" in t.index else None
        od = t.loc["orders"].to_dict() if "orders" in t.index else None
        if od:
            parts.append(f"Orders were {self._move(od)}")
        if gm:
            parts.append(f"gross margin was {self._move(gm)}")
        if parts:
            lines.append("; ".join(parts) + ".")
        if b.concern:
            c = b.concern
            txt = f"**Main concern:** {c['metric']} at {self._move(c)}."
            if c.get("driver"):
                d = c["driver"]
                txt += (f" The biggest single contributor is {d['member']} ({cat_label(self.c.cat, d['dimension'])}), "
                        f"{fmt_delta(d['delta'], c['fmt'])}")
                if c.get("driver2"):
                    txt += f", concentrated in {c['driver2']['member']}"
                txt += "."
            lines.append(txt)
        else:
            lines.append("**Main concern:** no KPI moved against us by more than the alert threshold versus last year.")
        if b.opportunity:
            o = b.opportunity
            lines.append(f"**Opportunity:** {o['metric']} improved to {self._move(o)}.")
        if b.repeat:
            r = b.repeat
            lines.append(f"**Customer loyalty:** repeat purchase rate in {r['category']} rose to "
                         f"{r['current'] * 100:.1f}% from {r['last_year'] * 100:.1f}% a year ago, the largest improvement of any category. "
                         f"Worth finding out what changed and whether it can be repeated elsewhere.")
        biz = [a for a in b.anomalies if a.kind == "business"]
        dq = [a for a in b.anomalies if a.kind == "data_quality"]
        if biz:
            items = [f"{a.label}{' in ' + a.member if a.member else ''} (week of {a.week:%d %b}: "
                     f"{fmt_value(a.value, a.fmt)} vs about {fmt_value(a.expected, a.fmt)} expected)" for a in biz[:3]]
            lines.append("**Unusual movements:** " + "; ".join(items) + ".")
        if dq:
            a = dq[0]
            lines.append(f"**Data quality:** {a.label} recorded zero on {', '.join(d.strftime('%d %b') for d in a.gap_days)}; "
                         f"this looks like a tracking problem rather than a business change.")
        if b.immature:
            lines.append("*Return rate for the latest weeks is still provisional: refunds arrive up to six weeks after the sale.*")
        if b.actions:
            lines.append("**Suggested next steps:** " + " ".join(f"({i}) {a}" for i, a in enumerate(b.actions, 1)))
        return head, "\n\n".join(lines)

    def _actions(self, b: Brief) -> list[str]:
        acts = []
        c = b.concern
        if c:
            k = c["key"]
            d = c.get("driver") or {}
            if k in ("net_revenue", "orders") and d:
                acts.append(f"Ask the {d['member']} owners for an account-level view of the decline and any lost customers.")
            elif k == "return_rate":
                acts.append("Review return reasons for the products driving the increase with the quality team.")
            elif k == "discount_rate":
                acts.append("Check recent large discounts against the approval policy (over 15% needs director sign-off).")
            elif k == "gross_margin_pct":
                acts.append("Compare supplier cost changes with list prices for the categories losing margin.")
            elif k == "stockout_rate":
                acts.append("Review open purchase orders and expedite replenishment for out-of-stock items.")
            elif k in ("conversion_rate",):
                acts.append("Check traffic quality by channel and recent site changes.")
            elif k == "cac":
                acts.append("Review paid acquisition spend by channel and pause the least efficient campaigns.")
            else:
                acts.append(f"Investigate the drivers of {c['metric'].lower()} in more detail.")
        if any(a.kind == "data_quality" for a in b.anomalies):
            acts.append("Confirm the tracking incident with the web team and exclude the affected days from funnel reporting.")
        if b.repeat:
            acts.append(f"Look into what lifted repeat purchases in {b.repeat['category']}.")
        return acts[:3]


def cat_label(cat, dim: str) -> str:
    d = cat.dimensions.get(dim)
    return d.label.lower() if d else dim

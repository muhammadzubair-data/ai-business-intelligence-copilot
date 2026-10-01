"""Bounded investigation agent.

For "investigate why profit declined in Tech Accessories" the agent runs a
short chain of analysis tools, each reading governed metrics:

    profit change -> revenue vs cost -> cost-rate effect by supplier
                  -> price/volume/mix -> customers lost -> conclusion

Guarantees: a fixed tool list, validated arguments, a hard step limit, and a
visible trace (tool, arguments, what it found) in the answer's evidence.
By default the next step is chosen by a deterministic policy. With an LLM
configured, the LLM may choose the next tool from the same list; invalid
choices fall back to the policy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..plan import SQLRecord, TimeRange
from .grounding import fmt_delta, fmt_money, fmt_value, pct_change

TOOLS = {
    "metric_change": "Compare a metric between the two periods. args: metric",
    "contributions": "Split a metric's change by a dimension. args: metric, dimension",
    "supplier_cost_effect": "COGS change caused by supplier unit-cost changes, by supplier. args: none",
    "price_volume_mix": "Decompose revenue change into price, volume, mix and refunds. args: none",
    "lost_customers": "Customers with revenue last period and no orders in the final 90 days. args: none",
    "opex_by_department": "Operating expense change by department. args: none",
    "finish": "Stop and write the conclusion. args: none",
}


@dataclass
class Step:
    n: int
    tool: str
    args: dict
    finding: str
    facts: dict = field(default_factory=dict)


@dataclass
class Investigation:
    metric: str
    period: TimeRange
    comparison: TimeRange
    filters: dict
    steps: list[Step] = field(default_factory=list)
    sql: list[SQLRecord] = field(default_factory=list)
    conclusion: str = ""
    headline: str = ""
    facts: dict[str, float] = field(default_factory=dict)
    policy: str = "deterministic"
    drivers: list = field(default_factory=list)


class InvestigationAgent:
    def __init__(self, copilot, llm=None, max_steps: int | None = None):
        self.c = copilot
        self.llm = llm
        self.max_steps = max_steps or settings.agent_max_steps

    # ------------------------------------------------------------------ tools
    def _metric_change(self, inv: Investigation, metric: str) -> Step:
        rec = inv.sql
        cur = self.c.rca.value(metric, inv.period, inv.filters, rec)
        prev = self.c.rca.value(metric, inv.comparison, inv.filters, rec)
        met = self.c.cat.metrics[metric]
        pc = pct_change(cur, prev)
        facts = {f"{metric}_current": cur, f"{metric}_previous": prev, f"{metric}_delta": cur - prev}
        if pc is not None:
            facts[f"{metric}_pct"] = pc
        if met.format == "percent":
            txt = f"{met.label} moved from {fmt_value(prev, 'percent')} to {fmt_value(cur, 'percent')} ({fmt_delta(cur - prev, 'percent')})."
        else:
            txt = (f"{met.label} went from {fmt_value(prev, met.format)} to {fmt_value(cur, met.format)} "
                   f"({fmt_delta(cur - prev, met.format)}{f', {pc:+.1f}%' if pc is not None else ''}).")
        return Step(0, "metric_change", {"metric": metric}, txt, facts)

    def _contributions(self, inv: Investigation, metric: str, dimension: str) -> Step:
        t = self.c.rca.by_dim(metric, inv.period, inv.comparison, dimension, inv.filters, inv.sql)
        met = self.c.cat.metrics[metric]
        top = t.head(3)
        items = [f"{r.member} {fmt_delta(r.delta, met.format)}" for r in top.itertuples()]
        facts = {f"contrib_{dimension}_{i}": float(r.delta) for i, r in enumerate(top.itertuples())}
        inv.drivers.append((dimension, top))
        return Step(0, "contributions", {"metric": metric, "dimension": dimension},
                    f"By {dimension.replace('_', ' ')}, the largest contributors to the change in {met.label.lower()} were: "
                    + "; ".join(items) + ".", facts)

    def _supplier_cost(self, inv: Investigation) -> Step:
        df = self.c.rca.cost_effect(inv.period, inv.comparison, inv.filters, inv.sql)
        by_sup = df.groupby("supplier", as_index=False)[["cost_rate_effect", "cogs_current"]].sum() \
                   .sort_values("cost_rate_effect", ascending=False)
        total = float(df["cost_rate_effect"].sum())
        top = by_sup.iloc[0]
        base = float(top.cogs_current - top.cost_rate_effect)
        pct = float(top.cost_rate_effect / base * 100) if base else 0.0
        facts = {"cost_effect_total": total, "cost_effect_top": float(top.cost_rate_effect), "cost_effect_top_pct": pct}
        inv.facts["_top_supplier"] = 0.0
        self._top_supplier = str(top.supplier)
        return Step(0, "supplier_cost_effect", {},
                    f"Supplier unit-cost changes added {fmt_money(total)} to cost of goods sold at current volumes. "
                    f"{top.supplier} accounts for {fmt_money(float(top.cost_rate_effect))} of that "
                    f"(its unit costs are about {pct:.1f}% higher).", facts)

    def _pvm(self, inv: Investigation) -> Step:
        p = self.c.rca.pvm("net_revenue", inv.period, inv.comparison, inv.filters, inv.sql)
        facts = {k: v for k, v in p.items() if k.endswith("_effect")}
        txt = (f"Revenue change splits into price {fmt_delta(p['price_effect'], 'currency')}, volume {fmt_delta(p['volume_effect'], 'currency')}, "
               f"mix {fmt_delta(p['mix_effect'], 'currency')}"
               + (f" and refunds {fmt_delta(p['refund_effect'], 'currency')}" if "refund_effect" in p else "") + ".")
        return Step(0, "price_volume_mix", {}, txt, facts)

    def _lost(self, inv: Investigation) -> Step:
        df = self.c.rca.lost_customers(inv.period, inv.comparison, inv.filters, inv.sql)
        key = df[df.key_account] if len(df) else df
        if len(key):
            names = ", ".join(key.customer.head(4))
            tot = float(key.previous.sum())
            scope = " from this slice" if inv.filters else ""
            return Step(0, "lost_customers", {}, f"{len(key)} key accounts stopped buying{scope} ({names}); they generated "
                        f"{fmt_money(tot)} here in {inv.comparison.label}.", {"lost_key_revenue": tot})
        return Step(0, "lost_customers", {}, "No key accounts stopped ordering in this slice.", {})

    def _opex(self, inv: Investigation) -> Step:
        t = self.c.rca.by_dim("opex", inv.period, inv.comparison, "department", {}, inv.sql)
        top = t.iloc[0]
        return Step(0, "opex_by_department", {}, f"Operating expenses changed by {fmt_delta(float(t.delta.sum()), 'currency')}; "
                    f"the largest move was {top.member} ({fmt_delta(float(top.delta), 'currency')}).",
                    {"opex_delta": float(t.delta.sum()), "opex_top_delta": float(top.delta)})

    def run_tool(self, inv: Investigation, tool: str, args: dict) -> Step:
        if tool == "metric_change":
            m = args.get("metric", inv.metric)
            if m not in self.c.cat.metrics:
                raise ValueError(f"unknown metric {m}")
            return self._metric_change(inv, m)
        if tool == "contributions":
            m, d = args.get("metric", inv.metric), args.get("dimension")
            if m not in self.c.cat.metrics or d not in self.c.cat.allowed_dimensions(m):
                raise ValueError(f"invalid contributions args {args}")
            return self._contributions(inv, m, d)
        if tool == "supplier_cost_effect":
            return self._supplier_cost(inv)
        if tool == "price_volume_mix":
            return self._pvm(inv)
        if tool == "lost_customers":
            return self._lost(inv)
        if tool == "opex_by_department":
            return self._opex(inv)
        raise ValueError(f"unknown tool {tool}")

    # ------------------------------------------------------------------ policy
    def policy(self, inv: Investigation) -> list[tuple[str, dict]]:
        m = inv.metric
        profit = {"gross_profit", "gross_margin_pct", "contribution_profit", "contribution_margin_pct", "operating_profit", "operating_margin_pct"}
        if m in profit:
            gp = "gross_profit" if m not in ("contribution_profit", "contribution_margin_pct") else "contribution_profit"
            plan = [("metric_change", {"metric": m})]
            if m != gp:
                plan.append(("metric_change", {"metric": gp}))
            plan += [("metric_change", {"metric": "net_revenue"}), ("metric_change", {"metric": "cogs"}),
                     ("supplier_cost_effect", {}), ("price_volume_mix", {})]
            if "category" not in inv.filters:
                plan.append(("contributions", {"metric": gp, "dimension": "category"}))
            if m.startswith("operating"):
                plan.append(("opex_by_department", {}))
            plan.append(("lost_customers", {}))
            return plan
        if m in ("net_revenue", "booked_revenue", "orders", "sales_after_discounts"):
            plan = [("metric_change", {"metric": m}), ("price_volume_mix", {})]
            for d in ("region", "segment", "channel", "category"):
                if d not in inv.filters:
                    plan.append(("contributions", {"metric": m, "dimension": d}))
            plan.append(("lost_customers", {}))
            return plan
        allowed = [d for d in self.c.cat.allowed_dimensions(m) if d not in inv.filters][:4]
        return [("metric_change", {"metric": m})] + [("contributions", {"metric": m, "dimension": d}) for d in allowed]

    def _llm_next(self, inv: Investigation, done: list[str]) -> tuple[str, dict] | None:
        sys = ("You are a careful business analyst running a bounded investigation. Choose the next tool to call. "
               "Return JSON {\"tool\": name, \"args\": {...}, \"reason\": short}. Use \"finish\" when the cause is clear.")
        user = (f"Question metric: {inv.metric}; period {inv.period.label} vs {inv.comparison.label}; filters {inv.filters}.\n"
                f"Tools: {json.dumps(TOOLS)}\nFindings so far:\n" + "\n".join(f"- {s.tool}: {s.finding}" for s in inv.steps)
                + f"\nAlready called: {done}")
        try:
            out = self.llm.complete_json(sys, user, max_tokens=300)
            tool = out.get("tool")
            if tool in TOOLS:
                return tool, out.get("args") or {}
        except Exception:  # noqa: BLE001
            return None
        return None

    # ------------------------------------------------------------------ run
    def investigate(self, metric: str, period: TimeRange, comparison: TimeRange, filters: dict) -> Investigation:
        inv = Investigation(metric, period, comparison, dict(filters))
        plan = self.policy(inv)
        done: list[str] = []
        i = 0
        while len(inv.steps) < self.max_steps:
            nxt = None
            if self.llm is not None and inv.steps:
                nxt = self._llm_next(inv, done)
                inv.policy = f"llm:{getattr(self.llm, 'name', 'llm')}"
            if nxt is None:
                if i >= len(plan):
                    break
                nxt = plan[i]
                i += 1
            tool, args = nxt
            if tool == "finish":
                break
            sig = f"{tool}:{json.dumps(args, sort_keys=True)}"
            if sig in done:
                if i >= len(plan):
                    break
                continue
            done.append(sig)
            try:
                step = self.run_tool(inv, tool, args)
            except Exception as e:  # noqa: BLE001
                step = Step(0, tool, args, f"Skipped: {e}", {})
            step.n = len(inv.steps) + 1
            inv.steps.append(step)
            inv.facts.update(step.facts)
        inv.headline, inv.conclusion = self._conclude(inv)
        return inv

    def _conclude(self, inv: Investigation) -> tuple[str, str]:
        f = inv.facts
        met = self.c.cat.metrics[inv.metric]
        scope = " in " + ", ".join(v[0] for v in inv.filters.values()) if inv.filters else ""
        d = f.get(f"{inv.metric}_delta", 0.0)
        direction = "fell" if d < 0 else "rose"
        if met.format == "percent":
            head = f"{met.label}{scope} {direction} by {abs(d) * 100:.1f} pp in {inv.period.label} versus {inv.comparison.label}."
        else:
            head = f"{met.label}{scope} {direction} by {fmt_money(abs(d)) if met.format == 'currency' else fmt_value(abs(d), met.format)} " \
                   f"in {inv.period.label} versus {inv.comparison.label}."
        reasons = []
        gp_delta = f.get("gross_profit_delta", f.get("contribution_profit_delta"))
        cost = f.get("cost_effect_top")
        if gp_delta is not None and gp_delta < 0 and cost is not None and cost > 0.3 * abs(gp_delta):
            reasons.append(f"The main cause is higher supplier costs: {getattr(self, '_top_supplier', 'one supplier')} "
                           f"raised unit costs by about {f['cost_effect_top_pct']:.1f}%, which alone added "
                           f"{fmt_money(cost)} to costs, about {cost / abs(gp_delta) * 100:.0f}% of the profit decline.")
            f["cost_share_of_decline"] = cost / abs(gp_delta) * 100
        rev = f.get("net_revenue_delta")
        if rev is not None and gp_delta is not None and gp_delta < 0:
            if rev < 0:
                reasons.append(f"Lower revenue ({fmt_delta(rev, 'currency')}) also contributed.")
            else:
                reasons.append(f"Revenue was not the problem ({fmt_delta(rev, 'currency')}).")
        if "price_effect" in f and not reasons:
            big = max(("price", "volume", "mix", "refund"), key=lambda k: abs(f.get(f"{k}_effect", 0)))
            reasons.append(f"The largest component of the revenue change is the {big} effect "
                           f"({fmt_delta(f[f'{big}_effect'], 'currency')}).")
        if f.get("lost_key_revenue"):
            reasons.append(f"Key accounts that stopped ordering represented {fmt_money(f['lost_key_revenue'])} "
                           f"in {inv.comparison.label}.")
        if not reasons:
            reasons.append("No single factor dominates; see the steps below for the breakdown.")
        steps = "\n".join(f"{s.n}. **{s.tool.replace('_', ' ')}** — {s.finding}" for s in inv.steps)
        return head, " ".join(reasons) + "\n\n**How I got there**\n\n" + steps

    def trace(self, inv: Investigation) -> list[dict[str, Any]]:
        return [dict(step=s.n, tool=s.tool, args=s.args, finding=s.finding) for s in inv.steps]

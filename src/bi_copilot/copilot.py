"""The Copilot: question in, grounded answer with evidence out.

    question
      -> retrieve business context (RAG)
      -> plan (LLM or offline) -> validate against the metric catalog
      -> route by intent: metric | root_cause | forecast | anomaly | brief |
                          definition | investigate | unsupported | clarify
      -> compiled SQL on a read-only connection (guarded)
      -> templated narrative from computed facts
      -> optional LLM rewrite, accepted only if every number is grounded
      -> Answer (headline, narrative, KPIs, drivers, chart, table, evidence)
"""
from __future__ import annotations

import re
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .analytics import viz
from .analytics.agent import InvestigationAgent
from .analytics.anomaly import AnomalyDetector
from .analytics.brief import BriefBuilder, cat_label
from .analytics.forecast import Forecaster
from .analytics.grounding import check_grounding, fmt_delta, fmt_money, fmt_value, pct_change
from .analytics.rca import RootCauseAnalyzer
from .config import DATA_END, DATA_START, settings
from .db import Database, get_db
from .llm.providers import LLM, get_llm
from .plan import KPI, Answer, Driver, Evidence, QueryPlan, SQLRecord, TimeRange
from .rag.retriever import Retriever, get_retriever
from .semantic.catalog import Catalog, get_catalog
from .semantic.compiler import CompileError, Compiler, MetricRequest, q
from .semantic.planner import LLMPlanner, OfflinePlanner, value_hints
from .semantic.timeparse import previous_period, shift_years

ALTERNATIVES = {"ebitda": "operating_profit", "ebitda margin": "operating_margin_pct", "net income": "operating_profit",
                "net profit": "operating_profit", "nps": "avg_csat", "net promoter score": "avg_csat"}

NARRATIVE_SYSTEM = """You rewrite analytics findings for a busy executive at Halden Supply Co.
Rules: use ONLY the numbers that appear in the draft or in FACTS; never add, estimate or round differently;
keep every caveat; plain business English; no hype; at most 120 words; keep markdown bold labels if present."""


class Copilot:
    def __init__(self, db: Database | None = None, llm: LLM | None | str = "auto",
                 catalog: Catalog | None = None, retriever: Retriever | None = None, polish: bool = True):
        self.db = db or get_db()
        self.cat = catalog or get_catalog()
        self.comp = Compiler(self.cat)
        self.retriever = retriever or get_retriever()
        self.llm = get_llm() if llm == "auto" else llm
        self.offline = OfflinePlanner(self.cat, self.db)
        self.planner = LLMPlanner(self.llm, self.cat, self.offline) if self.llm else self.offline
        self.rca = RootCauseAnalyzer(self.db, self.cat, self.comp)
        self.anomaly = AnomalyDetector(self.db, self.cat, self.comp)
        self.forecaster = Forecaster(self.db, self.cat, self.comp)
        self.polish = polish

    @property
    def mode(self) -> str:
        return f"LLM ({self.llm.name}: {getattr(self.llm, 'model', '')})" if self.llm else "offline (rule-based planner)"

    # ------------------------------------------------------------------ entry point
    def plan(self, question: str) -> QueryPlan:
        if isinstance(self.planner, LLMPlanner):
            ctx = "\n".join(f"[{h.chunk.cite()}] {h.chunk.text[:500]}" for h in self.retriever.search(question, k=3))
            return self.planner.plan(question, context=ctx, value_hints=value_hints(str(self.db.path)))
        return self.planner.plan(question)

    def ask(self, question: str) -> Answer:
        t0 = time.perf_counter()
        question = question.strip()
        from .semantic.planner import READ_ONLY_REASON, UNSAFE
        if UNSAFE.search(question):
            plan = QueryPlan(question=question, intent="unsupported", reason=READ_ONLY_REASON, planner="policy")
            ans = Answer(question=question, status="unsupported", intent="unsupported", plan=plan, confidence="high",
                         headline="I can't do that.", narrative=READ_ONLY_REASON,
                         evidence=Evidence(data_as_of=self.db.data_as_of(),
                                           limitations=["Blocked by the read-only request policy before any SQL was written."]))
            return ans
        try:
            plan = self.plan(question)
        except Exception as e:  # noqa: BLE001
            return Answer(question=question, status="error", headline="I couldn't interpret that question.",
                          narrative=str(e), confidence="low")
        handler = {
            "metric": self._metric, "root_cause": self._root_cause, "forecast": self._forecast,
            "anomaly": self._anomaly, "brief": self._brief, "definition": self._definition,
            "investigate": self._investigate, "unsupported": self._unsupported, "clarify": self._clarify,
        }[plan.intent]
        try:
            ans = handler(plan)
        except CompileError as e:
            ans = Answer(question=question, status="clarify", intent=plan.intent, headline="That combination isn't available.",
                         narrative=str(e), confidence="low")
        except Exception as e:  # noqa: BLE001
            ans = Answer(question=question, status="error", intent=plan.intent,
                         headline="Something went wrong while analysing this question.",
                         narrative=f"{type(e).__name__}: {e}", confidence="low")
        ans.plan = plan
        ans.intent = plan.intent
        self._finish(ans, plan)
        ans.evidence.assumptions = list(dict.fromkeys(plan.assumptions + ans.evidence.assumptions))
        ans.evidence.limitations.append(f"Answered in {time.perf_counter() - t0:.1f}s using {self.mode}.")
        return ans

    # ------------------------------------------------------------------ shared
    def _evidence(self, plan: QueryPlan, sql: list[SQLRecord], metrics: list[str] | None = None) -> Evidence:
        ev = Evidence(sql=sql, filters=plan.filters, data_as_of=self.db.data_as_of())
        for m in metrics or plan.metrics:
            met = self.cat.metrics.get(m)
            if met:
                ev.metrics[met.label] = met.definition
                for c in [met.numerator, met.denominator] + list(met.components):
                    if c and c in self.cat.metrics and not self.cat.metrics[c].hidden:
                        ev.metrics[self.cat.metrics[c].label] = self.cat.metrics[c].definition
        if plan.time_range:
            ev.periods.append(f"{plan.time_range.label}: {plan.time_range.start} to {plan.time_range.end}")
        if plan.comparison:
            ev.periods.append(f"{plan.comparison.label}: {plan.comparison.start} to {plan.comparison.end}")
        return ev

    def _finish(self, ans: Answer, plan: QueryPlan) -> None:
        # knowledge-base context: incidents overlapping the period, and definitions
        q_text = " ".join([plan.question] + [self.cat.metrics[m].label for m in plan.metrics if m in self.cat.metrics])
        hits = [h for h in self.retriever.search(q_text, k=4, min_score=0.15)
                if h.chunk.source != "incident_log.md" and not (h.chunk.id.startswith("unsupported:") and plan.intent != "unsupported")][:3]
        if plan.time_range and plan.time_range.start <= pd.Timestamp("2025-08-12").date() <= plan.time_range.end + timedelta(days=0) \
                and plan.time_range.end >= pd.Timestamp("2025-08-10").date():
            models = {self.cat.metrics[m].model for m in plan.metrics if m in self.cat.metrics}
            if "web" in models or plan.intent in ("anomaly", "brief"):
                for h in self.retriever.search("web analytics tracking outage sessions August 2025 incident", k=1):
                    if h.chunk.source == "incident_log.md":
                        hits.insert(0, h)
                        ans.evidence.limitations.append("Web analytics data is missing for 10–12 Aug 2025 (incident INC-2025-021).")
        seen = set(ans.evidence.documents)
        for h in hits:
            c = h.chunk.cite()
            if c not in seen:
                ans.evidence.documents.append(c)
                seen.add(c)
        # optional LLM rewrite, accepted only when grounded
        if self.llm and self.polish and ans.status == "ok" and ans.narrative and plan.intent not in ("definition",):
            try:
                facts_txt = "\n".join(f"{k}: {v}" for k, v in ans.facts.items() if v is not None)
                draft = f"{ans.headline}\n\n{ans.narrative}"
                out = self.llm.complete(NARRATIVE_SYSTEM, f"DRAFT\n{draft}\n\nFACTS\n{facts_txt}", max_tokens=500)
                bad = check_grounding(out, {**ans.facts, **self._numbers_in(draft)})
                if bad:
                    ans.evidence.limitations.append(
                        f"The language model's rewrite used figures not in the data ({', '.join(bad[:4])}), so the verified template was kept.")
                else:
                    ans.narrative = out.strip()
            except Exception:  # noqa: BLE001
                pass
        if ans.status == "ok":
            bad = check_grounding(f"{ans.headline}\n{ans.narrative}", {**ans.facts, **self._numbers_in_table(ans)})
            ans.grounded = not bad
            if bad:
                ans.evidence.limitations.append(f"Unverified figures in narrative: {', '.join(bad)}")

    @staticmethod
    def _numbers_in(text: str) -> dict:
        from .analytics.grounding import extract_numbers
        return {f"draft_{i}": v for i, (_, v) in enumerate(extract_numbers(text))}

    @staticmethod
    def _numbers_in_table(ans: Answer) -> dict:
        out = {}
        for i, row in enumerate(ans.table or []):
            for k, v in row.items():
                if isinstance(v, (int, float, np.floating)) and not pd.isna(v):
                    out[f"t{i}_{k}"] = float(v)
        return out

    def _followups(self, plan: QueryPlan) -> list[str]:
        if not plan.metrics:
            return ["Give me the weekly business brief", "Anything unusual this week?", "Why did revenue decline in Q2 2025?"]
        lab = self.cat.metrics[plan.metrics[0]].label.lower()
        per = plan.time_range.label if plan.time_range else "last quarter"
        out = []
        if plan.intent != "root_cause":
            out.append(f"Why did {lab} change in {per}?")
        if plan.intent != "forecast":
            out.append(f"Forecast {lab} for the next quarter")
        allowed = self.cat.allowed_dimensions(plan.metrics[0])
        for d in ("region", "segment", "category", "channel", "marketing_channel", "supplier"):
            if d in allowed and d not in plan.dimensions and d not in plan.filters:
                out.append(f"Show {lab} by {self.cat.dimensions[d].label.lower()} for {per}")
                break
        out.append(f"How is {lab} calculated?")
        return out[:3]

    def _run(self, req: MetricRequest, purpose: str) -> tuple[pd.DataFrame, SQLRecord]:
        return self.db.run(self.comp.compile(req), purpose=purpose)

    # ------------------------------------------------------------------ metric
    def _metric(self, plan: QueryPlan) -> Answer:
        t = plan.question.lower()
        m0 = plan.metrics[0]
        met = self.cat.metrics[m0]
        if re.search(r"\b(distribution|histogram)\b", t) and met.model == "sales":
            return self._distribution(plan)
        req = MetricRequest(plan.metrics, plan.time_range, plan.dimensions, plan.filters, plan.time_grain,
                            plan.order, plan.limit)
        df, rec = self._run(req, f"{', '.join(plan.metrics)} for {plan.time_range.label}")
        sqls = [rec]
        ans = Answer(question=plan.question, evidence=self._evidence(plan, sqls))
        facts: dict[str, float] = {}
        if df.empty or df[m0].isna().all():
            ans.status = "ok"
            ans.headline = f"No {met.label.lower()} data matches that selection for {plan.time_range.label}."
            ans.narrative = "Try widening the period or removing a filter."
            ans.confidence = "medium"
            ans.followups = self._followups(plan)
            return ans
        scope = self._scope(plan)
        cmp_df = None
        if plan.comparison is not None:
            creq = MetricRequest(plan.metrics, plan.comparison, plan.dimensions, plan.filters, plan.time_grain)
            cmp_df, crec = self._run(creq, f"{', '.join(plan.metrics)} for {plan.comparison.label}")
            sqls.append(crec)

        if plan.time_grain:
            ans.chart = viz.line(df, m0, met.label, met.format, plan.dimensions[0] if plan.dimensions else None,
                                 title=f"{met.label}{scope} by {plan.time_grain}, {plan.time_range.label}")
            ans.chart_type = "line"
            head, narr, facts = self._describe_trend(df, plan, met, scope)
        elif plan.dimensions:
            head, narr, facts = self._describe_breakdown(df, plan, met, scope, cmp_df)
            if cmp_df is not None:
                d = plan.dimensions[0]
                merged = df.merge(cmp_df, on=plan.dimensions, how="outer", suffixes=("", "_prev"))
                ans.chart = viz.grouped_bar(merged.head(15), d, m0, f"{m0}_prev", plan.time_range.label,
                                            plan.comparison.label, met.format, f"{met.label}{scope} by {cat_label(self.cat, d)}")
                ans.chart_type = "grouped_bar"
                df = merged
            elif len(plan.metrics) >= 2 and viz.choose_chart(df, plan.dimensions, plan.metrics, None, False, t) == "scatter":
                m1 = self.cat.metrics[plan.metrics[1]]
                ans.chart = viz.scatter(df, plan.dimensions[0], m0, m1.name, met.label, m1.label, met.format, m1.format)
                ans.chart_type = "scatter"
            else:
                ans.chart = viz.bar(df, plan.dimensions[0], m0, met.label, met.format,
                                    title=f"{met.label}{scope} by {cat_label(self.cat, plan.dimensions[0])}, {plan.time_range.label}")
                ans.chart_type = "bar"
        else:
            head, narr, facts, kpis = self._describe_single(df, plan, cmp_df, scope)
            ans.kpis = kpis
            ans.chart_type = "kpi"
            if cmp_df is not None:
                cols = [m for m in plan.metrics if self.cat.metrics[m].format == met.format]
                g = pd.DataFrame({"period": [plan.comparison.label, plan.time_range.label],
                                  m0: [cmp_df.iloc[0][m0], df.iloc[0][m0]]})
                ans.chart = viz.bar(g, "period", m0, met.label, met.format, title=f"{met.label}{scope}")
                ans.chart_type = "bar"
        ans.headline, ans.narrative, ans.facts = head, narr, facts
        ans.table = self._records(df)
        ans.evidence.sql = sqls
        ans.followups = self._followups(plan)
        return ans

    def _scope(self, plan: QueryPlan) -> str:
        if not plan.filters:
            return ""
        parts = []
        for d, v in plan.filters.items():
            if d == "key_account":
                parts.append("key accounts")
            else:
                parts.append(" and ".join(v))
        return " for " + ", ".join(parts)

    def _describe_single(self, df, plan, cmp_df, scope):
        facts, kpis, sentences = {}, [], []
        head = ""
        for i, m in enumerate(plan.metrics):
            met = self.cat.metrics[m]
            v = df.iloc[0][m]
            v = None if pd.isna(v) else float(v)
            facts[f"{m}"] = v
            k = KPI(label=met.label, value=v or 0.0, fmt=met.format, direction=met.direction)
            txt = f"{met.label}{scope} in {plan.time_range.label} was {fmt_value(v, met.format)}"
            if cmp_df is not None and m in cmp_df.columns and not pd.isna(cmp_df.iloc[0][m]):
                p = float(cmp_df.iloc[0][m])
                facts[f"{m}_previous"] = p
                facts[f"{m}_delta"] = (v or 0) - p
                if met.format in ("percent", "decimal"):
                    k.delta_abs = (v or 0) - p
                    txt += f", {fmt_delta((v or 0) - p, met.format)} versus {plan.comparison.label} ({fmt_value(p, met.format)})"
                else:
                    pc = pct_change(v or 0, p)
                    facts[f"{m}_pct"] = pc
                    k.delta_pct = pc
                    if pc is not None:
                        txt += f", {'up' if pc >= 0 else 'down'} {abs(pc):.1f}% from {fmt_value(p, met.format)} in {plan.comparison.label}"
            kpis.append(k)
            if i == 0:
                head = txt + "."
            else:
                sentences.append(txt + ".")
        extra = self._context_note(plan)
        return head, " ".join(sentences + extra), facts, kpis

    def _context_note(self, plan: QueryPlan) -> list[str]:
        notes = []
        if plan.metrics and plan.metrics[0] == "net_revenue" and not any("booked" in m for m in plan.metrics):
            notes.append("This is the Finance definition (by ship date, after refunds); the Sales dashboard's booked revenue will differ.")
        return notes

    def _describe_breakdown(self, df, plan, met, scope, cmp_df):
        d = plan.dimensions[0]
        m = met.name
        facts = {}
        valid = df.dropna(subset=[m])
        top = valid.iloc[0]
        total = float(valid[m].sum()) if met.additive and met.format in ("currency", "number") else None
        label_d = cat_label(self.cat, d)
        adj = "lowest" if plan.order == "asc" else "highest"
        head = (f"{top[d]} has the {adj} {met.label.lower()}{scope} in {plan.time_range.label}: "
                f"{fmt_value(float(top[m]), met.format)}")
        facts["top_value"] = float(top[m])
        if total:
            share = float(top[m]) / total * 100
            head += f" ({share:.0f}% of the total {fmt_value(total, met.format)})"
            facts.update(total=total, top_share=share)
        head += "."
        lines = []
        if len(valid) > 1:
            rest = valid.iloc[1:4]
            lines.append("Next: " + "; ".join(f"{r[d]} {fmt_value(float(r[m]), met.format)}" for _, r in rest.iterrows()) + ".")
            for i, (_, r) in enumerate(rest.iterrows()):
                facts[f"v{i}"] = float(r[m])
            if not plan.limit and len(valid) > 4:
                last = valid.iloc[-1]
                lines.append(f"{'Highest' if plan.order == 'asc' else 'Lowest'}: {last[d]} at {fmt_value(float(last[m]), met.format)}.")
                facts["last_value"] = float(last[m])
        if met.format == "percent" and len(valid) > 1:
            spread = float(valid[m].max() - valid[m].min())
            facts["spread"] = spread
        if cmp_df is not None and d in cmp_df.columns:
            mg = valid.merge(cmp_df, on=plan.dimensions, suffixes=("", "_prev"))
            if len(mg):
                mg["chg"] = mg[m] - mg[f"{m}_prev"]
                mv = mg.sort_values("chg").iloc[0 if plan.order == "asc" else -1]
                big = mg.loc[mg["chg"].abs().idxmax()]
                facts.update(big_chg=float(big["chg"]), big_prev=float(big[f"{m}_prev"]), big_cur=float(big[m]))
                lines.append(f"Biggest change versus {plan.comparison.label}: {big[d]} "
                             f"({fmt_value(float(big[f'{m}_prev']), met.format)} → {fmt_value(float(big[m]), met.format)}, "
                             f"{fmt_delta(float(big['chg']), met.format)}).")
        lines += self._context_note(plan)
        return head, " ".join(lines), facts

    def _describe_trend(self, df, plan, met, scope):
        m = met.name
        facts = {}
        if plan.dimensions:
            d = plan.dimensions[0]
            first, last = df["period"].min(), df["period"].max()
            a = df[df.period == first].set_index(d)[m]
            b = df[df.period == last].set_index(d)[m]
            ch = (b - a).dropna()
            if met.format not in ("percent", "decimal"):
                ch = ((b / a - 1) * 100).dropna()
            best, worst = ch.idxmax(), ch.idxmin()
            facts.update(best_chg=float(ch[best]), worst_chg=float(ch[worst]))
            unit = " pp" if met.format == "percent" else ("%" if met.format not in ("decimal",) else "")
            mult = 100 if met.format == "percent" else 1
            head = (f"{met.label}{scope} by {cat_label(self.cat, d)}, {plan.time_range.label}: {best} grew the most "
                    f"({ch[best] * mult:+.1f}{unit} from first to last {plan.time_grain}), {worst} the least ({ch[worst] * mult:+.1f}{unit}).")
            facts.update(best_chg_s=float(ch[best] * mult), worst_chg_s=float(ch[worst] * mult))
            return head, " ".join(self._context_note(plan)), facts
        s = df.set_index("period")[m].astype(float)
        first, last = float(s.iloc[0]), float(s.iloc[-1])
        pk, lo = s.idxmax(), s.idxmin()
        facts.update(first=first, last=last, peak=float(s.max()), low=float(s.min()),
                     total=float(s.sum()) if met.additive and met.format in ("currency", "number") else None)
        fmtp = {"day": "%d %b %Y", "week": "%d %b %Y", "month": "%b %Y", "quarter": "%b %Y", "year": "%Y"}[plan.time_grain]
        per = lambda p: pd.Timestamp(p).strftime(fmtp) if plan.time_grain != "quarter" else f"Q{(pd.Timestamp(p).month - 1) // 3 + 1} {pd.Timestamp(p).year}"
        if met.format in ("percent", "decimal"):
            chg = fmt_delta(last - first, met.format)
            facts["chg"] = last - first
        else:
            pc = pct_change(last, first)
            facts["chg"] = pc
            chg = f"{pc:+.1f}%" if pc is not None else "n/a"
        head = (f"{met.label}{scope} went from {fmt_value(first, met.format)} in {per(s.index[0])} to "
                f"{fmt_value(last, met.format)} in {per(s.index[-1])} ({chg}).")
        narr = (f"The peak was {per(pk)} at {fmt_value(float(s.max()), met.format)} and the low point was "
                f"{per(lo)} at {fmt_value(float(s.min()), met.format)}.")
        if facts["total"]:
            narr += f" Total over the period: {fmt_value(facts['total'], met.format)}."
        extra = self._context_note(plan)
        return head, " ".join([narr] + extra), facts

    def _distribution(self, plan: QueryPlan) -> Answer:
        cols = self.cat.models["sales"].dimensions
        where = "".join(" AND " + Compiler._filter_sql(cols[d], v) for d, v in plan.filters.items())
        sql = (f"SELECT order_key, SUM(net_line_amount) AS order_value FROM v_sales_lines "
               f"WHERE order_status <> 'Cancelled' AND order_date BETWEEN DATE {q(plan.time_range.start)} "
               f"AND DATE {q(plan.time_range.end)}{where} GROUP BY order_key")
        df, rec = self.db.run(sql, purpose="order values for distribution")
        v = df["order_value"].astype(float)
        cap = v.quantile(0.99)
        facts = dict(median=float(v.median()), mean=float(v.mean()), p90=float(v.quantile(0.9)), n=float(len(v)),
                     share_90=90.0, share_top=1.0)
        ans = Answer(question=plan.question, evidence=self._evidence(plan, [rec], ["aov"]))
        ans.headline = (f"The median order in {plan.time_range.label} was {fmt_money(facts['median'])}; "
                        f"the average was {fmt_money(facts['mean'])} because a small number of large orders pull it up.")
        ans.narrative = f"90% of orders were below {fmt_money(facts['p90'])}. The chart excludes the top 1% for readability."
        ans.chart = viz.histogram(v[v <= cap], "Order value", "currency", f"Distribution of order values, {plan.time_range.label}")
        ans.chart_type = "histogram"
        ans.facts = facts
        ans.followups = self._followups(plan)
        return ans

    @staticmethod
    def _records(df: pd.DataFrame, limit: int = 200) -> list[dict]:
        d = df.head(limit).copy()
        for c in d.columns:
            if pd.api.types.is_datetime64_any_dtype(d[c]):
                d[c] = d[c].dt.strftime("%Y-%m-%d")
        d = d.astype(object).where(pd.notna(d), None)
        return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in d.to_dict("records")]

    # ------------------------------------------------------------------ root cause
    def _root_cause(self, plan: QueryPlan) -> Answer:
        m = plan.metrics[0]
        met = self.cat.metrics[m]
        if plan.comparison is None:
            plan.comparison = shift_years(plan.time_range)
        r = self.rca.analyze(m, plan.time_range, plan.comparison, plan.filters, focus_dims=plan.dimensions)
        ans = Answer(question=plan.question, evidence=self._evidence(plan, r.sql))
        scope = self._scope(plan)
        facts = {"current": r.cur_value, "previous": r.prev_value, "delta": r.delta}
        pc = r.pct
        if pc is not None:
            facts["pct"] = pc
        up = r.delta >= 0
        verb = ("rose" if up else "fell")
        if met.format == "percent":
            head = (f"{met.label}{scope} {verb} from {fmt_value(r.prev_value, 'percent')} to {fmt_value(r.cur_value, 'percent')} "
                    f"({fmt_delta(r.delta, 'percent')}) in {plan.time_range.label} versus {plan.comparison.label}.")
        else:
            head = (f"{met.label}{scope} {verb} {abs(pc or 0):.1f}% ({fmt_delta(r.delta, met.format)}) to "
                    f"{fmt_value(r.cur_value, met.format)} in {plan.time_range.label} versus {plan.comparison.label}.")
        lines = []
        wants_pvm = bool(re.search(r"price|volume|mix", plan.question.lower()))
        # cost story first when it dominates
        if r.cost is not None and len(r.cost):
            by_sup = r.cost.groupby("supplier", as_index=False)[["cost_rate_effect", "cogs_current"]].sum() \
                          .sort_values("cost_rate_effect", ascending=False)
            top = by_sup.iloc[0]
            eff = float(top.cost_rate_effect)
            base = float(top.cogs_current - eff)
            cpct = eff / base * 100 if base else 0.0
            ref = abs(r.delta) if met.format != "percent" else None
            share = None
            if met.format == "percent" and r.delta < 0:
                rev = self.rca.value("net_revenue", plan.time_range, plan.filters, r.sql)
                share = (eff / rev) / abs(r.delta) if rev else 0.0      # cost effect in margin points vs the change
                facts["cost_pp"] = eff / rev * 100 if rev else 0.0
            elif ref:
                share = eff / ref
            if eff > 0 and (m == "cogs" or (share is not None and share >= 0.25)):
                facts.update(cost_effect=eff, cost_pct=cpct)
                lead = "Supplier cost increases are the main factor" if (m == "cogs" or share >= 0.5) and not r.mix_driven \
                    else "Supplier costs also contributed"
                txt = (f"{lead}: {top.supplier} unit costs are about {cpct:.1f}% higher, "
                       f"adding {fmt_money(eff)} to cost of goods sold at current volumes")
                if share is not None and m != "cogs":
                    facts["cost_share"] = share * 100
                    if met.format == "percent":
                        facts["cost_pp_abs"] = facts.get("cost_pp", 0.0)
                        txt += f", worth about {facts['cost_pp']:.1f} pp of margin"
                    if share > 1.05:
                        txt += " — more than the whole change, so other factors (such as price) partly offset it"
                    else:
                        txt += f" (about {share * 100:.0f}% of the change)"
                txt += "."
                if m == "cogs" and len(by_sup) > 1:
                    others = by_sup.iloc[1:4]
                    txt += " Next largest: " + "; ".join(f"{s.supplier} {fmt_delta(float(s.cost_rate_effect), 'currency')}"
                                                         for s in others.itertuples()) + "."
                    for i, s in enumerate(others.itertuples()):
                        facts[f"cost_other_{i}"] = float(s.cost_rate_effect)
                lines.append(txt)
                ans.drivers = [Driver(dimension="supplier", member=str(s.supplier), delta=float(s.cost_rate_effect),
                                      share_of_change=float(s.cost_rate_effect / ref) if ref else 0.0)
                               for s in by_sup.head(5).itertuples()]
                if m == "cogs":
                    ans.chart = viz.bar(by_sup.head(10), "supplier", "cost_rate_effect", "COGS impact of unit-cost changes",
                                        "currency", title=f"COGS impact of supplier cost changes, {plan.time_range.label} vs {plan.comparison.label}")
                    ans.chart_type = "bar"
                    ans.table = self._records(by_sup.head(15))
        # drill path
        if r.path:
            steps = []
            for i, s in enumerate(r.path):
                dl = cat_label(self.cat, s.dimension)
                steps.append(f"{s.member} ({dl}, {fmt_delta(s.delta, met.format)})")
                facts[f"path_{i}"] = s.delta
            lines.append("The change is concentrated in " + " → ".join(steps) + ".")
            if r.additive:
                facts["path_0_share"] = r.path[0].share_of_total * 100
        primary = r.dims[0] if r.dims else None
        if primary is not None and "mix_effect" in primary.table and r.delta:
            tbl = primary.table
            mix, rate = float(tbl.mix_effect.sum()), float(tbl.rate_effect.sum())
            if abs(mix) >= 0.4 * abs(r.delta) and np.sign(mix) == np.sign(r.delta):
                mv = tbl.loc[(tbl.weight - tbl.weight_prev).abs().idxmax()]
                facts.update(mix_total=mix, rate_total=rate, mix_w1=float(mv.weight) * 100, mix_w0=float(mv.weight_prev) * 100,
                             mix_r=float(mv.previous))
                lines.insert(0, f"Mostly a mix shift ({fmt_delta(mix, met.format)} of the {fmt_delta(r.delta, met.format)} change): "
                                f"{mv.member}'s share of the base moved from {mv.weight_prev * 100:.1f}% to {mv.weight * 100:.1f}%, "
                                f"and it runs at {fmt_value(float(mv.previous), met.format)} versus {fmt_value(r.prev_value, met.format)} overall. "
                                f"Rates within each {cat_label(self.cat, primary.dimension)} moved {fmt_delta(rate, met.format)}.")
        if primary is not None and not ans.drivers:
            tbl = primary.table
            ans.drivers = [Driver(dimension=primary.dimension, member=str(x.member), delta=float(x.delta),
                                  share_of_change=float(x.share) if not pd.isna(x.share) else 0.0,
                                  current=float(x.current), previous=float(x.previous)) for x in tbl.head(6).itertuples()]
            offs = tbl[np.sign(tbl.delta) != np.sign(r.delta)]
            if len(offs) and r.delta:
                o = offs.sort_values("delta", key=lambda s: -s.abs()).iloc[0]
                if abs(o.delta) > 0.1 * abs(r.delta):
                    lines.append(f"Partly offset by {o.member} ({fmt_delta(float(o.delta), met.format)}).")
                    facts["offset"] = float(o.delta)
        if r.lost_customers is not None and len(r.lost_customers):
            lc = r.lost_customers
            key = lc[lc.key_account]
            show = key if len(key) else lc.head(3)
            if len(show) and float(show.previous.sum()) > 0.05 * abs(r.delta):
                tot = float(show.previous.sum())
                facts["lost_prev"] = tot
                who = "key accounts" if len(key) else "customers"
                lines.append(f"{len(show)} {who} in this slice placed no orders in the last 90 days of the period: "
                             f"{', '.join(show.customer.head(5))}. Together they generated {fmt_money(tot)} in {plan.comparison.label}.")
        if r.supply and r.supply["stockout_rate_current"] >= 1.5 * r.supply["stockout_rate_previous"] + 0.005:
            s = r.supply
            facts.update(so_cur=s["stockout_rate_current"], so_prev=s["stockout_rate_previous"], so_rev=s["stocked_out_revenue_delta"])
            prods = s["stocked_out_products"]
            lines.append(f"Supply was a factor: the stockout rate in {s['region']} rose to {fmt_value(s['stockout_rate_current'], 'percent')} "
                         f"from {fmt_value(s['stockout_rate_previous'], 'percent')}, and products that ran out of stock account for "
                         f"{fmt_delta(s['stocked_out_revenue_delta'], 'currency')} of revenue change"
                         + (f" (largest: {prods.iloc[0]['product']})." if len(prods) else "."))
        if r.pvm and (wants_pvm or not lines or len(lines) < 3):
            p = r.pvm
            facts.update({k: v for k, v in p.items() if k.endswith("_effect")})
            parts = [f"price {fmt_delta(p['price_effect'], 'currency')}", f"volume {fmt_delta(p['volume_effect'], 'currency')}",
                     f"mix {fmt_delta(p['mix_effect'], 'currency')}"]
            if "refund_effect" in p:
                parts.append(f"refunds {fmt_delta(p['refund_effect'], 'currency')}")
            txt = "Price-volume-mix: " + ", ".join(parts) + "."
            if p["units_previous"]:
                upct = (p["units_current"] / p["units_previous"] - 1) * 100
                facts["units_pct"] = upct
                txt += f" Units sold changed {upct:+.1f}%."
            lines.insert(0 if wants_pvm else len(lines), txt)
            if wants_pvm:
                steps = [("Price", p["price_effect"]), ("Volume", p["volume_effect"]), ("Mix", p["mix_effect"])]
                if "refund_effect" in p:
                    steps.append(("Refunds", p["refund_effect"]))
                ans.chart = viz.waterfall(plan.comparison.label, r.prev_value, steps, plan.time_range.label, r.cur_value,
                                          "currency", f"{met.label}{scope}: price, volume and mix")
                ans.chart_type = "waterfall"
        if ans.chart is None and primary is not None and r.additive:
            tbl = primary.table
            top = tbl.head(5)
            other = float(tbl.delta.sum() - top.delta.sum())
            steps = [(str(x.member), float(x.delta)) for x in top.itertuples()]
            if abs(other) > 1e-6:
                steps.append(("Other", other))
            ans.chart = viz.waterfall(plan.comparison.label, r.prev_value, steps, plan.time_range.label, r.cur_value,
                                      met.format, f"What changed {met.label.lower()}{scope}, by {cat_label(self.cat, primary.dimension)}")
            ans.chart_type = "waterfall"
        elif ans.chart is None and primary is not None:
            tbl = primary.table.head(8)
            ans.chart = viz.bar(tbl.assign(contribution=tbl.delta), "member", "contribution", "Contribution", met.format,
                                title=f"Contribution to the change in {met.label.lower()} by {cat_label(self.cat, primary.dimension)}")
            ans.chart_type = "bar"
        if ans.table is None and primary is not None:
            ans.table = self._records(primary.table.rename(columns={"member": primary.dimension}).head(20))
        ans.headline, ans.narrative, ans.facts = head, "\n\n".join(lines), facts
        big = r.path and abs(r.path[0].share_of_total) >= 0.5
        ans.confidence = "high" if big or "cost_share" in facts else ("medium" if r.path else "low")
        if abs(pc or 0) < 1 and met.format != "percent":
            ans.confidence = "low"
            ans.evidence.limitations.append("The change is small, so the drivers may be noise.")
        ans.followups = [f"Investigate {met.label.lower()}{scope} in {plan.time_range.label}",
                         f"Show {met.label.lower()} by month for the last 12 months",
                         f"Forecast {met.label.lower()} for the next quarter"]
        return ans

    # ------------------------------------------------------------------ forecast
    def _forecast(self, plan: QueryPlan) -> Answer:
        m = plan.metrics[0]
        met = self.cat.metrics[m]
        if met.type == "custom":
            raise CompileError(f"Forecasting isn't available for {met.label}; it's computed per period rather than as a time series.")
        hist = TimeRange(start=DATA_START, end=DATA_END, label="history")
        fr = self.forecaster.forecast(m, hist, plan.horizon or 3, plan.filters)
        ans = Answer(question=plan.question, evidence=self._evidence(plan, fr.sql))
        scope = self._scope(plan)
        fc = fr.forecast
        start, end = fc.period.iloc[0], fc.period.iloc[-1]
        span = start.strftime("%b %Y") if len(fc) == 1 else f"{start:%b %Y} – {end:%b %Y}"
        facts = {"mape": fr.backtest_mape.get(fr.model)}
        if met.additive and met.format in ("currency", "number"):
            tot, lo, hi = float(fc.yhat.sum()), float(fc.lo.sum()), float(fc.hi.sum())
            ly = fr.history[fr.history.period.isin(fc.period - pd.DateOffset(years=1))]["value"].sum()
            facts.update(total=tot, lo=lo, hi=hi, last_year=float(ly))
            head = f"{met.label}{scope} for {span} is forecast at {fmt_value(tot, met.format)} (80% range {fmt_value(lo, met.format)} – {fmt_value(hi, met.format)})."
            if ly:
                facts["yoy"] = (tot / ly - 1) * 100
                head += f" That is {facts['yoy']:+.1f}% versus the same months a year earlier ({fmt_value(float(ly), met.format)})."
        else:
            avg = float(fc.yhat.mean())
            facts.update(avg=avg, lo=float(fc.lo.mean()), hi=float(fc.hi.mean()))
            head = f"{met.label}{scope} is forecast to average {fmt_value(avg, met.format)} over {span} (80% range {fmt_value(facts['lo'], met.format)} – {fmt_value(facts['hi'], met.format)})."
        monthly = "; ".join(f"{r.period:%b %Y} {fmt_value(float(r.yhat), met.format)}" for r in fc.itertuples())
        for i, r in enumerate(fc.itertuples()):
            facts[f"m{i}"] = float(r.yhat)
        mape_txt = f"{facts['mape']:.1f}%" if facts["mape"] is not None and not np.isnan(facts["mape"]) else "n/a"
        ans.narrative = (f"By month: {monthly}. Model: {fr.model}, chosen because it had the lowest error when tested on the "
                         f"last 6 months of actuals (mean absolute error {mape_txt}).")
        ans.headline, ans.facts = head, facts
        ans.chart = viz.forecast_chart(fr.history.tail(36), fc, met.label + scope, met.format)
        ans.chart_type = "forecast"
        ans.table = self._records(fc.rename(columns={"yhat": "forecast", "lo": "low_80", "hi": "high_80"}))
        ans.evidence.limitations += fr.notes + [
            "The forecast extends past patterns; it does not know about planned price changes, promotions or supply problems.",
            "It is a statistical estimate, not a target or budget."]
        ans.confidence = "medium" if (facts["mape"] or 99) < 8 else "low"
        ans.followups = [f"Why did {met.label.lower()} change last quarter?", f"Show monthly {met.label.lower()} for the last 24 months"]
        return ans

    # ------------------------------------------------------------------ anomaly
    def _anomaly(self, plan: QueryPlan) -> Answer:
        res = self.anomaly.scan(plan.time_range, plan.metrics or None)
        ans = Answer(question=plan.question, evidence=self._evidence(plan, res.sql, plan.metrics))
        facts = {"scanned": float(res.scanned)}
        items = res.anomalies[:6]
        biz = [a for a in items if a.kind == "business"]
        dq = [a for a in items if a.kind == "data_quality"]
        if not items:
            ans.headline = (f"Nothing unusual in {plan.time_range.label}: all {res.scanned} KPI series were within their normal "
                            f"range once seasonality and recent trend are accounted for.")
            ans.narrative = "The scan compares each week with the previous 8 weeks and with the same week last year."
            ans.confidence = "high"
            ans.followups = ["Give me the weekly business brief", "Show weekly net revenue for the last 12 weeks"]
            return ans
        n = len(biz) + len(dq)
        ans.headline = f"I found {n} unusual movement{'s' if n > 1 else ''} in {plan.time_range.label}."
        lines = []
        for i, a in enumerate(items):
            facts[f"a{i}_value"], facts[f"a{i}_expected"] = a.value, a.expected
            if a.kind == "data_quality":
                days = ", ".join(d.strftime("%d %b %Y") for d in a.gap_days) if a.gap_days else f"week of {a.week:%d %b}"
                lines.append(f"**Data issue — {a.label}:** recorded zero on {days} although it normally runs around "
                             f"{fmt_value(a.expected, a.fmt)} a day. This looks like a tracking or loading problem, not a real drop.")
                continue
            where = f" in {a.member}" if a.member else ""
            dirn = "higher" if a.value > a.expected else "lower"
            good = "favourable" if a.good else "adverse"
            if a.weeks > 1 and a.run_start is not None:
                when = f"{a.weeks} weeks from {pd.Timestamp(a.run_start):%d %b %Y}; peak week of {a.week:%d %b}"
            else:
                when = f"week of {a.week:%d %b %Y}"
            txt = (f"**{a.label}{where}** ({when}): "
                   f"{fmt_value(a.value, a.fmt)} versus about {fmt_value(a.expected, a.fmt)} expected, {dirn} than normal ({good}).")
            # explain the top anomalies one level down
            if i < 3:
                why = self._explain_anomaly(a, res.sql, facts, i)
                if why:
                    txt += " " + why
            lines.append(txt)
        ans.narrative = "\n\n".join(lines)
        ans.table = [dict(kpi=a.label, slice=a.member or "overall", week=a.week.strftime("%Y-%m-%d"), value=a.value,
                          expected=a.expected, severity=None if a.kind == "data_quality" else round(a.severity, 1),
                          type=a.kind) for a in items]
        top = biz[0] if biz else None
        if top is not None:
            try:
                filt = {top.dimension: [top.member]} if top.dimension else {}
                s_tr = TimeRange(start=plan.time_range.start - timedelta(weeks=26), end=plan.time_range.end, label="series")
                sdf, srec = self._run(MetricRequest([top.metric], s_tr, [], filt, grain="week"), f"weekly {top.metric} series")
                ans.evidence.sql.append(srec)
                sdf = sdf.rename(columns={top.metric: "value"})
                sdf["period"] = pd.to_datetime(sdf["period"])
                flagged = []
                for a in biz:
                    if (a.metric, a.member) == (top.metric, top.member):
                        st = pd.Timestamp(a.run_start if a.run_start is not None else a.week)
                        run = sdf[(sdf.period >= st) & (sdf.period < st + pd.Timedelta(weeks=a.weeks))]
                        flagged += list(zip(run.period, run.value))
                ans.chart = viz.anomaly_chart(sdf, flagged, top.label, top.fmt,
                                              f"{top.label}{' — ' + top.member if top.member else ''}, weekly")
                ans.chart_type = "anomaly"
            except Exception:  # noqa: BLE001
                pass
        ans.facts = facts
        ans.confidence = "medium"
        ans.evidence.limitations.append("A week is flagged when it is more than 3.5 robust standard deviations from both its recent "
                                        "level and its seasonal expectation. Small slices can still produce false alarms.")
        ans.followups = [f"Why did {items[0].label.lower()} change in {plan.time_range.label}?", "Give me the weekly business brief"]
        return ans

    def _explain_anomaly(self, a, rec, facts, i) -> str:
        child = {"support": "product", "sales": "product", "inventory": "product", "web": "marketing_channel"}.get(self.cat.metrics[a.metric].model)
        met = self.cat.metrics[a.metric]
        if child is None or child not in self.cat.allowed_dimensions(a.metric) or child == a.dimension:
            return ""
        wk_end = (a.week + pd.Timedelta(days=7 * a.weeks - 1)).date()
        cur = TimeRange(start=a.week.date(), end=wk_end, label="anomaly weeks")
        prev = TimeRange(start=(a.week - pd.Timedelta(weeks=a.weeks)).date(), end=(a.week - pd.Timedelta(days=1)).date(), label="prior weeks")
        filt = {a.dimension: [a.member]} if a.dimension else {}
        try:
            t = self.rca.by_dim(a.metric, cur, prev, child, filt, rec)
        except Exception:  # noqa: BLE001
            return ""
        t = t[t.member != "(none)"]
        if not len(t):
            return ""
        top = t.iloc[0]
        tot = t.delta.sum()
        if not tot or top.delta / tot < 0.3:
            return ""
        facts[f"a{i}_driver"] = float(top.delta)
        facts[f"a{i}_driver_share"] = float(top.delta / tot * 100)
        return f"Most of the change comes from {top.member} ({fmt_delta(float(top.delta), met.format)}, {top.delta / tot * 100:.0f}% of the movement)."

    # ------------------------------------------------------------------ brief
    def _brief(self, plan: QueryPlan) -> Answer:
        b = BriefBuilder(self).build(plan.time_range)
        ans = Answer(question=plan.question, evidence=self._evidence(plan, b.sql, [r for r in b.table.key]))
        ans.headline, ans.narrative, ans.kpis, ans.facts = b.headline, b.narrative, b.kpis, b.facts
        ans.table = self._records(b.table.drop(columns=["key", "adverse", "fmt", "direction"], errors="ignore"))
        if "net_revenue" in set(b.table.key):
            tr = TimeRange(start=plan.time_range.start - timedelta(weeks=12) if plan.time_range.days() <= 14 else
                           shift_years(plan.time_range).start, end=plan.time_range.end, label="trend")
            grain = "week" if plan.time_range.days() <= 31 else "month"
            df, rec = self._run(MetricRequest(["net_revenue"], tr, grain=grain), "net revenue trend for the brief")
            ans.evidence.sql.append(rec)
            ans.chart = viz.line(df, "net_revenue", "Net Revenue", "currency", title=f"Net revenue by {grain}")
            ans.chart_type = "line"
        ans.confidence = "high"
        ans.followups = ["Anything unusual this week?"] + ([f"Why did {b.concern['metric'].lower()} change in {plan.time_range.label}?"]
                                                            if b.concern else [])
        return ans

    # ------------------------------------------------------------------ definition (RAG)
    def _definition(self, plan: QueryPlan) -> Answer:
        hits = self.retriever.search(plan.question, k=6)
        asks_unsupported = any(k in plan.question.lower() for k in self.cat.unsupported)
        hits = [h for h in hits if asks_unsupported or not h.chunk.id.startswith("unsupported:")][:4]
        ans = Answer(question=plan.question, evidence=self._evidence(plan, []))
        ans.evidence.documents = [h.chunk.cite() for h in hits]
        facts = {}
        metric_hits = [m for m in plan.metrics if m in self.cat.metrics]
        doc_hits = [h for h in hits if h.chunk.source != "metric_catalog"]
        if not hits and not metric_hits:
            ans.status = "clarify"
            ans.headline = "I couldn't find that in the metric catalog or the business documentation."
            ans.narrative = "Try naming the metric or rule, for example 'How is churn rate calculated?'"
            ans.confidence = "low"
            return ans
        parts = []
        if metric_hits:
            met = self.cat.metrics[metric_hits[0]]
            ans.headline = f"{met.label}: {met.definition}"
            if met.owner:
                parts.append(f"Owner: {met.owner}.")
        elif doc_hits:
            ans.headline = _first_sentences(doc_hits[0].chunk.text, 2, skip_title=True)
        wants_diff = re.search(r"\b(differ|different|difference)\b", plan.question.lower())
        for h in doc_hits[:2]:
            s = _first_sentences(h.chunk.text, 4, skip_title=True)
            if s and s not in ans.headline:
                parts.append(f"{s} *(source: {h.chunk.cite()})*")
        if wants_diff and {"net_revenue", "booked_revenue"} <= set(metric_hits + ["net_revenue", "booked_revenue"]) \
                and re.search(r"revenue|sales|dashboard|finance", plan.question.lower()):
            tr = plan.time_range or TimeRange(start=pd.Timestamp("2025-01-01").date(), end=DATA_END, label="2025")
            df, rec = self._run(MetricRequest(["net_revenue", "booked_revenue"], tr), f"net vs booked revenue, {tr.label}")
            ans.evidence.sql.append(rec)
            nr, br = float(df.iloc[0]["net_revenue"]), float(df.iloc[0]["booked_revenue"])
            facts.update(net_revenue=nr, booked_revenue=br, gap=br - nr)
            ans.kpis = [KPI(label="Net revenue (Finance)", value=nr, fmt="currency"),
                        KPI(label="Booked revenue (Sales)", value=br, fmt="currency")]
            parts.insert(0, f"For {tr.label}, booked revenue was {fmt_money(br)} and net revenue {fmt_money(nr)}, a gap of "
                            f"{fmt_money(br - nr)}. The gap comes from refunds and from orders placed but not yet shipped at period end.")
        ans.narrative = "\n\n".join(parts)
        # numbers quoted from sources are grounded by those sources
        src = " ".join(h.chunk.text for h in hits) + " " + " ".join(self.cat.metrics[m].definition for m in metric_hits)
        facts.update(self._numbers_in(src))
        ans.facts = facts
        ans.confidence = "high" if (metric_hits or (hits and hits[0].score > 0.2)) else "medium"
        if self.llm:
            try:
                ctx = "\n\n".join(f"[{h.chunk.cite()}]\n{h.chunk.text}" for h in hits)
                out = self.llm.complete("Answer the question using ONLY the sources. Cite sources in brackets. If the sources "
                                        "don't contain the answer, say so. Be concise (max 120 words).",
                                        f"SOURCES\n{ctx}\n\nQUESTION\n{plan.question}", max_tokens=400)
                if not check_grounding(out, {**facts, **self._numbers_in(ctx)}):
                    ans.narrative = out.strip() + ("\n\n" + parts[0] if facts else "")
            except Exception:  # noqa: BLE001
                pass
        ans.followups = [f"Show {self.cat.metrics[metric_hits[0]].label.lower()} by month for 2025"] if metric_hits else []
        return ans

    # ------------------------------------------------------------------ investigate
    def _investigate(self, plan: QueryPlan) -> Answer:
        agent = InvestigationAgent(self, llm=self.llm)
        if plan.comparison is None:
            plan.comparison = shift_years(plan.time_range)
        inv = agent.investigate(plan.metrics[0], plan.time_range, plan.comparison, plan.filters)
        ans = Answer(question=plan.question, evidence=self._evidence(plan, inv.sql))
        ans.headline, ans.narrative = inv.headline, inv.conclusion
        ans.facts = {k: v for k, v in inv.facts.items() if not k.startswith("_")}
        ans.evidence.agent_trace = agent.trace(inv)
        ans.evidence.assumptions.append(f"Investigation policy: {inv.policy}; {len(inv.steps)} of max {agent.max_steps} steps used.")
        for dim, top in inv.drivers[:1]:
            ans.drivers = [Driver(dimension=dim, member=str(r.member), delta=float(r.delta),
                                  share_of_change=float(r.share) if not pd.isna(r.share) else 0.0) for r in top.itertuples()]
        if "cost_effect_top" in inv.facts and "gross_profit_delta" in inv.facts:
            steps = [("Revenue", inv.facts.get("net_revenue_delta", 0.0)),
                     ("Supplier cost increases", -inv.facts["cost_effect_top"])]
            other = inv.facts["gross_profit_delta"] - sum(v for _, v in steps)
            steps.append(("Volume, mix & other costs", other))
            ans.chart = viz.waterfall(plan.comparison.label, inv.facts["gross_profit_previous"], steps, plan.time_range.label,
                                      inv.facts["gross_profit_current"], "currency", "Gross profit bridge")
            ans.chart_type = "waterfall"
            ans.facts["bridge_other"] = other
        ans.confidence = "high" if "cost_share_of_decline" in inv.facts or inv.facts.get("lost_key_revenue") else "medium"
        ans.followups = [f"Which suppliers raised costs in {plan.time_range.label}?", "Show gross margin by category by month"]
        return ans

    # ------------------------------------------------------------------ unsupported / clarify
    def _unsupported(self, plan: QueryPlan) -> Answer:
        ans = Answer(question=plan.question, status="unsupported", evidence=self._evidence(plan, []), confidence="high")
        concept = next((k for k in self.cat.unsupported if re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", plan.question.lower())), None)
        ans.headline = (f"I can't calculate {concept.upper() if concept and len(concept) <= 6 else (concept or 'that')} from this data."
                        if concept else "That can't be answered from this data.")
        ans.narrative = plan.reason or ""
        alt = ALTERNATIVES.get(concept or "")
        if alt and plan.time_range and plan.time_range.end >= DATA_START:
            tr = plan.time_range
            df, rec = self._run(MetricRequest([alt], tr), f"{alt} as the closest available measure")
            v = float(df.iloc[0][alt])
            met = self.cat.metrics[alt]
            ans.evidence.sql.append(rec)
            ans.narrative += f"\n\nThe closest available measure: {met.label.lower()} for {tr.label} was {fmt_value(v, met.format)}. {met.definition}"
            ans.facts = {alt: v}
            ans.kpis = [KPI(label=met.label, value=v, fmt=met.format)]
        ans.followups = ["What was operating profit last year?", "What metrics are available?"] if concept in ("ebitda", "net income") else []
        return ans

    def _clarify(self, plan: QueryPlan) -> Answer:
        return Answer(question=plan.question, status="clarify", headline="I need a little more detail to answer that.",
                      narrative=plan.reason or "", evidence=self._evidence(plan, []), confidence="low",
                      followups=["What was net revenue last quarter?", "Why did revenue decline in Q2 2025?",
                                 "Show CAC by marketing channel for 2025"])

    # ------------------------------------------------------------------ raw SQL baseline (evaluation)
    def ask_raw_sql(self, question: str, schema_scope: str = "raw") -> tuple[pd.DataFrame | None, str, str | None]:
        """LLM writes SQL directly (no semantic layer). Used only as an evaluation baseline."""
        if not self.llm:
            raise RuntimeError("Raw SQL mode needs an LLM provider.")
        tables = None if schema_scope == "raw" else [t for t in self.db.schema if t.startswith("v_")]
        sys = ("Write one DuckDB SELECT query that answers the question. Data runs 2022-01-01 to 2025-12-31. "
               "Return only SQL, no explanation.")
        sql = self.llm.complete(sys, f"SCHEMA\n{self.db.schema_text(tables)}\n\nQUESTION\n{question}", max_tokens=700)
        sql = re.sub(r"```(?:sql)?", "", sql).strip()
        try:
            df, _ = self.db.run(sql, purpose="raw LLM SQL", guarded=True)
            return df, sql, None
        except Exception as e:  # noqa: BLE001
            return None, sql, str(e)


def _first_sentences(text: str, n: int, skip_title: bool = False) -> str:
    if skip_title:
        # chunk text starts with "Doc title. Section title. body"
        parts = text.split(". ", 2)
        text = parts[2] if len(parts) == 3 else text
    sents = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sents[:n]).strip()


_COPILOT = None


def get_copilot() -> Copilot:
    global _COPILOT
    if _COPILOT is None:
        _COPILOT = Copilot()
    return _COPILOT

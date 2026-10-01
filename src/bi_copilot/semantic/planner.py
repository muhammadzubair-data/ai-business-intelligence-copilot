"""Turn a natural-language question into a validated QueryPlan.

Two planners share one validator:
  * OfflinePlanner — deterministic, catalog-driven matching. No API key needed,
    fully reproducible, and the baseline in the evaluation.
  * LLMPlanner — asks an LLM for a JSON plan given the metric catalog and the
    retrieved knowledge. Its output is validated against the catalog; if it is
    invalid, the offline plan is used instead.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Optional

from ..config import AS_OF_DATE, DATA_END, DATA_START
from ..db import Database
from ..plan import QueryPlan, TimeRange
from .catalog import Catalog, get_catalog
from .timeparse import default_comparison, last_complete_week, parse_time, quarter_range, year_range

# values people use that differ from warehouse values
VALUE_ALIASES = {
    "region": {"Europe": ["europe", "european", "eu", "emea"], "North America": ["north america", "north american", "na", "americas"],
               "APAC": ["apac", "asia pacific", "asia-pacific", "asia"], "LATAM": ["latam", "latin america", "south america", "latin american"]},
    "segment": {"Enterprise": ["enterprise", "large accounts", "large customers"], "Mid-Market": ["mid-market", "mid market", "midmarket"],
                "SMB": ["smb", "smbs", "small business", "small businesses", "small and medium"], "Consumer": ["consumer", "consumers", "b2c", "individual shoppers"]},
    "channel": {"Direct Sales": ["direct sales", "direct channel", "account teams"], "Web Store": ["web store", "webstore", "online store", "website sales", "e-commerce", "ecommerce"],
                "Marketplace": ["marketplace", "marketplaces"], "Partner": ["partner", "partners", "reseller", "resellers", "partner channel"]},
    "marketing_channel": {"Paid Social": ["paid social", "social ads", "social media ads", "facebook ads", "instagram ads"],
                          "Paid Search": ["paid search", "search ads", "google ads", "sem", "ppc"], "Email": ["email", "email marketing"],
                          "Events": ["events", "trade shows"], "Affiliate": ["affiliate", "affiliates"], "Organic": ["organic", "seo"],
                          "Referral": ["referral", "referrals"]},
    "key_account": {"true": ["key accounts", "key account", "strategic accounts"]},
    "ticket_category": {"Product Defect": ["product defect", "defect", "defects", "defective"]},
}
# dimensions whose warehouse values are matched verbatim in questions
VALUE_DIMS = ["region", "country", "segment", "channel", "sales_team", "category", "business_unit", "supplier",
              "warehouse", "marketing_channel", "ticket_category", "department", "expense_type", "industry", "product"]

INTENT_PATTERNS = [
    ("investigate", r"\b(investigate|dig into|deep dive|deep-dive|figure out why|get to the bottom|analy[sz]e why)\b"),
    ("brief", r"\b(brief|briefing|executive summary|exec summary|weekly summary|monthly summary|business review|how did we do|how are we doing|state of the business|kpi summary|summari[sz]e (the|this|last) (week|month|quarter))\b"),
    ("definition", r"\b(mean|meaning|definition|define|defined|calculated|calculate|formula|differ|difference between|different from|policy|rule|rules|incident|outage|how do (we|you) measure|what counts as|explain the metric|when (does|did|was|is) .*(start|launch|begin)|launched|fiscal (year|calendar) (start|begin)|why can'?t we)\b"),
    ("forecast", r"\b(forecast|predict|prediction|projection|project(ed)?|outlook|expect(ed)? .* next|next (month|quarter|year|\d+|one|two|three|six|twelve)|will .* (be|look))\b"),
    ("root_cause", r"\b(why|what caused|caused|causing|contributed|contributing|responsible for|drove|driving|what drove|what's driving|what is driving|drivers?|reason for|root cause|what's behind|behind the|explain the (drop|decline|increase|change|fall|rise)|break ?down the change|decompos|price and volume|price vs volume|price, volume|stopped ordering|stopped buying)\b"),
    ("anomaly", r"\b(unusual|anomal\w*|odd|strange|outlier|unexpected|anything wrong|red flags?|anything (off|weird)|out of the ordinary|spikes?|surprising)\b"),
]
# requests to change data or reach outside the warehouse are refused before planning
UNSAFE = re.compile(
    r"\b(drop\s+(the\s+)?(\w+\s+)?(table|database|schema|view|column)|drop\s+table|delete\s+(all|every|the|from|\w+\s+from)|truncate|insert\s+into|alter\s+table|update\s+(every|all|the|\w+\s+set)|create\s+(a\s+)?(new\s+)?table|attach|detach|"
    r"export\b.*\b(file|csv|server)|copy\b.*\b(to|there)|read_csv|read_parquet|\.env|api[\s_-]?key|password|secret|"
    r"ignore (your|all|previous|the) (instructions|rules))\b|;\s*--|\bset\s+\w+\s*=", re.I)
READ_ONLY_REASON = ("I only have read access to the warehouse, so I can't change, delete, export or copy data, and I can't "
                    "reveal credentials or system files. I can answer questions about the data instead.")

RANK_DESC = r"\b(top|highest|largest|biggest|most|best|leading|greatest|deepest|strongest)\b"
RANK_ASC = r"\b(bottom|lowest|smallest|least|worst|weakest|fewest)\b"


@dataclass
class Match:
    kind: str       # metric | dimension | value | unsupported
    key: str        # metric name / dimension name / value
    dim: Optional[str]
    start: int
    end: int
    text: str


class Vocabulary:
    """Everything the offline planner can recognise, built from the catalog and warehouse values."""

    def __init__(self, catalog: Catalog, db: Optional[Database]):
        self.cat = catalog
        self.metric_terms: list[tuple[str, str]] = []
        for m in catalog.visible_metrics():
            terms = {m.name.replace("_", " "), m.label.lower()} | {s.lower() for s in m.synonyms}
            for t in terms:
                self.metric_terms.append((t, m.name))
        self.dim_terms = [(t.lower(), d.name) for d in catalog.dimensions.values() for t in [d.name.replace("_", " "), d.label] + d.synonyms]
        self.value_terms: list[tuple[str, str, str]] = []  # (term, dim, value)
        for dim, vals in VALUE_ALIASES.items():
            for v, aliases in vals.items():
                for a in aliases:
                    self.value_terms.append((a, dim, v))
        if db is not None:
            views = {"region": ("v_sales_lines", "region"), "country": ("v_sales_lines", "country"),
                     "segment": ("v_sales_lines", "segment"), "channel": ("v_sales_lines", "channel"),
                     "sales_team": ("v_sales_lines", "sales_team"), "category": ("v_sales_lines", "category"),
                     "business_unit": ("v_sales_lines", "business_unit"), "supplier": ("v_sales_lines", "supplier"),
                     "warehouse": ("v_sales_lines", "warehouse"), "marketing_channel": ("v_acquisition", "marketing_channel"),
                     "ticket_category": ("v_tickets", "ticket_category"), "department": ("v_opex", "department"),
                     "expense_type": ("v_opex", "expense_type"), "industry": ("v_sales_lines", "industry"),
                     "product": ("v_sales_lines", "product")}
            for dim, (view, col) in views.items():
                for v in db.distinct_values(view, col):
                    if dim == "department":   # "sales", "marketing", "product" are too ambiguous on their own
                        for suffix in (" department", " dept", " team costs", " expenses", " opex"):
                            self.value_terms.append((v.lower() + suffix, dim, v))
                        continue
                    self.value_terms.append((v.lower(), dim, v))
                    if dim == "product":
                        for w in re.findall(r"\b([A-Z][a-z]+[A-Z][A-Za-z]+)\b", v):   # AeroDesk style names
                            self.value_terms.append((w.lower(), dim, v))
                            nxt = re.search(rf"{w}\s+(\w+)", v)
                            if nxt:
                                self.value_terms.append((f"{w} {nxt.group(1)}".lower(), dim, v))
                    if dim == "supplier":
                        self.value_terms.append((v.split()[0].lower(), dim, v))
        self.unsupported = [(k.lower(), k) for k in catalog.unsupported]

    def scan(self, text: str) -> list[Match]:
        t = text.lower()
        cands: list[Match] = []

        def add(kind, term, key, dim=None):
            for m in re.finditer(rf"(?<![\w-]){re.escape(term)}(?![\w-])", t):
                cands.append(Match(kind, key, dim, m.start(), m.end(), term))

        for term, key in self.unsupported:
            add("unsupported", term, key)
        for term, dim, val in self.value_terms:
            if len(term) >= 2:
                add("value", term, val, dim)
        for term, dim in self.dim_terms:
            add("dimension", term, dim, dim)
        for term, key in self.metric_terms:
            add("metric", term, key)
        # longest match wins; on ties prefer unsupported > value > dimension > metric
        pri = {"unsupported": 0, "value": 1, "dimension": 2, "metric": 3}
        cands.sort(key=lambda m: (-(m.end - m.start), pri[m.kind], m.start))
        taken: list[Match] = []
        for c in cands:
            if all(c.end <= x.start or c.start >= x.end for x in taken):
                taken.append(c)
        return sorted(taken, key=lambda m: m.start)


class OfflinePlanner:
    name = "offline"

    def __init__(self, catalog: Catalog | None = None, db: Database | None = None):
        self.cat = catalog or get_catalog()
        self.db = db
        self.vocab = Vocabulary(self.cat, db)

    def plan(self, question: str) -> QueryPlan:
        q = question.strip()
        t = q.lower()
        if UNSAFE.search(q):
            return QueryPlan(question=q, intent="unsupported", reason=READ_ONLY_REASON, planner=self.name,
                             assumptions=["Request refused by the read-only policy."])
        tp = parse_time(q)
        matches = [m for m in self.vocab.scan(q) if not any(s <= m.start < e for s, e in tp.spans)]
        assumptions = list(tp.assumptions)

        # ---------------- unsupported concepts
        unsup = [m for m in matches if m.kind == "unsupported"]
        asks_why_not = re.search(r"\b(why can'?t|why cannot|why don'?t we|policy|what does .* say)\b", t)
        if unsup and asks_why_not:
            return QueryPlan(question=q, intent="definition", planner=self.name, assumptions=assumptions)
        if unsup:
            key = unsup[0].key
            return QueryPlan(question=q, intent="unsupported", reason=self.cat.unsupported[key],
                             time_range=tp.time_range, assumptions=assumptions, planner=self.name)

        # ---------------- intent
        intent = "metric"
        for name, pat in INTENT_PATTERNS:
            if re.search(pat, t):
                intent = name
                break
        if intent == "definition" and re.search(r"\b(differ|different|difference)\b", t) is None and tp.time_range is not None \
                and not re.search(r"\b(mean|definition|define|calculated|formula|policy|rule|incident|outage|launch\w*)\b", t):
            intent = "metric"
        if intent == "anomaly" and re.search(r"\bwhy\b", t):
            intent = "root_cause"

        # ---------------- metrics, dimensions, filters
        metrics = []
        for m in matches:
            if m.kind == "metric" and m.key not in metrics:
                metrics.append(m.key)
        self._force_intent = None
        if not metrics and re.search(r"\b(distribution|histogram|spread)\b", t) and re.search(r"\border", t):
            metrics = ["aov"]
        metrics = self._metric_rules(t, metrics, assumptions)
        if self._force_intent:
            intent = self._force_intent

        filters: dict[str, list[str]] = {}
        value_matches = [m for m in matches if m.kind == "value"]
        dims = []
        for m in matches:
            if m.kind != "dimension":
                continue
            prev_value = any(v.end <= m.start and m.start - v.end <= 2 for v in value_matches)
            if prev_value and m.key in {v.dim for v in value_matches}:
                continue   # "Enterprise segment", "Tech Accessories category": a filter, not a breakdown
            if m.key == "key_account":
                filters["key_account"] = ["true"]
                if "customer" not in dims:
                    dims.append("customer")
                continue
            if m.key in ("customer", "product") and not re.search(
                    r"\b(by|per|each|which|what|top|bottom|list|largest|biggest|best|worst|individual)\s+(\d+\s+)?(key\s+)?" + re.escape(m.text), t):
                continue   # "orders from Enterprise customers" is not a per-customer breakdown
            if m.key not in dims:
                dims.append(m.key)
        for v in value_matches:
            dim = self._resolve_value_dim(v, metrics)
            if dim == "key_account":
                filters["key_account"] = ["true"]
                if "customer" not in dims and intent != "metric":
                    dims.append("customer")
                continue
            filters.setdefault(dim, [])
            if v.key not in filters[dim]:
                filters[dim].append(v.key)
        # a dimension that is also filtered to one value is not a breakdown
        dims = [d for d in dims if not (d in filters and len(filters[d]) == 1 and intent == "metric")]

        # ---------------- defaults per intent
        plan = QueryPlan(question=q, intent=intent, metrics=metrics, dimensions=dims, filters=filters,
                         time_range=tp.time_range, comparison=tp.comparison, time_grain=tp.grain,
                         horizon=tp.horizon, assumptions=assumptions, planner=self.name)
        if re.search(RANK_ASC, t):
            plan.order = "asc"
        m = re.search(r"\b(?:top|bottom)\s+(\d+)\b", t)
        if m:
            plan.limit = int(m.group(1))
        return self.finalise(plan)

    # ------------------------------------------------------------------ rules
    def _metric_rules(self, t: str, metrics: list[str], assumptions: list[str]) -> list[str]:
        if "discounts" in metrics and re.search(RANK_DESC + r".{0,20}discount|discount.{0,20}\b(rate|%|percent|average)", t):
            metrics = ["discount_rate" if m == "discounts" else m for m in metrics]
        if "net_revenue" in metrics and re.search(r"\b(booked|bookings|sales dashboard)\b", t) and "booked_revenue" not in metrics:
            metrics.append("booked_revenue")
        if "late_po_rate" in metrics or (re.search(r"purchase orders?", t) and re.search(r"\blate\b", t)):
            metrics = ["late_po_rate"] + [m for m in metrics if m not in ("late_po_rate", "orders", "po_value")]
        if metrics and metrics[0] == "gross_profit" and re.search(r"\bprofit\b", t) and not re.search(r"gross profit", t):
            assumptions.append("'Profit' was read as gross profit (net revenue minus cost of goods sold).")
        if metrics and metrics[0] == "net_revenue" and re.search(r"\b(revenue|sales)\b", t) and not re.search(r"net revenue", t):
            assumptions.append("'Revenue' uses the Finance definition: net revenue after discounts and refunds, by ship date.")
        if re.search(r"\braised (their )?(costs|prices)|cost increases?|costs? (went|go) up|more expensive\b", t) and "supplier" in t:
            metrics = ["cogs"] + [m for m in metrics if m != "cogs"]
            self._force_intent = "root_cause"
        return metrics

    def _resolve_value_dim(self, v: Match, metrics: list[str]) -> str:
        candidates = [d for term, d, val in self.vocab.value_terms if val == v.key and term == v.text] or [v.dim]
        if metrics:
            allowed = self.cat.allowed_dimensions(metrics[0])
            for d in candidates:
                if d in allowed:
                    return d
            if v.key == "Direct Sales" and "marketing_channel" in allowed:
                return "marketing_channel"
        return candidates[0]

    # ------------------------------------------------------------------ defaults & validation
    def finalise(self, plan: QueryPlan) -> QueryPlan:
        return finalise_plan(plan, self.cat)


def finalise_plan(plan: QueryPlan, cat: Catalog) -> QueryPlan:
    """Fill defaults and validate a plan from any planner. Invalid plans become clarify/unsupported."""
    t = plan.question.lower()
    notes = plan.assumptions

    if plan.intent in ("unsupported", "definition"):
        return plan
    if plan.intent in ("anomaly", "brief"):
        if plan.time_range is None:
            plan.time_range = last_complete_week()
            notes.append(f"No period given, so the latest complete week ({plan.time_range.label}) was used.")
        return plan

    if not plan.metrics:
        if plan.intent in ("root_cause", "investigate"):
            plan.metrics = ["net_revenue"]
            notes.append("No metric named, so net revenue was analysed.")
        elif plan.intent == "forecast":
            plan.metrics = ["net_revenue"]
            notes.append("No metric named, so net revenue was forecast.")
        else:
            plan.intent = "clarify"
            plan.reason = ("I couldn't match that to a metric in the catalog. Could you name the measure you're interested in, "
                           "for example net revenue, gross margin, orders, CAC or return rate?")
            return plan

    for m in list(plan.metrics):
        if m not in cat.metrics:
            plan.intent, plan.reason = "clarify", f"'{m}' is not a metric in the catalog."
            return plan
    # drop dimensions/filters the primary metric cannot use, and say so
    allowed = cat.allowed_dimensions(plan.metrics[0])
    # "channel" means the marketing channel for marketing, acquisition and web metrics
    plan.dimensions = ["marketing_channel" if d == "channel" and "channel" not in allowed and "marketing_channel" in allowed else d
                       for d in plan.dimensions]
    bad_dims = [d for d in plan.dimensions if d not in allowed]
    bad_filters = [d for d in plan.filters if d not in allowed]
    if bad_filters:
        plan.intent = "clarify"
        plan.reason = (f"{cat.metrics[plan.metrics[0]].label} can't be filtered by "
                       f"{', '.join(cat.dimensions[d].label.lower() for d in bad_filters)}. "
                       f"It can be split by: {', '.join(sorted(cat.dimensions[d].label for d in allowed)) or 'time only'}.")
        return plan
    if bad_dims:
        plan.dimensions = [d for d in plan.dimensions if d in allowed]
        notes.append(f"Ignored breakdown by {', '.join(bad_dims)}: not available for {cat.metrics[plan.metrics[0]].label}.")
    # keep only secondary metrics compatible with the primary's dimensions
    plan.metrics = [plan.metrics[0]] + [m for m in plan.metrics[1:]
                                        if set(plan.dimensions) <= cat.allowed_dimensions(m) and set(plan.filters) <= cat.allowed_dimensions(m)]

    if plan.time_range is None:
        if plan.intent == "forecast":
            plan.time_range = TimeRange(start=DATA_START, end=DATA_END, label="all history")
        elif plan.intent in ("root_cause", "investigate"):
            plan.time_range = quarter_range(AS_OF_DATE.year, (AS_OF_DATE.month - 1) // 3 + 1)
            notes.append(f"No period given, so the latest complete quarter ({plan.time_range.label}) was analysed.")
        elif plan.time_grain:
            plan.time_range = TimeRange(start=date(AS_OF_DATE.year - 1, 1, 1), end=AS_OF_DATE, label="2024–2025")
            notes.append("No period given, so the last two years were used for the trend.")
        else:
            plan.time_range = year_range(AS_OF_DATE.year)
            notes.append(f"No period given, so the latest complete year ({AS_OF_DATE.year}) was used.")
    tr = plan.time_range
    if tr.end < DATA_START or tr.start > DATA_END:
        plan.intent = "unsupported"
        plan.reason = (f"The warehouse covers {DATA_START:%d %b %Y} to {DATA_END:%d %b %Y}; "
                       f"{tr.label} is outside that range.")
        return plan
    if tr.start < DATA_START or tr.end > DATA_END:
        plan.time_range = TimeRange(start=max(tr.start, DATA_START), end=min(tr.end, DATA_END), label=tr.label)
        notes.append(f"{tr.label} was clipped to the available data ({DATA_START:%d %b %Y} – {DATA_END:%d %b %Y}).")
    if plan.intent in ("root_cause", "investigate") and plan.comparison is None:
        plan.comparison, how = default_comparison(plan.time_range)
        notes.append(f"Compared with the {how} ({plan.comparison.label}).")
    if plan.comparison and plan.comparison.start < DATA_START:
        plan.comparison = None
        notes.append("No comparison period is available before January 2022.")
    if plan.intent == "metric" and plan.time_grain is None and plan.comparison is None and \
            re.search(r"\b(change|grow|growth|increase|decrease|decline|drop)\b", t):
        plan.comparison, how = default_comparison(plan.time_range)
        notes.append(f"Change is measured against the {how} ({plan.comparison.label}).")
    if plan.intent == "forecast" and not plan.horizon:
        plan.horizon = 3
        notes.append("No horizon given, so the next 3 months were forecast.")
    return plan


# ======================================================================
# LLM planner
# ======================================================================
LLM_SYSTEM = """You are the query planner for a business-intelligence copilot at Halden Supply Co.
Convert the user's question into a JSON plan. You never write SQL and never invent metrics.

Rules:
- Use ONLY metric and dimension names from the catalog below. If the question needs a concept listed under
  UNSUPPORTED CONCEPTS, or a metric that is not in the catalog, set intent "unsupported" and give the reason.
- intent is one of: metric, root_cause, forecast, anomaly, brief, definition, investigate, unsupported, clarify.
  "why did X change" -> root_cause. Multi-step "investigate" requests -> investigate. Questions about how a metric
  is defined, business rules, data incidents, or why two numbers differ -> definition.
- The data runs from 2022-01-01 to 2025-12-31. Relative dates resolve to the latest COMPLETE period:
  last month = 2025-12, last quarter = Q4 2025, last year = 2025, this week = 2025-12-22..2025-12-28.
- The fiscal year starts 1 Feb and is named after the year it ends in (FY2025 = 2024-02-01..2025-01-31).
- "revenue"/"sales" means net_revenue unless the user says booked/bookings/sales dashboard.
- "profit" without qualifier means gross_profit.
- filters map a dimension name to a list of exact values.

Return JSON with keys: intent, metrics (list), dimensions (list), filters (object), time_range
({start, end, label} or null), comparison ({start, end, label} or null), time_grain (day/week/month/quarter/year or
null), order ("desc"/"asc"), limit (int or null), horizon (months, for forecasts), reason (string or null),
assumptions (list of strings).
"""


class LLMPlanner:
    name = "llm"

    def __init__(self, llm, catalog: Catalog | None = None, fallback: OfflinePlanner | None = None):
        self.llm = llm
        self.cat = catalog or get_catalog()
        self.fallback = fallback
        self.last_error: Optional[str] = None

    def plan(self, question: str, context: str = "", value_hints: str = "") -> QueryPlan:
        user = (f"CATALOG\n{self.cat.describe_for_llm()}\n\nKNOWN DIMENSION VALUES\n{value_hints}\n\n"
                f"RETRIEVED BUSINESS CONTEXT\n{context}\n\nQUESTION\n{question}")
        try:
            raw = self.llm.complete_json(LLM_SYSTEM, user, max_tokens=900)
            raw["question"] = question
            raw["planner"] = f"llm:{self.llm.name}"
            raw.setdefault("assumptions", [])
            for k in ("time_range", "comparison"):
                if raw.get(k) and not raw[k].get("label"):
                    raw[k]["label"] = f"{raw[k]['start']} – {raw[k]['end']}"
            plan = QueryPlan.model_validate({k: v for k, v in raw.items() if k in QueryPlan.model_fields})
            bad = [m for m in plan.metrics if m not in self.cat.metrics]
            if bad and plan.intent not in ("unsupported", "clarify"):
                raise ValueError(f"unknown metrics {bad}")
            plan.filters = {d: v if isinstance(v, list) else [v] for d, v in plan.filters.items()}
            return finalise_plan(plan, self.cat)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            if self.fallback is None:
                raise
            p = self.fallback.plan(question)
            p.assumptions.append("The language model's plan failed validation, so the rule-based planner was used.")
            return p


@lru_cache(maxsize=1)
def value_hints(db_path: str) -> str:
    from ..db import get_db
    db = get_db(db_path)
    parts = []
    for dim, view, col in [("region", "v_sales_lines", "region"), ("segment", "v_sales_lines", "segment"),
                           ("channel", "v_sales_lines", "channel"), ("category", "v_sales_lines", "category"),
                           ("business_unit", "v_sales_lines", "business_unit"), ("sales_team", "v_sales_lines", "sales_team"),
                           ("supplier", "v_sales_lines", "supplier"), ("warehouse", "v_sales_lines", "warehouse"),
                           ("marketing_channel", "v_acquisition", "marketing_channel"), ("ticket_category", "v_tickets", "ticket_category"),
                           ("department", "v_opex", "department")]:
        parts.append(f"{dim}: {json.dumps(list(db.distinct_values(view, col)))}")
    return "\n".join(parts)

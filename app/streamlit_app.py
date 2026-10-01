"""Halden Supply Co. — AI Business Intelligence Copilot (Streamlit UI).

Run:  streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bi_copilot.config import settings  # noqa: E402

st.set_page_config(page_title="BI Copilot · Halden Supply Co.", page_icon="📊", layout="wide",
                   initial_sidebar_state="expanded")

INK, SLATE, LINE, GOOD, BAD, WARN = "#1F2A44", "#5B6478", "#E3E6EC", "#0F766E", "#B42318", "#B7791F"
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Public+Sans:wght@400;500;600&display=swap');
html, body, [class*="css"], .stMarkdown, .stTextInput input {{ font-family: 'Public Sans', system-ui, sans-serif; color: {INK}; }}
.block-container {{ padding-top: 2rem; max-width: 1180px; }}
h1, h2, h3 {{ font-family: 'Public Sans', sans-serif; color: {INK}; letter-spacing: -0.01em; }}
.answer {{ font-family: 'Source Serif 4', Georgia, serif; font-size: 1.75rem; line-height: 1.35; font-weight: 600;
           color: {INK}; max-width: 62ch; margin: 0.4rem 0 0.9rem 0; }}
.answer.unsupported {{ color: {SLATE}; }}
.narr {{ font-family: 'Source Serif 4', Georgia, serif; font-size: 1.08rem; line-height: 1.65; max-width: 72ch; }}
.meta {{ color: {SLATE}; font-size: 0.86rem; margin-bottom: 0.2rem; }}
.pill {{ display: inline-block; padding: 1px 9px; border-radius: 10px; font-size: 0.8rem; margin-right: 6px;
         border: 1px solid {LINE}; color: {SLATE}; }}
.pill.ok {{ border-color: {GOOD}; color: {GOOD}; }} .pill.warn {{ border-color: {WARN}; color: {WARN}; }}
.pill.bad {{ border-color: {BAD}; color: {BAD}; }}
.kpi {{ border-left: 3px solid {INK}; padding: 2px 0 2px 12px; margin-bottom: 10px; }}
.kpi .v {{ font-size: 1.45rem; font-weight: 600; }} .kpi .l {{ color: {SLATE}; font-size: 0.85rem; }}
.kpi .d.good {{ color: {GOOD}; }} .kpi .d.bad {{ color: {BAD}; }} .kpi .d {{ font-size: 0.85rem; }}
.driver {{ display: grid; grid-template-columns: 1fr auto; padding: 6px 0; border-bottom: 1px solid {LINE}; }}
.driver .m {{ font-weight: 500; }} .driver .dim {{ color: {SLATE}; font-size: 0.82rem; }}
.driver .x.neg {{ color: {BAD}; }} .driver .x.pos {{ color: {GOOD}; }}
section[data-testid="stSidebar"] {{ background: #F5F7FA; }}
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------ data bootstrap
@st.cache_resource(show_spinner=False)
def ensure_warehouse() -> str:
    if not settings.db_path.exists():
        from bi_copilot.data.generator import generate
        generate(customers=12_000)
    return str(settings.db_path)


@st.cache_resource(show_spinner=False)
def load_copilot(provider: str):
    from bi_copilot.copilot import Copilot
    from bi_copilot.llm.providers import get_llm
    llm = None if provider == "offline" else get_llm(provider)
    return Copilot(llm=llm)


if not settings.db_path.exists():
    with st.spinner("First run: building the Halden Supply Co. warehouse (about 30 seconds)…"):
        ensure_warehouse()
ensure_warehouse()

SAMPLES = {
    "Performance": ["What was net revenue last quarter?", "Show monthly revenue by region for the last 12 months.",
                    "Which sales team gives the largest discounts in Q3 2025?", "What is our CAC by marketing channel in H2 2025?"],
    "Why did it change?": ["Why did revenue decline in Q2 2025?", "Why did APAC revenue drop in November 2025?",
                           "Which suppliers raised costs this year?",
                           "Break down the change in Office Supplies revenue in 2024 into price and volume."],
    "Investigate": ["Investigate why profit declined in Tech Accessories in 2025.", "Anything unusual in Q4 2024?",
                    "Give me the weekly business brief", "What will revenue look like next quarter?"],
    "Definitions and limits": ["Why does Finance revenue differ from the sales dashboard?", "How is churn rate calculated?",
                               "What was EBITDA last year?", "Anything unusual in August 2025?"],
}

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown("### Halden Supply Co.")
    st.caption("A synthetic workspace-products distributor: 4 regions, 6 categories, 2022–2025.")
    import os
    configured = settings.llm_provider if settings.llm_provider in ("anthropic", "openai") else "offline"
    has_key = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY"))
    options = ["offline"] + ([configured] if configured != "offline" and has_key else [])
    provider = st.radio("Question understanding", options, index=len(options) - 1, horizontal=True,
                        help="Offline uses the rule-based planner and needs no API key. With a key set, an LLM plans the query "
                             "and writes the narrative; every number is still computed by SQL and checked.")
    cop = load_copilot(provider)
    st.caption(f"Mode: {cop.mode}")

    st.markdown("#### Try a question")
    for group, qs in SAMPLES.items():
        with st.expander(group, expanded=group == "Why did it change?"):
            for q in qs:
                if st.button(q, key=f"s_{q}", width="stretch"):
                    st.session_state["question"] = q
                    st.session_state["run"] = True

    st.markdown("#### Data")
    st.caption(cop.db.data_as_of())
    fr = cop.db.freshness()
    with st.expander("Tables and freshness"):
        st.dataframe(fr[["table_name", "row_count", "max_event_date"]].rename(
            columns={"table_name": "table", "row_count": "rows", "max_event_date": "data to"}),
            hide_index=True, width="stretch")
    with st.expander(f"Metric catalog ({len(cop.cat.visible_metrics())})"):
        term = st.text_input("Filter metrics", "", label_visibility="collapsed", placeholder="Filter metrics")
        for m in cop.cat.visible_metrics():
            if term.lower() in (m.label + m.definition + " ".join(m.synonyms)).lower():
                st.markdown(f"**{m.label}** — {m.definition}")
    if st.session_state.get("history"):
        st.markdown("#### Recent")
        for h in st.session_state["history"][-6:][::-1]:
            if st.button(h, key=f"h_{h}", width="stretch"):
                st.session_state["question"] = h
                st.session_state["run"] = True

# ------------------------------------------------------------------ question
st.markdown("## Ask about the business")
with st.form("ask", clear_on_submit=False, border=False):
    c1, c2 = st.columns([6, 1])
    q = c1.text_input("Question", value=st.session_state.get("question", ""), label_visibility="collapsed",
                      placeholder="e.g. Why did gross margin fall in Tech Accessories in 2025?")
    submitted = c2.form_submit_button("Ask", width="stretch", type="primary")
if submitted and q.strip():
    st.session_state["question"] = q.strip()
    st.session_state["run"] = True

if st.session_state.get("run") and st.session_state.get("question"):
    question = st.session_state["question"]
    st.session_state["run"] = False
    with st.spinner("Working it out…"):
        ans = cop.ask(question)
    st.session_state["answer"] = ans
    hist = st.session_state.setdefault("history", [])
    if question in hist:
        hist.remove(question)
    hist.append(question)

ans = st.session_state.get("answer")
if ans is None:
    st.markdown('<p class="narr">Ask a question in plain English, or pick one from the sidebar. Every answer shows the '
                'SQL, the metric definitions and the assumptions behind it.</p>', unsafe_allow_html=True)
    st.stop()


def fmt(v, f):
    from bi_copilot.analytics.grounding import fmt_value
    return fmt_value(v, f)


# ------------------------------------------------------------------ answer
conf_cls = {"high": "ok", "medium": "warn", "low": "bad"}[ans.confidence]
pills = [f'<span class="pill">{ans.intent.replace("_", " ")}</span>',
         f'<span class="pill {conf_cls}">confidence: {ans.confidence}</span>']
if ans.grounded is not None:
    pills.append(f'<span class="pill {"ok" if ans.grounded else "bad"}">'
                 f'{"all figures verified" if ans.grounded else "unverified figures"}</span>')
st.markdown(f'<div class="meta">{ans.question}</div>' + "".join(pills), unsafe_allow_html=True)
st.markdown(f'<div class="answer {"unsupported" if ans.status != "ok" else ""}">{ans.headline.replace("$", "&#36;")}</div>',
            unsafe_allow_html=True)

if ans.kpis:
    cols = st.columns(min(5, len(ans.kpis)))
    for i, k in enumerate(ans.kpis[:10]):
        d = ""
        if k.delta_pct is not None:
            good = (k.delta_pct >= 0) == (k.direction == "up")
            d = f'<div class="d {"good" if good else "bad"}">{k.delta_pct:+.1f}% vs last year</div>'
        elif k.delta_abs is not None:
            good = (k.delta_abs >= 0) == (k.direction == "up")
            unit = " pp" if k.fmt == "percent" else ""
            val = k.delta_abs * 100 if k.fmt == "percent" else k.delta_abs
            d = f'<div class="d {"good" if good else "bad"}">{val:+.1f}{unit} vs last year</div>'
        cols[i % len(cols)].markdown(f'<div class="kpi"><div class="v">{fmt(k.value, k.fmt)}</div>'
                                     f'<div class="l">{k.label}</div>{d}</div>', unsafe_allow_html=True)

left, right = st.columns([3, 2] if ans.drivers else [1, 0.0001])
with left:
    if ans.narrative:
        st.markdown(ans.narrative.replace("$", "\\$"))
    if ans.chart:
        st.plotly_chart(go.Figure(ans.chart), width="stretch", config={"displaylogo": False})
if ans.drivers:
    with right:
        st.markdown("#### Key drivers")
        met = ans.plan.metrics[0] if ans.plan and ans.plan.metrics else None
        f = cop.cat.metrics[met].format if met in cop.cat.metrics else "number"
        f = "currency" if ans.drivers[0].dimension == "supplier" and f == "percent" else f
        from bi_copilot.analytics.grounding import fmt_delta
        html = ""
        for d in ans.drivers[:6]:
            html += (f'<div class="driver"><div><div class="m">{d.member}</div><div class="dim">{d.dimension.replace("_", " ")}</div></div>'
                     f'<div class="x {"neg" if d.delta < 0 else "pos"}">{fmt_delta(d.delta, f)}</div></div>')
        st.markdown(html, unsafe_allow_html=True)

if ans.followups:
    st.markdown("#### Ask next")
    fc = st.columns(len(ans.followups))
    for i, fq in enumerate(ans.followups):
        if fc[i].button(fq, key=f"f_{i}_{fq}", width="stretch"):
            st.session_state["question"] = fq
            st.session_state["run"] = True
            st.rerun()

# ------------------------------------------------------------------ evidence
ev = ans.evidence
tabs = st.tabs(["Evidence", f"SQL ({len(ev.sql)})", "Data", "Plan"] + (["Investigation steps"] if ev.agent_trace else []))
with tabs[0]:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**Data as of:** {ev.data_as_of}")
        if ev.periods:
            st.markdown("**Periods:** " + "; ".join(ev.periods))
        if ev.filters:
            st.markdown("**Filters:** " + "; ".join(f"{k} = {', '.join(v)}" for k, v in ev.filters.items()))
        if ev.tables():
            st.markdown("**Tables used:** " + ", ".join(f"`{t}`" for t in ev.tables()))
        if ev.metrics:
            st.markdown("**Metric definitions**")
            for k, v in ev.metrics.items():
                st.markdown(f"- **{k}:** {v}")
    with c2:
        if ev.documents:
            st.markdown("**Business context used**")
            for d in ev.documents:
                st.markdown(f"- {d}")
        if ev.assumptions:
            st.markdown("**Assumptions**")
            for a in ev.assumptions:
                st.markdown(f"- {a}")
        if ev.limitations:
            st.markdown("**Limitations**")
            for a in ev.limitations:
                st.markdown(f"- {a}")
with tabs[1]:
    for s in ev.sql:
        st.caption(f"{s.purpose} · {s.rows} rows · {s.ms} ms")
        st.code(s.sql, language="sql")
with tabs[2]:
    if ans.table:
        df = pd.DataFrame(ans.table)
        st.dataframe(df, hide_index=True, width="stretch")
        st.download_button("Download CSV", df.to_csv(index=False).encode(), "copilot_result.csv", "text/csv")
    else:
        st.caption("No table for this answer.")
with tabs[3]:
    if ans.plan:
        st.json(ans.plan.model_dump(mode="json"))
if ev.agent_trace:
    with tabs[4]:
        for t in ev.agent_trace:
            st.markdown(f"**{t['step']}. {t['tool'].replace('_', ' ')}** {t['args'] or ''}".replace("$", "\\$"))
            st.markdown(t["finding"].replace("$", "\\$"))

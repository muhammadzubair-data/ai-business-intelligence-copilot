# Case study: an AI analyst that shows its working

## The problem

Most "chat with your data" demos answer the easy question, "what was revenue last quarter?", and stop. That isn't where analysts spend their time. They spend it on the follow-up: *why* did it fall, is it a real problem or a data glitch, and what do I tell the leadership team on Monday?

That second question is also where AI tools get dangerous. A language model writing its own SQL will happily use the wrong revenue definition, forget refunds, or produce a confident explanation for a drop that was really a broken tracking tag. The answer reads well and is wrong. Nobody can tell until it's in a board deck.

The goal of this project was a copilot that answers the "why" questions and earns trust by showing its working, so that every answer can be checked.

## The approach

**I built the business first.** Halden Supply Co. is a synthetic distributor of office and workspace products, with four regions, six categories, four sales channels and four years of history. Its warehouse covers sales, marketing, web traffic, support, inventory, purchasing and operating costs. Inside that history I planted nine real business stories and five traps. A group of European key accounts quietly stops ordering. A supplier raises prices. A new standing desk has a defect. A warehouse runs out of stock in the busiest month. A tracking outage makes web traffic look like it collapsed.

Because I know what really happened, I can measure whether the copilot finds it. Before any question is asked, an independent script confirms each planted effect is in the data and detectable.

**Definitions live in one place.** The copilot doesn't write SQL for business metrics. It picks a metric from a reviewed catalog of 56 definitions, and a compiler turns that into SQL. "Revenue" means the same thing in every answer, and the answer says which definition it used.

**The "why" answers come from analysis, not from prose.** For a question like *"Why did revenue decline in Q2 2025?"* the copilot:

1. measures the change (−2.8%, −$528K versus Q2 2024);
2. finds where the change is concentrated: Enterprise customers, then Direct Sales, then the EU Enterprise sales team;
3. checks for lost customers and finds three European key accounts that stopped ordering in March and had generated $753K a year earlier;
4. checks whether supply was a factor (it wasn't).

It reports what it found as "concentrated in" and "contributed to". Contribution analysis doesn't prove causation, and the answer doesn't pretend it does.

**Every answer carries its evidence.** It shows the SQL that ran, the metric definitions, the period and filters, the business documents it relied on, its assumptions, and its limitations. If a language model rewrites the narrative, the rewrite is checked number by number against the query results and rejected if anything doesn't match.

## What it can do

- **Root-cause analysis.** It traces a change down the business hierarchy, splits revenue into price, volume, mix and refunds, and separates margin changes into mix shifts and rate changes. It also measures how much of a profit decline comes from supplier cost increases.
- **A multi-step investigation agent.** It works through revenue, then costs, then suppliers, then customers in a bounded number of steps, and shows each step.
- **Anomaly detection.** It ignores normal seasonality, and it tells a tracking outage apart from a real drop by pointing to the incident log.
- **Forecasts.** Statistical models are chosen by backtest, with a range rather than a single number, and the limits are stated.
- **An executive brief.** It reports KPI movements, the main concern and its driver, an opportunity, unusual movements, and suggested next steps.
- **Saying no.** It declines EBITDA (depreciation isn't broken out in the data) and offers operating profit instead. It refuses requests to change data and asks for clarification when a question is ambiguous.

## Results

On the 137-question development benchmark, every metric answer matched independently written gold SQL. Every planted cause was recovered, every unsafe request was refused and every malicious SQL query was blocked.

The more honest number comes from a held-out set of 30 differently worded questions, written after development and run once: **83.3%**. The misses are informative. When the rule-based planner meets unfamiliar phrasing ("how much did we *sell*", "what's *behind* higher discounting") it asks for clarification or answers literally rather than inventing something. That is the right failure mode for a business tool, and it marks exactly where the optional language-model planner adds value.

## What I'd do with real data

- Connect the semantic layer to the client's warehouse (Snowflake, BigQuery, Postgres) and move their existing metric definitions into the catalog. That step is usually the most valuable part of the engagement on its own.
- Start with the 20–30 questions their team actually asks every week, and turn them into the benchmark.
- Run the LLM planner against that benchmark before rollout, and keep the rule-based path as the fallback.

## Built with

Python, DuckDB, sqlglot, Pydantic, scikit-learn, Plotly, Streamlit. It optionally uses Claude or any OpenAI-compatible model, and it runs fully offline without one.

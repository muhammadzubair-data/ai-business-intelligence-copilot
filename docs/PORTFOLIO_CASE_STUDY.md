# AI Business Intelligence Copilot

## Executive Portfolio Case Study

An end-to-end Business Intelligence Copilot designed to turn natural-language business questions into governed, evidence-backed analytical answers.

Instead of functioning as a static dashboard, the application allows decision-makers to ask questions such as:

- Why did revenue decline?
- Which suppliers increased costs?
- What changed in customer return rates?
- Are there unusual business movements?
- What will revenue look like next quarter?

The system analyzes the underlying business data and returns structured answers with supporting evidence, business definitions, assumptions, SQL traces, confidence indicators, visualizations, and limitations.

---

## Business Problem

Traditional dashboards are useful for monitoring KPIs, but they often require users to already know:

1. which dashboard to open,
2. which metric to inspect,
3. which filters to apply, and
4. how to investigate the cause of a change.

This project explores a different analytical workflow:

**Question → Metric → Analysis → Root Cause → Evidence → Decision**

The goal is to make business intelligence more accessible while keeping the analytical process transparent and auditable.

---

## Core Capabilities

### Natural-Language Business Questions

Users can ask business questions through a conversational interface instead of manually navigating multiple dashboards.

### Governed Metric Layer

Business metrics use predefined definitions so that calculations remain consistent across analyses.

Examples include:

- Net Revenue
- Cost of Goods Sold
- Return Rate
- Customer Churn
- Gross Profit
- Discounts
- Support Tickets
- Product Defect Tickets

### Root-Cause Analysis

The analytical engine can drill through relevant business dimensions to identify where a change is concentrated.

Possible dimensions include:

- customer segment
- region
- sales channel
- product
- category
- supplier
- customer account

This allows the system to move beyond reporting **what changed** toward explaining **where the change came from**.

### Forecasting

The application can generate forward-looking revenue forecasts using historical business data.

Forecast outputs include:

- point forecasts,
- uncertainty ranges,
- historical comparison,
- monthly projections, and
- model evaluation information.

### Anomaly Detection

The system identifies unusual movements in operational and financial metrics and highlights observations that differ materially from expected behavior.

### Evidence-Backed Answers

Analytical responses can include:

- data freshness,
- periods analyzed,
- tables used,
- metric definitions,
- business context,
- assumptions,
- limitations, and
- SQL evidence.

This helps make analytical conclusions inspectable rather than presenting them as unexplained AI outputs.

---

## Example Insight: Revenue Root Cause

A user can ask:

> Why did revenue decline in Q2 2025?

The application identifies the revenue change, evaluates contributing dimensions, ranks the major drivers, and produces a drill-down path showing where the decline is concentrated.

The result can include a waterfall visualization together with key drivers and supporting business evidence.

---

## Example Insight: Supplier Cost Analysis

A user can ask:

> Which suppliers raised costs this year?

The system evaluates Cost of Goods Sold and supplier-level cost effects to identify which suppliers contributed most to the increase.

This demonstrates how the copilot can support procurement and margin analysis rather than only sales reporting.

---

## Example Insight: Return Rate Investigation

A question such as:

> Why did return rate change in Q4 2024?

can trigger ratio-metric root-cause analysis across dimensions such as customer segment, product, and sales channel.

The result provides a structured analytical path from the high-level KPI movement to more specific contributing factors.

---

## Example Insight: Revenue Forecast

A user can ask:

> What will revenue look like next quarter?

The application generates a forecast for the upcoming period together with an uncertainty interval and historical context.

This allows the same interface to support both diagnostic and predictive analytics.

---

## Analytical Architecture

The project combines several components:

**Business Data**

↓

**Governed Metric Catalog**

↓

**Question Understanding**

↓

**Analytical Planner**

↓

**SQL / Analytical Engine**

↓

**Root-Cause Analysis / Forecasting / Anomaly Detection**

↓

**Evidence & Validation**

↓

**Business Narrative + Visualization**

---

## Technology Stack

- Python
- SQL
- DuckDB
- Pandas
- Streamlit
- Plotly
- Statistical forecasting
- Root-cause analysis
- Anomaly detection
- Natural-language analytical workflows
- Automated testing
- GitHub Actions CI

---

## Engineering Quality

The repository includes automated testing and continuous integration through GitHub Actions.

The analytical logic is tested to help ensure that changes to the codebase do not silently break expected business results.

The project also separates business definitions, analytical logic, application components, evaluation assets, and supporting documentation.

---

## Responsible AI / Analytical Guardrails

A useful analytical copilot should know when it does **not** have enough information.

For unsupported questions, the application can explicitly indicate that the requested metric or information is unavailable rather than fabricating an answer.

This behavior is important for trustworthy business intelligence systems.

---

## Why This Project Matters

The project demonstrates skills across multiple areas rather than focusing only on visualization:

- Business Intelligence
- Data Analytics
- Data Science
- SQL
- Python
- Data Modeling
- Root-Cause Analysis
- Forecasting
- Anomaly Detection
- Decision Intelligence
- Analytics Engineering
- AI-assisted analytics
- Data governance
- Testing and validation

The result is a portfolio project designed to demonstrate how analytical systems can move from simply displaying data toward helping users understand **what happened, why it happened, and what may happen next**.

---

## Project Status

The application is deployed as an interactive Streamlit project and the repository includes the analytical engine, tests, business knowledge layer, evaluation components, documentation, and CI workflow.

This case study is part of Muhammad Zubair's Data Science and Business Intelligence portfolio.

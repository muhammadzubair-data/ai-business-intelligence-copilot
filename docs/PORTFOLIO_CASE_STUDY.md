# AI Business Intelligence Copilot — Portfolio Case Study

## Executive Summary

AI Business Intelligence Copilot is an end-to-end analytics application designed to turn natural-language business questions into governed, evidence-backed insights.

Instead of requiring business users to manually explore dashboards, write SQL, or inspect multiple reports, the copilot allows users to ask questions such as:

- Why did revenue decline in Q2 2025?
- Which suppliers raised costs this year?
- Why did return rate change in Q4 2024?
- What will revenue look like next quarter?
- Anything unusual in Q4 2024?
- How is customer churn calculated?

The application combines business metric definitions, analytical workflows, root-cause analysis, anomaly detection, forecasting, SQL evidence, and business context in a single interactive Streamlit interface.

---

## Business Problem

Traditional BI dashboards are useful for monitoring KPIs, but answering follow-up questions often requires additional analysis.

A business user may know that revenue declined, but still needs to understand:

1. What caused the decline?
2. Which business dimension contributed most?
3. Which customers, products, regions, channels, or suppliers were involved?
4. Is the change unusual?
5. What is likely to happen next?
6. What evidence supports the answer?

This project was built to bridge the gap between dashboards and analytical investigation.

---

## Solution

The AI Business Intelligence Copilot provides a natural-language interface over a governed analytics layer.

The system can:

- answer KPI and performance questions;
- compare business periods;
- perform root-cause analysis;
- drill through business dimensions;
- detect unusual KPI behavior;
- analyze supplier cost movements;
- generate business forecasts;
- explain metric definitions;
- surface supporting SQL and data sources;
- show assumptions and limitations;
- provide evidence-backed analytical narratives.

---

## Core Capabilities

### 1. Natural-Language Business Questions

Users interact with the application through business questions rather than SQL.

The interface supports several analytical question types, including:

**Performance**
- What was net revenue last quarter?
- Show monthly revenue by region.
- Which sales team gives the largest discounts?
- What is CAC by marketing channel?

**Root Cause**
- Why did revenue decline?
- Why did APAC revenue drop?
- Which suppliers raised costs?
- Break down revenue changes into price and volume.

**Investigation**
- Investigate profit decline.
- Find unusual business behavior.
- Generate a weekly business brief.

**Forecasting**
- Forecast revenue for the next quarter.

**Definitions**
- Explain customer churn.
- Explain differences between Finance and Sales metrics.

---

## Root-Cause Analysis

The RCA engine analyzes changes across eligible business dimensions and identifies the dimensions contributing most strongly to a KPI movement.

Depending on the metric and available data, drill-down dimensions can include:

- customer segment;
- region;
- product;
- category;
- sales channel;
- supplier;
- account;
- other governed dimensions.

The engine creates a drill-down path rather than simply returning the largest independent values.

This makes it possible to move from a high-level KPI change toward increasingly specific business drivers.

---

## Example: Revenue Decline Investigation

For a revenue decline question, the application can:

1. calculate the current-period KPI;
2. calculate the comparison-period KPI;
3. measure the absolute and percentage change;
4. rank contributing dimensions;
5. drill into the strongest business slice;
6. identify relevant accounts or entities;
7. generate a concise business explanation;
8. expose supporting data and SQL.

The result is presented as an analytical narrative rather than an unexplained chart.

---

## Supplier Cost Analysis

The application also supports procurement and cost investigation.

For questions such as:

> Which suppliers raised costs this year?

the copilot can identify supplier-level cost effects and show their contribution to Cost of Goods Sold.

This enables users to distinguish overall COGS movement from supplier-specific cost pressure.

---

## Forecasting

The forecasting workflow generates forward-looking KPI estimates using historical business data.

The application presents:

- historical actuals;
- forecast values;
- forecast intervals;
- period-over-period comparison;
- model information;
- assumptions;
- limitations.

Forecasts are explicitly presented as statistical estimates rather than budgets or guaranteed outcomes.

---

## Anomaly Detection

The anomaly engine monitors KPI behavior at weekly granularity and evaluates deviations against historical patterns.

The implementation includes controls for:

- historical baseline requirements;
- metric maturity;
- minimum denominator thresholds;
- ratio metrics;
- seasonal behavior;
- data-quality anomalies;
- consecutive anomaly consolidation;
- incomplete or immature periods.

Special handling prevents immature or misleading periods from being surfaced as meaningful business anomalies.

---

## Governed Metric Layer

Business metrics are not treated as arbitrary calculations.

The application uses a governed metric catalog containing business definitions and analytical metadata.

This helps maintain consistency between:

- business questions;
- calculations;
- SQL;
- visualizations;
- narratives;
- evidence.

For example, Net Revenue follows the project's Finance-oriented definition rather than allowing the application to silently redefine revenue during analysis.

---

## Evidence-Backed Answers

A major design goal of the project is analytical traceability.

Answers can expose supporting information including:

- data freshness;
- periods analyzed;
- tables used;
- metric definitions;
- business context;
- assumptions;
- limitations;
- generated SQL;
- analytical plan.

This allows users to inspect how an answer was produced instead of treating the copilot as a black box.

---

## Business Context

The analytical system incorporates documented business context alongside structured data.

Context can include:

- metric definitions;
- business rules;
- product notes;
- revenue-recognition rules;
- known differences between reporting systems.

This allows analytical answers to reflect the organization's documented business logic.

---

## Interactive Visualizations

Depending on the analytical question, the application generates visual explanations such as:

- waterfall charts;
- time-series charts;
- forecast charts;
- supplier contribution charts;
- KPI breakdowns;
- ranked key-driver views.

The purpose of visualization is not simply presentation. Each visual supports the analytical explanation produced by the copilot.

---

## Example Analytical Workflow

A typical investigation follows:

**Question → Metric → Period → Comparison → Dimension Analysis → Drill Down → Evidence → Business Explanation**

For example:

**Revenue decline**

→ Net Revenue  
→ Q2 2025  
→ Q2 2024 comparison  
→ Customer Segment  
→ Sales Channel  
→ Account-level evidence  
→ Root-cause narrative

This structure makes the analytical reasoning easier to audit and communicate.

---

## Technology Stack

The project uses a modern Python analytics stack including:

- Python
- Pandas
- NumPy
- DuckDB
- Streamlit
- SQL
- Pytest
- GitHub Actions

The repository is organized into separate components for application logic, analytics, configuration, data, evaluation, business knowledge, testing, and warehouse functionality.

---

## Software Quality and Testing

Automated testing is integrated into the repository through GitHub Actions.

The CI workflow validates the application after repository changes.

### Current verified status

```text
63 passed in 35.45s

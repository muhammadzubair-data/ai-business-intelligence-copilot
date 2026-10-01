# Evaluation

## What is measured

The benchmark checks the things that matter for a business user: whether the number is right, whether the explanation finds the real cause, and whether the system says no when it should.

It works because the data is synthetic and the true causes are known. Nine business events were planted in the warehouse: a lost group of European key accounts, a supplier cost increase, a defective product, an acquisition-cost spike, a price increase, a channel mix shift, a regional stockout, a discounting outlier, and a loyalty improvement. Five decoys were planted as well, such as a tracking outage that looks like a traffic collapse and normal seasonality. Before any question is asked, `python -m bi_copilot.cli measure` confirms with independent SQL that every event is present and detectable. It currently passes 22 of 22 checks.

| Category | n | Pass condition |
|---|---|---|
| Metric questions (scalar, filtered, breakdowns) | 63 | Result matches **independently hand-written gold SQL** within 0.5% |
| Trends | 5 | Same, per period, and the right time grain |
| Root cause | 13 | The planted driver appears in the drivers or explanation |
| Investigation (agent) | 3 | The planted driver appears in the conclusion or trace |
| Anomaly scan | 5 | Planted anomalies found; nothing flagged in a quiet week; outage reported as a data issue |
| Forecast | 4 | Correct metric and horizon, with a backtest and an interval |
| Executive brief | 3 | Brief produced with at least 5 KPIs |
| Definitions (RAG) | 15 | The expected source is in the top 3 retrieved |
| Unsupported | 12 | Declined (EBITDA, NPS, LTV, market share, out-of-range years…) |
| Ambiguous | 4 | Sent back for clarification without running SQL |
| Unsafe requests | 10 | Refused, no SQL executed, warehouse unchanged |
| SQL guard | 21 | 18 malicious queries blocked, 3 legitimate ones allowed |

The gold SQL lives in `scripts/build_benchmark.py`. It is written against the views by hand and does not use the Copilot's compiler, so accuracy isn't the compiler grading its own work.

## Results (offline mode, no API key)

### Development set — 137 questions

| Measure | Result |
|---|---|
| Overall pass rate | **100%** |
| Metric result accuracy vs gold SQL | 100% |
| Metric / dimension / period selection | 100% / 100% / 100% |
| Root-cause driver recovery on planted events | 100% |
| Anomaly scenarios | 100% |
| Retrieval hit@3 | 100% |
| Unsupported declined / ambiguous clarified / unsafe refused | 100% / 100% / 100% |
| False rejections of answerable questions | 0% |
| Narratives with every number verified | 99.1% |
| SQL guard: blocked / allowed | 100% / 100% |
| Warehouse unchanged after the run | Yes |
| Latency p50 / p95 | 0.15s / 2.3s |

### Held-out set — 30 questions

| Measure | Result |
|---|---|
| Overall pass rate | **83.3%** |
| Metric result accuracy vs gold SQL | 90% |
| Root-cause driver recovery | 1 of 4 |
| Retrieval hit@3 | 100% |
| Unsupported declined | 100% |

Full per-question results are in `eval/results/` (`benchmark_results.md` and `holdout_results.md`, plus JSON).

## How to read these numbers

The development set was written alongside the system, and the planner and analytics were fixed against its failures. A score of 100% there means the system handles the questions it was designed for. It is not an estimate of accuracy on new questions.

The held-out set is the honest number. Those 30 questions were written after development, in different words, and run once. Nothing was changed to improve them. They scored 83.3%. The failures show where the rule-based planner runs out of vocabulary:

- "how much did we **sell** in the second quarter" and "how much money did we **refund** customers": verbs instead of the metric nouns the planner matches, so it asked for clarification.
- "**explain the jump** in refunds", "**what is behind** higher discounting", "why is Tech Accessories **less profitable**": causal phrasings it doesn't recognise, so it gave the number but not the cause, or analysed revenue instead of profit.

None of these are wrong answers; the misses are a clarification request or a less useful answer. That is the failure pattern you want: when the system doesn't understand, it asks or stays literal rather than inventing. Closing the gap is the job of the LLM planner, and it is the main thing to measure next (see below).

## Running it

```bash
python -m bi_copilot.cli eval                    # development set, offline
python -m bi_copilot.cli eval --set holdout      # held-out set
python -m bi_copilot.cli eval --modes semantic_offline,semantic_llm,raw_sql_llm,views_sql_llm
```

The LLM modes need `BI_COPILOT_LLM_PROVIDER` and an API key. Without one they are reported as skipped rather than estimated. Results here are from offline mode only; no LLM comparison numbers are claimed until those runs have actually been done.

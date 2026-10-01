"""Command-line entry point.

    python -m bi_copilot.cli generate --customers 12000
    python -m bi_copilot.cli measure
    python -m bi_copilot.cli ask "Why did revenue decline in Q2 2025?"
    python -m bi_copilot.cli eval
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import settings

DEMO_CUSTOMERS = 12_000
FULL_CUSTOMERS = 60_000


def _cmd_generate(a):
    from .data.generator import generate
    n = FULL_CUSTOMERS if a.full else a.customers
    generate(customers=n, seed=a.seed, db_path=Path(a.db) if a.db else None)


def _cmd_measure(a):
    from .data.measure import measure
    r = measure(Path(a.db) if a.db else None)
    s = r["_summary"]
    print(f"Planted-event checks: {s['passed']}/{s['total']} passed" + (f"; failed: {s['failed']}" if s["failed"] else ""))
    sys.exit(0 if not s["failed"] else 1)


def _cmd_ask(a):
    from .copilot import Copilot
    c = Copilot(llm=None if a.offline else "auto")
    ans = c.ask(a.question)
    if a.json:
        print(ans.model_dump_json(indent=2, exclude={"chart"}))
        return
    print(f"\n{ans.headline}\n")
    if ans.narrative:
        print(ans.narrative + "\n")
    for d in ans.drivers[:5]:
        print(f"  - {d.dimension}: {d.member} ({d.delta:+,.2f})")
    if ans.evidence.assumptions:
        print("\nAssumptions: " + " ".join(ans.evidence.assumptions))
    print(f"\n[{ans.intent} | confidence {ans.confidence} | grounded {ans.grounded} | {len(ans.evidence.sql)} queries | {ans.evidence.data_as_of}]")
    if a.sql:
        for s in ans.evidence.sql:
            print(f"\n-- {s.purpose} ({s.rows} rows, {s.ms} ms)\n{s.sql}")


def _cmd_eval(a):
    from .evaluation import run_benchmark
    res = run_benchmark(modes=a.modes.split(","), limit=a.limit, question_set=a.set)
    print(json.dumps(res["summary"], indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="bi-copilot", description="AI Business Intelligence Copilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="build the synthetic warehouse")
    g.add_argument("--customers", type=int, default=DEMO_CUSTOMERS)
    g.add_argument("--full", action="store_true", help=f"full scale ({FULL_CUSTOMERS:,} customers, ~4M rows)")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--db", default=None)
    g.set_defaults(fn=_cmd_generate)
    m = sub.add_parser("measure", help="verify planted events are present and detectable")
    m.add_argument("--db", default=None)
    m.set_defaults(fn=_cmd_measure)
    q = sub.add_parser("ask", help="ask a question")
    q.add_argument("question")
    q.add_argument("--offline", action="store_true", help="ignore any configured LLM")
    q.add_argument("--json", action="store_true")
    q.add_argument("--sql", action="store_true", help="print the SQL that was run")
    q.set_defaults(fn=_cmd_ask)
    e = sub.add_parser("eval", help="run the benchmark")
    e.add_argument("--modes", default="semantic_offline")
    e.add_argument("--limit", type=int, default=None)
    e.add_argument("--set", default="main", choices=["main", "holdout"])
    e.set_defaults(fn=_cmd_eval)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

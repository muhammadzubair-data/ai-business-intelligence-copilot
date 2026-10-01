from datetime import date

import pytest

from bi_copilot.plan import TimeRange
from bi_copilot.semantic.catalog import get_catalog
from bi_copilot.semantic.compiler import CompileError, Compiler, MetricRequest
from bi_copilot.semantic.timeparse import parse_time


def test_catalog_loads_and_validates():
    cat = get_catalog()
    assert len(cat.metrics) >= 50
    assert "ebitda" in cat.unsupported
    assert cat.metrics["gross_margin_pct"].type == "ratio"


def test_net_revenue_matches_hand_written_sql(db, tr):
    sql = Compiler().compile(MetricRequest(["net_revenue"], tr["q2_25"]))
    got = db.run(sql)[0].iloc[0, 0]
    gold = db.scalar("""SELECT SUM(net_line_amount) - SUM(refund_amount) FROM v_sales_lines
                        WHERE order_status='Shipped' AND ship_date BETWEEN '2025-04-01' AND '2025-06-30'""")
    assert got == pytest.approx(gold, rel=1e-9)


def test_ratio_and_derived_metrics(db, tr):
    df = db.run(Compiler().compile(MetricRequest(["gross_margin_pct", "operating_profit"], tr["y25"], grain="quarter")))[0]
    assert len(df) == 4
    assert df.gross_margin_pct.between(0.2, 0.5).all()


def test_custom_template_metric(db, tr):
    df = db.run(Compiler().compile(MetricRequest(["churn_rate"], tr["q2_25"], ["segment"])))[0]
    assert set(df.segment) == {"Enterprise", "Mid-Market", "SMB", "Consumer"}
    assert df.churn_rate.between(0, 1).all()


def test_invalid_dimension_rejected(tr):
    with pytest.raises(CompileError):
        Compiler().compile(MetricRequest(["opex"], tr["y25"], ["region"]))


def test_filter_values_are_quoted_safely(db, tr):
    sql = Compiler().compile(MetricRequest(["net_revenue"], tr["y25"], filters={"region": ["Europe'; DROP TABLE x; --"]}))
    assert "'Europe''; DROP TABLE x; --'" in sql      # the quote is escaped inside the literal
    df, _ = db.run(sql)                              # runs as a harmless filter that matches nothing
    import pandas as pd
    assert pd.isna(df.iloc[0, 0])


@pytest.mark.parametrize("text,start,end", [
    ("revenue in Q2 2025", date(2025, 4, 1), date(2025, 6, 30)),
    ("FY2025 revenue", date(2024, 2, 1), date(2025, 1, 31)),
    ("last quarter", date(2025, 10, 1), date(2025, 12, 31)),
    ("last 12 months", date(2025, 1, 1), date(2025, 12, 31)),
    ("refunds in Oct-Nov 2024", date(2024, 10, 1), date(2024, 11, 30)),
    ("H2 2025", date(2025, 7, 1), date(2025, 12, 31)),
    ("this week", date(2025, 12, 22), date(2025, 12, 28)),
])
def test_time_parsing(text, start, end):
    tp = parse_time(text)
    assert (tp.time_range.start, tp.time_range.end) == (start, end)


def test_comparisons():
    tp = parse_time("revenue in 2025 vs 2023")
    assert tp.time_range.label == "2025" and tp.comparison.label == "2023"
    tp = parse_time("Q3 2025 year over year")
    assert tp.comparison.label == "Q3 2024"

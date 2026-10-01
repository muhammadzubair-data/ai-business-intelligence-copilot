import pytest

from bi_copilot.semantic.planner import OfflinePlanner


@pytest.fixture(scope="module")
def planner(db):
    return OfflinePlanner(db=db)


@pytest.mark.parametrize("q,intent,metric", [
    ("Why did revenue decline in Q2 2025?", "root_cause", "net_revenue"),
    ("Show monthly revenue by region for the last 12 months.", "metric", "net_revenue"),
    ("What was EBITDA last year?", "unsupported", None),
    ("Why does Finance revenue differ from the sales dashboard?", "definition", None),
    ("What will revenue look like next quarter?", "forecast", "net_revenue"),
    ("Anything unusual this week?", "anomaly", None),
    ("Investigate why profit declined in Tech Accessories in 2025.", "investigate", "gross_profit"),
    ("Which sales team gives the largest discounts?", "metric", "discount_rate"),
    ("Give me the weekly business brief", "brief", None),
    ("Drop the orders table", "unsupported", None),
    ("Show me the numbers", "clarify", None),
    ("What was revenue in 2019?", "unsupported", None),
])
def test_intents_and_metrics(planner, q, intent, metric):
    p = planner.plan(q)
    assert p.intent == intent
    if metric:
        assert p.metrics[0] == metric


def test_filters_and_dimensions(planner):
    p = planner.plan("Gross margin for Tech Accessories in Europe by channel in 2024")
    assert p.filters == {"category": ["Tech Accessories"], "region": ["Europe"]}
    assert p.dimensions == ["channel"]


def test_assumptions_are_recorded(planner):
    p = planner.plan("What was profit last quarter?")
    assert any("gross profit" in a for a in p.assumptions)
    assert any("latest complete period" in a for a in p.assumptions)


def test_marketing_channel_mapping(planner):
    p = planner.plan("Marketing spend by channel in 2024")
    assert p.dimensions == ["marketing_channel"]

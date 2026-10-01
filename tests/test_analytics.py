from datetime import date

import pytest

from bi_copilot.analytics.grounding import check_grounding, extract_numbers
from bi_copilot.plan import TimeRange


def test_pvm_components_sum_to_total(copilot, tr):
    p = copilot.rca.pvm("net_revenue", tr["y25"], tr["y24"], {"category": ["Office Supplies"]}, [])
    assert p["price_effect"] + p["volume_effect"] + p["mix_effect"] + p["refund_effect"] == pytest.approx(p["total"])
    total = copilot.rca.value("net_revenue", tr["y25"], {"category": ["Office Supplies"]}, []) - \
        copilot.rca.value("net_revenue", tr["y24"], {"category": ["Office Supplies"]}, [])
    assert p["total"] == pytest.approx(total, rel=1e-6)


def test_ratio_contributions_sum_to_change(copilot, tr):
    t = copilot.rca.by_dim("gross_margin_pct", tr["y25"], tr["y24"], "category", {}, [])
    cur = copilot.rca.value("gross_margin_pct", tr["y25"], {}, [])
    prev = copilot.rca.value("gross_margin_pct", tr["y24"], {}, [])
    assert t.delta.sum() == pytest.approx(cur - prev, abs=1e-9)
    assert (t.rate_effect + t.mix_effect).sum() == pytest.approx(cur - prev, abs=1e-9)


def test_rca_recovers_e01_lost_enterprise_accounts(copilot, tr):
    r = copilot.rca.analyze("net_revenue", tr["q2_25"], tr["q2_24"])
    members = [s.member for s in r.path]
    assert "Enterprise" in members and "EU Enterprise" in members
    assert r.lost_customers.key_account.sum() >= 3


def test_rca_recovers_e03_aerodesk_returns(copilot, tr):
    r = copilot.rca.analyze("return_rate", tr["oct24"], tr["oct23"])
    assert [s.member for s in r.path][:2] == ["Office Furniture", "Halden AeroDesk Pro Standing Desk"]


def test_rca_recovers_e02_supplier_cost(copilot, tr):
    r = copilot.rca.analyze("gross_profit", tr["y25"], tr["y24"], {"category": ["Tech Accessories"]})
    top = r.cost.groupby("supplier").cost_rate_effect.sum().idxmax()
    assert top == "Kestrel Components"


def test_anomaly_detects_tracking_outage_as_data_quality(copilot):
    res = copilot.anomaly.scan(TimeRange(start=date(2025, 8, 4), end=date(2025, 8, 17), label="aug"))
    dq = [a for a in res.anomalies if a.kind == "data_quality"]
    assert dq and dq[0].metric == "sessions" and date(2025, 8, 11) in dq[0].gap_days
    assert not [a for a in res.anomalies if a.metric == "sessions" and a.kind == "business"]


def test_anomaly_quiet_in_holiday_week(copilot):
    from bi_copilot.semantic.timeparse import last_complete_week
    assert copilot.anomaly.scan(last_complete_week()).anomalies == []


def test_forecast_backtest_and_interval(copilot):
    from bi_copilot.config import DATA_END, DATA_START
    fr = copilot.forecaster.forecast("net_revenue", TimeRange(start=DATA_START, end=DATA_END, label="h"), 3)
    assert len(fr.forecast) == 3 and (fr.forecast.lo < fr.forecast.yhat).all() and (fr.forecast.hi > fr.forecast.yhat).all()
    assert fr.backtest_mape[fr.model] < 15


def test_grounding_checker():
    facts = {"rev": 18_524_091.14, "pct": -2.77, "gm": 0.307}
    assert check_grounding("Revenue fell 2.8% to $18.52M; margin 30.7%.", facts) == []
    assert check_grounding("Revenue fell 2.8% to $19.9M.", facts) == ["$19.9M"]
    assert extract_numbers("in Q2 2025, United Corporation 06970, over 90 days") == []


def test_retrieval_finds_incident_and_rules():
    from bi_copilot.rag.retriever import get_retriever
    r = get_retriever()
    assert r.search("why were web sessions zero in August 2025")[0].chunk.source == "incident_log.md"
    assert "Fiscal calendar" in r.search("when does the fiscal year start")[0].chunk.title

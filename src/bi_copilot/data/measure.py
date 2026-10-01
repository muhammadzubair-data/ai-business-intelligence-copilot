"""Measure each planted event's realised effect with hand-written SQL.

This is deliberately independent of the Copilot's semantic layer: it is the
answer key. Output: eval/ground_truth/realised_effects.json, plus a pass/fail
detectability check for every event (e.g. "E01 is the largest negative
contributor to the Q2 2025 decline").
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from ..config import settings

SHIPPED = "order_status = 'Shipped'"
NET = "SUM(net_line_amount - refund_amount)"


def _one(con, sql):
    return con.execute(sql).fetchone()


def measure(db_path: Path | None = None, out_path: Path | None = None) -> dict:
    con = duckdb.connect(str(db_path or settings.db_path), read_only=True)
    res: dict = {}

    # ---------------- E01 --------------------------------------------------
    q = f"""
    WITH b AS (
      SELECT region, segment,
        {NET} FILTER (WHERE ship_date BETWEEN '2025-04-01' AND '2025-06-30') cur,
        {NET} FILTER (WHERE ship_date BETWEEN '2024-04-01' AND '2024-06-30') prev
      FROM v_sales_lines WHERE {SHIPPED} GROUP BY ALL)
    SELECT region, segment, cur, prev, cur - prev delta FROM b ORDER BY delta"""
    rows = con.execute(q).fetchall()
    tot_cur = sum(r[2] or 0 for r in rows); tot_prev = sum(r[3] or 0 for r in rows)
    top = rows[0]
    accts = con.execute(f"""
      SELECT customer_name,
        {NET} FILTER (WHERE ship_date BETWEEN '2024-04-01' AND '2024-06-30') prev,
        COALESCE({NET} FILTER (WHERE ship_date BETWEEN '2025-04-01' AND '2025-06-30'), 0) cur,
        MAX(order_date) last_order
      FROM v_sales_lines WHERE {SHIPPED} AND segment='Enterprise' AND region='Europe' AND is_key_account
      GROUP BY 1 HAVING MAX(order_date) < DATE '2025-04-01' AND prev > 0 ORDER BY prev DESC""").fetchall()
    lost_prev = sum(a[1] for a in accts)
    res["E01"] = dict(
        q2_2025_net_revenue=tot_cur, q2_2024_net_revenue=tot_prev, yoy_change_pct=(tot_cur / tot_prev - 1) * 100,
        top_negative_contributor=f"{top[0]} x {top[1]}", top_negative_delta=top[4],
        lost_key_accounts=[a[0] for a in accts], lost_accounts_q2_2024_revenue=lost_prev,
        checks=dict(largest_negative_is_europe_enterprise=(top[0], top[1]) == ("Europe", "Enterprise"),
                    total_declines_yoy=tot_cur < tot_prev, three_accounts_lost=len(accts) >= 3))

    # ---------------- E02 --------------------------------------------------
    r = _one(con, f"""
      SELECT
        SUM(net_line_amount) FILTER (WHERE ship_date BETWEEN '2024-03-01' AND '2024-12-31') rev_prev,
        SUM(net_line_amount) FILTER (WHERE ship_date BETWEEN '2025-03-01' AND '2025-12-31') rev_cur,
        1 - SUM(cogs_amount) FILTER (WHERE ship_date BETWEEN '2024-03-01' AND '2024-12-31')
          / SUM(net_line_amount) FILTER (WHERE ship_date BETWEEN '2024-03-01' AND '2024-12-31') gm_prev,
        1 - SUM(cogs_amount) FILTER (WHERE ship_date BETWEEN '2025-03-01' AND '2025-12-31')
          / SUM(net_line_amount) FILTER (WHERE ship_date BETWEEN '2025-03-01' AND '2025-12-31') gm_cur
      FROM v_sales_lines WHERE {SHIPPED} AND category='Tech Accessories'""")
    k = _one(con, """SELECT AVG(unit_cost / list_price) FILTER (WHERE order_date BETWEEN '2025-03-01' AND '2025-12-31')
                          / AVG(unit_cost / list_price) FILTER (WHERE order_date BETWEEN '2024-03-01' AND '2024-12-31') - 1
                     FROM v_sales_lines WHERE supplier='Kestrel Components'""")
    sup = con.execute(f"""
      SELECT supplier, NULL AS x,
        AVG(unit_cost/list_price) FILTER (WHERE ship_date BETWEEN '2025-03-01' AND '2025-12-31')
        - AVG(unit_cost/list_price) FILTER (WHERE ship_date BETWEEN '2024-03-01' AND '2024-12-31') d
      FROM v_sales_lines WHERE {SHIPPED} GROUP BY 1 ORDER BY d DESC LIMIT 1""").fetchone()
    res["E02"] = dict(revenue_change_pct=(r[1] / r[0] - 1) * 100, gross_margin_prev_pct=r[2] * 100,
                      gross_margin_cur_pct=r[3] * 100, margin_change_pp=(r[3] - r[2]) * 100,
                      kestrel_cost_ratio_change_pct=k[0] * 100, supplier_with_largest_cost_increase=sup[0],
                      checks=dict(margin_falls=r[3] < r[2] - 0.03, kestrel_is_top_cost_increase=sup[0] == "Kestrel Components"))

    # ---------------- E03 --------------------------------------------------
    rr = con.execute(f"""
      SELECT product, SUM(refund_amount)/SUM(net_line_amount) rr, SUM(refund_amount) refunds
      FROM v_sales_lines WHERE {SHIPPED} AND ship_date BETWEEN '2024-10-01' AND '2024-11-30'
      GROUP BY 1 ORDER BY refunds DESC LIMIT 1""").fetchone()
    base = _one(con, f"""SELECT SUM(refund_amount)/SUM(net_line_amount) FROM v_sales_lines
                          WHERE {SHIPPED} AND category='Office Furniture' AND product <> 'Halden AeroDesk Pro Standing Desk'
                          AND ship_date BETWEEN '2024-10-01' AND '2024-11-30'""")[0]
    t = _one(con, """SELECT COUNT(*) FILTER (WHERE created_date BETWEEN '2024-10-01' AND '2024-11-30'),
                            COUNT(*) FILTER (WHERE created_date BETWEEN '2024-08-01' AND '2024-09-30')
                     FROM v_tickets WHERE ticket_category='Product Defect'""")
    res["E03"] = dict(top_refund_amount_product=rr[0], product_refund_rate_pct=rr[1] * 100,
                      furniture_baseline_refund_rate_pct=base * 100,
                      defect_tickets_oct_nov_2024=t[0], defect_tickets_aug_sep_2024=t[1],
                      checks=dict(aerodesk_is_top_refund_amount=rr[0] == "Halden AeroDesk Pro Standing Desk",
                                  aerodesk_rate_3x_baseline=rr[1] > 3 * base,
                                  defect_tickets_up=t[0] > 1.3 * t[1]))

    # ---------------- E04 --------------------------------------------------
    c = con.execute("""
      SELECT CASE WHEN month BETWEEN '2025-07-01' AND '2025-12-31' THEN 'H2 2025'
                  WHEN month BETWEEN '2025-01-01' AND '2025-06-30' THEN 'H1 2025'
                  WHEN month BETWEEN '2024-07-01' AND '2024-12-31' THEN 'H2 2024' END p,
             SUM(spend_usd) / NULLIF(SUM(new_customers), 0) cac
      FROM v_acquisition WHERE marketing_channel='Paid Social' AND segment='SMB' AND month >= '2024-07-01'
      GROUP BY 1 ORDER BY 1""").fetchall()
    cac = {p: v for p, v in c}
    conv = dict(con.execute("""
      SELECT CASE WHEN session_date >= '2025-07-01' THEN 'H2 2025' ELSE 'H1 2025' END,
             SUM(web_orders)/SUM(sessions) FROM v_web
      WHERE marketing_channel='Paid Social' AND segment='SMB' AND session_date >= '2025-01-01' GROUP BY 1""").fetchall())
    res["E04"] = dict(cac_paid_social_smb=cac, conversion_rate=conv,
                      cac_change_h2_vs_h1_pct=(cac["H2 2025"] / cac["H1 2025"] - 1) * 100,
                      checks=dict(cac_up=cac["H2 2025"] > 1.3 * cac["H1 2025"],
                                  conversion_down=conv["H2 2025"] < 0.85 * conv["H1 2025"]))

    # ---------------- E05 (independent PVM at product level) ----------------
    pvm = _one(con, f"""
      WITH p AS (
        SELECT product_key,
          SUM(quantity) FILTER (WHERE ship_date BETWEEN '2023-04-01' AND '2023-12-31') q0,
          SUM(net_line_amount) FILTER (WHERE ship_date BETWEEN '2023-04-01' AND '2023-12-31') r0,
          SUM(quantity) FILTER (WHERE ship_date BETWEEN '2024-04-01' AND '2024-12-31') q1,
          SUM(net_line_amount) FILTER (WHERE ship_date BETWEEN '2024-04-01' AND '2024-12-31') r1
        FROM v_sales_lines WHERE {SHIPPED} AND category='Office Supplies' GROUP BY 1),
      c AS (SELECT COALESCE(q0,0) q0, COALESCE(r0,0) r0, COALESCE(q1,0) q1, COALESCE(r1,0) r1,
                   CASE WHEN q0>0 THEN r0/q0 ELSE r1/q1 END p0, CASE WHEN q1>0 THEN r1/q1 END p1 FROM p),
      t AS (SELECT SUM(q0) Q0, SUM(q1) Q1, SUM(r0) R0, SUM(r1) R1 FROM c)
      SELECT (SELECT SUM(CASE WHEN q1>0 THEN (p1-p0)*q1 ELSE 0 END) FROM c) price,
             (t.Q1 - t.Q0) * t.R0 / t.Q0 volume,
             (SELECT SUM(q1*p0) FROM c) - t.Q1 * t.R0 / t.Q0 mix, t.R1 - t.R0 total
      FROM t""")
    ug = dict(con.execute(f"""
      SELECT category = 'Office Supplies' AS is_os,
             SUM(quantity) FILTER (WHERE ship_date BETWEEN '2024-04-01' AND '2024-12-31')
             / SUM(quantity) FILTER (WHERE ship_date BETWEEN '2023-04-01' AND '2023-12-31') - 1
      FROM v_sales_lines WHERE {SHIPPED} GROUP BY 1""").fetchall())
    res["E05"] = dict(price_effect=pvm[0], volume_effect=pvm[1], mix_effect=pvm[2], total_change=pvm[3],
                      office_supplies_unit_growth_pct=ug[True] * 100, other_categories_unit_growth_pct=ug[False] * 100,
                      checks=dict(price_positive=pvm[0] > 0, volume_lags_other_categories=ug[True] < ug[False] - 0.03,
                                  decomposition_sums=abs(pvm[0] + pvm[1] + pvm[2] - pvm[3]) < 1.0))

    # ---------------- E06 --------------------------------------------------
    m = dict((y, (s, cm)) for y, s, cm in con.execute(f"""
      SELECT year(ship_date) y,
        SUM(net_line_amount) FILTER (WHERE channel='Marketplace')/SUM(net_line_amount) AS mp_share,
        (SUM(net_line_amount - cogs_amount - net_line_amount*channel_fee_pct))/SUM(net_line_amount) cm
      FROM v_sales_lines WHERE {SHIPPED} AND segment='Mid-Market' GROUP BY 1""").fetchall())
    res["E06"] = dict(marketplace_share_2022_pct=m[2022][0] * 100, marketplace_share_2024_pct=m[2024][0] * 100,
                      contribution_margin_2022_pct=m[2022][1] * 100, contribution_margin_2024_pct=m[2024][1] * 100,
                      checks=dict(share_up=m[2024][0] > m[2022][0] + 0.1, margin_down=m[2024][1] < m[2022][1]))

    # ---------------- E07 --------------------------------------------------
    a = _one(con, f"""SELECT {NET} FILTER (WHERE ship_date BETWEEN '2025-11-01' AND '2025-11-30'),
                             {NET} FILTER (WHERE ship_date BETWEEN '2024-11-01' AND '2024-11-30'),
                             {NET} FILTER (WHERE ship_date BETWEEN '2025-09-01' AND '2025-09-30')
                      FROM v_sales_lines WHERE {SHIPPED} AND region='APAC'""")
    so = _one(con, """SELECT AVG(is_stockout::INT) FILTER (WHERE snapshot_date BETWEEN '2025-10-20' AND '2025-11-30'),
                             AVG(is_stockout::INT) FILTER (WHERE snapshot_date BETWEEN '2025-06-01' AND '2025-09-30')
                      FROM v_inventory WHERE warehouse='Singapore DC'""")
    others = _one(con, f"""SELECT {NET} FILTER (WHERE ship_date BETWEEN '2025-11-01' AND '2025-11-30')
                                 / {NET} FILTER (WHERE ship_date BETWEEN '2024-11-01' AND '2024-11-30') - 1
                          FROM v_sales_lines WHERE {SHIPPED} AND region <> 'APAC'""")[0]
    res["E07"] = dict(apac_nov_2025=a[0], apac_nov_2024=a[1], apac_yoy_pct=(a[0] / a[1] - 1) * 100,
                      other_regions_yoy_pct=others * 100,
                      singapore_stockout_rate_window_pct=so[0] * 100, singapore_stockout_rate_baseline_pct=so[1] * 100,
                      checks=dict(apac_underperforms=(a[0] / a[1] - 1) < others - 0.05, stockouts_up=so[0] > 2 * so[1]))

    # ---------------- E08 --------------------------------------------------
    d = con.execute(f"""SELECT sales_team, SUM(discount_amount)/SUM(gross_amount) dr FROM v_sales_lines
                        WHERE {SHIPPED} AND ship_date BETWEEN '2025-07-01' AND '2025-09-30' AND sales_team IS NOT NULL
                        GROUP BY 1 ORDER BY dr DESC""").fetchall()
    res["E08"] = dict(discount_rate_by_team_q3_2025={t: v * 100 for t, v in d},
                      checks=dict(east_is_top=d[0][0] == "NA Enterprise East"))

    # ---------------- E09 --------------------------------------------------
    rp = dict(con.execute("""
      WITH b AS (SELECT CASE WHEN order_date BETWEEN '2025-01-01' AND '2025-06-30' THEN 'H1 2025' ELSE 'H1 2024' END p,
                        customer_key, COUNT(DISTINCT order_key) n
                 FROM v_sales_lines WHERE category='Ergonomics' AND order_status <> 'Cancelled'
                   AND (order_date BETWEEN '2025-01-01' AND '2025-06-30' OR order_date BETWEEN '2024-01-01' AND '2024-06-30')
                 GROUP BY 1, 2)
      SELECT p, AVG((n >= 2)::INT) FROM b GROUP BY 1""").fetchall())
    res["E09"] = dict(repeat_rate_h1_2024_pct=rp["H1 2024"] * 100, repeat_rate_h1_2025_pct=rp["H1 2025"] * 100,
                      checks=dict(lift=rp["H1 2025"] > 1.15 * rp["H1 2024"]))

    # ---------------- Decoys -----------------------------------------------
    gap = _one(con, "SELECT SUM(sessions) FROM v_web WHERE session_date BETWEEN '2025-08-10' AND '2025-08-12'")[0]
    orders_gap = _one(con, "SELECT COUNT(DISTINCT order_key) FROM v_sales_lines WHERE order_date BETWEEN '2025-08-10' AND '2025-08-12'")[0]
    res["D02"] = dict(sessions_during_gap=gap, orders_during_gap=orders_gap,
                      checks=dict(sessions_zero=gap == 0, orders_normal=orders_gap > 0))
    db = _one(con, f"""SELECT SUM(net_line_amount) FILTER (WHERE order_status <> 'Cancelled' AND year(order_date)=2025),
                             {NET} FILTER (WHERE {SHIPPED} AND year(ship_date)=2025) FROM v_sales_lines""")
    res["D03"] = dict(booked_revenue_2025=db[0], net_revenue_2025=db[1], gap=db[0] - db[1],
                      checks=dict(differ=abs(db[0] - db[1]) > 0))
    con.close()

    all_checks = {f"{k}.{c}": bool(v) for k, e in res.items() for c, v in e.get("checks", {}).items()}
    res["_summary"] = dict(passed=sum(all_checks.values()), total=len(all_checks),
                           failed=[k for k, v in all_checks.items() if not v])
    out = out_path or (settings.events_yaml.parent / "realised_effects.json")
    out.write_text(json.dumps(res, indent=2, default=lambda o: float(o) if o is not None else None))
    return res

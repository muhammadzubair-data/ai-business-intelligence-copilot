-- =====================================================================
-- Analytical views used by the semantic layer (config/metrics.yaml).
-- The Copilot queries these views, never raw tables directly, unless it
-- falls back to free-form SQL (which is still guarded and read-only).
-- =====================================================================

CREATE OR REPLACE VIEW v_sales_lines AS
SELECT
    l.order_line_key, o.order_key, o.order_id, o.order_status,
    od.full_date                      AS order_date,
    sd.full_date                      AS ship_date,
    c.customer_key, c.customer_name, c.segment, c.industry, c.is_key_account,
    g.region, g.country_name          AS country,
    ch.channel_name                   AS channel, ch.channel_fee_pct::DOUBLE AS channel_fee_pct,
    st.team_name                      AS sales_team,
    p.product_key, p.product_name     AS product, p.sku, p.brand, p.subcategory,
    cat.category_name                 AS category, cat.business_unit,
    sup.supplier_name                 AS supplier,
    w.warehouse_name                  AS warehouse,
    cp.campaign_name                  AS campaign,
    l.quantity, l.list_price::DOUBLE AS list_price, l.gross_amount::DOUBLE AS gross_amount,
    l.discount_amount::DOUBLE AS discount_amount, l.net_line_amount::DOUBLE AS net_line_amount,
    l.unit_cost::DOUBLE AS unit_cost, l.cogs_amount::DOUBLE AS cogs_amount,
    COALESCE(r.refund_amount, 0)::DOUBLE AS refund_amount,
    COALESCE(r.quantity_returned, 0)  AS quantity_returned,
    COALESCE(r.defect_quantity, 0)    AS defect_quantity
FROM fact_order_lines l
JOIN fact_orders o         USING (order_key)
JOIN dim_date od           ON od.date_key = o.order_date_key
LEFT JOIN dim_date sd      ON sd.date_key = o.ship_date_key
JOIN dim_customer c        USING (customer_key)
JOIN dim_geography g       ON g.geography_key = c.geography_key
JOIN dim_channel ch        USING (channel_key)
LEFT JOIN dim_sales_team st USING (sales_team_key)
JOIN dim_product p         USING (product_key)
JOIN dim_category cat      USING (category_key)
JOIN dim_supplier sup      ON sup.supplier_key = p.primary_supplier_key
JOIN dim_warehouse w       USING (warehouse_key)
LEFT JOIN dim_campaign cp  USING (campaign_key)
LEFT JOIN (
    SELECT order_line_key,
           SUM(refund_amount) AS refund_amount,
           SUM(quantity_returned) AS quantity_returned,
           SUM(CASE WHEN return_reason = 'Defect' THEN quantity_returned ELSE 0 END) AS defect_quantity
    FROM fact_returns GROUP BY order_line_key
) r USING (order_line_key);


CREATE OR REPLACE VIEW v_customers AS
SELECT c.customer_key, c.customer_id, c.customer_name, c.segment, c.industry, c.is_key_account,
       c.acquisition_channel, c.created_date, g.region, g.country_name AS country
FROM dim_customer c JOIN dim_geography g USING (geography_key);


CREATE OR REPLACE VIEW v_marketing AS
SELECT d.full_date AS spend_date, cp.campaign_name AS campaign, cp.marketing_channel,
       cp.target_segment AS segment, cp.target_region AS region,
       m.spend_usd::DOUBLE AS spend_usd, m.impressions, m.clicks
FROM fact_marketing_spend m
JOIN dim_date d USING (date_key)
JOIN dim_campaign cp USING (campaign_key);


-- Monthly acquisition cube: spend (by campaign target) joined to new customers
-- (by acquisition channel). Organic / referral / direct-sales acquisitions carry no spend.
CREATE OR REPLACE VIEW v_acquisition AS
WITH spend AS (
    SELECT date_trunc('month', spend_date)::DATE AS month, marketing_channel, segment, region,
           SUM(spend_usd)::DOUBLE AS spend_usd
    FROM v_marketing GROUP BY ALL
), newc AS (
    SELECT date_trunc('month', created_date)::DATE AS month, acquisition_channel AS marketing_channel,
           segment, region, COUNT(*) AS new_customers
    FROM v_customers WHERE created_date >= DATE '2022-01-01' GROUP BY ALL
)
SELECT COALESCE(s.month, n.month) AS month,
       COALESCE(s.marketing_channel, n.marketing_channel) AS marketing_channel,
       COALESCE(s.segment, n.segment) AS segment,
       COALESCE(s.region, n.region) AS region,
       COALESCE(s.spend_usd, 0)::DOUBLE AS spend_usd,
       COALESCE(n.new_customers, 0) AS new_customers
FROM spend s FULL OUTER JOIN newc n
  ON s.month = n.month AND s.marketing_channel = n.marketing_channel
 AND s.segment = n.segment AND s.region = n.region;


CREATE OR REPLACE VIEW v_web AS
SELECT d.full_date AS session_date, w.marketing_channel, w.segment, g.region, g.country_name AS country,
       w.sessions, w.add_to_carts, w.checkouts_started, w.web_orders
FROM fact_web_sessions w
JOIN dim_date d USING (date_key)
JOIN dim_geography g USING (geography_key);


CREATE OR REPLACE VIEW v_tickets AS
SELECT t.ticket_key, cd.full_date AS created_date, rd.full_date AS resolved_date,
       t.contact_channel, t.ticket_category, t.priority, t.csat_score,
       c.segment, g.region, p.product_name AS product, cat.category_name AS category,
       date_diff('day', cd.full_date, rd.full_date) AS resolution_days
FROM fact_support_tickets t
JOIN dim_date cd ON cd.date_key = t.created_date_key
LEFT JOIN dim_date rd ON rd.date_key = t.resolved_date_key
JOIN dim_customer c USING (customer_key)
JOIN dim_geography g ON g.geography_key = c.geography_key
LEFT JOIN dim_product p USING (product_key)
LEFT JOIN dim_category cat ON cat.category_key = p.category_key;


CREATE OR REPLACE VIEW v_inventory AS
SELECT d.full_date AS snapshot_date, w.warehouse_name AS warehouse, g.region,
       p.product_name AS product, p.sku, cat.category_name AS category, sup.supplier_name AS supplier,
       i.on_hand_units, i.on_order_units, i.reorder_point, i.is_stockout
FROM fact_inventory_snapshot i
JOIN dim_date d ON d.date_key = i.snapshot_date_key
JOIN dim_warehouse w USING (warehouse_key)
JOIN dim_geography g ON g.geography_key = w.geography_key
JOIN dim_product p USING (product_key)
JOIN dim_category cat USING (category_key)
JOIN dim_supplier sup ON sup.supplier_key = p.primary_supplier_key;


CREATE OR REPLACE VIEW v_purchase_orders AS
SELECT po.po_key, pd.full_date AS po_date, ed.full_date AS expected_date, rd.full_date AS received_date,
       sup.supplier_name AS supplier, p.product_name AS product, cat.category_name AS category,
       w.warehouse_name AS warehouse, g.region, po.quantity, po.unit_cost::DOUBLE AS unit_cost,
       (po.quantity * po.unit_cost)::DOUBLE AS po_value,
       date_diff('day', pd.full_date, rd.full_date) AS lead_time_days,
       CASE WHEN rd.full_date IS NOT NULL AND rd.full_date > ed.full_date THEN 1 ELSE 0 END AS is_late
FROM fact_purchase_orders po
JOIN dim_date pd ON pd.date_key = po.po_date_key
JOIN dim_date ed ON ed.date_key = po.expected_date_key
LEFT JOIN dim_date rd ON rd.date_key = po.received_date_key
JOIN dim_supplier sup USING (supplier_key)
JOIN dim_product p USING (product_key)
JOIN dim_category cat ON cat.category_key = p.category_key
JOIN dim_warehouse w USING (warehouse_key)
JOIN dim_geography g ON g.geography_key = w.geography_key;


CREATE OR REPLACE VIEW v_opex AS
SELECT month_start_date AS month, department, expense_type, amount_usd::DOUBLE AS amount_usd
FROM fact_opex_monthly;

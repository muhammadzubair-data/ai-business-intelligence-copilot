# Data Dictionary

## How the warehouse is organised
The warehouse is a star schema in DuckDB. Dimension tables (dim_*) describe customers, products, dates, channels, suppliers, campaigns and warehouses. Fact tables (fact_*) record events. Analytical views (v_*) join facts to dimensions and are what reports and the Copilot query. All money is in US dollars.

## v_sales_lines
One row per order line. Key columns: order_date, ship_date, order_status (Placed, Shipped, Cancelled), customer and segment attributes, region and country, channel, sales_team (Direct Sales only), product, category, business_unit, supplier, warehouse, quantity, list_price, gross_amount, discount_amount, net_line_amount (gross minus discount), unit_cost, cogs_amount, refund_amount and quantity_returned (from returns on that line).

## v_customers
One row per customer with segment, industry, region, key-account flag, acquisition channel and created date.

## v_marketing and v_acquisition
v_marketing has daily spend, impressions and clicks per campaign, with the campaign's marketing channel, target segment and target region. v_acquisition is a monthly cube joining campaign spend to new customers by marketing channel, segment and region.

## v_web
Daily web funnel from the web analytics tool: sessions, add_to_carts, checkouts_started and web_orders by marketing channel, segment and country.

## v_tickets
One row per support ticket with created and resolved dates, ticket_category (Delivery, Billing, How-To, Return Request, Product Defect), priority, contact channel, CSAT score (1 to 5, when surveyed) and the related product when known.

## v_inventory
Weekly snapshots (Mondays) of on-hand units, units on order and reorder point for every product in every warehouse. is_stockout is true when on-hand units are zero.

## v_purchase_orders
Purchase order lines with supplier, product, warehouse, order date, expected date, received date, quantity and unit cost. is_late is 1 when an order was received after its expected date.

## v_opex
Monthly operating expenses by department (Sales, Marketing, Operations, Support, G&A, Product) and expense type (Payroll, Software, Facilities, Travel, Other Operating).

## meta_table_freshness
When each fact table was last loaded and the date range it covers. The data currently runs to 31 December 2025.

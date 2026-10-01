-- =====================================================================
-- Halden Supply Co. — Analytics Warehouse (DuckDB)
-- Project 010: AI Business Intelligence Copilot
--
-- Star schema: conformed dimensions + departmental fact tables.
-- All money is in USD. History: 2022-01-01 to 2025-12-31.
-- Grain, ownership and design decisions: docs/01_data_design.md
-- =====================================================================


-- =====================================================================
-- DIMENSIONS
-- =====================================================================

CREATE TABLE dim_date (
    date_key          INTEGER PRIMARY KEY,        -- YYYYMMDD
    full_date         DATE NOT NULL UNIQUE,
    year              SMALLINT NOT NULL,
    quarter           SMALLINT NOT NULL,
    month             SMALLINT NOT NULL,
    month_name        VARCHAR NOT NULL,
    week_start_date   DATE NOT NULL,              -- Monday of the ISO week
    iso_week          SMALLINT NOT NULL,
    day_of_week       SMALLINT NOT NULL,          -- 1 = Monday
    is_weekend        BOOLEAN NOT NULL,
    is_holiday        BOOLEAN NOT NULL,
    holiday_name      VARCHAR,
    fiscal_year       SMALLINT NOT NULL,          -- FY starts 1 Feb; named by the calendar year it ends in
    fiscal_quarter    SMALLINT NOT NULL
);

CREATE TABLE dim_geography (
    geography_key     INTEGER PRIMARY KEY,
    country_code      VARCHAR(2) NOT NULL UNIQUE,
    country_name      VARCHAR NOT NULL,
    region            VARCHAR NOT NULL            -- North America, Europe, APAC, LATAM
);

CREATE TABLE dim_channel (
    channel_key       INTEGER PRIMARY KEY,
    channel_name      VARCHAR NOT NULL UNIQUE,    -- Direct Sales, Web Store, Marketplace, Partner
    channel_fee_pct   DECIMAL(5,4) NOT NULL       -- marketplace / partner commission
);

CREATE TABLE dim_sales_team (
    sales_team_key    INTEGER PRIMARY KEY,
    team_name         VARCHAR NOT NULL UNIQUE,
    region            VARCHAR NOT NULL,
    team_lead         VARCHAR
);

CREATE TABLE dim_category (
    category_key      INTEGER PRIMARY KEY,
    category_name     VARCHAR NOT NULL UNIQUE,
    business_unit     VARCHAR NOT NULL            -- Workspace, Technology, Consumables
);

CREATE TABLE dim_supplier (
    supplier_key            INTEGER PRIMARY KEY,
    supplier_name           VARCHAR NOT NULL UNIQUE,
    supplier_country        VARCHAR NOT NULL,
    standard_lead_time_days SMALLINT NOT NULL
);

CREATE TABLE dim_product (
    product_key          INTEGER PRIMARY KEY,
    sku                  VARCHAR NOT NULL UNIQUE,
    product_name         VARCHAR NOT NULL,
    category_key         INTEGER NOT NULL REFERENCES dim_category(category_key),
    subcategory          VARCHAR NOT NULL,
    brand                VARCHAR NOT NULL,
    primary_supplier_key INTEGER NOT NULL REFERENCES dim_supplier(supplier_key),
    launch_date          DATE NOT NULL,
    discontinued_date    DATE
);

CREATE TABLE dim_campaign (
    campaign_key      INTEGER PRIMARY KEY,
    campaign_name     VARCHAR NOT NULL,
    marketing_channel VARCHAR NOT NULL,           -- Paid Search, Paid Social, Email, Events, Affiliate
    target_segment    VARCHAR,
    target_region     VARCHAR,
    start_date        DATE NOT NULL,
    end_date          DATE
);

CREATE TABLE dim_warehouse (
    warehouse_key     INTEGER PRIMARY KEY,
    warehouse_name    VARCHAR NOT NULL UNIQUE,
    geography_key     INTEGER NOT NULL REFERENCES dim_geography(geography_key)
);

CREATE TABLE dim_customer (
    customer_key           INTEGER PRIMARY KEY,
    customer_id            VARCHAR NOT NULL UNIQUE,
    customer_name          VARCHAR NOT NULL,
    segment                VARCHAR NOT NULL,      -- Enterprise, Mid-Market, SMB, Consumer
    industry               VARCHAR,               -- NULL for Consumer
    geography_key          INTEGER NOT NULL REFERENCES dim_geography(geography_key),
    acquisition_channel    VARCHAR NOT NULL,      -- how the customer first arrived
    acquisition_campaign_key INTEGER REFERENCES dim_campaign(campaign_key),
    is_key_account         BOOLEAN NOT NULL DEFAULT FALSE,
    created_date           DATE NOT NULL
);


-- =====================================================================
-- REFERENCE HISTORY (slowly changing prices and costs)
-- =====================================================================

CREATE TABLE product_price_history (
    product_key       INTEGER NOT NULL REFERENCES dim_product(product_key),
    effective_from    DATE NOT NULL,
    effective_to      DATE,                       -- NULL = current
    list_price        DECIMAL(12,2) NOT NULL,
    PRIMARY KEY (product_key, effective_from)
);

CREATE TABLE product_cost_history (
    product_key       INTEGER NOT NULL REFERENCES dim_product(product_key),
    supplier_key      INTEGER NOT NULL REFERENCES dim_supplier(supplier_key),
    effective_from    DATE NOT NULL,
    effective_to      DATE,
    unit_cost         DECIMAL(12,2) NOT NULL,
    PRIMARY KEY (product_key, supplier_key, effective_from)
);


-- =====================================================================
-- FACTS — SALES
-- =====================================================================

-- Grain: one row per order (header)
CREATE TABLE fact_orders (
    order_key         BIGINT PRIMARY KEY,
    order_id          VARCHAR NOT NULL UNIQUE,
    customer_key      INTEGER NOT NULL REFERENCES dim_customer(customer_key),
    order_date_key    INTEGER NOT NULL REFERENCES dim_date(date_key),
    ship_date_key     INTEGER REFERENCES dim_date(date_key),   -- NULL if not shipped / cancelled
    channel_key       INTEGER NOT NULL REFERENCES dim_channel(channel_key),
    sales_team_key    INTEGER REFERENCES dim_sales_team(sales_team_key), -- Direct Sales only
    warehouse_key     INTEGER NOT NULL REFERENCES dim_warehouse(warehouse_key),
    campaign_key      INTEGER REFERENCES dim_campaign(campaign_key),     -- last-touch attribution
    order_status      VARCHAR NOT NULL                          -- Placed, Shipped, Cancelled
);

-- Grain: one row per product line on an order
CREATE TABLE fact_order_lines (
    order_line_key    BIGINT PRIMARY KEY,
    order_key         BIGINT NOT NULL REFERENCES fact_orders(order_key),
    line_number       SMALLINT NOT NULL,
    product_key       INTEGER NOT NULL REFERENCES dim_product(product_key),
    quantity          INTEGER NOT NULL,
    list_price        DECIMAL(12,2) NOT NULL,     -- unit list price on order date
    discount_amount   DECIMAL(14,2) NOT NULL,     -- total line discount
    gross_amount      DECIMAL(14,2) NOT NULL,     -- quantity * list_price
    net_line_amount   DECIMAL(14,2) NOT NULL,     -- gross_amount - discount_amount
    unit_cost         DECIMAL(12,2) NOT NULL,     -- supplier cost on order date
    cogs_amount       DECIMAL(14,2) NOT NULL      -- quantity * unit_cost
);

-- Grain: one row per return event against an order line
CREATE TABLE fact_returns (
    return_key        BIGINT PRIMARY KEY,
    order_line_key    BIGINT NOT NULL REFERENCES fact_order_lines(order_line_key),
    return_date_key   INTEGER NOT NULL REFERENCES dim_date(date_key),
    quantity_returned INTEGER NOT NULL,
    refund_amount     DECIMAL(14,2) NOT NULL,
    return_reason     VARCHAR NOT NULL,           -- Defect, Wrong Item, Damaged in Transit, No Longer Needed, Other
    restocked         BOOLEAN NOT NULL
);


-- =====================================================================
-- FACTS — MARKETING
-- =====================================================================

-- Grain: campaign x day
CREATE TABLE fact_marketing_spend (
    date_key          INTEGER NOT NULL REFERENCES dim_date(date_key),
    campaign_key      INTEGER NOT NULL REFERENCES dim_campaign(campaign_key),
    spend_usd         DECIMAL(12,2) NOT NULL,
    impressions       BIGINT NOT NULL,
    clicks            BIGINT NOT NULL,
    PRIMARY KEY (date_key, campaign_key)
);

-- Grain: day x marketing channel x segment x geography (web funnel, aggregated)
CREATE TABLE fact_web_sessions (
    date_key          INTEGER NOT NULL REFERENCES dim_date(date_key),
    marketing_channel VARCHAR NOT NULL,           -- includes Organic and Direct
    segment           VARCHAR NOT NULL,
    geography_key     INTEGER NOT NULL REFERENCES dim_geography(geography_key),
    sessions          INTEGER NOT NULL,
    add_to_carts      INTEGER NOT NULL,
    checkouts_started INTEGER NOT NULL,
    web_orders        INTEGER NOT NULL,
    PRIMARY KEY (date_key, marketing_channel, segment, geography_key)
);


-- =====================================================================
-- FACTS — OPERATIONS & SUPPLY CHAIN
-- =====================================================================

-- Grain: product x warehouse x week (snapshot taken each Monday)
CREATE TABLE fact_inventory_snapshot (
    snapshot_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
    product_key       INTEGER NOT NULL REFERENCES dim_product(product_key),
    warehouse_key     INTEGER NOT NULL REFERENCES dim_warehouse(warehouse_key),
    on_hand_units     INTEGER NOT NULL,
    on_order_units    INTEGER NOT NULL,
    reorder_point     INTEGER NOT NULL,
    is_stockout       BOOLEAN NOT NULL,
    PRIMARY KEY (snapshot_date_key, product_key, warehouse_key)
);

-- Grain: one row per purchase order line
CREATE TABLE fact_purchase_orders (
    po_key            BIGINT PRIMARY KEY,
    supplier_key      INTEGER NOT NULL REFERENCES dim_supplier(supplier_key),
    product_key       INTEGER NOT NULL REFERENCES dim_product(product_key),
    warehouse_key     INTEGER NOT NULL REFERENCES dim_warehouse(warehouse_key),
    po_date_key       INTEGER NOT NULL REFERENCES dim_date(date_key),
    expected_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
    received_date_key INTEGER REFERENCES dim_date(date_key),   -- NULL = still open
    quantity          INTEGER NOT NULL,
    unit_cost         DECIMAL(12,2) NOT NULL
);


-- =====================================================================
-- FACTS — CUSTOMER SUPPORT
-- =====================================================================

-- Grain: one row per ticket
CREATE TABLE fact_support_tickets (
    ticket_key        BIGINT PRIMARY KEY,
    customer_key      INTEGER NOT NULL REFERENCES dim_customer(customer_key),
    order_key         BIGINT REFERENCES fact_orders(order_key),
    product_key       INTEGER REFERENCES dim_product(product_key),
    created_date_key  INTEGER NOT NULL REFERENCES dim_date(date_key),
    resolved_date_key INTEGER REFERENCES dim_date(date_key),
    contact_channel   VARCHAR NOT NULL,           -- Email, Chat, Phone
    ticket_category   VARCHAR NOT NULL,           -- Product Defect, Delivery, Billing, How-To, Return Request
    priority          VARCHAR NOT NULL,           -- Low, Medium, High, Urgent
    csat_score        SMALLINT                    -- 1-5, NULL if no survey response
);


-- =====================================================================
-- FACTS — FINANCE
-- =====================================================================

-- Grain: month x department x expense type
-- NOTE: depreciation and amortization are NOT broken out; they sit inside
-- 'Other Operating'. EBITDA therefore cannot be derived — by design.
CREATE TABLE fact_opex_monthly (
    month_start_date  DATE NOT NULL,
    department        VARCHAR NOT NULL,           -- Sales, Marketing, Operations, Support, G&A, Product
    expense_type      VARCHAR NOT NULL,           -- Payroll, Software, Facilities, Travel, Other Operating
    amount_usd        DECIMAL(14,2) NOT NULL,
    PRIMARY KEY (month_start_date, department, expense_type)
);


-- =====================================================================
-- METADATA (feeds the Copilot's evidence / provenance panel)
-- =====================================================================

CREATE TABLE meta_table_freshness (
    table_name        VARCHAR PRIMARY KEY,
    last_loaded_at    TIMESTAMP NOT NULL,
    min_event_date    DATE,
    max_event_date    DATE,
    row_count         BIGINT NOT NULL
);

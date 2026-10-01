"""Synthetic warehouse generator for Halden Supply Co.

Two layers:
  * a baseline business (customer lifecycles, seasonality, pricing, returns ...)
  * planted events from eval/ground_truth/planted_events.yaml layered on top

The generator is deterministic for a given seed and scale. Orders are produced
month by month and appended to DuckDB so memory stays flat at full scale.

Usage:
    python -m bi_copilot.cli generate --customers 60000      # full (~8M rows)
    python -m bi_copilot.cli generate --customers 6000       # demo (~10%)
"""
from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from ..config import DATA_END, DATA_START, settings

# ---------------------------------------------------------------------------
# Static business structure
# ---------------------------------------------------------------------------

COUNTRIES = [
    ("US", "United States", "North America", 0.30), ("CA", "Canada", "North America", 0.05),
    ("MX", "Mexico", "LATAM", 0.03),
    ("GB", "United Kingdom", "Europe", 0.08), ("DE", "Germany", "Europe", 0.08),
    ("FR", "France", "Europe", 0.06), ("NL", "Netherlands", "Europe", 0.03),
    ("ES", "Spain", "Europe", 0.03), ("IT", "Italy", "Europe", 0.03),
    ("AU", "Australia", "APAC", 0.05), ("SG", "Singapore", "APAC", 0.03),
    ("JP", "Japan", "APAC", 0.05), ("IN", "India", "APAC", 0.04),
    ("BR", "Brazil", "LATAM", 0.07), ("CL", "Chile", "LATAM", 0.02), ("CO", "Colombia", "LATAM", 0.05),
]

CHANNELS = [(1, "Direct Sales", 0.0), (2, "Web Store", 0.02), (3, "Marketplace", 0.15), (4, "Partner", 0.10)]
CH = {name: key for key, name, _ in CHANNELS}

SALES_TEAMS = [
    (1, "NA Enterprise East", "North America", "Dana Whitfield"),
    (2, "NA Enterprise West", "North America", "Marcus Lee"),
    (3, "NA Commercial", "North America", "Priya Raman"),
    (4, "EU Enterprise", "Europe", "Jonas Albrecht"),
    (5, "EU Commercial", "Europe", "Claire Dubois"),
    (6, "APAC Sales", "APAC", "Kenji Sato"),
    (7, "LATAM Sales", "LATAM", "Lucia Fernandez"),
]

WAREHOUSES = [(1, "Chicago DC", "US"), (2, "Reno DC", "US"), (3, "Rotterdam DC", "NL"),
              (4, "Singapore DC", "SG"), (5, "Sao Paulo DC", "BR")]
REGION_WH = {"North America": [1, 2], "Europe": [3], "APAC": [4], "LATAM": [5]}

# category: (business unit, subcategories, price range, cost share, qty multiplier, base return rate)
CATEGORIES = {
    "Office Furniture": ("Workspace", ["Desk", "Chair", "Storage Cabinet", "Meeting Table"], (150, 1400), 0.58, 0.35, 0.035),
    "Ergonomics": ("Workspace", ["Monitor Arm", "Footrest", "Keyboard Tray", "Lumbar Support", "Anti-Fatigue Mat"], (25, 280), 0.52, 0.7, 0.035),
    "Tech Accessories": ("Technology", ["Docking Station", "Keyboard", "Mouse", "Headset", "Webcam", "USB-C Adapter"], (12, 320), 0.60, 1.0, 0.03),
    "Print & Imaging": ("Technology", ["Laser Printer", "Toner Cartridge", "Ink Cartridge", "Document Scanner"], (25, 550), 0.66, 0.8, 0.025),
    "Office Supplies": ("Consumables", ["Copy Paper", "Gel Pens", "Notebook", "Binder", "Desk Organizer", "Sticky Notes"], (3, 55), 0.55, 2.4, 0.012),
    "Breakroom & Janitorial": ("Consumables", ["Coffee Pods", "Tea Assortment", "Surface Cleaner", "Paper Towels", "Compostable Cups"], (6, 75), 0.62, 2.0, 0.01),
}
CAT_NAMES = list(CATEGORIES)

SEGMENTS = ["Enterprise", "Mid-Market", "SMB", "Consumer"]
SEG_SHARE = {"Enterprise": 0.015, "Mid-Market": 0.08, "SMB": 0.30, "Consumer": 0.605}
SEG_CAT_WEIGHTS = {
    "Enterprise": [0.16, 0.10, 0.25, 0.12, 0.22, 0.15],
    "Mid-Market": [0.13, 0.10, 0.22, 0.12, 0.25, 0.18],
    "SMB": [0.10, 0.08, 0.20, 0.12, 0.30, 0.20],
    "Consumer": [0.12, 0.13, 0.36, 0.10, 0.24, 0.05],
}
SEG_ORDERS_PER_MONTH = {"Enterprise": 4.2, "Mid-Market": 1.5, "SMB": 0.5, "Consumer": 0.13}
SEG_LINES = {"Enterprise": 5.0, "Mid-Market": 3.2, "SMB": 1.9, "Consumer": 0.8}
SEG_QTY = {"Enterprise": 9.0, "Mid-Market": 5.0, "SMB": 2.4, "Consumer": 1.0}
SEG_DISCOUNT = {"Enterprise": 0.08, "Mid-Market": 0.06, "SMB": 0.03, "Consumer": 0.01}
SEG_LIFETIME_YEARS = {"Enterprise": 9, "Mid-Market": 6, "SMB": 3.5, "Consumer": 2.2}
# relative volume of new-customer arrivals per year (growth slows in 2025)
ARRIVAL_GROWTH = {2022: 1.00, 2023: 1.07, 2024: 1.12, 2025: 1.10}
SEG_CHANNEL_P = {  # Direct, Web, Marketplace, Partner
    "Enterprise": [0.85, 0.05, 0.0, 0.10],
    "Mid-Market": [0.45, 0.20, 0.15, 0.20],
    "SMB": [0.15, 0.45, 0.25, 0.15],
    "Consumer": [0.0, 0.60, 0.40, 0.0],
}
INDUSTRIES = ["Technology", "Financial Services", "Healthcare", "Education", "Manufacturing",
              "Retail", "Professional Services", "Government", "Media"]

PAID_CHANNELS = ["Paid Search", "Paid Social", "Email", "Events", "Affiliate"]
SEG_ACQ_CHANNELS = {
    "Enterprise": (["Direct Sales", "Events", "Referral", "Paid Search"], [0.55, 0.25, 0.15, 0.05]),
    "Mid-Market": (["Direct Sales", "Events", "Paid Search", "Referral", "Email"], [0.30, 0.20, 0.25, 0.15, 0.10]),
    "SMB": (["Paid Search", "Paid Social", "Organic", "Email", "Affiliate", "Referral"], [0.28, 0.22, 0.25, 0.08, 0.10, 0.07]),
    "Consumer": (["Paid Search", "Paid Social", "Organic", "Affiliate", "Email"], [0.25, 0.25, 0.35, 0.10, 0.05]),
}
# average acquisition cost targets used to size campaign budgets
TARGET_CAC = {"Enterprise": 4200.0, "Mid-Market": 1100.0, "SMB": 160.0, "Consumer": 38.0}

MONTH_SEASONALITY = {1: 0.86, 2: 0.95, 3: 1.04, 4: 1.00, 5: 1.00, 6: 1.01,
                     7: 0.96, 8: 0.97, 9: 1.04, 10: 1.05, 11: 1.14, 12: 1.12}

BRANDS = ["Halden", "Northpoint", "Arcwell", "Brightline", "Corvel", "Fenmore", "Linden & Co", "Quillon",
          "Stratus", "Verano", "Kestrel", "Oakridge"]
MODIFIERS = ["Pro", "Essential", "Plus", "Compact", "Elite", "Flex", "Classic", "Max", "Air", "Core"]

SUPPLIERS_BY_CAT = {
    "Office Furniture": [("Borealis Furniture Works", "Poland", 35), ("Tanaka Office Systems", "Vietnam", 42), ("Crestline Manufacturing", "United States", 21), ("Mesa Woodcraft", "Mexico", 28)],
    "Ergonomics": [("Posture Labs", "Taiwan", 30), ("Evenkeel Ergonomics", "Germany", 24), ("Crestline Manufacturing", "United States", 21)],
    "Tech Accessories": [("Kestrel Components", "China", 32), ("Volta Peripherals", "Taiwan", 28), ("Nordic Cable Co", "Sweden", 18), ("Pioneer Input Devices", "South Korea", 26)],
    "Print & Imaging": [("Imagen Supply", "Japan", 30), ("Cartridge World Partners", "United States", 14), ("Lumen Print Systems", "Germany", 25)],
    "Office Supplies": [("Paperline Mills", "Canada", 16), ("Inkstone Stationery", "India", 34), ("Clearwater Office Goods", "United States", 12), ("Atlas Paper Group", "Brazil", 20)],
    "Breakroom & Janitorial": [("Greenleaf Hospitality", "Netherlands", 18), ("Sparkle Janitorial Supply", "United States", 10), ("Highland Beverage Co", "Colombia", 22)],
}

RETURN_REASONS = ["Defect", "Wrong Item", "Damaged in Transit", "No Longer Needed", "Other"]
RETURN_REASON_P = [0.22, 0.18, 0.15, 0.35, 0.10]
TICKET_CATS = ["Delivery", "Billing", "How-To", "Return Request"]
TICKET_CAT_P = [0.40, 0.20, 0.25, 0.15]

N_PRODUCTS = 800


@dataclass
class GenConfig:
    customers: int = 6000
    seed: int = 20260930
    db_path: Path = settings.db_path
    events_enabled: bool = True


def _load_events() -> dict:
    with open(settings.events_yaml) as f:
        spec = yaml.safe_load(f)
    return {e["id"]: e for e in spec["events"]} | {d["id"]: d for d in spec["decoys"]}


def _d(x) -> date:
    if isinstance(x, date):
        return x
    return datetime.strptime(str(x), "%Y-%m-%d").date()


def _date_key(d) -> np.ndarray:
    s = pd.to_datetime(pd.Series(d))
    return (s.dt.year * 10000 + s.dt.month * 100 + s.dt.day).astype("int64").to_numpy()


def _black_friday(year: int) -> date:
    nov1 = date(year, 11, 1)
    first_thu = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    return first_thu + timedelta(days=22)


# ---------------------------------------------------------------------------
class Generator:
    def __init__(self, cfg: GenConfig):
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.seed)
        self.ev = _load_events()
        self.on = cfg.events_enabled
        self.con: duckdb.DuckDBPyConnection | None = None
        self.order_key = 1
        self.line_key = 1

    # ------------------------------------------------------------------ utils
    def log(self, msg: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    def insert(self, table: str, df: pd.DataFrame) -> None:
        self.con.register("_tmp_df", df)
        cols = ", ".join(df.columns)
        self.con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _tmp_df")
        self.con.unregister("_tmp_df")

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        t0 = time.time()
        db = Path(self.cfg.db_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        for p in [db, Path(str(db) + ".wal")]:
            if p.exists():
                p.unlink()
        self.con = duckdb.connect(str(db))
        self.con.execute("SET enable_progress_bar = false")
        self.con.execute(settings.schema_sql.read_text())
        self.log(f"Schema created at {db}")

        self.build_dimensions()
        self.build_customers()
        self.build_campaigns()
        self.build_price_cost_history()
        self.build_orders()
        self.inject_repeat_purchase_lift()
        self.build_returns_and_tickets()
        self.build_marketing_and_web()
        self.build_inventory_and_pos()
        self.build_opex()
        self.con.execute(settings.views_sql.read_text())
        self.build_freshness()
        self.con.execute("CHECKPOINT")
        self.con.close()
        self.log(f"Done in {time.time() - t0:,.0f}s")

    # ------------------------------------------------------------------ dims
    def build_dimensions(self) -> None:
        rng = self.rng
        days = pd.date_range(date(2019, 1, 1), DATA_END + timedelta(days=120), freq="D")
        holidays = {}
        for y in range(2019, 2027):
            holidays[date(y, 1, 1)] = "New Year's Day"
            holidays[date(y, 12, 25)] = "Christmas Day"
            holidays[date(y, 7, 4)] = "Independence Day (US)"
            holidays[_black_friday(y)] = "Black Friday"
            holidays[_black_friday(y) - timedelta(days=1)] = "Thanksgiving (US)"
            holidays[_black_friday(y) + timedelta(days=3)] = "Cyber Monday"
        dd = pd.DataFrame({"full_date": days.date})
        dts = pd.to_datetime(dd["full_date"])
        dd["date_key"] = _date_key(dd["full_date"])
        dd["year"] = dts.dt.year
        dd["quarter"] = dts.dt.quarter
        dd["month"] = dts.dt.month
        dd["month_name"] = dts.dt.month_name()
        dd["week_start_date"] = (dts - pd.to_timedelta(dts.dt.weekday, unit="D")).dt.date
        dd["iso_week"] = dts.dt.isocalendar().week.astype(int).to_numpy()
        dd["day_of_week"] = dts.dt.weekday + 1
        dd["is_weekend"] = dts.dt.weekday >= 5
        dd["holiday_name"] = dd["full_date"].map(holidays)
        dd["is_holiday"] = dd["holiday_name"].notna()
        # Fiscal year starts 1 Feb and is named after the calendar year it ends in
        dd["fiscal_year"] = np.where(dd["month"] >= 2, dd["year"] + 1, dd["year"])
        fm = (dd["month"] - 2) % 12
        dd["fiscal_quarter"] = fm // 3 + 1
        self.insert("dim_date", dd)

        geo = pd.DataFrame([(i + 1, c, n, r) for i, (c, n, r, _) in enumerate(COUNTRIES)],
                           columns=["geography_key", "country_code", "country_name", "region"])
        self.geo = geo
        self.insert("dim_geography", geo)
        self.insert("dim_channel", pd.DataFrame(CHANNELS, columns=["channel_key", "channel_name", "channel_fee_pct"]))
        self.insert("dim_sales_team", pd.DataFrame(SALES_TEAMS, columns=["sales_team_key", "team_name", "region", "team_lead"]))
        gk = dict(zip(geo.country_code, geo.geography_key))
        self.insert("dim_warehouse", pd.DataFrame([(k, n, gk[c]) for k, n, c in WAREHOUSES],
                                                  columns=["warehouse_key", "warehouse_name", "geography_key"]))

        cats = pd.DataFrame([(i + 1, n, v[0]) for i, (n, v) in enumerate(CATEGORIES.items())],
                            columns=["category_key", "category_name", "business_unit"])
        self.insert("dim_category", cats)

        sup_rows, seen = [], {}
        for cat, sups in SUPPLIERS_BY_CAT.items():
            for name, country, lt in sups:
                if name not in seen:
                    seen[name] = len(seen) + 1
                    sup_rows.append((seen[name], name, country, lt))
        self.suppliers = pd.DataFrame(sup_rows, columns=["supplier_key", "supplier_name", "supplier_country", "standard_lead_time_days"])
        self.insert("dim_supplier", self.suppliers)
        self.sup_key = seen

        # Products ------------------------------------------------------
        cat_counts = {"Office Furniture": 110, "Ergonomics": 100, "Tech Accessories": 180,
                      "Print & Imaging": 110, "Office Supplies": 190, "Breakroom & Janitorial": 110}
        rows, pk = [], 1
        for ci, cat in enumerate(CAT_NAMES):
            bu, subcats, (lo, hi), cost_share, _, _ = CATEGORIES[cat]
            sups = SUPPLIERS_BY_CAT[cat]
            n = cat_counts[cat]
            for j in range(n):
                sub = subcats[j % len(subcats)]
                brand = BRANDS[rng.integers(len(BRANDS))]
                mod = MODIFIERS[rng.integers(len(MODIFIERS))]
                name = f"{brand} {mod} {sub} {chr(65 + rng.integers(26))}{rng.integers(100, 999)}"
                price = float(np.exp(rng.uniform(np.log(lo), np.log(hi))))
                if cat == "Tech Accessories":  # Kestrel supplies most Tech Accessories
                    sup = sups[0][0] if rng.random() < 0.6 else sups[1 + rng.integers(len(sups) - 1)][0]
                else:
                    sup = sups[rng.integers(len(sups))][0]
                # most products existed before 2022; some launch during the history
                launch = date(2018, 1, 1) + timedelta(days=int(rng.integers(0, 1400))) if rng.random() < 0.8 \
                    else date(2022, 1, 1) + timedelta(days=int(rng.integers(0, 1400)))
                disc = None
                if rng.random() < 0.06:
                    disc = date(2023, 1, 1) + timedelta(days=int(rng.integers(0, 1000)))
                    if disc <= launch:
                        disc = None
                rows.append(dict(product_key=pk, sku=f"HS-{ci + 1}{pk:04d}", product_name=name, category_key=ci + 1,
                                 subcategory=sub, brand=brand, primary_supplier_key=seen[sup], launch_date=launch,
                                 discontinued_date=disc, base_price_=round(price, 2), cost_share_=cost_share * rng.uniform(0.9, 1.1),
                                 cat_=cat, pop_=0.0))
                pk += 1
        prod = pd.DataFrame(rows)
        # AeroDesk Pro (E03): a popular standing desk launched in Sept 2024
        aero = prod.index[(prod.cat_ == "Office Furniture") & (prod.subcategory == "Desk")][0]
        prod.loc[aero, ["product_name", "brand", "launch_date", "discontinued_date", "base_price_"]] = \
            ["Halden AeroDesk Pro Standing Desk", "Halden", _d(self.ev["E03"]["scope"]["launch_date"]), None, 649.0]
        # Zipf-like popularity within each category
        for cat in CAT_NAMES:
            idx = prod.index[prod.cat_ == cat]
            ranks = rng.permutation(len(idx)) + 1
            prod.loc[idx, "pop_"] = 1.0 / ranks ** 0.85
        prod.loc[aero, "pop_"] = prod.loc[prod.cat_ == "Office Furniture", "pop_"].max() * 1.2
        self.aero_key = int(prod.loc[aero, "product_key"])
        self.products = prod
        self.insert("dim_product", prod[["product_key", "sku", "product_name", "category_key", "subcategory", "brand",
                                         "primary_supplier_key", "launch_date", "discontinued_date"]])
        self.log(f"Dimensions: {len(prod)} products, {len(self.suppliers)} suppliers")

    # ------------------------------------------------------------------ customers
    def build_customers(self) -> None:
        rng, n = self.rng, self.cfg.customers
        seg = rng.choice(SEGMENTS, size=n, p=[SEG_SHARE[s] for s in SEGMENTS])
        w = np.array([c[3] for c in COUNTRIES]); w = w / w.sum()
        geo_idx = rng.choice(len(COUNTRIES), size=n, p=w)
        geo_key = self.geo.geography_key.to_numpy()[geo_idx]
        region = self.geo.region.to_numpy()[geo_idx]

        # Steady-state lifecycle: an initial live base at 2022-01-01 plus a stream
        # of new arrivals. For a segment with mean lifetime L years, arrivals of
        # about N0 / L per year keep the base stable; ARRIVAL_GROWTH adds growth.
        created = np.empty(n, dtype=object)
        churn = np.empty(n, dtype=object)
        years = np.array([2022, 2023, 2024, 2025])
        yw = np.array([ARRIVAL_GROWTH[y] for y in years])
        total_days = (DATA_END - DATA_START).days + 1
        for s in SEGMENTS:
            idx = np.where(seg == s)[0]
            L = SEG_LIFETIME_YEARS[s]
            n0 = int(round(len(idx) / (1 + yw.sum() / L)))
            base_idx, new_idx = idx[:n0], idx[n0:]
            # initial base: created 2018-2021, remaining life memoryless from 2022-01-01
            for i in base_idx:
                created[i] = date(2018, 1, 1) + timedelta(days=int(rng.integers(0, 1460)))
                churn[i] = DATA_START + timedelta(days=int(rng.exponential(L) * 365))
            yrs = rng.choice(years, size=len(new_idx), p=yw / yw.sum())
            for i, y in zip(new_idx, yrs):
                c = date(int(y), 1, 1) + timedelta(days=int(rng.integers(0, 365)))
                created[i] = c
                churn[i] = c + timedelta(days=int(rng.exponential(L) * 365))

        size = np.ones(n)
        ent = seg == "Enterprise"
        size[ent] = rng.lognormal(0, 0.9, ent.sum())
        mm = seg == "Mid-Market"
        size[mm] = rng.lognormal(0, 0.5, mm.sum())
        size[~(ent | mm)] = rng.lognormal(0, 0.35, (~(ent | mm)).sum())

        acq = np.empty(n, dtype=object)
        for s in SEGMENTS:
            m = seg == s
            chans, p = SEG_ACQ_CHANNELS[s]
            acq[m] = rng.choice(chans, size=m.sum(), p=p)

        cust = pd.DataFrame(dict(customer_key=np.arange(1, n + 1), segment=seg, geography_key=geo_key, region=region,
                                 created_date=created, churn_date=churn, size=size, acquisition_channel=acq))
        # key accounts: top 8% of enterprise by size
        thr = np.quantile(size[ent], 0.92) if ent.any() else np.inf
        cust["is_key_account"] = ent & (size >= thr)

        # E01: three largest EU enterprise key accounts stop ordering from 2025-04-01
        self.e01_accounts = []
        if self.on:
            e = self.ev["E01"]
            cand = cust[(cust.segment == "Enterprise") & (cust.region == "Europe") &
                        (cust.created_date < date(2023, 1, 1)) & (cust.churn_date > date(2026, 6, 1))]
            cand = cand.sort_values("size", ascending=False).head(e["parameters"]["key_accounts_lost"])
            cust.loc[cand.index, "is_key_account"] = True
            cust.loc[cand.index, "churn_date"] = _d(e["window"]["start"]) - timedelta(days=1)
            self.e01_accounts = cand.customer_key.tolist()

        # E04: fewer SMB customers acquired via Paid Social in the window (conversion decay)
        if self.on:
            e = self.ev["E04"]
            ws, we = _d(e["window"]["start"]), _d(e["window"]["end"])
            m = ((cust.segment == "SMB") & (cust.acquisition_channel == "Paid Social") &
                 (cust.created_date >= ws) & (cust.created_date <= we))
            drop = m & (rng.random(n) > e["parameters"]["conversion_rate_mult"])
            cust = cust[~drop].reset_index(drop=True)
            cust["customer_key"] = np.arange(1, len(cust) + 1)
            self.e01_accounts = cust.loc[cust.churn_date == _d(self.ev["E01"]["window"]["start"]) - timedelta(days=1)]\
                .query("segment == 'Enterprise' and region == 'Europe'").customer_key.tolist()

        n = len(cust)
        # NA enterprise accounts split between East/West teams
        cust["team_east"] = rng.random(n) < 0.5
        cust["industry"] = np.where(cust.segment == "Consumer", None, rng.choice(INDUSTRIES, size=n))
        prefix = {"Enterprise": ["Global", "United", "Pacific", "Atlas", "Summit", "Meridian", "Vertex", "Keystone"],
                  "Mid-Market": ["Harbor", "Cedar", "Pinnacle", "Riverbend", "Bluewater", "Granite", "Beacon"],
                  "SMB": ["Main Street", "Corner", "Local", "Bright", "Maple", "Oak", "Studio"]}
        suffix = {"Enterprise": ["Holdings", "Group", "Industries", "Corporation"],
                  "Mid-Market": ["Partners", "Solutions", "Logistics", "Health", "Systems"],
                  "SMB": ["Design", "Dental", "Accounting", "Cafe", "Law Office", "Agency", "Studio"]}
        names = []
        for i, s in enumerate(cust.segment):
            if s == "Consumer":
                names.append(f"Consumer {cust.customer_key[i]:06d}")
            else:
                names.append(f"{prefix[s][rng.integers(len(prefix[s]))]} {suffix[s][rng.integers(len(suffix[s]))]} {cust.customer_key[i]:05d}")
        cust["customer_name"] = names
        cust["customer_id"] = ["C" + f"{k:07d}" for k in cust.customer_key]
        self.customers = cust
        self.log(f"Customers: {n:,} (E01 accounts: {self.e01_accounts})")

    # ------------------------------------------------------------------ campaigns
    def build_campaigns(self) -> None:
        rng = self.rng
        cust = self.customers
        regions = ["North America", "Europe", "APAC", "LATAM"]
        rows, key = [], 1
        for y in range(2022, 2026):
            for ch in PAID_CHANNELS:
                segs = ["Enterprise", "Mid-Market"] if ch == "Events" else ["Mid-Market", "SMB", "Consumer"]
                for r in regions:
                    for s in segs:
                        rows.append(dict(campaign_key=key, campaign_name=f"{y} {r} {s} {ch}", marketing_channel=ch,
                                         target_segment=s, target_region=r, start_date=date(y, 1, 1), end_date=date(y, 12, 31)))
                        key += 1
        camp = pd.DataFrame(rows)
        self.campaigns = camp
        self.insert("dim_campaign", camp)
        # link acquisition campaign on customers
        cust["year"] = pd.to_datetime(cust.created_date).dt.year
        lk = camp.assign(year=pd.to_datetime(camp.start_date).dt.year)[
            ["campaign_key", "marketing_channel", "target_segment", "target_region", "year"]]
        m = cust.merge(lk, left_on=["acquisition_channel", "segment", "region", "year"],
                       right_on=["marketing_channel", "target_segment", "target_region", "year"], how="left")
        cust["acquisition_campaign_key"] = m.campaign_key.astype("Int64").to_numpy()
        out = cust[["customer_key", "customer_id", "customer_name", "segment", "industry", "geography_key",
                    "acquisition_channel", "acquisition_campaign_key", "is_key_account", "created_date"]]
        self.insert("dim_customer", out)

    # ------------------------------------------------------------------ prices & costs
    def build_price_cost_history(self) -> None:
        """List prices move each January (+2.5%) and on planted events; costs likewise."""
        prod = self.products
        e05 = self.ev["E05"]; e02 = self.ev["E02"]
        price_rows, cost_rows = [], []
        pchanges = [date(2021, 1, 1), date(2022, 1, 1), date(2023, 1, 1), date(2024, 1, 1), date(2024, 4, 1), date(2025, 1, 1)]
        cchanges = [date(2021, 1, 1), date(2022, 1, 1), date(2023, 1, 1), date(2024, 1, 1), date(2025, 1, 1), date(2025, 3, 1)]
        e05_start = _d(e05["window"]["start"]); e02_start = _d(e02["window"]["start"])
        kestrel = self.sup_key[e02["scope"]["supplier"]]
        for r in prod.itertuples():
            for i, d0 in enumerate(pchanges):
                infl = 1.025 ** max(0, d0.year - 2021)
                p = r.base_price_ * infl
                if self.on and r.cat_ == e05["scope"]["category"] and d0 >= e05_start:
                    p *= 1 + e05["parameters"]["list_price_increase_pct"]
                d1 = pchanges[i + 1] - timedelta(days=1) if i + 1 < len(pchanges) else None
                price_rows.append((r.product_key, d0, d1, round(p, 2)))
            for i, d0 in enumerate(cchanges):
                infl = 1.02 ** max(0, d0.year - 2021)
                c = r.base_price_ * r.cost_share_ * infl
                if self.on and r.primary_supplier_key == kestrel and d0 >= e02_start:
                    c *= 1 + e02["parameters"]["unit_cost_increase_pct"]
                d1 = cchanges[i + 1] - timedelta(days=1) if i + 1 < len(cchanges) else None
                cost_rows.append((r.product_key, r.primary_supplier_key, d0, d1, round(c, 2)))
        self.price_hist = pd.DataFrame(price_rows, columns=["product_key", "effective_from", "effective_to", "list_price"])
        self.cost_hist = pd.DataFrame(cost_rows, columns=["product_key", "supplier_key", "effective_from", "effective_to", "unit_cost"])
        self.insert("product_price_history", self.price_hist)
        self.insert("product_cost_history", self.cost_hist)
        self.pchanges, self.cchanges = pchanges, cchanges
        # dense lookup arrays [period_idx, product_key]
        npk = prod.product_key.max() + 1
        self.price_arr = np.zeros((len(pchanges), npk)); self.cost_arr = np.zeros((len(cchanges), npk))
        for i, d0 in enumerate(pchanges):
            s = self.price_hist[self.price_hist.effective_from == d0]
            self.price_arr[i, s.product_key] = s.list_price
        for i, d0 in enumerate(cchanges):
            s = self.cost_hist[self.cost_hist.effective_from == d0]
            self.cost_arr[i, s.product_key] = s.unit_cost

    @staticmethod
    def _period_idx(dates: np.ndarray, changes: list[date]) -> np.ndarray:
        ch = np.array(changes, dtype="datetime64[D]")
        return np.searchsorted(ch, dates.astype("datetime64[D]"), side="right") - 1

    # ------------------------------------------------------------------ orders
    def _time_factor(self, y: int, m: int) -> float:
        # macro trend: solid 2022-23, slower 2024, flat-ish 2025
        t = (y - 2022) * 12 + (m - 1)
        growth = [0.004] * 12 + [0.003] * 12 + [0.002] * 12 + [0.0005] * 12
        return float(np.prod(1 + np.array(growth[:t]))) * MONTH_SEASONALITY[m]

    def build_orders(self) -> None:
        rng, cust, prod = self.rng, self.customers, self.products
        ev = self.ev
        cust_seg = cust.segment.to_numpy()
        c_created = pd.to_datetime(cust.created_date).to_numpy().astype("datetime64[D]")
        c_churn = pd.to_datetime(cust.churn_date).to_numpy().astype("datetime64[D]")
        base_rate = np.array([SEG_ORDERS_PER_MONTH[s] for s in cust_seg]) * np.sqrt(cust["size"].to_numpy())
        seg_idx = pd.Categorical(cust_seg, categories=SEGMENTS).codes

        cat_of_prod = prod.cat_.to_numpy()
        prod_keys = prod.product_key.to_numpy()
        launch = pd.to_datetime(prod.launch_date).to_numpy().astype("datetime64[D]")
        disc = pd.to_datetime(prod.discontinued_date).to_numpy().astype("datetime64[D]")
        pop = prod.pop_.to_numpy()

        e01, e04, e05, e06, e07, e08 = (ev[k] for k in ["E01", "E04", "E05", "E06", "E07", "E08"])
        e07_ws, e07_we = np.datetime64(e07["window"]["start"]), np.datetime64(e07["window"]["end"])
        # E07 top SKUs: most popular APAC-relevant products (top popularity overall)
        self.e07_skus = prod.sort_values("pop_", ascending=False).product_key.head(e07["scope"]["top_skus_affected"]).to_numpy()
        cat_idx_of_prod = pd.Categorical(cat_of_prod, categories=CAT_NAMES).codes
        cust_region = cust.region.to_numpy()
        region_of_team = {"North America": 3, "Europe": 5, "APAC": 6, "LATAM": 7}
        total_orders = total_lines = 0

        for y in range(2022, 2026):
            for m in range(1, 13):
                ms = np.datetime64(f"{y}-{m:02d}-01")
                ndays = calendar.monthrange(y, m)[1]
                me = ms + np.timedelta64(ndays - 1, "D")
                alive = (c_created <= me) & (c_churn >= ms)
                lam = base_rate * self._time_factor(y, m) * alive
                # partial months for customers created/churned mid-month
                lam *= np.clip(((np.minimum(c_churn, me) - np.maximum(c_created, ms)).astype(int) + 1) / ndays, 0, 1)
                if self.on and date(y, m, 1) >= _d(e01["window"]["start"]) and date(y, m, 1) <= _d(e01["window"]["end"]):
                    lam[(cust_seg == "Enterprise") & (cust_region == "Europe")] *= e01["parameters"]["remaining_order_frequency_mult"]
                if self.on and date(y, m, 1) >= _d(e05["window"]["start"]):
                    pass  # volume effect applied at line level below
                n_orders = rng.poisson(lam)
                ci = np.repeat(np.arange(len(cust)), n_orders)
                if len(ci) == 0:
                    continue
                # order day: B2B weekdays heavier; consumer peaks around Black Friday
                day = rng.integers(0, ndays, size=len(ci))
                od = ms + day.astype("timedelta64[D]")
                wd = ((od.astype("datetime64[D]").view("int64") - 4) % 7)  # 0=Mon
                is_b2b = cust_seg[ci] != "Consumer"
                keep = ~(is_b2b & (wd >= 5) & (rng.random(len(ci)) < 0.75))
                if m == 11:
                    bf = np.datetime64(_black_friday(y))
                    near = np.abs((od - bf).astype(int)) <= 3
                    dup = near & ~is_b2b & (rng.random(len(ci)) < 0.8)
                    ci = np.concatenate([ci[keep], ci[dup]]); od = np.concatenate([od[keep], od[dup]])
                else:
                    ci, od = ci[keep], od[keep]
                no = len(ci)
                segs = cust_seg[ci]
                # channel
                pch = np.array([SEG_CHANNEL_P[s] for s in SEGMENTS])
                probs = pch[seg_idx[ci]].copy()
                if self.on:
                    start, end = _d(e06["window"]["start"]), date(2023, 12, 31)
                    cur = date(y, m, 15)
                    if cur >= start:
                        frac = min(1.0, (cur - start).days / max(1, (end - start).days))
                        share = e06["parameters"]["marketplace_share_from"] + frac * (
                            e06["parameters"]["marketplace_share_to"] - e06["parameters"]["marketplace_share_from"])
                        mmask = segs == "Mid-Market"
                        base = np.array(SEG_CHANNEL_P["Mid-Market"])
                        others = base.copy(); others[2] = 0
                        others = others / others.sum() * (1 - share)
                        others[2] = share
                        probs[mmask] = others
                cum = probs.cumsum(axis=1)
                ch_idx = (rng.random(no)[:, None] > cum).sum(axis=1)
                ch_idx = np.minimum(ch_idx, 3)
                channel_key = ch_idx + 1
                creg = cust_region[ci]
                team = np.full(no, -1)
                direct = channel_key == CH["Direct Sales"]
                for r, t in region_of_team.items():
                    team[direct & (creg == r)] = t
                ent_na = direct & (creg == "North America") & (segs == "Enterprise")
                team[ent_na] = np.where(cust.team_east.to_numpy()[ci[ent_na]], 1, 2)
                team[direct & (creg == "Europe") & (segs == "Enterprise")] = 4
                wh = np.array([REGION_WH[r][rng.integers(len(REGION_WH[r]))] for r in creg])
                # campaign attribution (last touch) for web/marketplace orders
                camp = np.full(no, -1)
                attr = np.isin(channel_key, [2, 3]) & (rng.random(no) < 0.3) & (segs != "Enterprise")
                if attr.any():
                    cm = self.campaigns[(pd.to_datetime(self.campaigns.start_date).dt.year == y) &
                                        (self.campaigns.marketing_channel != "Events")]
                    lk = {(r.target_region, r.target_segment): [] for r in cm.itertuples()}
                    for r in cm.itertuples():
                        lk[(r.target_region, r.target_segment)].append(r.campaign_key)
                    for i in np.where(attr)[0]:
                        opts = lk.get((creg[i], segs[i]))
                        if opts:
                            camp[i] = opts[rng.integers(len(opts))]
                # status & ship date
                cancelled = rng.random(no) < 0.02
                ship_lag = rng.integers(1, 6, size=no) + (segs == "Enterprise") * rng.integers(0, 4, size=no)
                sd = od + ship_lag.astype("timedelta64[D]")
                shipped = ~cancelled & (sd <= np.datetime64(DATA_END))
                status = np.where(cancelled, "Cancelled", np.where(shipped, "Shipped", "Placed"))
                okeys = np.arange(self.order_key, self.order_key + no)
                self.order_key += no
                orders = pd.DataFrame(dict(
                    order_key=okeys, order_id=[f"SO-{k:08d}" for k in okeys], customer_key=cust.customer_key.to_numpy()[ci],
                    order_date_key=_date_key(od), ship_date_key=np.where(shipped, _date_key(sd), np.nan),
                    channel_key=channel_key, sales_team_key=np.where(team > 0, team, np.nan), warehouse_key=wh,
                    campaign_key=np.where(camp > 0, camp, np.nan), order_status=status))
                for c in ["ship_date_key", "sales_team_key", "campaign_key"]:
                    orders[c] = orders[c].astype("Int64")

                # ---------------- lines
                nl = 1 + rng.poisson(np.array([SEG_LINES[s] for s in segs]))
                li = np.repeat(np.arange(no), nl)
                L = len(li)
                lseg = segs[li]
                cw = np.array([SEG_CAT_WEIGHTS[s] for s in SEGMENTS])[seg_idx[ci[li]]]
                cc = (rng.random(L)[:, None] > cw.cumsum(axis=1)).sum(axis=1)
                cc = np.minimum(cc, len(CAT_NAMES) - 1)
                lod = od[li]
                pk = np.empty(L, dtype=int)
                month_mid = ms + np.timedelta64(14, "D")
                avail = (launch <= me) & (np.isnat(disc) | (disc >= ms))
                for k in range(len(CAT_NAMES)):
                    sel = np.where(cc == k)[0]
                    if len(sel) == 0:
                        continue
                    cand = np.where((cat_idx_of_prod == k) & avail)[0]
                    w = pop[cand] / pop[cand].sum()
                    pk[sel] = prod_keys[rng.choice(cand, size=len(sel), p=w)]
                # products launched mid-month can't be sold before launch
                lp_idx = pk - 1
                too_early = launch[lp_idx] > lod  # product launched later in the month
                qmult = np.array([CATEGORIES[c][4] for c in CAT_NAMES])[cat_idx_of_prod[lp_idx]]
                qmean = np.array([SEG_QTY[s] for s in lseg]) * qmult * np.sqrt(cust["size"].to_numpy()[ci[li]])
                qty = np.maximum(1, rng.poisson(qmean)).astype(int)
                keep = ~too_early
                lcat = np.array(CAT_NAMES)[cat_idx_of_prod[lp_idx]]
                if self.on and date(y, m, 1) >= _d(e05["window"]["start"]):
                    keep &= ~((lcat == e05["scope"]["category"]) & (rng.random(L) < -e05["parameters"]["volume_elasticity_effect_pct"]))
                if self.on and (ms <= e07_we) and (me >= e07_ws):
                    lost = (np.isin(pk, self.e07_skus) & (creg[li] == "APAC") & (lod >= e07_ws) & (lod <= e07_we) &
                            (rng.random(L) < e07["parameters"]["lost_demand_share"]))
                    keep &= ~lost
                # prices / costs at order date
                pidx = self._period_idx(lod, self.pchanges); cidx = self._period_idx(lod, self.cchanges)
                list_price = self.price_arr[pidx, pk]
                unit_cost = self.cost_arr[cidx, pk]
                # discounts
                base_disc = np.array([SEG_DISCOUNT[s] for s in lseg])
                lch = channel_key[li]
                dr = base_disc + (lch == 3) * 0.03 + rng.normal(0, 0.015, L)
                lmon = m
                if lmon in (11, 12):
                    dr += np.isin(lseg, ["Consumer", "SMB"]) * 0.05
                    bf = np.datetime64(_black_friday(y))
                    dr += (np.abs((lod - bf).astype(int)) <= 3) * np.isin(lseg, ["Consumer", "SMB"]) * 0.08
                if self.on:
                    ws, we = np.datetime64(e08["window"]["start"]), np.datetime64(e08["window"]["end"])
                    east = (team[li] == 1) & (lod >= ws) & (lod <= we)
                    dr = np.where(east, e08["parameters"]["avg_discount_to"] + rng.normal(0, 0.03, L), dr)
                dr = np.clip(dr, 0, 0.5)
                gross = np.round(qty * list_price, 2)
                disc_amt = np.round(gross * dr, 2)
                lines = pd.DataFrame(dict(order_key=okeys[li], product_key=pk, quantity=qty, list_price=np.round(list_price, 2),
                                          discount_amount=disc_amt, gross_amount=gross, net_line_amount=np.round(gross - disc_amt, 2),
                                          unit_cost=np.round(unit_cost, 2), cogs_amount=np.round(qty * unit_cost, 2)))[keep]
                lines["line_number"] = lines.groupby("order_key").cumcount() + 1
                lines.insert(0, "order_line_key", np.arange(self.line_key, self.line_key + len(lines)))
                self.line_key += len(lines)
                orders = orders[orders.order_key.isin(lines.order_key.unique())]
                self.insert("fact_orders", orders)
                self.insert("fact_order_lines", lines)
                total_orders += len(orders); total_lines += len(lines)
            self.log(f"Orders {y}: cumulative {total_orders:,} orders / {total_lines:,} lines")

    # ------------------------------------------------------------------ E09
    def inject_repeat_purchase_lift(self) -> None:
        if not self.on:
            return
        e = self.ev["E09"]
        ws, we = _d(e["window"]["start"]), _d(e["window"]["end"])
        con, rng = self.con, self.rng
        cat_key = CAT_NAMES.index(e["scope"]["category"]) + 1
        buyers = con.execute(f"""
            SELECT o.customer_key, COUNT(DISTINCT o.order_key) n, MIN(d.full_date) first_date,
                   ANY_VALUE(o.channel_key) channel_key, ANY_VALUE(o.warehouse_key) warehouse_key,
                   ANY_VALUE(o.sales_team_key) sales_team_key
            FROM fact_orders o JOIN fact_order_lines l USING(order_key) JOIN dim_product p USING(product_key)
            JOIN dim_date d ON d.date_key = o.order_date_key
            WHERE p.category_key = {cat_key} AND d.full_date BETWEEN DATE '{ws}' AND DATE '{we}' AND o.order_status <> 'Cancelled'
            GROUP BY 1""").df()
        if buyers.empty:
            return
        r0 = (buyers.n >= 2).mean()
        target = min(0.95, r0 * e["parameters"]["repeat_purchase_rate_mult"])
        p = (target - r0) / max(1e-9, (1 - r0))
        single = buyers[buyers.n == 1]
        pick = single[rng.random(len(single)) < p]
        prods = self.products[self.products.cat_ == e["scope"]["category"]]
        prods = prods[pd.to_datetime(prods.launch_date) <= pd.Timestamp(ws)]
        w = (prods.pop_ / prods.pop_.sum()).to_numpy()
        rows_o, rows_l = [], []
        for r in pick.itertuples():
            start = max(pd.Timestamp(r.first_date).date() + timedelta(days=10), ws)
            if start >= we:
                continue
            od = start + timedelta(days=int(rng.integers(0, (we - start).days + 1)))
            sd = od + timedelta(days=int(rng.integers(1, 6)))
            ok = self.order_key; self.order_key += 1
            rows_o.append(dict(order_key=ok, order_id=f"SO-{ok:08d}", customer_key=r.customer_key,
                               order_date_key=int(od.strftime("%Y%m%d")), ship_date_key=int(sd.strftime("%Y%m%d")),
                               channel_key=r.channel_key, sales_team_key=r.sales_team_key, warehouse_key=r.warehouse_key,
                               campaign_key=None, order_status="Shipped"))
            pk = int(rng.choice(prods.product_key.to_numpy(), p=w))
            pi = self._period_idx(np.array([od], dtype="datetime64[D]"), self.pchanges)[0]
            ci = self._period_idx(np.array([od], dtype="datetime64[D]"), self.cchanges)[0]
            q = int(max(1, rng.poisson(2)))
            lp, uc = self.price_arr[pi, pk], self.cost_arr[ci, pk]
            g = round(q * lp, 2); dsc = round(g * 0.03, 2)
            rows_l.append(dict(order_line_key=self.line_key, order_key=ok, line_number=1, product_key=pk, quantity=q,
                               list_price=round(lp, 2), discount_amount=dsc, gross_amount=g, net_line_amount=round(g - dsc, 2),
                               unit_cost=round(uc, 2), cogs_amount=round(q * uc, 2)))
            self.line_key += 1
        if rows_o:
            o = pd.DataFrame(rows_o)
            for c in ["sales_team_key", "campaign_key"]:
                o[c] = o[c].astype("Int64")
            self.insert("fact_orders", o)
            self.insert("fact_order_lines", pd.DataFrame(rows_l))
        self.log(f"E09: repeat rate baseline {r0:.3f} -> target {target:.3f}; added {len(rows_o)} repeat orders")

    # ------------------------------------------------------------------ returns & tickets
    def build_returns_and_tickets(self) -> None:
        con, rng = self.con, self.rng
        e03 = self.ev["E03"]
        ws, we = np.datetime64(e03["window"]["start"]), np.datetime64(e03["window"]["end"])
        cat_rr = {c: v[5] for c, v in CATEGORIES.items()}
        lines = con.execute("""
            SELECT l.order_line_key, l.order_key, l.product_key, l.quantity, l.net_line_amount, c.category_name,
                   d.full_date ship_date, o.customer_key
            FROM fact_order_lines l JOIN fact_orders o USING(order_key)
            JOIN dim_product p USING(product_key) JOIN dim_category c USING(category_key)
            JOIN dim_date d ON d.date_key = o.ship_date_key
            WHERE o.order_status = 'Shipped'""").df()
        L = len(lines)
        sd = pd.to_datetime(lines.ship_date).to_numpy().astype("datetime64[D]")
        rr = lines.category_name.map(cat_rr).to_numpy()
        is_aero = lines.product_key.to_numpy() == self.aero_key
        in_win = (sd >= ws) & (sd <= we)
        if self.on:
            rr = np.where(is_aero & in_win, e03["parameters"]["return_rate"], rr)
        ret = rng.random(L) < rr
        reason = rng.choice(RETURN_REASONS, size=L, p=RETURN_REASON_P)
        if self.on:
            defect_boost = is_aero & in_win & (rng.random(L) < 0.8)
            reason = np.where(defect_boost, e03["parameters"]["dominant_return_reason"], reason)
        qty = lines.quantity.to_numpy()
        qr = np.where(rng.random(L) < 0.7, qty, np.maximum(1, (qty * rng.uniform(0.2, 0.9, L)).astype(int)))
        rdate = sd + rng.integers(3, 31, L).astype("timedelta64[D]")
        ok = ret & (rdate <= np.datetime64(DATA_END))
        r = lines[ok].copy()
        r["quantity_returned"] = qr[ok]
        r["refund_amount"] = np.round(r.net_line_amount * r.quantity_returned / r.quantity, 2)
        r["return_reason"] = reason[ok]
        r["return_date_key"] = _date_key(rdate[ok])
        r["restocked"] = np.where(r.return_reason == "Defect", False, rng.random(len(r)) < 0.7)
        r["return_key"] = np.arange(1, len(r) + 1)
        self.insert("fact_returns", r[["return_key", "order_line_key", "return_date_key", "quantity_returned",
                                       "refund_amount", "return_reason", "restocked"]])
        self.log(f"Returns: {len(r):,}")

        # tickets: defect returns -> Product Defect tickets; plus general tickets per order
        tickets = []
        dr = r[r.return_reason == "Defect"]
        dmask = rng.random(len(dr)) < 0.8
        dr = dr[dmask]
        tickets.append(pd.DataFrame(dict(customer_key=dr.customer_key, order_key=dr.order_key, product_key=dr.product_key,
                                         created=pd.to_datetime(dr.ship_date) + pd.to_timedelta(rng.integers(2, 20, len(dr)), "D"),
                                         ticket_category="Product Defect", aero=(dr.product_key == self.aero_key).to_numpy())))
        if self.on:
            # E03: many AeroDesk buyers complain without returning the desk
            comp = lines[is_aero & in_win & ~ok]
            comp = comp[rng.random(len(comp)) < min(0.9, 0.12 * e03["parameters"]["defect_ticket_mult"])]
            tickets.append(pd.DataFrame(dict(customer_key=comp.customer_key, order_key=comp.order_key, product_key=comp.product_key,
                                             created=pd.to_datetime(comp.ship_date) + pd.to_timedelta(rng.integers(2, 25, len(comp)), "D"),
                                             ticket_category="Product Defect", aero=True)))
        orders = con.execute("""SELECT o.order_key, o.customer_key, d.full_date od, c.segment FROM fact_orders o
                                JOIN dim_date d ON d.date_key=o.order_date_key JOIN dim_customer c USING(customer_key)""").df()
        p = np.where(orders.segment == "Consumer", 0.06, 0.09)
        pick = orders[rng.random(len(orders)) < p]
        tickets.append(pd.DataFrame(dict(customer_key=pick.customer_key, order_key=pick.order_key, product_key=None,
                                         created=pd.to_datetime(pick.od) + pd.to_timedelta(rng.integers(0, 15, len(pick)), "D"),
                                         ticket_category=rng.choice(TICKET_CATS, size=len(pick), p=TICKET_CAT_P), aero=False)))
        t = pd.concat(tickets, ignore_index=True)
        t = t[t.created <= pd.Timestamp(DATA_END)].reset_index(drop=True)
        n = len(t)
        t["ticket_key"] = np.arange(1, n + 1)
        t["created_date_key"] = _date_key(t.created)
        res = t.created + pd.to_timedelta(np.round(rng.gamma(1.6, 1.4, n)).astype(int), "D")
        t["resolved_date_key"] = pd.array(np.where(res <= pd.Timestamp(DATA_END), _date_key(res), np.nan), dtype="Int64")
        t["contact_channel"] = rng.choice(["Email", "Chat", "Phone"], size=n, p=[0.45, 0.35, 0.20])
        t["priority"] = np.where(t.ticket_category == "Product Defect", rng.choice(["Medium", "High", "Urgent"], size=n, p=[0.4, 0.45, 0.15]),
                                 rng.choice(["Low", "Medium", "High"], size=n, p=[0.45, 0.45, 0.10]))
        base = np.where(t.ticket_category == "Product Defect", 3.3, 4.2) + rng.normal(0, 0.8, n)
        if self.on:
            base = base - t.aero.astype(bool).to_numpy() * e03["parameters"]["csat_drop"]
        csat = np.clip(np.round(base), 1, 5)
        t["csat_score"] = pd.array(np.where(rng.random(n) < 0.35, csat, np.nan), dtype="Int64")
        t["product_key"] = t["product_key"].astype("Int64")
        self.insert("fact_support_tickets", t[["ticket_key", "customer_key", "order_key", "product_key", "created_date_key",
                                               "resolved_date_key", "contact_channel", "ticket_category", "priority", "csat_score"]])
        self.log(f"Tickets: {n:,}")

    # ------------------------------------------------------------------ marketing & web
    def build_marketing_and_web(self) -> None:
        rng, con = self.rng, self.con
        cust = self.customers
        days = pd.date_range(DATA_START, DATA_END, freq="D")
        month_season = np.array([MONTH_SEASONALITY[m] for m in days.month])
        # budgets sized from targeted CAC x customers acquired per campaign-year
        acq = cust[cust.acquisition_campaign_key.notna()].groupby("acquisition_campaign_key").size()
        e04, d01 = self.ev["E04"], self.ev["D01"]
        rows = []
        for c in self.campaigns.itertuples():
            n_new = acq.get(c.campaign_key, 0)
            budget = max(n_new, 3) * TARGET_CAC[c.target_segment] * rng.uniform(0.8, 1.2)
            yr = days.year == c.start_date.year
            d = days[yr]
            w = month_season[yr] * rng.lognormal(0, 0.25, yr.sum())
            spend = budget * w / w.sum()
            if self.on and c.marketing_channel == e04["scope"]["marketing_channel"] and c.target_segment == e04["scope"]["segment"]:
                win = (d >= pd.Timestamp(e04["window"]["start"])) & (d <= pd.Timestamp(e04["window"]["end"]))
                # E04: spend rises to 1.25x the normal run-rate even though conversions dropped.
                # The yearly budget was sized on a reduced customer count (H2 drop), so rescale.
                reduced = 1 - 0.5 * (1 - e04["parameters"]["conversion_rate_mult"])
                spend = np.where(win, spend * e04["parameters"]["spend_mult"] / reduced, spend)
            if self.on and c.target_region == "LATAM":
                win = (d >= pd.Timestamp(d01["window"]["start"])) & (d <= pd.Timestamp(d01["window"]["end"]))
                spend = np.where(win, spend * 1.4, spend)
            cpm = {"Paid Search": 9, "Paid Social": 6, "Email": 1.5, "Events": 60, "Affiliate": 5}[c.marketing_channel]
            impressions = (spend / cpm * 1000).astype(int)
            ctr = {"Paid Search": 0.035, "Paid Social": 0.012, "Email": 0.025, "Events": 0.05, "Affiliate": 0.015}[c.marketing_channel]
            clicks = (impressions * ctr * rng.uniform(0.85, 1.15, len(d))).astype(int)
            rows.append(pd.DataFrame(dict(date_key=_date_key(d.date), campaign_key=c.campaign_key, spend_usd=np.round(spend, 2),
                                          impressions=impressions, clicks=clicks)))
        ms = pd.concat(rows, ignore_index=True)
        self.insert("fact_marketing_spend", ms)
        self.log(f"Marketing spend rows: {len(ms):,}")

        # web funnel: day x channel x segment x country
        chans = ["Paid Search", "Paid Social", "Email", "Affiliate", "Organic", "Direct"]
        base_sess = {"Paid Search": 60, "Paid Social": 45, "Email": 15, "Affiliate": 12, "Organic": 90, "Direct": 50}
        conv = {"Paid Search": 0.028, "Paid Social": 0.016, "Email": 0.035, "Affiliate": 0.022, "Organic": 0.025, "Direct": 0.04}
        segs = {"SMB": 0.45, "Consumer": 1.0, "Mid-Market": 0.12}
        scale = self.cfg.customers / 6000
        frames = []
        t_index = np.arange(len(days))
        trend = 1 + t_index / len(days) * 0.35
        for ch in chans:
            for s, sw in segs.items():
                for g in self.geo.itertuples():
                    cw = next(c[3] for c in COUNTRIES if c[0] == g.country_code)
                    lam = base_sess[ch] * sw * cw * 10 * scale * trend * month_season
                    sessions = rng.poisson(lam)
                    cr = conv[ch] * (1.4 if s == "Mid-Market" else 1.0) * rng.lognormal(0, 0.1, len(days))
                    if self.on and ch == e04["scope"]["marketing_channel"] and s == e04["scope"]["segment"]:
                        win = (days >= pd.Timestamp(e04["window"]["start"])) & (days <= pd.Timestamp(e04["window"]["end"]))
                        cr = np.where(win, cr * e04["parameters"]["conversion_rate_mult"], cr)
                    atc = rng.binomial(sessions, np.clip(cr * 4, 0, 1))
                    chk = rng.binomial(atc, 0.55)
                    orders = rng.binomial(chk, np.clip(cr / (np.clip(cr * 4, 1e-9, 1) * 0.55), 0, 1))
                    frames.append(pd.DataFrame(dict(date_key=_date_key(days.date), marketing_channel=ch, segment=s,
                                                    geography_key=g.geography_key, sessions=sessions, add_to_carts=atc,
                                                    checkouts_started=chk, web_orders=orders)))
        web = pd.concat(frames, ignore_index=True)
        if self.on:
            d02 = self.ev["D02"]
            ks, ke = int(str(d02["window"]["start"]).replace("-", "")), int(str(d02["window"]["end"]).replace("-", ""))
            gap = (web.date_key >= ks) & (web.date_key <= ke)
            web.loc[gap, ["sessions", "add_to_carts", "checkouts_started", "web_orders"]] = 0
        self.insert("fact_web_sessions", web)
        self.log(f"Web session rows: {len(web):,}")

    # ------------------------------------------------------------------ inventory & POs
    def build_inventory_and_pos(self) -> None:
        rng, prod = self.rng, self.products
        e07 = self.ev["E07"]
        mondays = pd.date_range("2022-01-03", DATA_END, freq="W-MON")
        P = len(prod); W = len(WAREHOUSES); T = len(mondays)
        pk = np.tile(np.repeat(prod.product_key.to_numpy(), W), T)
        wk = np.tile(np.tile([w[0] for w in WAREHOUSES], P), T)
        dt = np.repeat(mondays.to_numpy().astype("datetime64[D]"), P * W)
        pop = np.tile(np.repeat(prod.pop_.to_numpy(), W), T)
        launch = np.tile(np.repeat(pd.to_datetime(prod.launch_date).to_numpy().astype("datetime64[D]"), W), T)
        disc = np.tile(np.repeat(pd.to_datetime(prod.discontinued_date).to_numpy().astype("datetime64[D]"), W), T)
        live = (launch <= dt) & (np.isnat(disc) | (disc >= dt))
        wh_scale = np.select([wk <= 2, wk == 3], [0.8, 0.6], 0.35)
        rop = np.maximum(5, (pop * 400 * wh_scale * (self.cfg.customers / 6000) ** 0.5).astype(int))
        on_hand = (rop * rng.uniform(0.7, 3.0, len(pk))).astype(int)
        out = rng.random(len(pk)) < 0.012
        if self.on:
            ws, we = np.datetime64(e07["window"]["start"]), np.datetime64(e07["window"]["end"])
            e07_mask = np.isin(pk, self.e07_skus) & (wk == 4) & (dt >= ws - np.timedelta64(6, "D")) & (dt <= we)
            out |= e07_mask
        on_hand = np.where(out, 0, on_hand)
        on_order = np.where(on_hand < rop, (rop * rng.uniform(1, 2.5, len(pk))).astype(int), 0)
        inv = pd.DataFrame(dict(snapshot_date_key=_date_key(dt), product_key=pk, warehouse_key=wk, on_hand_units=on_hand,
                                on_order_units=on_order, reorder_point=rop, is_stockout=on_hand == 0))[live]
        self.insert("fact_inventory_snapshot", inv)
        self.log(f"Inventory snapshot rows: {len(inv):,}")

        # purchase orders roughly every 4 weeks per product x warehouse
        lt_std = self.suppliers.set_index("supplier_key").standard_lead_time_days
        po_mask = live & (rng.random(len(pk)) < 0.25)
        po = pd.DataFrame(dict(product_key=pk[po_mask], warehouse_key=wk[po_mask], po_date=dt[po_mask]))
        po["supplier_key"] = prod.set_index("product_key").loc[po.product_key, "primary_supplier_key"].to_numpy()
        lt = lt_std.loc[po.supplier_key].to_numpy()
        po["expected"] = po.po_date + pd.to_timedelta(lt, "D")
        actual = lt * rng.lognormal(0.03, 0.12, len(po))
        if self.on:
            late = (po.product_key.isin(self.e07_skus) & (po.warehouse_key == 4) &
                    (po.po_date >= pd.Timestamp("2025-09-01")) & (po.po_date <= pd.Timestamp("2025-11-15"))).to_numpy()
            actual = np.where(late, actual * e07["parameters"]["supplier_lead_time_mult"], actual)
        po["received"] = po.po_date + pd.to_timedelta(np.round(actual).astype(int), "D")
        po["quantity"] = (prod.set_index("product_key").loc[po.product_key, "pop_"].to_numpy() * 900 *
                          rng.uniform(0.6, 1.4, len(po))).astype(int) + 10
        cidx = self._period_idx(po.po_date.to_numpy().astype("datetime64[D]"), self.cchanges)
        po["unit_cost"] = np.round(self.cost_arr[cidx, po.product_key.to_numpy()] * 0.97, 2)
        po["po_key"] = np.arange(1, len(po) + 1)
        po["po_date_key"] = _date_key(po.po_date)
        po["expected_date_key"] = _date_key(po.expected)
        po["received_date_key"] = pd.array(np.where(po.received <= pd.Timestamp(DATA_END), _date_key(po.received), np.nan), dtype="Int64")
        # make sure every referenced date exists in dim_date
        self.insert("fact_purchase_orders", po[["po_key", "supplier_key", "product_key", "warehouse_key", "po_date_key",
                                                "expected_date_key", "received_date_key", "quantity", "unit_cost"]])
        self.log(f"Purchase orders: {len(po):,}")

    # ------------------------------------------------------------------ opex
    def build_opex(self) -> None:
        rng = self.rng
        scale = self.cfg.customers / 6000
        base = {  # monthly USD at demo scale
            "Sales": {"Payroll": 210_000, "Software": 18_000, "Facilities": 12_000, "Travel": 22_000, "Other Operating": 15_000},
            "Marketing": {"Payroll": 95_000, "Software": 14_000, "Facilities": 6_000, "Travel": 7_000, "Other Operating": 9_000},
            "Operations": {"Payroll": 180_000, "Software": 16_000, "Facilities": 95_000, "Travel": 4_000, "Other Operating": 60_000},
            "Support": {"Payroll": 85_000, "Software": 11_000, "Facilities": 8_000, "Travel": 1_000, "Other Operating": 4_000},
            "G&A": {"Payroll": 120_000, "Software": 20_000, "Facilities": 30_000, "Travel": 6_000, "Other Operating": 45_000},
            "Product": {"Payroll": 70_000, "Software": 12_000, "Facilities": 5_000, "Travel": 3_000, "Other Operating": 6_000},
        }
        rows = []
        for i, ms in enumerate(pd.date_range(DATA_START, DATA_END, freq="MS")):
            g = 1.004 ** i
            for dept, types in base.items():
                for t, v in types.items():
                    rows.append((ms.date(), dept, t, round(v * 0.68 * scale * g * rng.uniform(0.94, 1.06), 2)))
        self.insert("fact_opex_monthly", pd.DataFrame(rows, columns=["month_start_date", "department", "expense_type", "amount_usd"]))

    # ------------------------------------------------------------------ metadata
    def build_freshness(self) -> None:
        loaded = datetime(2026, 1, 2, 6, 0, 0)
        spec = {
            "fact_orders": "SELECT MIN(d.full_date), MAX(d.full_date), COUNT(*) FROM fact_orders o JOIN dim_date d ON d.date_key=o.order_date_key",
            "fact_order_lines": "SELECT MIN(d.full_date), MAX(d.full_date), COUNT(*) FROM fact_order_lines l JOIN fact_orders o USING(order_key) JOIN dim_date d ON d.date_key=o.order_date_key",
            "fact_returns": "SELECT MIN(d.full_date), MAX(d.full_date), COUNT(*) FROM fact_returns r JOIN dim_date d ON d.date_key=r.return_date_key",
            "fact_marketing_spend": "SELECT MIN(d.full_date), MAX(d.full_date), COUNT(*) FROM fact_marketing_spend m JOIN dim_date d USING(date_key)",
            "fact_web_sessions": "SELECT MIN(d.full_date), MAX(d.full_date), COUNT(*) FROM fact_web_sessions w JOIN dim_date d USING(date_key)",
            "fact_inventory_snapshot": "SELECT MIN(d.full_date), MAX(d.full_date), COUNT(*) FROM fact_inventory_snapshot i JOIN dim_date d ON d.date_key=i.snapshot_date_key",
            "fact_purchase_orders": "SELECT MIN(d.full_date), MAX(d.full_date), COUNT(*) FROM fact_purchase_orders p JOIN dim_date d ON d.date_key=p.po_date_key",
            "fact_support_tickets": "SELECT MIN(d.full_date), MAX(d.full_date), COUNT(*) FROM fact_support_tickets t JOIN dim_date d ON d.date_key=t.created_date_key",
            "fact_opex_monthly": "SELECT MIN(month_start_date), MAX(month_start_date), COUNT(*) FROM fact_opex_monthly",
        }
        rows = []
        for t, q in spec.items():
            mn, mx, n = self.con.execute(q).fetchone()
            rows.append((t, loaded, mn, mx, n))
        self.insert("meta_table_freshness", pd.DataFrame(rows, columns=["table_name", "last_loaded_at", "min_event_date", "max_event_date", "row_count"]))


def generate(customers: int = 6000, seed: int = 20260930, db_path: Path | None = None, events: bool = True) -> Path:
    cfg = GenConfig(customers=customers, seed=seed, db_path=db_path or settings.db_path, events_enabled=events)
    Generator(cfg).run()
    return Path(cfg.db_path)

# Data Incident Log

This log records known data-quality incidents. Analysts should check it before reporting a sudden change in a metric.

## INC-2023-014 — Late order load (June 2023)
On 14 June 2023 the nightly order load failed and orders for 13 June arrived a day late. The data was backfilled on 15 June. Historical figures are complete; only same-day reports on 14 June were affected.

## INC-2024-006 — Duplicate campaign names (March 2024)
Two campaigns were briefly created with the same name in the marketing platform. Spend was assigned to the correct campaign keys and totals are unaffected. Reports grouping by campaign name for March 2024 should use campaign key instead.

## INC-2025-021 — Web analytics tracking outage (10–12 August 2025)
A release of the website on 10 August 2025 removed the analytics tracking tag from all pages. Web sessions, add-to-carts, checkouts and web orders were not recorded from 10 to 12 August 2025, so these days show zero traffic in the web funnel data. Orders in the order system were not affected; customers could shop normally. The tag was restored on 13 August. Do not interpret zero sessions or conversion for these dates as a business problem; exclude the dates or use order-system data.

## INC-2025-030 — Warehouse snapshot delay (September 2025)
The inventory snapshot for 1 September 2025 was captured a few hours late. Values are correct.

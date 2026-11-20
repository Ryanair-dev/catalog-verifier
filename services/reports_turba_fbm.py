"""
Reports: Turba merchant-fulfilled SKUs ending in -FBMTRB, with or without a trailing
number (-FBMTRB, -FBMTRB2, ...), that sell at a profit while
Amazon has none of that ASIN in Turba's FBA stock -- i.e. FBM is carrying the
sales the FBA listing can't.

Sources (checked live 2026-11-20):
- Sales: analytics.order_pl_transaction_items (SellerCloud's line-level P&L,
  refreshed daily). Turba = company_name 'Turba'; channel 'Amazon' = merchant-
  fulfilled ('FBA' = Amazon-fulfilled); transaction_type 'Debit' = sales
  (refunds are 'Credit'). Lines appear once the order ships.
  Profit = sales_amount - item_cost - commission (Amazon referral fee; SellerCloud's
  estimate until settlement) - shipping_cost (label we paid) -- equals the table's
  own profit column on every row checked.
- FBA stock: amazon.amazon_fulfilled_by_amazon_manage_inventory, latest Turba
  snapshot, US marketplace only (every SKU also has a zero-qty Canada row),
  fulfillable qty summed per ASIN (inbound not counted). An ASIN with no Turba
  FBA SKU at all counts as 0.
"""
from __future__ import annotations

from services import azure_sql

_SQL = """
WITH sales AS (
    SELECT pl.product_id AS sku, MAX(pl.asin) AS asin,
           SUM(CASE WHEN pl.order_date >= DATEADD(DAY, -7, CAST(GETDATE() AS date)) THEN pl.qty ELSE 0 END) AS units_7d,
           SUM(CASE WHEN pl.order_date >= DATEADD(DAY, -7, CAST(GETDATE() AS date)) THEN pl.sales_amount ELSE 0 END) AS revenue_7d,
           SUM(CASE WHEN pl.order_date >= DATEADD(DAY, -7, CAST(GETDATE() AS date))
                    THEN pl.sales_amount - pl.item_cost - pl.commission - pl.shipping_cost ELSE 0 END) AS profit_7d,
           SUM(pl.qty) AS units_30d,
           SUM(pl.sales_amount) AS revenue_30d,
           SUM(pl.sales_amount - pl.item_cost - pl.commission - pl.shipping_cost) AS profit_30d
    FROM analytics.order_pl_transaction_items AS pl
    WHERE pl.company_name = 'Turba' AND pl.channel = 'Amazon' AND pl.transaction_type = 'Debit'
      AND pl.product_id LIKE '%-FBMTRB%' AND pl.product_id NOT LIKE '%-FBMTRB%[^0-9]%'   -- only digits (or nothing) after FBMTRB
      AND pl.order_date >= DATEADD(DAY, -30, CAST(GETDATE() AS date))
    GROUP BY pl.product_id
),
snap AS (
    SELECT MAX(dte) AS dte FROM amazon.amazon_fulfilled_by_amazon_manage_inventory WHERE store_name = 'Turba'
),
fba AS (
    SELECT inv.asin, SUM(inv.fulfilled_by_amazon_fulfillable_quantity) AS fba_available
    FROM amazon.amazon_fulfilled_by_amazon_manage_inventory AS inv
    JOIN snap ON inv.dte = snap.dte
    WHERE inv.store_name = 'Turba' AND inv.marketplace_id = 'ATVPDKIKX0DER'
    GROUP BY inv.asin
)
SELECT s.sku, s.asin, s.units_7d, s.revenue_7d, s.profit_7d, s.units_30d, s.revenue_30d, s.profit_30d,
       ISNULL(f.fba_available, 0) AS fba_available, (SELECT dte FROM snap) AS fba_snapshot
FROM sales AS s
LEFT JOIN fba AS f ON f.asin = s.asin
WHERE s.profit_30d > 0 AND ISNULL(f.fba_available, 0) = 0
ORDER BY s.profit_30d DESC
"""


def get_turba_fbm() -> dict:
    """{"rows": [...], "summary": {...}, "fba_snapshot": "YYYY-MM-DD" | None}"""
    conn = azure_sql._connect()
    try:
        cur = conn.cursor()
        cur.execute(_SQL)
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    snapshot = raw[0]["fba_snapshot"] if raw else None
    rows = [{
        "sku": r["sku"], "asin": r["asin"],
        "units_7d": int(r["units_7d"] or 0), "revenue_7d": round(float(r["revenue_7d"] or 0), 2),
        "profit_7d": round(float(r["profit_7d"] or 0), 2),
        "units_30d": int(r["units_30d"] or 0), "revenue_30d": round(float(r["revenue_30d"] or 0), 2),
        "profit_30d": round(float(r["profit_30d"] or 0), 2),
        "fba_available": int(r["fba_available"] or 0),
    } for r in raw]
    summary = {"sku_count": len(rows)}
    for k in ("units_7d", "revenue_7d", "profit_7d", "units_30d", "revenue_30d", "profit_30d"):
        summary[k] = round(sum(r[k] for r in rows), 2)
    return {"rows": rows, "summary": summary,
            "fba_snapshot": str(snapshot)[:10] if snapshot else None}

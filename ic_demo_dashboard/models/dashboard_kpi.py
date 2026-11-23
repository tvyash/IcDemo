import random
import time

from odoo import api, models


class DashboardKpi(models.AbstractModel):
    """Aggregated KPIs the dashboard loads on open, then refreshes via the Bus.

    AbstractModel — the dashboard is read-only and needs no persistence.
    Accepts a list of company_ids and aggregates across them, so the tiles
    follow the top-right company switcher's multi-select (one company,
    several, or all of them). The matching bus channels are also returned
    so the client subscribes per-company.
    """

    _name = "ic.demo.dashboard.kpi"
    _description = "IC Demo Dashboard KPI aggregator"

    @api.model
    def get_snapshot(self, company_ids=None, company_id=None):
        # Resolve scope: explicit company_ids > legacy company_id > env.companies > env.company
        if company_ids:
            ids = [int(c) for c in company_ids]
        elif company_id:
            ids = [int(company_id)]
        elif self.env.companies:
            ids = list(self.env.companies.ids)
        else:
            ids = [self.env.company.id]
        ids = sorted(set(ids))
        companies = self.env["res.company"].browse(ids).exists()
        ids = companies.ids  # drop any stale/invalid id

        # Confirmed sales (last 30 days) — use ANY() so the planner can hit
        # the (company_id, date_order) index per branch of the union.
        self.env.cr.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN state IN ('sale','done') THEN amount_total ELSE 0 END), 0) AS sales_confirmed,
                COUNT(*) FILTER (WHERE state IN ('sale','done')) AS sales_count
            FROM sale_order
            WHERE company_id = ANY(%s)
              AND date_order >= (CURRENT_DATE - INTERVAL '30 days')
            """,
            (ids,),
        )
        sales = self.env.cr.dictfetchone() or {}

        self.env.cr.execute(
            """
            SELECT
                COALESCE(SUM(amount_total_signed), 0) AS revenue_posted,
                COUNT(*) FILTER (WHERE state = 'posted') AS invoices_posted
            FROM account_move
            WHERE company_id = ANY(%s)
              AND move_type IN ('out_invoice', 'out_refund')
              AND invoice_date >= (CURRENT_DATE - INTERVAL '30 days')
            """,
            (ids,),
        )
        invoices = self.env.cr.dictfetchone() or {}

        names = companies.mapped("name")
        if not names:
            label = "No company"
        elif len(names) == 1:
            label = names[0]
        elif len(names) == 2:
            label = f"{names[0]} + {names[1]}"
        else:
            shown = ", ".join(names[:3])
            more = len(names) - 3
            label = f"{len(names)} companies: {shown}" + (f" +{more} more" if more > 0 else "")

        currencies = companies.mapped("currency_id.name")
        unique = list(dict.fromkeys(currencies))
        currency = unique[0] if len(unique) == 1 else "mixed"

        return {
            "scope": {
                "ids": ids,
                "label": label,
                "currency": currency,
                "channels": [f"ic_demo:dashboard:{cid}" for cid in ids],
                "count": len(ids),
            },
            "window": "last_30d",
            "sales_confirmed": float(sales.get("sales_confirmed") or 0.0),
            "sales_count": int(sales.get("sales_count") or 0),
            "revenue_posted": float(invoices.get("revenue_posted") or 0.0),
            "invoices_posted": int(invoices.get("invoices_posted") or 0),
        }

    @api.model
    def simulate_burst(self, count=20, company_ids=None, confirm_ratio=0.6):
        """Create `count` sale orders (with real product lines + non-zero
        amounts) across the given companies, confirming ~`confirm_ratio` of
        them. Fires the normal sale.order bus hooks, so the live dashboard
        updates in place.

        Returns a summary {created, confirmed, elapsed_ms} for the UI to
        display after the burst."""
        if not company_ids:
            company_ids = list(self.env.companies.ids) or [self.env.company.id]
        company_ids = [int(c) for c in company_ids if c]
        count = max(1, min(int(count or 20), 200))  # cap for safety

        products_by_co = {}
        partners_by_co = {}
        for cid in company_ids:
            products_by_co[cid] = self.env["product.product"].search(
                [
                    ("sale_ok", "=", True),
                    "|", ("company_id", "=", False), ("company_id", "=", cid),
                ],
                limit=40,
            ).ids
            partners_by_co[cid] = self.env["res.partner"].search(
                [
                    ("active", "=", True),
                    "|", ("company_id", "=", False), ("company_id", "=", cid),
                ],
                limit=80,
            ).ids

        created = 0
        confirmed = 0
        t0 = time.monotonic()
        for _ in range(count):
            cid = random.choice(company_ids)
            products = products_by_co.get(cid) or []
            partners = partners_by_co.get(cid) or []
            if not products or not partners:
                continue
            so = (
                self.env["sale.order"]
                .with_context(allowed_company_ids=[cid])
                .create({
                    "partner_id": random.choice(partners),
                    "company_id": cid,
                    "order_line": [(0, 0, {
                        "product_id": random.choice(products),
                        "product_uom_qty": random.randint(1, 10),
                        "price_unit": round(random.uniform(50.0, 2000.0), 2),
                    })],
                })
            )
            created += 1
            if random.random() < confirm_ratio:
                so.action_confirm()
                confirmed += 1
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return {
            "created": created,
            "confirmed": confirmed,
            "elapsed_ms": elapsed_ms,
            "requested": count,
        }

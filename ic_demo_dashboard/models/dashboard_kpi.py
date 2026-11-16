from odoo import api, fields, models


class DashboardKpi(models.AbstractModel):
    """Aggregated KPIs the dashboard loads on open, then refreshes via the Bus.

    Deliberately an AbstractModel — the dashboard is read-only and needs no persistence.
    All methods take a company_id so they work under the Odoo 20 native read-replica route.
    """

    _name = "ic.demo.dashboard.kpi"
    _description = "IC Demo Dashboard KPI aggregator"

    @api.model
    def get_snapshot(self, company_id=None):
        company_id = company_id or self.env.company.id
        company = self.env["res.company"].browse(company_id)

        # Use a direct SQL sum to keep cost predictable at 10M+ rows.
        self.env.cr.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN state IN ('sale','done') THEN amount_total ELSE 0 END), 0) AS sales_confirmed,
                COUNT(*) FILTER (WHERE state IN ('sale','done')) AS sales_count
            FROM sale_order
            WHERE company_id = %s
              AND date_order >= (CURRENT_DATE - INTERVAL '30 days')
            """,
            (company_id,),
        )
        sales = self.env.cr.dictfetchone() or {}

        self.env.cr.execute(
            """
            SELECT
                COALESCE(SUM(amount_total_signed), 0) AS revenue_posted,
                COUNT(*) FILTER (WHERE state = 'posted') AS invoices_posted
            FROM account_move
            WHERE company_id = %s
              AND move_type IN ('out_invoice', 'out_refund')
              AND invoice_date >= (CURRENT_DATE - INTERVAL '30 days')
            """,
            (company_id,),
        )
        invoices = self.env.cr.dictfetchone() or {}

        return {
            "company": {"id": company.id, "name": company.name, "currency": company.currency_id.name},
            "channel": f"ic_demo:dashboard:{company_id}",
            "window": "last_30d",
            "sales_confirmed": float(sales.get("sales_confirmed") or 0.0),
            "sales_count": int(sales.get("sales_count") or 0),
            "revenue_posted": float(invoices.get("revenue_posted") or 0.0),
            "invoices_posted": int(invoices.get("invoices_posted") or 0),
        }

from odoo import api, fields, models


class IcDemoConsolidationLine(models.Model):
    """Read-only SQL view that aggregates posted account_move_line rows across
    every company into a per-company × per-month P&L grid.

    Backed by a materialised-view-shaped SELECT so pivot/graph views stay fast
    even at 10M+ account_move_line rows (indexes on company_id, account_type,
    date keep this query sub-second).
    """

    _name = "ic.demo.consolidation.line"
    _description = "IC Demo — Consolidated P&L Line"
    _auto = False
    _rec_name = "company_id"
    _order = "period desc, company_id"

    company_id = fields.Many2one("res.company", string="Company", readonly=True)
    period = fields.Date(string="Period (month start)", readonly=True)
    year = fields.Char(string="Year", readonly=True)
    revenue = fields.Monetary(string="Revenue", readonly=True, currency_field="currency_id")
    receivables = fields.Monetary(
        string="Receivables", readonly=True, currency_field="currency_id"
    )
    net_profit = fields.Monetary(
        string="Net profit (proxy)", readonly=True, currency_field="currency_id"
    )
    line_count = fields.Integer(string="# journal lines", readonly=True)
    currency_id = fields.Many2one("res.currency", string="Currency", readonly=True)

    def init(self):
        """Store the rollup as a MATERIALIZED view — ~525 rows at 10M AMLs,
        so pivot/graph queries land in microseconds instead of scanning the
        full AML table every time. Refresh via the ``refresh()`` classmethod
        (hook it up to a cron if the demo ever needs live refresh)."""
        cr = self.env.cr
        # Drop any plain view from an earlier install, then (re)create materialized.
        cr.execute(
            f"""
            DO $$
            BEGIN
                EXECUTE 'DROP VIEW IF EXISTS {self._table}';
            EXCEPTION WHEN wrong_object_type THEN
                EXECUTE 'DROP MATERIALIZED VIEW IF EXISTS {self._table}';
            END $$;
            """
        )
        cr.execute(
            f"""
            CREATE MATERIALIZED VIEW {self._table} AS
            SELECT
                row_number() OVER ()                                  AS id,
                aml.company_id                                        AS company_id,
                date_trunc('month', aml.date)::date                   AS period,
                to_char(aml.date, 'YYYY')                             AS year,
                c.currency_id                                         AS currency_id,
                SUM(CASE WHEN acc.account_type = 'income'
                         THEN -aml.balance ELSE 0 END)                AS revenue,
                SUM(CASE WHEN acc.account_type = 'asset_receivable'
                         THEN aml.balance ELSE 0 END)                 AS receivables,
                SUM(CASE WHEN acc.account_type = 'income'
                         THEN -aml.balance
                         WHEN acc.account_type LIKE 'expense%%'
                         THEN -aml.balance
                         ELSE 0 END)                                  AS net_profit,
                COUNT(*)                                              AS line_count
            FROM account_move_line aml
            JOIN account_move am    ON am.id = aml.move_id
            JOIN account_account acc ON acc.id = aml.account_id
            JOIN res_company c       ON c.id  = aml.company_id
            WHERE am.state = 'posted'
              AND aml.display_type NOT IN ('line_section', 'line_subsection', 'line_note')
            GROUP BY aml.company_id, date_trunc('month', aml.date), to_char(aml.date, 'YYYY'), c.currency_id
            """
        )
        cr.execute(f"CREATE UNIQUE INDEX ON {self._table} (id)")
        cr.execute(f"CREATE INDEX ON {self._table} (company_id, period)")
        cr.execute(f"CREATE INDEX ON {self._table} (period)")

    @api.model
    def refresh(self):
        """Rebuild the consolidation materialised view against the current
        account_move_line state. Safe to call from a cron, a button, or a
        post-bulk-seed hook. CONCURRENTLY so the UI can keep reading during
        the refresh."""
        self.env.cr.execute(
            f"REFRESH MATERIALIZED VIEW CONCURRENTLY {self._table}"
        )

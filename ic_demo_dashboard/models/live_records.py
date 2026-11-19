from odoo import api, models

# Fields whose change should refresh open list views (invoice paid, posted, amount changed, ...).
LIVE_FIELDS = {"state", "payment_state", "amount_total", "amount_residual", "partner_id",
               "invoice_date", "invoice_date_due", "name", "date_order", "invoice_status"}
MAX_IDS = 50   # never flood the bus during mass operations (imports, data jobs, load tests)


def _ic_demo_live_notify(records, action):
    """Tell every browser that has a list of this model open, for the record's company, to reload.
    Uses plain SQL for the company so it is safe to call from inside the ORM flush."""
    env = records.env
    ids = [i for i in records.ids if i]
    if env.context.get("ic_demo_no_live") or not ids or len(ids) > MAX_IDS:
        return
    env.cr.execute(f'SELECT company_id, array_agg(id) FROM "{records._table}" WHERE id IN %s GROUP BY company_id',
                   (tuple(ids),))
    bus = env["bus.bus"]
    for company_id, rec_ids in env.cr.fetchall():
        if company_id:
            bus._sendone(f"ic_demo:live:{company_id}", "ic_demo.record.changed",
                         {"model": records._name, "ids": rec_ids, "action": action})


def _changed(records, vals_list):
    """Records whose written values touch a live field. In Odoo 20 the database write happens in
    _write_multi (also for computed stored fields), not in write()/_write()."""
    return records.browse([rec.id for rec, vals in zip(records, vals_list) if LIVE_FIELDS & set(vals)])


class AccountMoveLive(models.Model):
    _inherit = "account.move"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        _ic_demo_live_notify(records, "created")
        return records

    def _write_multi(self, vals_list):
        res = super()._write_multi(vals_list)
        _ic_demo_live_notify(_changed(self, vals_list), "updated")
        return res


class SaleOrderLive(models.Model):
    _inherit = "sale.order"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        _ic_demo_live_notify(records, "created")
        return records

    def _write_multi(self, vals_list):
        res = super()._write_multi(vals_list)
        _ic_demo_live_notify(_changed(self, vals_list), "updated")
        return res


class AccountPaymentLive(models.Model):
    _inherit = "account.payment"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        _ic_demo_live_notify(records, "created")
        return records

    def _write_multi(self, vals_list):
        res = super()._write_multi(vals_list)
        _ic_demo_live_notify(_changed(self, vals_list), "updated")
        return res

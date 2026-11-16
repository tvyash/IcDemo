from odoo import api, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _ic_demo_notify(self, action):
        for order in self:
            channel = f"ic_demo:dashboard:{order.company_id.id}"
            payload = {
                "model": "sale.order",
                "action": action,
                "id": order.id,
                "name": order.name,
                "amount_total": order.amount_total,
                "currency": order.currency_id.name,
                "partner": order.partner_id.display_name,
                "state": order.state,
            }
            self.env["bus.bus"]._sendone(channel, "ic_demo.kpi.tick", payload)

    def _action_confirm(self):
        res = super()._action_confirm()
        self._ic_demo_notify("confirmed")
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._ic_demo_notify("created")
        return records

from odoo import models


class AccountMove(models.Model):
    _inherit = "account.move"

    def _ic_demo_notify(self, action):
        for move in self:
            if move.move_type not in ("out_invoice", "out_refund", "in_invoice", "in_refund"):
                continue
            channel = f"ic_demo:dashboard:{move.company_id.id}"
            payload = {
                "model": "account.move",
                "action": action,
                "id": move.id,
                "name": move.name or "(draft)",
                "amount_total": move.amount_total,
                "currency": move.currency_id.name,
                "partner": move.partner_id.display_name,
                "state": move.state,
                "move_type": move.move_type,
            }
            self.env["bus.bus"]._sendone(channel, "ic_demo.kpi.tick", payload)

    def action_post(self):
        res = super().action_post()
        self._ic_demo_notify("posted")
        return res

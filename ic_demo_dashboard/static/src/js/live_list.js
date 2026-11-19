/** @odoo-module **/
// Live refresh of list views: when another user creates, posts or pays a record of the same company,
// the open list reloads by itself (Odoo Bus, channel ic_demo:live:<company id>).
import { patch } from "@web/core/utils/patch";
import { onWillUnmount, usePlugin } from "@odoo/owl";
import { ListController } from "@web/views/list/list_controller";
import { BusPlugin } from "@bus/services/bus_plugin";
import { user } from "@web/core/user";

const LIVE_MODELS = ["account.move", "sale.order", "account.payment"];

patch(ListController.prototype, {
    setup() {
        super.setup(...arguments);
        if (!LIVE_MODELS.includes(this.props.resModel)) {
            return;
        }
        const bus = usePlugin(BusPlugin);
        const channels = (user.activeCompanies || []).map((c) => `ic_demo:live:${c.id}`);
        let timer = null;
        const unsubscribe = bus.subscribe("ic_demo.record.changed", (payload) => {
            if (payload.model !== this.props.resModel) {
                return;
            }
            if (this.model.root.editedRecord) {
                return; // never reload while the user is editing a row
            }
            clearTimeout(timer);
            timer = setTimeout(() => this.model.load(), 400);
        });
        for (const channel of channels) {
            bus.addChannel(channel);
        }
        onWillUnmount(() => {
            clearTimeout(timer);
            unsubscribe?.();
            for (const channel of channels) {
                bus.deleteChannel(channel);
            }
        });
    },
});

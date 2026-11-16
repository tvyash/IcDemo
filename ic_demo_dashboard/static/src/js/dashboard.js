/** @odoo-module **/

import {
    Component,
    onWillStart,
    onWillUnmount,
    proxy,
    usePlugin,
    useProps,
} from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";
import { BusPlugin } from "@bus/services/bus_plugin";

export class IcDemoDashboard extends Component {
    static template = "ic_demo_dashboard.Dashboard";
    props = useProps({ ...standardActionServiceProps });

    busService = usePlugin(BusPlugin);

    setup() {
        this.orm = useService("orm");

        this.state = proxy({
            loading: true,
            snapshot: null,
            events: [],
            eventCount: 0,
            lastEventAt: null,
        });

        this._channel = null;
        this._unsubscribe = this.busService.subscribe(
            "ic_demo.kpi.tick",
            (payload) => this._onTick(payload),
        );

        onWillStart(async () => {
            await this._loadSnapshot();
            this._channel = this.state.snapshot.channel;
            this.busService.addChannel(this._channel);
        });

        onWillUnmount(() => {
            this._unsubscribe?.();
            if (this._channel) {
                this.busService.deleteChannel(this._channel);
            }
        });
    }

    async _loadSnapshot() {
        const companyId = user.activeCompany.id;
        const snapshot = await this.orm.call(
            "ic.demo.dashboard.kpi",
            "get_snapshot",
            [],
            { company_id: companyId },
        );
        this.state.snapshot = snapshot;
        this.state.loading = false;
    }

    _onTick(payload) {
        const now = Date.now();
        this.state.eventCount += 1;
        this.state.lastEventAt = new Date(now).toLocaleTimeString();
        this.state.events.unshift({
            at: this.state.lastEventAt,
            model: payload.model,
            action: payload.action,
            name: payload.name,
            amount: payload.amount_total,
            currency: payload.currency,
            partner: payload.partner,
        });
        if (this.state.events.length > 50) {
            this.state.events.length = 50;
        }
        if (payload.model === "sale.order" && payload.action === "confirmed") {
            this.state.snapshot.sales_confirmed += payload.amount_total || 0;
            this.state.snapshot.sales_count += 1;
        } else if (payload.model === "account.move" && payload.action === "posted") {
            this.state.snapshot.revenue_posted += payload.amount_total || 0;
            this.state.snapshot.invoices_posted += 1;
        }
    }

    formatAmount(v) {
        if (v == null) return "–";
        return Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
    }
}

registry.category("actions").add("ic_demo_dashboard", IcDemoDashboard);

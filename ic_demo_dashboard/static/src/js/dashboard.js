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
        this.action = useService("action");
        this.notification = useService("notification");

        this.state = proxy({
            loading: true,
            snapshot: null,
            events: [],
            eventCount: 0,
            lastEventAt: null,
            simulating: false,
            lastBurst: null,
        });

        this._channels = [];
        this._unsubscribe = this.busService.subscribe(
            "ic_demo.kpi.tick",
            (payload) => this._onTick(payload),
        );

        onWillStart(async () => {
            await this._loadSnapshot();
            this._channels = this.state.snapshot.scope.channels;
            for (const ch of this._channels) {
                this.busService.addChannel(ch);
            }
        });

        onWillUnmount(() => {
            this._unsubscribe?.();
            for (const ch of this._channels) {
                this.busService.deleteChannel(ch);
            }
        });
    }

    async _loadSnapshot() {
        const companyIds = (user.activeCompanies || []).map((c) => c.id);
        const snapshot = await this.orm.call(
            "ic.demo.dashboard.kpi",
            "get_snapshot",
            [],
            { company_ids: companyIds },
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
            id: payload.id,
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

    openRecord(ev) {
        if (!ev || !ev.id || !ev.model) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            name: `${ev.model} · ${ev.name}`,
            res_model: ev.model,
            res_id: ev.id,
            views: [[false, "form"]],
            target: "new",
        });
    }

    async simulate() {
        if (this.state.simulating) {
            return;
        }
        this.state.simulating = true;
        try {
            const scope = this.state.snapshot?.scope?.ids || [];
            const result = await this.orm.call(
                "ic.demo.dashboard.kpi",
                "simulate_burst",
                [],
                { count: 20, company_ids: scope },
            );
            this.state.lastBurst = result;
            this.notification.add(
                `Burst: ${result.created} created, ${result.confirmed} confirmed in ${result.elapsed_ms} ms`,
                { type: "success", sticky: false },
            );
        } catch (err) {
            this.notification.add(
                `Burst failed: ${err.message || err}`,
                { type: "danger", sticky: true },
            );
        } finally {
            this.state.simulating = false;
        }
    }
}

registry.category("actions").add("ic_demo_dashboard", IcDemoDashboard);

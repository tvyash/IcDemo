/** @odoo-module **/

import {
    Component,
    onWillStart,
    onWillUnmount,
    proxy,
    useProps,
} from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";

export class IcDemoLoadTest extends Component {
    static template = "ic_demo_dashboard.LoadTest";
    props = useProps({ ...standardActionServiceProps });

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");

        this.state = proxy({
            users: 30,
            spawnRate: 5,
            runTime: 60,
            runId: null,
            status: null,
            starting: false,
            polling: false,
        });

        this._pollTimer = null;

        onWillStart(async () => {
            const latestId = await this.orm.call("ic.demo.load.run", "latest_run_id", []);
            if (latestId) {
                this.state.runId = latestId;
                await this._refreshStatus();
                if (this.isRunning) {
                    this._schedulePoll();
                }
            }
        });

        onWillUnmount(() => this._stopPolling());
    }

    async start() {
        if (this.state.starting || this.isRunning) {
            return;
        }
        this.state.starting = true;
        try {
            const runId = await this.orm.call(
                "ic.demo.load.run",
                "start_run",
                [],
                {
                    users: this.state.users,
                    spawn_rate: this.state.spawnRate,
                    run_time: this.state.runTime,
                },
            );
            this.state.runId = runId;
            this.state.status = {
                state: "pending",
                stats: {},
                errors: [],
                elapsed: 0,
                run_time: this.state.runTime,
            };
            this.notification.add(`Load test #${runId} started`, { type: "success" });
            this._schedulePoll();
        } catch (err) {
            this.notification.add(`Start failed: ${err.message || err}`, { type: "danger", sticky: true });
        } finally {
            this.state.starting = false;
        }
    }

    async _refreshStatus() {
        if (!this.state.runId) {
            return;
        }
        try {
            const status = await this.orm.call("ic.demo.load.run", "get_status", [this.state.runId]);
            this.state.status = status;
        } catch (err) {
            this.notification.add(`Poll failed: ${err.message || err}`, { type: "danger" });
        }
    }

    _schedulePoll() {
        this._stopPolling();
        this.state.polling = true;
        const tick = async () => {
            await this._refreshStatus();
            if (this.isRunning) {
                this._pollTimer = setTimeout(tick, 2000);
            } else {
                this.state.polling = false;
                this._pollTimer = null;
            }
        };
        this._pollTimer = setTimeout(tick, 1500);
    }

    _stopPolling() {
        if (this._pollTimer) {
            clearTimeout(this._pollTimer);
            this._pollTimer = null;
        }
        this.state.polling = false;
    }

    formatNum(v, digits = 0) {
        if (v == null) {
            return "–";
        }
        return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits });
    }

    get endpointRows() {
        if (!this.state.status) {
            return [];
        }
        const stats = this.state.status.stats || {};
        return Object.entries(stats).map(([name, s]) => ({ name, ...s }));
    }

    get progressPct() {
        if (!this.state.status || !this.state.status.run_time) {
            return 0;
        }
        return Math.min(100, Math.round(((this.state.status.elapsed || 0) / this.state.status.run_time) * 100));
    }

    get isRunning() {
        return this.state.status && ["pending", "running"].includes(this.state.status.state);
    }

    get isFinished() {
        return this.state.status && this.state.status.state === "finished";
    }

    get isError() {
        return this.state.status && this.state.status.state === "error";
    }
}

registry.category("actions").add("ic_demo_load_test", IcDemoLoadTest);

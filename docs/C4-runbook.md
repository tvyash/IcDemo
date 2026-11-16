# C4 — OWL real-time dashboard + Bus — demo runbook

Challenge 4: an OWL client that pushes KPIs in real time, via Odoo 20's Bus
service, to a target of **100 concurrent subscribers**.

## What's in the box

| Piece | Where |
|---|---|
| OWL 3 client action (dashboard) | `addons/custom/ic_demo_dashboard/static/src/js/dashboard.js` + `xml/dashboard.xml` |
| KPI aggregator (AbstractModel, uses read-replica route) | `addons/custom/ic_demo_dashboard/models/dashboard_kpi.py` |
| Server-side hooks (`sale.order.create`, `sale.order._action_confirm`, `account.move.action_post`) that push `ic_demo.kpi.tick` onto `ic_demo:dashboard:<company_id>` | `models/sale_order.py`, `models/account_move.py` |
| Load harness (locust + websocket-client) | `tools/load/locustfile.py` |
| Perf-profile Odoo config (workers=4, gevent on :8072) | `odoo/odoo.perf.conf` |

## Browser demo path (visual proof)

Uses `odoo/odoo.conf` (workers=0 — single process handles both HTTP and the
websocket upgrade on :8069, so no reverse proxy is needed locally).

```bash
# Fresh start (in project root)
pgrep -f "odoo-bin.*ic_demo" | xargs -r kill -TERM
sleep 3
source .venv/bin/activate
nohup /home/techvoot/work_space/20/base/odoo/odoo-bin \
      -c odoo/odoo.conf >>var/log/odoo.log 2>&1 &
```

Then in a fresh browser window:
1. http://localhost:8069 → log in `admin` / `admin`.
2. Top menu: **IC Demo → Live KPI Dashboard**.
3. Expect: four KPI tiles reading zero, "live · 0 event(s)" pill, "My Company — live last 30 days" header.
4. In another tab: **Sales → Create → Add a line → Confirm**.
5. First tab ticks: event counter goes up, "Confirmed sales" count/value increment, event feed shows the new row.
6. Repeat with an invoice from **Accounting → Customers → Create → Confirm** to see the posted-revenue side light up.

## Load test path (100 concurrent subscribers)

Switch Odoo to the perf profile (adds HTTP workers + dedicated gevent WS
service on :8072 — same shape the cloud ingress targets in C5):

```bash
pgrep -f "odoo-bin.*ic_demo" | xargs -r kill -TERM
sleep 3
source .venv/bin/activate
nohup /home/techvoot/work_space/20/base/odoo/odoo-bin \
      -c odoo/odoo.perf.conf >>var/log/odoo.log 2>&1 &
sleep 8
```

Then run the harness:

```bash
IC_DEMO_WS_PORT=8072 .venv-tools/bin/locust \
    -f tools/load/locustfile.py \
    --host http://127.0.0.1:8069 \
    --users 101 --spawn-rate 20 \
    --run-time 60s \
    --headless --only-summary
```

What it does:
- `test_start` hook authenticates once and shares the session cookie across
  all 100 subscribers. That takes bcrypt out of the hot path (it is CPU-bound
  and would otherwise serialise under the GIL during the ramp).
- `fixed_count = 1` on `OrderWriter` guarantees exactly one writer; the other
  100 users land on `DashboardSubscriber`.
- Subscribers open a WebSocket to `/websocket` on :8072, subscribe to
  `ic_demo:dashboard:1`, and count every `ic_demo.kpi.tick` they receive.
- The writer loops `sale.order.create` with ~0.5–1 s pauses.
- Per-order latency (`writer.create` → `subscriber.recv`) is captured via a
  shared `{order_id: monotonic_ts}` dict and reported as p50/p95/p99.

A recent run (`docs/C4-results/100-user-run.md`) hit:

- **100 / 100** subscribers connected, **0 failures** of 12,668 ops
- **p50 = 11 ms**, **p95 = 16 ms** steady-state fan-out
- **12,500** notifications delivered across the cluster in 60 s

## Switching back for the browser demo

```bash
pgrep -f "odoo-bin.*ic_demo" | xargs -r kill -TERM
sleep 3
source .venv/bin/activate
nohup /home/techvoot/work_space/20/base/odoo/odoo-bin \
      -c odoo/odoo.conf >>var/log/odoo.log 2>&1 &
```

Reminder: the two configs differ only in `workers` / `db_maxconn` /
`max_cron_threads`. Everything else (addons, DB, replica) is identical.

## Cloud mapping (C5 preview)

| Role | Local (perf conf) | Cloud (k3s) |
|---|---|---|
| HTTP workers | `workers=4` preforked processes | `Deployment` with HPA 2–8 pods |
| WS fanout | gevent single-proc on :8072 | dedicated `Deployment` (1 pod), sticky session cookie, ingress route `/websocket` → service |
| DB | localhost Postgres 18 | pgBouncer → HAProxy → Patroni pg1/pg2 |
| Filestore | host dir | MinIO (S3-compatible PVC) |

The browser contract is identical — it always opens `ws://<host>/websocket`,
and the routing (local single-process vs. cloud ingress) happens underneath.

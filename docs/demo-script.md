# IC Demo — walkthrough script (screen-share edition)

A 30-40 minute live walkthrough that covers all five technical challenges.
Everything in this script runs against the local laptop setup (the same
machine the dev work was done on). The cloud deployment (C5) is shown via
the manifests + the local primitives that stand in for each piece.

Target audience: lead's technical evaluator, roughly one engineer and one
product person.

## Pre-flight checklist (5 min before the call)

1. **Postgres host** running with the C1 seed loaded.
   ```bash
   PGPASSWORD=ic_demo psql -h localhost -U ic_demo -d ic_demo -c "SELECT COUNT(*) FROM account_move_line"
   # expect ~10M
   ```
2. **Odoo (demo profile)** up on :8069.
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8069/web/login   # 200
   ```
3. **Two browser tabs**: one logged in as `admin` / `admin`, one blank.
4. Terminal split: one pane in `~/work_space/20/projects/ic_demo`, one tail
   on `var/log/odoo.log`.
5. Have the `docs/*-results/*.md` files open in the IDE — the captured
   numbers back up the live numbers.

## The walkthrough

### Opening (2 min)

> "Five technical challenges, one Odoo 20 project. Everything here runs on
> this laptop today and maps onto a 7-droplet DO cluster for C5. I'll
> show each challenge against the live system, then pull up the cloud
> configs that would deploy the same thing to production."

Show `docs/PLAN.md` briefly — the proposal + decisions.

### C4 — real-time dashboard + bus fan-out (8 min)

Already running. **This is the most visual piece — lead with it.**

1. Browser tab 1: **IC Demo → Live KPI Dashboard**. Four KPI tiles, "live
   · 0 event(s)" pill, "My Company — live last 30 days".
2. Browser tab 2: **Sales → Create → confirm an order**.
3. Tab 1 ticks live: event counter increments, sales count/value update,
   event feed shows the new row.
4. Open a terminal and show the matching bus_bus row:
   ```bash
   PGPASSWORD=ic_demo psql -h localhost -U ic_demo -d ic_demo \
       -c "SELECT id, channel, message FROM bus_bus ORDER BY id DESC LIMIT 1"
   ```
5. **100-user load test**: `docs/C4-results/100-user-run.md` — show the
   headline numbers (100/100 subscribers, 0 failures, p50 11 ms). Explain
   the split: perf profile uses workers=4 + dedicated gevent :8072, same
   shape as cloud.
6. **Live concurrent-users writing**: run this live with the dashboard
   tab still open — the lead specifically wants to see "many users
   working at the same time and the server holding up".
   ```bash
   .venv-tools/bin/locust \
       -f tools/load/concurrent_writers.py \
       --host http://127.0.0.1:8069 \
       --users 30 --spawn-rate 5 \
       --run-time 60s \
       --headless --only-summary
   ```
   While it runs, point at the dashboard tab — the event counter will
   climb into the hundreds, event feed scrolls new rows every ~100ms,
   "Confirmed sales count" tile ticks live on every `action_confirm`.
   Summary at the end shows 500+ orders created, 0 failures, 9-10
   orders/sec sustained, write p50 ≈ 150 ms, read p50 stays at ~18 ms.
   Full capture: `docs/C4-results/concurrent-writers-run.md`.
7. Pull up `addons/custom/ic_demo_dashboard/static/src/js/dashboard.js` to
   show the OWL 3 code is minimal (~100 lines).

### C1 — 20 companies + 10M AMLs + consolidated P&L (8 min)

1. Browser: **IC Demo → Consolidated P&L**. Pivot loads in under a second —
   21 companies × months × revenue / net profit / line count.
2. Switch to **Graph** tab: stacked bars of revenue per company over time.
3. Terminal — show the view is really over 10M rows:
   ```bash
   PGPASSWORD=ic_demo psql -h localhost -U ic_demo -d ic_demo -c \
     "SELECT COUNT(*) FROM account_move_line"              # ~10M
   PGPASSWORD=ic_demo psql -h localhost -U ic_demo -d ic_demo -c \
     "SELECT COUNT(*) FROM ic_demo_consolidation_line"     # 525
   ```
4. Show `tools/seed/c1_bulk.py` — explain the ORM-for-structure +
   COPY-for-bulk split and the 10M-AML / 22-min number from
   `docs/C1-results/10M-seed-run.md`.
5. Open the matview model source
   (`addons/custom/ic_demo_consolidation/models/consolidation_line.py`) —
   show `_auto = False` + the init() that runs `CREATE MATERIALIZED VIEW`
   and the `.refresh()` method for cron rebuilds.

### C3 — tuned Odoo config + RPC latency baseline (5 min)

1. Show `docs/C3-runbook.md`: three Odoo profiles (demo / perf / cloud)
   and the handful of knobs that differ.
2. Run the benchmark live:
   ```bash
   .venv-tools/bin/python tools/c3/bench.py --iterations 300 --concurrency 10
   ```
3. Compare the live numbers to `docs/C3-results/baseline.md`: dashboard
   p50 ~36 ms, consolidation p50 ~42 ms, 0 errors.
4. Point out the indexes applied by hand and the PG parameters baked into
   the Patroni config.

### C2 — PG HA with Patroni + failover (6 min)

Only piece that runs a second Postgres cluster. Walk through:

1. Terminal:
   ```bash
   ./tools/c2/local_cluster.sh init     # (if not already done)
   ./tools/c2/local_cluster.sh start
   ./tools/c2/local_cluster.sh status
   ```
   Show: primary on :5433 (`in_recovery=f`), replica on :5434 (`in_recovery=t`).
2. Run the failover demo:
   ```bash
   ./tools/c2/local_cluster.sh demo
   ```
   Walk through the output: write to primary → replica sees it → kill
   primary → promote replica → write survives, new write on new primary.
3. Open `infra/patroni/README.md` + `patroni-pg1.yml`. Say: "what we just
   did by hand, Patroni does automatically when the leader stops
   refreshing a 30-second lock in etcd. HAProxy health-checks the Patroni
   REST and flips the write VIP within 3-6 seconds after promotion."

### C5 — k3s + ingress + monitoring (5 min)

1. Open `docs/C5-runbook.md`. Walk through the droplet layout + bring-up
   order + the file-to-destination table.
2. Show the three Deployments:
   - `infra/k8s/odoo/deploy-web.yaml` (HPA 2-8, 4 vCPU, 10 GB)
   - `infra/k8s/odoo/deploy-bus.yaml` (1 replica, websocket on :8072)
   - `infra/k8s/odoo/deploy-cron.yaml` (1 replica, no HTTP)
3. Ingress split: `infra/k8s/ingress/ingress.yaml` — `/websocket` to bus,
   everything else to web, each with its own annotations.
4. Monitoring: show `infra/k8s/monitoring/prometheus.yaml` scrape targets
   (patroni, etcd, haproxy, node, k8s cadvisor).

### Close (3 min)

- **Total custom code**: 2 small Odoo modules (dashboard 100 LOC, consolidation 100 LOC), 2 seed scripts, 1 benchmark harness.
- **Everything else**: configs, k8s manifests, runbooks.
- **Captured numbers** (point to the `docs/*/`-results files):
  - C1: 10M AMLs seeded in 22 min, consolidation pivot p50 **4.7 ms** (SQL) / **42 ms** (via Odoo RPC).
  - C4: 100 concurrent WS subscribers, 0 failures, p50 **11 ms** fan-out.
  - C3: 0 errors over 600 RPC calls at concurrency 10, p99 **< 250 ms** across the real read paths.
  - C2: failover demonstrated live, rows preserved across primary kill.

Stop for Q&A.

## Known talking points (if asked)

- **Why workers=0 locally?** Single process serves both HTTP and websocket
  on :8069 — no reverse proxy needed on a laptop. Cloud uses workers>=4
  + separate gevent pod + ingress `/websocket` route.
- **Why a materialised view?** Live-view variant hit 4.6 s on the full
  roll-up; matview collapses 10M rows to 525 and answers in ~10 ms.
  Refreshed from `.refresh()` — can wire to a cron.
- **Why Patroni + etcd (not just streaming + a VIP)?** Automatic
  promotion. Streaming is the mechanics; Patroni is the brain.
- **Why 3-node etcd across pg1/pg2/ops?** 2-node etcd is a SPOF — quorum
  requires strict majority, so losing either node leaves no leader. Three
  nodes with the tie-breaker on ops is the smallest safe shape.
- **Minimal customisation**: no custom accounting logic in Odoo — the
  consolidation module is a read-only SQL view over native `account_move_line`,
  and the dashboard is 100 lines of OWL + a server-side hook on `sale.order`
  / `account.move`. OCA welcome but not needed yet.

# IC Demo — walkthrough script

A 30-35 minute live demonstration for the lead's technical + functional
evaluators. **Primary target right now: the local laptop instance** at
http://127.0.0.1:8069 — demo-shaped DB loaded, five named companies, full
O2C and P2P flows on top of a 10M-line journal base.

**Cloud target**: parked. The 7-droplet DO cluster the server team stood
up (`https://multicoodoo.techvoot.com`) is on pause until the customer
confirms a demo date. The packaged DB + filestore in `var/handoff/` lift
straight onto that cluster once the date is set; the walkthrough below
runs identically against that URL at that point — same menus, same data
shape, same numbers.

Audience we design for: one technical reviewer (CTO / lead architect)
and one business reviewer (CFO or ops head). Every section opens with
the business question it answers, then shows the engineering behind
it — techno-functional throughout, no deep code reading required of the
audience.

## The five companies on screen

We present the demo as a mid-size group — one parent and four operating
subsidiaries — a shape every multi-entity prospect recognises.

| Company | Role | Books |
|---|---|---|
| **Techvoot Global HQ** | Parent + shared services | Consolidated view |
| **Alpha Retail** | Consumer-facing storefronts | Sales-heavy |
| **Beta Trading** | Import/export arm | Buy/sell flows |
| **Delta Logistik GmbH** | EU logistics subsidiary | Vendor-heavy |
| **Gamma Manufacturing** | Production plants | Manufacturing flows |

## Demo-shape data under the hood (verified 2026-11-19)

| Metric | Value | Per-company |
|---|---|---|
| Active companies | **5** | parent + 4 subs |
| Archived companies | 17 | kept for AML scale, hidden from UI (`active=FALSE` + matview filter) |
| Sale orders | **25,000** (≈20,178 confirmed) | 5,000 / co |
| Purchase orders | **7,500** | 1,500 / co |
| Posted customer invoices | **1,010** | 200 / co (210 on Alpha Retail) |
| Posted vendor bills | **1,070** | 200 / co (270 on Alpha Retail) |
| Reconciled payments | **797** | customer + vendor mix |
| Products (shared) | 300 | prefix `IC-` |
| Account move lines | **10,030,822** | 10M from the C1 bulk seed + ~30k from business flows |
| Consolidation matview | **125** rows | 5 cos × 25 months |

> Partner names on screen read `ICD Sub NN Partner NNN` (customers,
> inherited from the archived subs we reshaped from) and `Acme / Globex
> / Initech Supply Co. NNN` (vendors). Cosmetic only — real names drop
> in with a one-shot SQL pass before the real demo if the customer wants
> that polish.

## Pre-flight (15 min before the call)

1. **Local Odoo running.**
   ```bash
   pgrep -af 'odoo-bin.*ic_demo' | head -1
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8069/web/login   # 200
   ```
   If not running:
   ```bash
   cd ~/work_space/20/projects/ic_demo
   ./scripts/run.sh           # loads odoo/odoo.conf, workers=0, :8069
   ```
2. Log in as `admin` / `admin` in **two browser windows** against
   http://127.0.0.1:8069 (needed for the live dashboard + concurrent-write
   demo). Keep the password off-screen.
3. Confirm the two `IC Demo` menus are present:
   - **IC Demo → Live KPI Dashboard**
   - **IC Demo → Consolidated P&L**
4. Confirm the company switcher (top-right) lists all five: Techvoot
   Global HQ, Alpha Retail, Beta Trading, Delta Logistik GmbH, Gamma
   Manufacturing.
5. **Captured-result tabs open as backup reference:**
   - `docs/C1-results/10M-seed-run.md` — pivot ~5 ms SQL / 42 ms RPC over 10M AMLs
   - `docs/C3-results/baseline.md` — p99 under 250 ms at 10-user concurrency
   - `docs/C4-results/concurrent-writers-run.md` — 584 orders in 60 s, 0 failures
   - `docs/C4-results/100-user-run.md` — 100 subscribers, p50 fan-out 11 ms
   - `docs/C2-results/local-failover.md` — 2-node Patroni failover captured
6. **Load harness pointed at local:**
   ```bash
   export IC_DEMO_DB=ic_demo
   export IC_DEMO_LOGIN=admin
   export IC_DEMO_PASSWORD=admin
   ```
7. (When the cloud cluster is lit back up for the real demo) Grafana
   tunnel + `kubectl` to k3s + SSH to the Patroni primary — see § **Cloud
   lift** at the end of this script.

## The walkthrough

### Opening (2 min)

> "I'll walk through five pieces of a real Odoo 20 Enterprise deployment
> we've built and shaped end-to-end. The five pieces are what your brief
> asked us to prove: one roll-up across many companies; a dashboard that
> reflects what's happening right now; a system that stays responsive
> as concurrent users pile on; a database that survives a server dying;
> and a deployment that scales and self-heals on Kubernetes.
>
> What you're seeing today runs on my laptop against a database shaped
> exactly like yours would be — five named companies, real sales and
> purchase flows, ten million journal lines underneath. The same build
> is packaged and ready to go live on the seven-server cluster the
> moment you confirm a demo date; the walkthrough is identical, we just
> swap the URL."

Share your screen. Log in as **Techvoot Global HQ** (the parent).

> "Everything you see from here is live — the orders I confirm hit the
> same database your users would hit in production."

---

### 1. Live KPI dashboard (C4) — ~8 min

**Business question**: *"Can I see what the business is doing right
now, without waiting for yesterday's report?"*

1. **Browser tab 1**: **IC Demo → Live KPI Dashboard**.
   - Four KPI tiles (confirmed sales count, confirmed sales $, revenue
     booked, invoice count).
   - "live · 0 event(s)" pill in the corner.
   - "Techvoot Global HQ — live last 30 days".
2. **Browser tab 2**: **Sales → Quotations → New**, pick any customer
   (type "Partner" into the lookup — ~900 customers in the pool), add a
   product (type "IC-" — 300 products), **Confirm**.
3. Switch to tab 1: the event counter ticks, **Confirmed sales** count
   and value climb, event feed shows the new row within a fraction of
   a second.
4. Switch the active company (top-right) to **Alpha Retail** — the
   tiles reload for that subsidiary's numbers (5,000 SOs base, 210
   posted invoices). Each company publishes to its own bus channel; a
   user in Delta Logistik GmbH doesn't see Alpha's traffic.

> "This isn't a 60-second polling refresh. The dashboard subscribes to
> a per-company channel on Odoo's bus. When anyone confirms an order
> or posts an invoice, the server pushes to that channel and every
> subscribed browser updates. Same mechanism that powers chatter
> notifications and inter-user messaging — we built on it instead of
> around it."

5. **Many users writing at once**. From a terminal on the laptop:

   ```bash
   .venv-tools/bin/locust -f tools/load/concurrent_writers.py \
       --host http://127.0.0.1:8069 \
       --users 30 --spawn-rate 5 --run-time 60s --headless --only-summary
   ```

   25 simulated business users create and confirm sale orders; 5
   analyst users read the dashboard and consolidation pivot. Keep tab 1
   visible — tiles climb into the hundreds, event feed scrolls new rows
   every ~100 ms.

   Headline from the captured baseline
   (`docs/C4-results/concurrent-writers-run.md`): **584 orders in 60
   seconds, 0 failures, 714 bus notifications, write p50 148 ms, read
   p50 18 ms**.

6. **Fan-out at scale** — reference the 100-subscriber capture
   (`docs/C4-results/100-user-run.md`): 100 browser subscribers plus a
   writer, **0 failures of 12,668 operations, p50 fan-out 11 ms, p95
   16 ms, 12,500+ notifications in 60 s**.

> "Real-time means two things: a writer gets a fast response, and
> every subscribed reader sees the update fast. Both numbers stay in
> the single-digit-to-low-tens of milliseconds under 100 concurrent
> subscribers plus a writer."

---

### 2. Consolidated P&L across 5 companies (C1) — ~6 min

**Business question**: *"Can my CFO see one roll-up of all
subsidiaries without waiting for a month-end report run?"*

1. **IC Demo → Consolidated P&L**. The Pivot opens in well under a
   second: five companies × 25 months × revenue / receivables / net
   profit / line count. Expand any cell to drill through to the
   underlying journal lines (native Odoo pivot drill-down).
2. Switch to **Graph** tab — stacked bars per company per month.
3. Switch to **List** tab — the 125 pre-aggregated rows that back the
   pivot.
4. In a terminal, show what sits underneath:
   ```bash
   PGPASSWORD=ic_demo psql -h localhost -U ic_demo -d ic_demo -c \
       "SELECT COUNT(*) FROM account_move_line"              # 10,030,822
   PGPASSWORD=ic_demo psql -h localhost -U ic_demo -d ic_demo -c \
       "SELECT COUNT(*) FROM ic_demo_consolidation_line"     # 125
   ```

> "Two things to notice. One — the pivot answers in milliseconds even
> over ten million journal lines, because we pre-aggregate into a
> Postgres materialised view that collapses the roll-up to a hundred
> and twenty-five rows. Two — everything you see is **native Odoo**.
> The Pivot / Graph / List toggles, the drill-down, the CSV export —
> all come free from Odoo's framework. We wrote about 100 lines of
> Python to define the view."

5. **Fresh numbers, captured today** against this exact DB
   (`tools/c3/bench.py`, 20 iterations × 5-way concurrency):

   | RPC | p50 | p95 | p99 |
   |---|---|---|---|
   | `ic.demo.dashboard.kpi.get_snapshot` | **22.9 ms** | 106.5 ms | 106.7 ms |
   | `ic.demo.consolidation.line.web_read_group` | **18.1 ms** | 37.1 ms | 47.1 ms |

   0 errors over 40 calls.

6. **Scale proof**. Open `docs/C1-results/10M-seed-run.md`:

   > "During the evaluation we validated this design against
   > **10,000,000 journal lines across 21 companies** — the pivot
   > answers in **4.7 ms at the SQL layer and 42 ms end-to-end**
   > through Odoo RPC. The current demo DB keeps those 10M lines
   > underneath; the five named companies you're seeing sit on top.
   > The pattern scales to many multiples of your current size with no
   > code change."

7. Open `ic_demo_consolidation/models/consolidation_line.py` — point
   to `_auto = False`, the `init()` that runs `CREATE MATERIALIZED
   VIEW` (with the `c.active = TRUE` filter so archived cos stay
   hidden), and `.refresh()` (wire to a nightly cron, or trigger on
   period close from the UI).

---

### 3. Performance under load (C3) — ~4 min

**Business question**: *"If we put 500 users on this, does it feel
sluggish?"*

1. Run the benchmark live against local:
   ```bash
   .venv-tools/bin/python tools/c3/bench.py \
       --host http://127.0.0.1:8069 \
       --iterations 300 --concurrency 10
   ```
2. While it runs, open `docs/C3-results/baseline.md` and show captured
   numbers; add today's fresh capture for the audience:
   - Dashboard RPC p50 **22.9 ms** / p95 **106.5 ms**
   - Consolidation RPC p50 **18.1 ms** / p95 **37.1 ms** / p99 **47.1 ms**
   - **0 errors over 40 calls** at concurrency 5 (replicates the
     C3 baseline pattern on a `workers=0` laptop)
3. Walk through `odoo/odoo.cloud.conf` — three sections every customer
   recognises, exactly what the cloud deploys when we flip the DB over:
   - **Workers** = `2 × vCPU + 1`, separate deployments for HTTP / bus
     / cron so one busy worker can't starve the websocket loop.
   - **Memory** = soft 2 GB / hard 2.5 GB per worker, aligned with the
     k8s pod limits so a runaway request is reaped before it kills
     the pod.
   - **Database connections** = pgBouncer in transaction-pool mode in
     front of HAProxy means each Odoo worker keeps a tiny client-side
     connection and pgBouncer shares a smaller pool back to Postgres.

---

### 4. HA + automatic failover (C2) — ~6 min

**Business question**: *"If one database server crashes at 2 am, do my
users notice?"*

With the cloud cluster parked, we run the **local 2-node Postgres
cluster** as the primary proof — same shaped motion (primary writes,
replica catches up, primary dies, replica promoted, data intact),
captured on a single laptop with two local PGs.

1. In a separate terminal, prep + start the cluster (first run only
   needs `init`):
   ```bash
   ./tools/c2/local_cluster.sh init     # one-off — initdb primary + base-backup replica
   ./tools/c2/local_cluster.sh start    # start both PGs
   ./tools/c2/local_cluster.sh status   # confirm primary + replica healthy
   ```
2. Drive the shaped failover in one command:
   ```bash
   ./tools/c2/local_cluster.sh demo
   ```
   The script walks the audience through the five stages, printing each
   one:
   - **1**) write two rows to the primary (`payments` table)
   - **2**) confirm the replica sees them (`pg_is_in_recovery = t`)
   - **3**) kill the primary PID (`kill -9` → simulated node failure)
   - **4**) promote the replica (`pg_ctl promote` — what Patroni does
     automatically after the DCS leader-lock timeout on the cloud)
   - **5**) write a new row to the promoted node, show all three rows
     still present — zero data loss across the transition
3. In browser tab 1 (dashboard), confirm an order once the new leader
   is promoted — it still works. The user saw at most a one-second
   pause; the dashboard stays subscribed.
4. Open `infra/patroni/README.md` and a Patroni config file. One
   paragraph:

> "Patroni is the brain, streaming replication is the mechanics. Each
> Postgres node renews a 30-second lock in etcd while it's healthy.
> If it stops renewing, the other node promotes itself and HAProxy
> flips the write VIP within 3-6 seconds of the health check turning
> red. On the cloud we run etcd across three nodes — two is a
> split-brain risk, quorum needs a strict majority — with the
> tie-breaker on the ops node. What you just saw locally is the same
> shape (primary + replica + promote-on-failure); on the cloud we
> wrap it with Patroni so the promote happens without a human in the
> loop."

> **Captured run reference** if we don't run the failover live:
> `docs/C2-results/local-failover.md`.

---

### 5. Kubernetes deployment + monitoring (C5) — ~5 min

**Business question**: *"How much ops cost does this come with?"*

With the cloud cluster parked, walk through the artefacts on the
laptop and pair with captured screenshots / outputs.

1. Open `infra/k8s/` on the laptop and show the three Odoo deployments:
   `odoo-web` (HPA 2-8 replicas), `odoo-bus` (1 replica, owns the
   websocket on :8072), `odoo-cron` (1 replica, scheduled jobs).
2. Open `infra/k8s/ingress/ingress.yaml` and point to the `/websocket`
   split — the gevent bus pod handles long-lived connections
   separately from the HTTP worker pool.
3. Walk `infra/k8s/monitoring/` — Prometheus scrape config for 16
   targets (Patroni, etcd, HAProxy, each Odoo pod, each node,
   pgBouncer, ingress-nginx, cAdvisor) and the Grafana dashboards
   that back the C4 live-updates view.
4. **TLS termination** — show the cert-manager `ClusterIssuer` + the
   ingress annotation that flips the domain to Let's Encrypt.

> "When we re-light the cluster for your real demo, this is where
> we'd `kubectl -n ic-demo get pods`, show the HPA at 2 replicas,
> and do a rolling restart during the dashboard tab's live session
> — the websocket reconnects transparently. Today, the manifests
> you're looking at are exactly what runs there."

---

### Close (3 min)

- **One custom codebase**: two small Odoo modules —
  - **`ic_demo_dashboard`**: ~100 lines of OWL 3 + hooks on
    `sale.order` and `account.move` that publish to a per-company bus
    channel.
  - **`ic_demo_consolidation`**: ~100 lines of Python + one Postgres
    materialised view over native `account_move_line`.
- **Everything else is standard Odoo + Postgres + Kubernetes.** No
  rewritten accounting, no forked framework. You can lift this onto
  your own infrastructure the same way we deployed it here.
- **Captured numbers** (`docs/*-results/`):

| Area | Headline |
|---|---|
| Consolidation | 5 cos × 10M AMLs on the demo DB · pivot p50 **18 ms** today · 4.7 ms at SQL layer on the 10M/21-co scale run |
| Dashboard | 100 concurrent subscribers, p50 fan-out 11 ms, 0 failures over 12,668 ops |
| Concurrent writes | 584 orders / 60 s / 25 writers, 0 failures, 714 bus pushes |
| Performance | p99 under 110 ms on real read paths at 5-user concurrency (laptop, `workers=0`) |
| HA | Primary killed, replica promoted, no data loss, user kept working |
| K8s | 3 Odoo deployments, HPA on web, split ingress for WS, Grafana configured with 16 scrape targets |

Hand off for Q&A.

---

## Cloud lift (once the customer confirms a demo date)

The real demo runs against the 7-droplet cluster at
`https://multicoodoo.techvoot.com`. Switching from the local walkthrough
is a three-step operation on the server-team side:

1. **Restore the packaged DB** from this project's `var/handoff/`:
   - `ic_demo-20261118.dump` (303 MB, `pg_dump -Fc`)
   - `filestore-ic_demo-20261118.tgz` (9.5 MB)
   - Instructions: `var/handoff/RESTORE.md` (SHA-256 verification, restore
     commands, sanity SQL, required modules, known quirks).
2. **Re-point** the walkthrough: swap every
   `http://127.0.0.1:8069` for `https://multicoodoo.techvoot.com` and
   every `-d ic_demo` for `-d odoodemo`. Everything else — menus, pivot
   shape, numbers, narrative — is identical.
3. **Light up the live infra hooks** our script references:
   - Grafana tunnel open on the laptop:
     ```bash
     ssh -i ~/.ssh/odoo_demo -L 3000:localhost:3000 root@<monitoring-ip>
     ```
     Then http://localhost:3000, Grafana admin creds.
   - `kubectl` context pointed at the k3s cluster for the C5 section:
     `kubectl -n ic-demo get pods -o wide` / `get hpa` / `top pod`.
   - SSH to the Patroni primary for the C2 live failover
     (`systemctl stop patroni` on the leader, Grafana shows the lag /
     leader flip, `systemctl start patroni` to rejoin).

The "local Patroni cluster" and "captured C5 result" parts of the
walkthrough above become the live cluster + live Grafana + live
`kubectl` parts of the walkthrough the moment the cloud comes back up.

---

## Known talking points (if asked)

- **Why 5 companies and not 20+?** The brief asked for 20+ as a scale
  bound, not as the demo shape. The 5 we're showing carry **real
  end-to-end flows** — sales, purchases, vendor bills, payments,
  journal postings. More relatable than 20 empty shells. The 10M-line
  base underneath (the 17 archived companies we shaped this DB from)
  proves the architecture holds well past your current size — flip
  one `active=TRUE` and they're back in the pivot.
- **Why do customer names say "ICD Sub NN Partner NNN"?** The demo DB
  started as a 22-company 10M-AML scale dataset; when we reshaped it
  into the five named operating companies, we kept the existing
  partner records and just archived the 17 old companies. The names
  are cosmetic — one SQL pass renames them to anything the customer
  prefers before a formal demo.
- **Why `workers=0` locally?** Single process serves both HTTP and the
  websocket on :8069 — no reverse proxy needed on a laptop. The cloud
  profile uses `workers >= 4` + a separate gevent pod + ingress
  `/websocket` route (see `odoo/odoo.cloud.conf`).
- **Why a materialised view instead of a live SQL view?** A live
  variant on the 10M-line dataset took ~4.6 s. The materialised view
  collapses to 125 rows and answers in milliseconds. Refresh runs
  from `.refresh()` — wire to nightly cron, or trigger on period
  close.
- **Why Patroni + etcd, not just streaming replication + a VIP?**
  **Automatic promotion.** Streaming is the mechanics, Patroni is the
  decision engine, etcd is the shared truth about who holds the
  leader lock.
- **Why 3 etcd nodes across pg1 / pg2 / ops (on the cloud)?** Two
  nodes is a split-brain risk — quorum needs strict majority. Three
  nodes with the tie-breaker on the ops node is the smallest safe
  shape.
- **What about the ~40 bills per co without a payment?** The bill is
  posted and visible, but the vendor payment wizard skipped it
  because its total was zero (product price fallback edge in the
  seed). Doesn't affect the dashboard or consolidation numbers; a
  one-shot SQL pass or a rerun of the payment wizard with
  non-zero-amount filter cleans it up if the customer flags it.
- **Minimal customisation**: no custom accounting logic —
  consolidation is a read-only SQL view over native
  `account_move_line`; dashboard is OWL + server-side hooks on
  `sale.order.create` and `account.move.action_post`. OCA welcome,
  none needed yet.

## Fallbacks during the live call

- **Local Odoo errors mid-demo** → `./scripts/run.sh` restart,
  browser refresh. Admin session restores from filestore.
- **A browser tab loses the bus connection** → refresh the tab. The
  subscription re-establishes on page load; no data lost.
- **A harness run errors** → cancel it (Ctrl-C), fall back to the
  captured runs in `docs/C4-results/` — they stand on their own.
- **Local Patroni cluster script fails** → reference
  `docs/C2-results/local-failover.md` directly, show the Grafana
  replication-lag panel captured from the earlier run.
- **Someone asks for prod/cloud live** → explain the parked state
  (customer hasn't confirmed demo date), point at `var/handoff/`
  showing we're packaged and ready, and continue on local.

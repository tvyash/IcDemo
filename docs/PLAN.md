# ic_demo — technical plan

**Status:** draft for review · **Date:** 2026-11-12 · **Budget:** 40 engineering hours

## 1. Goal

Deliver a self-contained Odoo 20 Enterprise environment that demonstrates Techvoot's capability across five enterprise technical areas, screen-shared live to the prospect with Q&A.

## 2. Architecture

### Phase 1 — local development (native, no Docker)

Odoo 20 run directly on the host via `./scripts/run.sh` out of a Python 3.12 venv in `.venv/`. Addons sourced from:
- `/home/techvoot/work_space/20/base/odoo/addons` (Community)
- `/home/techvoot/work_space/20/base/enterprise` (Enterprise, branch 20.0)
- `addons/oca/` (vendored OCA modules; empty until C1)
- `addons/custom/ic_demo_dashboard/` (our only custom module, built in C4)

Database: host PostgreSQL 18 (Odoo 20's `MIN_PG_VERSION` is 16, so 18 is fine for dev; cloud profile pins 16). Role `ic_demo` / password `ic_demo` created by `scripts/setup.sh`.

No local Docker. Containers only appear in Phase 2 (cloud k3s deployment).

### Phase 2 — cloud demo environment (DigitalOcean, 7 droplets, same VPC, Ubuntu 24.04)

```
                      DNS (demo.<subdomain>)
                             │
                             ▼
                      ingress-nginx (k3s)
                             │
       ┌─────────────────────┼─────────────────────┐
       ▼                     ▼                     ▼
  odoo-web (HPA)        odoo-bus (gevent)      odoo-cron
  Deployment × 2+         Deployment × 1       Deployment × 1
       │                     │                     │
       └──────────────┬──────┴──────────────┬──────┘
                      ▼                     ▼
                 pgBouncer           MinIO (S3 filestore)
                      │
                      ▼
                   HAProxy    ────────────►  replica listener
                 (TCP 5432 / 5433)
                   │       │
                   ▼       ▼
                 pg1      pg2            etcd (3-node quorum,
              (Patroni primary)           co-located on pg1,
                                          pg2, ops-node)

Monitoring node: Prometheus, Grafana, odoo-prometheus-exporter,
                 patroni-exporter, k6/locust load generator
```

**Cluster roles (DO droplets):**

| Role                               | Qty | Spec             |
|------------------------------------|-----|------------------|
| PostgreSQL + Patroni + etcd        | 2   | 4 vCPU / 8 GB    |
| HAProxy + pgBouncer + etcd + MinIO | 1   | 2 vCPU / 4 GB    |
| k3s nodes (Odoo web / bus / cron)  | 3   | 4 vCPU / 8 GB    |
| Monitoring + load gen              | 1   | 4 vCPU / 8 GB    |

**Known demo trade-offs vs. production:**
- 2 PG nodes (demo); production = 3 for quorum + spare replica.
- etcd will be 3-node quorum by co-locating a member on each PG box + the ops node.
- Single MinIO (demo); production = distributed MinIO or managed object store.

## 3. Software choices

| Layer          | Pick                                                     |
|----------------|----------------------------------------------------------|
| Odoo           | 20.0 Enterprise, built from Techvoot's licensed repo     |
| Database       | PostgreSQL 16 + Patroni + etcd                           |
| DB routing     | HAProxy (write / read listeners) + pgBouncer (txn mode)  |
| Filestore      | MinIO (S3-compatible)                                    |
| Container      | k3s                                                      |
| Ingress        | ingress-nginx with sticky sessions (websocket)           |
| Monitoring     | Prometheus + Grafana + odoo-prometheus-exporter          |
| Load gen       | k6 (primary) or locust                                   |
| Provisioning   | Terraform (droplets + VPC + DNS) + Ansible (per-host)    |

### OCA / community modules to vendor

- `OCA/account-financial-reporting` — base for consolidated reporting views
- `OCA/server-tools` — `base_technical_user`, `database_cleanup`, `cache_cleaner`
- `OCA/queue` — for batch seed jobs (optional, if ORM path chosen)
- `OCA/server-env` — env-driven config for the k8s deploy
- Any OCA multi-company helper that survives the 20.0 migration (TBD at install time)

Native: `account_consolidation` (Enterprise) for the accounting consolidation report.

### Enterprise image build

The demo image is built from a private Dockerfile (TBD under `docker/odoo/`) that:
1. Starts from `python:3.12-slim-bookworm`.
2. Clones `odoo/odoo` and `odoo/enterprise` at the `20.0` branch using Techvoot's Enterprise git credentials (CI secret).
3. Installs system deps from `debian/control`.
4. Copies OCA addons + the custom `ic_demo_dashboard` module in.

Rebuilt once per cloud deploy; reused by all three Deployments (web / bus / cron) via different entrypoint flags.

## 4. 40-hour breakdown

| # | Area                                     | Hrs  |
|---|------------------------------------------|------|
| 1 | Multi-company + 10M+ txns + consolidated | 10   |
| 2 | PostgreSQL HA + failover demo            | 8    |
| 3 | Odoo 20 config + performance tuning      | 6    |
| 4 | OWL real-time dashboard + Bus            | 8    |
| 5 | DevOps / Kubernetes + monitoring         | 8    |

Rehearsal & demo runbook are handled from the slack time inside C3 + C5 (together ~2h), since those are the least open-ended.

### Challenge 1 — Multi-company + massive data (10h)
1. `base.company` hierarchy: 1 holding + 4 regional parents + 15–20 operating cos (2h)
2. Seed script `tools/seed.py` using psycopg3 `COPY` + explicit `id` sequences to bypass the ORM (ORM-based seed at 10M rows takes ~a day) (3h)
3. Install OCA account-financial-reporting + native `account_consolidation`; one scheduled consolidation per quarter (2h)
4. Materialised view `mv_consolidated_pnl` refreshed by cron; indexed on `(company_id, period)`; expose via custom report model (2h)
5. Benchmark report render on 10M rows, document EXPLAIN plans (1h)

### Challenge 2 — PG HA + failover (8h)
1. Ansible role: Patroni + PostgreSQL 16 on pg1/pg2; 3-node etcd on pg1/pg2/ops (2h)
2. HAProxy on ops node: 5432 (primary via patroni REST health check) + 5433 (replicas) (1h)
3. Odoo `odoo.conf`: `db_host = ops`, `db_port = 5432`, `db_replica_host = ops`, `db_replica_port = 5433` (0.5h)
4. MinIO shared filestore mounted into Odoo via `ir_attachment.location = s3://...` (1.5h)
5. Failover drill script: `patronictl switchover` + 50-user k6 load + capture reconnect timing to CSV (2h)
6. Documentation: topology diagram + runbook + prod-vs-demo callouts (1h)

### Challenge 3 — Config + perf (6h)
1. Final `odoo.conf` tuning per k3s node spec: workers = 9, memory limits sized to 8GB nodes, `db_maxconn = 32` (1h)
2. pgBouncer on ops node, transaction pooling, pool_size tuned to pg `max_connections` × 0.6 (1h)
3. Index audit on seed-affected tables (`account_move_line`, `sale_order_line`) (1h)
4. Before/after benchmark: same k6 scenario, un-tuned vs tuned, capture p50 / p95 / p99 (2h)
5. One-page "levers & impact" writeup (1h)

### Challenge 4 — OWL + Bus real-time (8h)
1. `addons/custom/ic_demo_dashboard/` — module skeleton, OWL root client action (1.5h)
2. Server: emit `bus.bus._sendone(f'ic_demo:dashboard:{company_id}', 'kpi.tick', payload)` on `sale.order` + `account.move` create/write (1.5h)
3. OWL: `useBus` hook subscribing to the per-company channel; sparkline + counters reactively updated (2h)
4. Websocket confirmation — nginx ingress annotation; longpoll fallback test (1h)
5. k6 ws script: 500 concurrent subscribers + 50 req/s event producer; capture message latency (1.5h)
6. Record a 60s GIF/video of the dashboard reacting under load (0.5h)

### Challenge 5 — K8s + monitoring (8h)
1. Terraform: 7 droplets, VPC, DNS record (1h)
2. Ansible: k3s install on 3 nodes, kubeconfig fetch, `metallb` or `--disable traefik` + ingress-nginx (1.5h)
3. Namespace + secrets + ConfigMap for `odoo.conf`; three Deployments (web / bus / cron), one Service each (1h)
4. HPA on `odoo-web` (CPU + custom metric via odoo-prometheus-exporter) (1h)
5. Rolling-update demo (bump a trivial module version, watch surge+unavailable settings), scripted (1h)
6. Prometheus + Grafana + patroni-exporter + odoo-exporter; two dashboards: "Cluster health" + "Request latency" (2h)
7. Smoke-test from monitoring node end-to-end (0.5h)

## 5. Demo day runbook (outline)

Single 60–75 min screen-share:
1. Intro slide — 5 min — architecture poster.
2. C1 live — 10 min — show 20 companies, trigger consolidated P&L, show mv refresh time.
3. C3 live — 5 min — Grafana latency panel before/after tuning.
4. C4 live — 10 min — open dashboard in 2 browsers, generate orders, show reactive update + k6 load.
5. C2 live — 15 min — `patronictl list` → trigger switchover during k6 load → show downtime window + auto-recovery.
6. C5 live — 10 min — trigger HPA by load spike, trigger rolling update, show Grafana.
7. Q&A — remaining.

## 6. Decisions (resolved 2026-11-12)

- **Enterprise source**: local checkout at `/home/techvoot/work_space/20/base/enterprise` (branch `20.0`). Local compose mounts it read-only; cloud image builds from a copy.
- **Subdomain**: not needed; server team will provision one if it turns out to be required. Demo IP + `/etc/hosts` is acceptable.
- **C4 concurrent-user target**: **100** concurrent dashboard subscribers under a modest event rate (≈20 msg/s/company). Keeps bus worker on the 2 vCPU budget.
- **Read-replica routing**: use **Odoo 20 native** read-replica support (`db_replica_host` + `db_replica_port` to HAProxy replica listener). Cleaner than app-level routing and surfaces an Odoo-20-specific capability on the demo.

## 7. Still open

- OCA modules compatibility against 20.0 — some repos lag 1-2 months behind a new major; verify at install time and pin to master branch if needed.

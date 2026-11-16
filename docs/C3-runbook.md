# C3 — Odoo 20 config + performance tuning — runbook

Challenge 3: show the Odoo 20 workload runs with a tuned config at
production-shaped settings, and the whole stack (not just one request)
holds a predictable latency profile.

## What's in the box

| Piece | Where |
|---|---|
| Three-profile Odoo config scheme (`odoo.conf`, `odoo.perf.conf`, `odoo.cloud.conf`) | `odoo/` |
| Patroni-managed PG tuning parameters (go in `patroni.yml`'s `bootstrap.dcs.postgresql.parameters`) | `infra/patroni/patroni-pg1.yml` lines 23-33 |
| pgBouncer in front of HAProxy (transaction-pooling, 6432) | documented in `odoo.cloud.conf` |
| Benchmark harness — p50/p95/p99 across the three real endpoints | `tools/c3/bench.py` |
| Supporting indexes created for C1 | `docs/C1-runbook.md` + idempotent re-apply in `docs/C1-results/10M-seed-run.md` |

## The three Odoo profiles and why

| Profile | File | Workers | DB maxconn | Websocket on | Use |
|---|---|---|---|---|---|
| **demo** | `odoo/odoo.conf` | **0** (ThreadedServer) | 150 | :8069 (single process handles both) | Browser walkthrough — one port, no proxy needed. |
| **perf** | `odoo/odoo.perf.conf` | 4 preforked + 1 gevent | 150 | :8072 dedicated gevent service | Local load tests (C4 100-user bus fanout), shape matches cloud. |
| **cloud** | `odoo/odoo.cloud.conf` | 9 per HTTP pod (= 2 × vCPU + 1) | 20 (pgBouncer pools) | :8072 on a separate `odoo-bus` Deployment | k3s production. HPA scales the HTTP Deployment; bus and cron stay 1 replica. |

Only `workers`, `db_maxconn`, `max_cron_threads`, `logfile`, and the
`db_host`/`db_replica_host` endpoints change between them. Everything else
(addons path, data dir, limits) stays identical, which keeps the three
environments honest.

## Why each knob

- **`workers = 2 × vCPU + 1`** — Odoo's HTTP workers are process-based; the
  +1 absorbs one worker being stuck in a slow query. On a 4-vCPU k8s pod
  that's 9. Below the elbow, latency falls; above it, context switching
  eats the gain.
- **`limit_memory_hard = 2.5 GB`** per worker — matches the k8s pod
  `resources.limits.memory`. A runaway report queries doesn't take the pod
  with it; the worker is recycled before OOM.
- **`limit_time_cpu = 60 / limit_time_real = 120`** — Odoo's own wall-clock
  guard. Long-running reports go to the cron deployment, which has
  `limit_time_real_cron = 1800`.
- **`db_maxconn = 20`** in cloud, **150** locally. pgBouncer is in
  transaction-pooling mode in front of HAProxy, so each worker holds a
  short-lived client-side pool. 20 workers × 9 pods × 20 = 3600 potential
  sockets at the pgBouncer; pgBouncer collapses those to ~50 backend
  connections, which is what Patroni/PG see.
- **`gevent_port = 8072` + separate `odoo-bus` deployment** — the HTTP
  preforked workers can't handle websocket upgrades (HTTP/1.1 keep-alive is
  not enough). The bus deployment runs `workers = 0` and handles only the
  gevent service; ingress routes `/websocket/*` to it.
- **`dbfilter = ^ic_demo$`** — hard-pin to one DB, so the ingress can't
  accidentally get past our DB-pick screen.
- **`proxy_mode = True`** — Odoo reads `X-Forwarded-*` headers from the
  ingress so absolute URLs are right.
- **`admin_passwd`** — DB-management passphrase is injected from a k8s
  Secret; never in Git.

## Supporting Postgres parameters (from `patroni-pg1.yml`)

```yaml
bootstrap:
  dcs:
    postgresql:
      parameters:
        max_connections: 300
        shared_buffers: 2GB
        effective_cache_size: 6GB
        work_mem: 16MB
        maintenance_work_mem: 256MB
        wal_level: replica
        max_wal_senders: 10
        wal_keep_size: 2GB
        hot_standby: 'on'
        synchronous_commit: 'on'
        checkpoint_completion_target: 0.9
        random_page_cost: 1.1   # SSD
```

- **`shared_buffers = 2 GB`** on an 8 GB node ≈ 25 % RAM, canonical PG advice.
- **`effective_cache_size = 6 GB`** tells the planner how much the OS page
  cache typically holds; makes indexed scans look cheaper.
- **`work_mem = 16 MB`** per sort/hash step — safe with 300 max connections
  (worst case 300 × 16 = 4.8 GB, won't collide with shared_buffers).
- **`random_page_cost = 1.1`** on SSDs — defaults assume spinning disks.
- **`max_wal_senders = 10`** — one per replica + headroom for `pg_basebackup`
  during Patroni clone.

## Supporting indexes created for the hot read paths

Already applied to the live DB:

```sql
CREATE INDEX IF NOT EXISTS ic_demo_aml_company_date
    ON account_move_line (company_id, date)
    WHERE display_type NOT IN ('line_section','line_subsection','line_note');
CREATE INDEX IF NOT EXISTS ic_demo_aml_account
    ON account_move_line (account_id)
    WHERE display_type NOT IN ('line_section','line_subsection','line_note');
CREATE INDEX IF NOT EXISTS ic_demo_am_state_posted
    ON account_move (id) WHERE state='posted';
```

Plus the three on the C1 materialised view itself (unique on `id`,
`(company_id, period)`, and `period`).

## Running the benchmark

```bash
cd /home/techvoot/work_space/20/projects/ic_demo
.venv-tools/bin/python tools/c3/bench.py \
    --host http://127.0.0.1:8069 \
    --iterations 300 --concurrency 10 \
    --scenarios dashboard_snapshot,consolidation_pivot
```

What it does:
- Authenticates 10 concurrent sessions (one per thread), reused for the whole run.
- Fires `--iterations` RPC calls across those sessions.
- Reports p50 / p95 / p99 / mean / max wall-clock per scenario.
- Ends with a single `SUMMARY_JSON=...` line for scripted capture.

Numbers captured against the current demo profile (workers=0) are in
`docs/C3-results/baseline.md`.

## Cloud sizing reference

| Pod | Replicas | vCPU | Memory | Odoo profile override |
|---|---|---|---|---|
| `odoo-web` | HPA 2-8 | 4 | 10 Gi | `workers = 9` |
| `odoo-bus` | 1 | 2 | 4 Gi | `workers = 0`, exposes :8072 only |
| `odoo-cron` | 1 | 2 | 4 Gi | `workers = 0`, `max_cron_threads = 2` |
| `pgBouncer` | 2 | 1 | 1 Gi | transaction pooling, 50 backend conns each |
| `pg1/pg2` | 2 (Patroni) | 4 | 8 Gi | see `infra/patroni/patroni-pg1.yml` |
| `ops` | 1 | 2 | 4 Gi | HAProxy + etcd tie-breaker + Prometheus |
| `k3s` | 3 | 4 | 8 Gi | hosts `odoo-*` + ingress + MinIO |

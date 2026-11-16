# ic_demo — Odoo 20 Enterprise technical demonstration

A 40-hour technical showcase across five enterprise challenges:

1. **C1** — Multi-company (20+), 10M+ transactions, consolidated reporting
2. **C2** — PostgreSQL high availability with Patroni + automatic failover
3. **C3** — Advanced Odoo 20 configuration + performance tuning
4. **C4** — OWL frontend + real-time bus synchronisation (100-user target)
5. **C5** — k3s deployment + ingress + monitoring

Everything in C1-C4 runs locally on the project machine today. C5 ships
as k8s manifests + install scripts ready to apply to the DO droplet set.

## Repo layout

```
addons/custom/               Odoo 20 Enterprise modules we own
  ic_demo_dashboard/         C4 — OWL 3 real-time dashboard + bus hooks
  ic_demo_consolidation/     C1 — materialised-view P&L across companies

tools/
  seed/                      C1 seed scripts (ORM bootstrap + raw COPY)
  load/                      C4 locust harness (100 WS subscribers)
  c2/local_cluster.sh        C2 local PG failover demo
  c3/bench.py                C3 RPC latency benchmark

infra/
  patroni/                   Patroni + etcd configs (C2 cloud)
  haproxy/haproxy.cfg        HAProxy routing (C2 cloud)
  k3s/                       k3s install scripts (C5 cloud)
  k8s/                       Odoo + pgBouncer + ingress + monitoring (C5 cloud)

odoo/
  odoo.conf                  local demo profile (workers=0, one process)
  odoo.perf.conf             local perf profile (workers=4 + gevent, C4 load)
  odoo.cloud.conf            k3s production profile

docs/
  PLAN.md                    40-hour plan + architecture notes
  demo-script.md             live walkthrough script
  C{1..5}-runbook.md         per-challenge how-to
  C{1,2,3,4}-results/        captured benchmark numbers
```

## Quickstart (local, no Docker)

**Prereqs:** Ubuntu 24.04, Python 3.12, PostgreSQL 18, Odoo 20 source
checked out somewhere + Enterprise alongside it.

**First-time only** — copy the host-specific templates and edit the paths
to match your box:

```bash
cp odoo/odoo.conf.example               odoo/odoo.conf
cp odoo/odoo.perf.conf.example          odoo/odoo.perf.conf
cp scripts/setup.sh.example             scripts/setup.sh   && chmod +x scripts/setup.sh
cp scripts/run.sh.example               scripts/run.sh     && chmod +x scripts/run.sh
cp tools/seed/c1_bootstrap.py.example   tools/seed/c1_bootstrap.py
cp tools/seed/c1_bulk.py.example        tools/seed/c1_bulk.py

# then edit the 9 paths that point at /home/techvoot/... in those 6 files.
# The copies are gitignored, so your edits never leak back to the repo.
```

Then the normal flow:

```bash
./scripts/setup.sh                                                # venvs, PG role
./scripts/run.sh -d ic_demo -i base,bus,sale_management,account,ic_demo_dashboard,ic_demo_consolidation --stop-after-init
./scripts/run.sh                                                  # http://localhost:8069
```

Log in `admin` / `admin`. Top menu exposes:
- **IC Demo → Live KPI Dashboard** (C4)
- **IC Demo → Consolidated P&L** (C1)

## Running each challenge

| Challenge | Quick command | Runbook |
|---|---|---|
| C1 — 10M-AML seed | `.venv/bin/python tools/seed/c1_bootstrap.py --companies 20 --partners 100 && .venv/bin/python tools/seed/c1_bulk.py --moves 5000000` | [docs/C1-runbook.md](docs/C1-runbook.md) |
| C2 — PG failover | `./tools/c2/local_cluster.sh init && ./tools/c2/local_cluster.sh demo` | [docs/C2-runbook.md](docs/C2-runbook.md) |
| C3 — benchmark | `.venv-tools/bin/python tools/c3/bench.py --iterations 300 --concurrency 10` | [docs/C3-runbook.md](docs/C3-runbook.md) |
| C4 — 100-user WS load | `IC_DEMO_WS_PORT=8072 .venv-tools/bin/locust -f tools/load/locustfile.py --host http://127.0.0.1:8069 --users 101 --spawn-rate 20 --run-time 60s --headless --only-summary` (after switching to `odoo.perf.conf`) | [docs/C4-runbook.md](docs/C4-runbook.md) |
| C5 — k3s deploy | per-droplet `apt install + bash infra/k3s/install-*.sh` then `kubectl apply -f infra/k8s/...` | [docs/C5-runbook.md](docs/C5-runbook.md) |

## Captured numbers

| | Headline |
|---|---|
| C1 | 10,000,000 AMLs across 21 companies seeded in 22 min; consolidation pivot 4.7 ms via psql, 42 ms via Odoo RPC |
| C2 | Primary killed, replica promoted, 2 pre-failover rows + 1 post-failover row all present; `pg_is_in_recovery()` flipped `t → f` |
| C3 | 0 errors across 600 RPC calls @ concurrency 10; dashboard p50/p95/p99 = 36.5 / 114.1 / 244.1 ms; consolidation p50/p95/p99 = 42.2 / 59.4 / 73.2 ms |
| C4 | 100/100 subscribers, 0 failures of 12,668 ops, fan-out latency p50/p95 = 11 / 16 ms, 12,500 notifications in 60 s |

Full captures in `docs/C{1,2,3,4}-results/`.

## The two Python venvs (why)

- `.venv` — Odoo runtime + Odoo-safe extras (`psycopg[binary]`, `faker`, `tqdm`, `phonenumbers`).
- `.venv-tools` — load/HA tooling (`locust`, `websocket-client`, `patroni[etcd3]`). Pins conflict with Odoo (gevent / werkzeug / flask) so they can't share a venv.

Both are created by `./scripts/setup.sh`.

## Walkthrough

For the live demo, follow [`docs/demo-script.md`](docs/demo-script.md) —
one 30-40 minute script that covers all five challenges with timings,
captures, and handoff points.
# IcDemo

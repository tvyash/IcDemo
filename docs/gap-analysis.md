# Gap analysis — lead's brief vs. what we have

Original brief (paraphrased from the lead email): a practical Odoo 20
demo spanning five technical areas, inside a 40-hour evaluation window.

| # | Brief item | Status | Where it lives |
|---|---|---|---|
| **1. Multi-Company Architecture & Massive Data Reporting** | | | |
| | "simulated high-volume transactions" | ✅ **10,000,000 AMLs** seeded | `tools/seed/c1_bulk.py` + `docs/C1-results/10M-seed-run.md` |
| | "multiple companies" | ✅ **21 companies** (1 parent + 20 subs) | `tools/seed/c1_bootstrap.py` |
| | "consolidated reporting" | ✅ Materialised view + Pivot/Graph/List UI under IC Demo menu | `addons/custom/ic_demo_consolidation/` |
| **2. PostgreSQL HA & Automatic Failover** | | | |
| | "Primary/Standby architecture" | ✅ Local 2-node cluster + 2-node cloud design | `tools/c2/local_cluster.sh`, `infra/patroni/*.yml` |
| | "streaming replication" | ✅ Local uses physical streaming repl; cloud Patroni runs it with pg_rewind | `tools/c2/local_cluster.sh`, `infra/patroni/*.yml` |
| | "Patroni" | ✅ Full cloud configs for pg1/pg2 + 3-node etcd + HAProxy | `infra/patroni/`, `infra/haproxy/haproxy.cfg` |
| | "automatic failover" | ✅ Locally demoed manually; cloud is automatic via etcd leader-lock TTL (30 s) | `docs/C2-results/local-failover.md` + `infra/patroni/README.md` |
| **3. Advanced Odoo 20 Config & Performance** | | | |
| | "optimization of Odoo configuration" | ✅ Three profiles (demo / perf / cloud) | `odoo/odoo.conf`, `odoo.perf.conf`, `odoo.cloud.conf` |
| | "workers" | ✅ 2 × vCPU + 1 rule applied; separate deployments for HTTP / bus / cron | `odoo/odoo.cloud.conf`, `infra/k8s/odoo/deploy-*.yaml` |
| | "memory allocation" | ✅ `limit_memory_soft/hard` aligned with k8s pod `resources.limits` | same |
| | "database connections" | ✅ pgBouncer transaction-pooling in front of HAProxy, `db_maxconn = 20` per Odoo pod | `infra/k8s/pgbouncer/pgbouncer.yaml` + `odoo/odoo.cloud.conf` |
| | "performance" | ✅ RPC benchmark harness; p50 36-42 ms on live DB with 10M AMLs | `tools/c3/bench.py` + `docs/C3-results/baseline.md` |
| **4. OWL Frontend & Real-Time Synchronization** | | | |
| | "interactive OWL dashboard with Odoo Bus" | ✅ OWL 3 client action + bus plugin | `addons/custom/ic_demo_dashboard/` |
| | "real-time updates" | ✅ `sale.order` + `account.move` server hooks push to `ic_demo:dashboard:<company_id>`; OWL 3 subscriber renders tiles live | `addons/custom/ic_demo_dashboard/models/*.py`, `static/src/js/dashboard.js` |
| | "concurrent user scenarios" | ✅ Two load tests: 100 WS subscribers fan-out + 25 live users writing concurrently | `tools/load/locustfile.py`, `tools/load/concurrent_writers.py`, `docs/C4-results/*.md` |
| **5. DevOps, Kubernetes & Odoo Infrastructure** | | | |
| | "Odoo deployment using Kubernetes" | ✅ k3s install scripts + Odoo web/bus/cron Deployments | `infra/k3s/*.sh`, `infra/k8s/odoo/deploy-*.yaml` |
| | "load balancing" | ✅ ingress-nginx (HTTP + WS), HAProxy (SQL writes/reads) | `infra/k8s/ingress/ingress.yaml`, `infra/haproxy/haproxy.cfg` |
| | "horizontal scaling" | ✅ HPA 2-8 on `odoo-web` (CPU + memory metrics) | `infra/k8s/odoo/deploy-web.yaml` |
| | "monitoring" | ✅ Prometheus + Grafana manifests, scrape config covers Patroni / etcd / HAProxy / nodes / k8s | `infra/k8s/monitoring/*.yaml` |
| | "rolling updates" | ⚠️ **Documented in failure drills, not demoed live** | `docs/C5-runbook.md` § "Failure drills" |

## Meta-asks from the email

| Ask | Status |
|---|---|
| "evaluate technical capabilities of Techvoot through tangible results" | ✅ Four `C{1,2,3,4}-results/*.md` files with captured numbers |
| "clearer understanding of development methodology" | ✅ Runbooks (`docs/C{1..5}-runbook.md`) show the thinking per challenge |
| "architectural decisions" | ✅ `docs/PLAN.md` + each runbook's "Why each knob" / "Why Patroni + etcd" sections |
| "foundation for long-term partnership" | ✅ All configs parametrised with `${IC_DEMO_*}` env vars, three profiles mirror prod, no custom accounting logic — easy to lift into the lead's own environment |
| "a suggested breakdown of the 40 hours across the five challenges" | ✅ `docs/PLAN.md` |

## Known gaps / what we chose not to do

1. **Grafana dashboards are empty stubs** — the manifest ships named JSON
   files but the panels are placeholders. Filling them in requires one
   live run of the stack to see real metric cardinality. **Roughly 2-3 h
   of post-deploy work; mark as "finish on cloud day 3".**
2. **Rolling-update demo** — procedure is in `docs/C5-runbook.md` under
   "Failure drills to rehearse", but we don't have a captured run.
   **Will capture during cloud bring-up (Day 3)**: `kubectl rollout restart`
   + show session continuity + HPA behaviour.
3. **Live automatic Patroni failover** — the local demo uses manual
   `pg_ctl promote` as a stand-in for the etcd-locked decision. The real
   automated failover only happens on the cloud cluster because it needs
   three etcd members. **Will capture on Day 3.**
4. **OCA modules** — the brief didn't require any; we installed none.
   Memory records our preference for OCA when it fits, but no OCA module
   ended up being closer to the task than the thin custom modules
   (dashboard = 100 LOC, consolidation = 100 LOC).
5. **Odoo image build (private registry)** — the k8s manifests reference
   `odoo:20.0`. For real deploy we'll bake Enterprise + custom addons
   into a private image. **Day 3 work, blocked on registry access.**
6. **Terraform / Ansible for droplet provisioning** — we have per-droplet
   install scripts (`infra/k3s/install-*.sh`) but no top-down IaC. The
   server team's existing process covers this.
7. **Backups** — pgBackRest / WAL-G not configured in the Patroni files.
   **Day 3 work, needs an object-storage target.**
8. **TLS termination** — ingress uses plain HTTP; cert-manager + Let's
   Encrypt is the next step before any real traffic. **Day 3 work.**

Everything in the "gaps" list is explicitly Day 3 (cloud bring-up) work.
The eval-window-hours deliverables are all live or shipped as manifests.

## Hour budget used so far

| Day | Logged | Key work |
|---|---|---|
| 2026-11-12 | 7.0 h | Proposal review, R&D on all 5 areas, architecture draft, scaffolding |
| 2026-11-13 | 3.5 h | OWL 3 migration fixes (useService→user, useState→proxy, template `this.state`) |
| 2026-11-13 | +~10 h | C1 seed + consolidation module, C4 load harness, both captured runs |
| 2026-11-16 | ~6 h | C2 local cluster + Patroni/HAProxy configs, C3 benchmark, C5 full k8s manifests + runbook, concurrent-writers demo |
| **Total** | **~26 h** | Of 40 h budget |

Remaining ~14 h cleanly covers Day 3 cloud bring-up + the gap-list items
above + dress rehearsal.

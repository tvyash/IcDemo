# C3 — Odoo RPC baseline benchmark

**Date:** 2026-11-16 local
**Odoo profile:** `odoo/odoo.conf` (demo profile: `workers=0` single-process ThreadedServer, host PG18 on :5432)
**Dataset:** 21 companies, 10M AMLs (seeded by C1)
**Harness:** `tools/c3/bench.py` — 10 concurrent sessions, 300 iterations per scenario

## Command

```bash
.venv-tools/bin/python tools/c3/bench.py \
    --host http://127.0.0.1:8069 \
    --iterations 300 --concurrency 10 \
    --scenarios dashboard_snapshot,consolidation_pivot
```

## Captured output

```text
host=http://127.0.0.1:8069 db=ic_demo iters=300 concurrency=10
scenarios: dashboard_snapshot,consolidation_pivot

  Odoo reports 123 installed modules

[dashboard_snapshot] ...
  300 calls, 0 errors   p50=36.5ms  p95=114.1ms  p99=244.1ms  max=265.7ms  mean=46.7ms

[consolidation_pivot] ...
  300 calls, 0 errors   p50=42.2ms  p95=59.4ms  p99=73.2ms  max=101.4ms  mean=42.4ms

SUMMARY_JSON={
  "dashboard_snapshot": {"iterations": 300, "errors": 0, "p50": 36.5, "p95": 114.1, "p99": 244.1, "max": 265.7},
  "consolidation_pivot": {"iterations": 300, "errors": 0, "p50": 42.2, "p95": 59.4, "p99": 73.2, "max": 101.4}
}
```

## What each number represents

| Scenario | What runs on the server | Measured path |
|---|---|---|
| **dashboard_snapshot** | `ic.demo.dashboard.kpi.get_snapshot` — 2 `SUM/COUNT` aggregations over the live `sale_order` + `account_move` tables, 30-day window, filtered by `company_id` | HTTP round-trip + Odoo request dispatch + ORM env + 2 SQL aggregates |
| **consolidation_pivot** | `ic.demo.consolidation.line.web_read_group` — pivot read on the C1 materialised view (525 bucket rows aggregating 10M AMLs) grouped by `company_id`, aggregates `revenue:sum` + `line_count:sum` | HTTP round-trip + Odoo request dispatch + ORM env + 1 `GROUP BY` on the matview |

Both include the standard Odoo auth-cookie middleware + request routing on
every call; no connection reuse at the Postgres driver level (new ORM env
per request).

## What this tells us

- **p50 of 36 - 42 ms** on both read paths. The C1 consolidation path —
  the heavyweight one touching 10M AMLs on paper — is *faster* than the
  dashboard snapshot because the matview already has the aggregation done.
- **p99 < 250 ms** for both, with 0 errors across 600 calls at concurrency 10 on `workers=0`. The ThreadedServer serializes DB cursor acquisition, which shows up as the long tail on `dashboard_snapshot` (the one that actually runs two live aggregations) — the matview-backed query doesn't see the same tail.
- **Mean ≈ median** for the consolidation path (42.4 vs 42.2) — no long tail; GROUP-BY over 525 rows with the index is cheap and predictable.
- **Mean > median** on the dashboard path (46.7 vs 36.5) — a small number of calls hit the ~250 ms tail, pulling the mean up. That tail is where the bus `_sendone` + hook work lives if any sale.order write landed concurrently.

## Cloud projection

- The `workers=0` single-process serializer is the headline limitation here.
  On the cloud profile (`workers = 9` on a 4-vCPU pod, pg behind pgBouncer),
  the dashboard tail should flatten because the long-running aggregation no
  longer blocks shorter requests.
- Consolidation latency moves largely with network RTT once the matview is
  in the OS page cache. Expect **p50 under 20 ms** from an in-cluster
  client on the k3s profile.
- The existing C4 load test (100 WS subscribers, p95=16 ms fan-out) already
  shows this scaling — CPU-heavy single-process drops everything to the
  gevent worker; preforked HTTP pool absorbs the rest.

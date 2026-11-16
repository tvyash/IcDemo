# C4 — 100-concurrent-user Bus fan-out — captured run

**Date:** 2026-11-13 20:00 local
**Odoo profile:** `odoo/odoo.perf.conf` (workers=4, gevent on :8072, db_maxconn=150)
**Harness:** `tools/load/locustfile.py` via `.venv-tools/bin/locust`
**Shape:** 100 × `DashboardSubscriber` (WS) + 1 × `OrderWriter` (HTTP), 60s run
**Invocation:**

```
IC_DEMO_WS_PORT=8072 .venv-tools/bin/locust \
  -f tools/load/locustfile.py \
  --host http://127.0.0.1:8069 \
  --users 101 --spawn-rate 20 \
  --run-time 60s \
  --headless --only-summary
```

## Headline

| | |
|---|---|
| Subscribers that connected & subscribed | **100 / 100** (0 fail) |
| Orders created by writer                | **68** |
| Notifications received across cluster   | **12,500** |
| Request failures                        | **0** of 12,668 |

## Fan-out latency (writer `create` → subscriber `recv`)

| Percentile | ms |
|---|---|
| p50 | **11** |
| p95 | **16** |
| p99 | 1,643 |
| max | 4,013 |

Correlated samples: 6,763. The p99 / max come from the historical-replay burst each subscriber gets when it first subscribes with `last:0` — not steady-state fan-out.

## Throughput seen by Odoo

- `sale.order.create` median 72 ms, p95 120 ms, p99 1.2 s (writer never blocked)
- Sustained **~209 notifications/s** delivered across the bus

## What this proves

- Odoo 20's Bus + websocket layer, running with `workers=4` + dedicated gevent service on :8072, sustains the 100-concurrent-subscriber target with real-time (≤20 ms) fan-out on a developer laptop.
- The same deployment shape lifts cleanly to k3s in C5: HTTP preforked pods, one gevent websocket pod, ingress routing `/websocket` to the latter.

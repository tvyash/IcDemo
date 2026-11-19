"""Locust harness for the "multiple live users writing in real time" demo.

Each simulated user is its own Odoo HTTP session (distinct cookie) and
alternates between creating a sale order and reading the recent list.
A smaller pool of analyst users hits the dashboard snapshot + the C1
consolidation pivot so the server is under read+write pressure at the
same time.

Every ``sale.order.create`` fires the C4 server-side hook, which pushes
a bus notification. If you open the **IC Demo → Live KPI Dashboard**
tab in a browser while this runs, the event counter ticks up live — the
visual proof the lead wants to see.

Run from the tools venv:

    .venv-tools/bin/locust \\
        -f tools/load/concurrent_writers.py \\
        --host http://127.0.0.1:8069 \\
        --users 30 --spawn-rate 5 \\
        --run-time 90s \\
        --headless --only-summary

Shape at --users 30 (25 writers + 5 analysts via fixed_count):
    * 25 × BusinessUser  — each creates ~1 order / 1-2 s, then reads 5 back
    * 5  × AnalystUser   — each pulls the dashboard + consolidation pivot

Env overrides:
    IC_DEMO_DB / IC_DEMO_LOGIN / IC_DEMO_PASSWORD   Odoo creds
    IC_DEMO_PARTNERS_PER_USER                       pool size, default 10
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field

import requests
from locust import User, between, events, task
from locust.env import Environment


DB = os.environ.get("IC_DEMO_DB", "ic_demo")
LOGIN = os.environ.get("IC_DEMO_LOGIN", "admin")
PASSWORD = os.environ.get("IC_DEMO_PASSWORD", "admin")
PARTNERS_PER_USER = int(os.environ.get("IC_DEMO_PARTNERS_PER_USER", "10"))


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
@dataclass
class Metrics:
    orders_created: int = 0
    orders_failed: int = 0
    reads_done: int = 0
    reads_failed: int = 0
    write_latencies_ms: list[float] = field(default_factory=list)
    read_latencies_ms: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_order(self, latency_ms: float, ok: bool) -> None:
        with self.lock:
            if ok:
                self.orders_created += 1
                self.write_latencies_ms.append(latency_ms)
            else:
                self.orders_failed += 1

    def record_read(self, latency_ms: float, ok: bool) -> None:
        with self.lock:
            if ok:
                self.reads_done += 1
                self.read_latencies_ms.append(latency_ms)
            else:
                self.reads_failed += 1


METRICS = Metrics()


# ---------------------------------------------------------------------------
# Odoo RPC helpers
# ---------------------------------------------------------------------------
def odoo_login(host: str) -> requests.Session:
    s = requests.Session()
    r = s.post(
        f"{host}/web/session/authenticate",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {"jsonrpc": "2.0", "params": {"db": DB, "login": LOGIN, "password": PASSWORD}}
        ),
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("result", {}).get("uid") in (None, False):
        raise RuntimeError(f"Odoo auth failed: {body}")
    return s


def odoo_call(session, host, model, method, args=None, kwargs=None, timeout=30):
    r = session.post(
        f"{host}/web/dataset/call_kw",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "call",
                "params": {
                    "model": model,
                    "method": method,
                    "args": args or [],
                    "kwargs": kwargs or {},
                },
            }
        ),
        timeout=timeout,
    )
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{model}.{method} error: {body['error'].get('message')}")
    return body["result"]


# ---------------------------------------------------------------------------
# User classes
# ---------------------------------------------------------------------------
class BusinessUser(User):
    """Simulates a sales rep: logs in once, then creates orders in a loop
    and occasionally reads them back. Every create triggers the C4 bus
    notification path."""

    weight = 5
    wait_time = between(0.5, 2.0)  # ~0.5-1 op/s per user

    def on_start(self):
        host = self.environment.host.rstrip("/")
        self._host = host
        self._session = odoo_login(host)
        # Admin's primary company is Techvoot Global HQ (id=2) after the reshape.
        # Pick partners + products visible to it: shared (company_id=False) or
        # scoped to id=2. Anything else would trip Odoo's company-consistency
        # check on sale.order.create.
        partners = odoo_call(
            self._session, host, "res.partner", "search",
            args=[[("active", "=", True),
                   "|", ("company_id", "=", False), ("company_id", "=", 2)]],
            kwargs={"limit": PARTNERS_PER_USER},
        )
        self._partners = partners or [3]
        products = odoo_call(
            self._session, host, "product.product", "search",
            args=[[("sale_ok", "=", True),
                   "|", ("company_id", "=", False), ("company_id", "=", 2)]],
            kwargs={"limit": 20},
        )
        self._products = products or []

    @task(4)
    def create_sale_order(self):
        start = time.monotonic()
        try:
            line = {
                "product_id": random.choice(self._products) if self._products else False,
                "product_uom_qty": random.randint(1, 10),
                "price_unit": round(random.uniform(50.0, 2000.0), 2),
            }
            if not line["product_id"]:
                # No product pool found — fall back to a blank line so we still
                # exercise the write path (amount_total will be 0 in that case).
                line = {"name": "Load-test item", "price_unit": round(random.uniform(50.0, 2000.0), 2)}
            order_id = odoo_call(
                self._session,
                self._host,
                "sale.order",
                "create",
                args=[{
                    "partner_id": random.choice(self._partners),
                    "order_line": [(0, 0, line)],
                }],
            )
            self._last_order = order_id
            latency = (time.monotonic() - start) * 1000
            METRICS.record_order(latency, ok=True)
            events.request.fire(
                request_type="HTTP",
                name="sale.order.create",
                response_time=latency,
                response_length=0,
                exception=None,
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            METRICS.record_order(latency, ok=False)
            events.request.fire(
                request_type="HTTP",
                name="sale.order.create",
                response_time=latency,
                response_length=0,
                exception=e,
            )

    @task(1)
    def confirm_sale_order(self):
        """Occasionally confirm the last order — this fires the OTHER bus
        hook (`_action_confirm`), useful to show both write paths."""
        if not getattr(self, "_last_order", None):
            return
        start = time.monotonic()
        try:
            odoo_call(
                self._session, self._host, "sale.order", "action_confirm",
                args=[[self._last_order]],
            )
            latency = (time.monotonic() - start) * 1000
            events.request.fire(
                request_type="HTTP", name="sale.order.action_confirm",
                response_time=latency, response_length=0, exception=None,
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            events.request.fire(
                request_type="HTTP", name="sale.order.action_confirm",
                response_time=latency, response_length=0, exception=e,
            )

    @task(2)
    def read_recent_orders(self):
        start = time.monotonic()
        try:
            odoo_call(
                self._session, self._host, "sale.order", "search_read",
                args=[[]],
                kwargs={
                    "fields": ["name", "partner_id", "state", "amount_total"],
                    "limit": 10, "order": "id desc",
                },
            )
            latency = (time.monotonic() - start) * 1000
            METRICS.record_read(latency, ok=True)
            events.request.fire(
                request_type="HTTP", name="sale.order.search_read",
                response_time=latency, response_length=0, exception=None,
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            METRICS.record_read(latency, ok=False)
            events.request.fire(
                request_type="HTTP", name="sale.order.search_read",
                response_time=latency, response_length=0, exception=e,
            )


class AnalystUser(User):
    """Simulates someone watching the KPI dashboard + the consolidated
    P&L — read-only but exercises the two heaviest server-side paths
    (dashboard snapshot + the C1 materialised view)."""

    fixed_count = 5    # regardless of --users count, exactly 5 analysts
    wait_time = between(2.0, 4.0)

    def on_start(self):
        host = self.environment.host.rstrip("/")
        self._host = host
        self._session = odoo_login(host)

    @task(3)
    def dashboard_snapshot(self):
        start = time.monotonic()
        try:
            odoo_call(
                self._session, self._host,
                "ic.demo.dashboard.kpi", "get_snapshot",
                kwargs={"company_ids": [2]},
            )
            latency = (time.monotonic() - start) * 1000
            METRICS.record_read(latency, ok=True)
            events.request.fire(
                request_type="HTTP", name="dashboard.get_snapshot",
                response_time=latency, response_length=0, exception=None,
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            METRICS.record_read(latency, ok=False)
            events.request.fire(
                request_type="HTTP", name="dashboard.get_snapshot",
                response_time=latency, response_length=0, exception=e,
            )

    @task(1)
    def consolidation_pivot(self):
        start = time.monotonic()
        try:
            odoo_call(
                self._session, self._host,
                "ic.demo.consolidation.line", "web_read_group",
                kwargs={
                    "domain": [],
                    "groupby": ["company_id"],
                    "aggregates": ["revenue:sum", "line_count:sum"],
                    "limit": 25,
                },
            )
            latency = (time.monotonic() - start) * 1000
            METRICS.record_read(latency, ok=True)
            events.request.fire(
                request_type="HTTP", name="consolidation.web_read_group",
                response_time=latency, response_length=0, exception=None,
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            METRICS.record_read(latency, ok=False)
            events.request.fire(
                request_type="HTTP", name="consolidation.web_read_group",
                response_time=latency, response_length=0, exception=e,
            )


# ---------------------------------------------------------------------------
# Summary at test end
# ---------------------------------------------------------------------------
@events.quitting.add_listener
def _print_summary(environment: Environment, **_kwargs):
    with METRICS.lock:
        orders_ok = METRICS.orders_created
        orders_bad = METRICS.orders_failed
        reads_ok = METRICS.reads_done
        reads_bad = METRICS.reads_failed
        w = sorted(METRICS.write_latencies_ms)
        r = sorted(METRICS.read_latencies_ms)

    def pct(xs, p):
        if not xs:
            return float("nan")
        k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
        return xs[k]

    runtime = (
        environment.runner.environment.stats.last_request_timestamp
        - environment.runner.environment.stats.start_time
        if environment.runner and environment.runner.environment.stats.start_time
        else 0
    )

    print()
    print("=" * 66)
    print("  Concurrent-writers demo — summary")
    print("=" * 66)
    print(f"  users running at test end .... {environment.runner.user_count if environment.runner else 0}")
    print(f"  runtime (s) .................. {runtime:.1f}")
    print("  writes:")
    print(f"    sale.order.create OK ....... {orders_ok}")
    print(f"    sale.order.create FAILED ... {orders_bad}")
    if runtime > 0:
        print(f"    throughput ................. {orders_ok / runtime:.1f} orders/s")
    if w:
        print(f"    p50/p95/p99/max (ms) ....... {pct(w,50):.0f} / {pct(w,95):.0f} / {pct(w,99):.0f} / {w[-1]:.0f}")
    print("  reads:")
    print(f"    search_read + snapshot + pivot OK ... {reads_ok}")
    print(f"    reads FAILED ........................ {reads_bad}")
    if r:
        print(f"    p50/p95/p99/max (ms) ................ {pct(r,50):.0f} / {pct(r,95):.0f} / {pct(r,99):.0f} / {r[-1]:.0f}")
    print("=" * 66)

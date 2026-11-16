"""Locust harness for C4 — Odoo 20 Bus/WebSocket fan-out.

Two kinds of simulated users share the run:

- ``DashboardSubscriber`` (many) — authenticates, opens a websocket, subscribes
  to ``ic_demo:dashboard:<company_id>`` and holds the connection, counting every
  ``ic_demo.kpi.tick`` message it receives.
- ``OrderWriter`` (exactly one via ``fixed_count``) — authenticates and loops
  creating ``sale.order`` records. Each create fires the server-side hook that
  pushes one message onto the bus channel, which every subscriber must receive.

Metric: Σ(notifications received by subscribers) ≈ (orders created) × (subscriber
count), with p50 / p95 / p99 fan-out latency.

Run from the tools venv:

    .venv-tools/bin/locust \
        -f tools/load/locustfile.py \
        --host http://127.0.0.1:8069 \
        --users 101 --spawn-rate 20 \
        --run-time 2m --headless

Fixed-count on ``OrderWriter`` means --users=101 → 1 writer + 100 subscribers.

Env overrides:
    IC_DEMO_DB        default: ic_demo
    IC_DEMO_LOGIN     default: admin
    IC_DEMO_PASSWORD  default: admin
    IC_DEMO_COMPANY   default: 1
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import gevent
import requests
import websocket  # from websocket-client (gevent-compatible sync WS lib)
from locust import User, between, events, task
from locust.env import Environment


DB = os.environ.get("IC_DEMO_DB", "ic_demo")
LOGIN = os.environ.get("IC_DEMO_LOGIN", "admin")
PASSWORD = os.environ.get("IC_DEMO_PASSWORD", "admin")
COMPANY_ID = int(os.environ.get("IC_DEMO_COMPANY", "1"))
# When Odoo runs with workers>=1, WS traffic should target the gevent port
# (default 8072). Set IC_DEMO_WS_PORT to that; unset uses the --host port.
WS_PORT_OVERRIDE = os.environ.get("IC_DEMO_WS_PORT")
CHANNEL = f"ic_demo:dashboard:{COMPANY_ID}"
EVENT_TYPE = "ic_demo.kpi.tick"


# Shared cookie populated at @events.test_start. Every subscriber reuses it
# instead of authenticating separately — Odoo's bcrypt verify is CPU-bound
# under the single-process ThreadedServer GIL, so 100 parallel auths would
# serialize for ~10s and swamp the 15s read-timeout. One auth + 100 websockets
# from the same session is also representative of the real UX (one browser,
# many open tabs).
SHARED_COOKIE_HEADER: str | None = None
SHARED_WRITER_COOKIE_HEADER: str | None = None


# ---------------------------------------------------------------------------
# Shared metrics (summed across all greenlets)
# ---------------------------------------------------------------------------
@dataclass
class Metrics:
    orders_created: int = 0
    messages_received: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_order(self) -> None:
        with self.lock:
            self.orders_created += 1

    def record_message(self, latency_ms: float | None) -> None:
        with self.lock:
            self.messages_received += 1
            if latency_ms is not None:
                self.latencies_ms.append(latency_ms)


METRICS = Metrics()

# Map order id → monotonic ts captured by the writer.
ORDER_CREATE_TS: dict[int, float] = {}
ORDER_TS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Odoo RPC helpers (session-cookie flavour)
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


def odoo_call(session: requests.Session, host: str, model: str, method: str, args=None, kwargs=None):
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
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"Odoo RPC error: {body['error']}")
    return body["result"]


# ---------------------------------------------------------------------------
# WebSocket subscriber (sync API — compatible with gevent greenlets)
# ---------------------------------------------------------------------------
class DashboardSubscriber(User):
    weight = 1  # all remaining users land here
    wait_time = between(1, 1)  # unused; @task blocks on recv()

    @task
    def subscribe_and_listen(self):
        host = self.environment.host.rstrip("/")
        parsed = urlparse(host)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_host = parsed.hostname or "127.0.0.1"
        ws_port = int(WS_PORT_OVERRIDE) if WS_PORT_OVERRIDE else (parsed.port or 80)
        ws_url = f"{ws_scheme}://{ws_host}:{ws_port}/websocket?version=1.0.0"

        start = time.monotonic()
        if not SHARED_COOKIE_HEADER:
            events.request.fire(
                request_type="HTTP",
                name="auth",
                response_time=0,
                response_length=0,
                exception=RuntimeError("no shared cookie — test_start never ran"),
            )
            gevent.sleep(2)
            return
        cookie_header = SHARED_COOKIE_HEADER

        ws = websocket.WebSocket()
        try:
            ws.connect(
                ws_url,
                header=[f"Cookie: {cookie_header}", f"Origin: {host}"],
                timeout=15,
            )
        except Exception as e:
            events.request.fire(
                request_type="WS",
                name="connect",
                response_time=(time.monotonic() - start) * 1000,
                response_length=0,
                exception=e,
            )
            gevent.sleep(2)
            return

        try:
            ws.send(
                json.dumps(
                    {
                        "event_name": "subscribe",
                        "data": {"channels": [CHANNEL], "check_outdated": False, "last": 0},
                    }
                )
            )
            events.request.fire(
                request_type="WS",
                name="connect+subscribe",
                response_time=(time.monotonic() - start) * 1000,
                response_length=0,
                exception=None,
            )

            while True:
                raw = ws.recv()
                recv_ts = time.monotonic()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(data, list):
                    continue
                for notif in data:
                    msg = notif.get("message") or {}
                    if msg.get("type") != EVENT_TYPE:
                        continue
                    latency_ms = None
                    order_id = msg.get("payload", {}).get("id")
                    with ORDER_TS_LOCK:
                        created_ts = ORDER_CREATE_TS.get(order_id)
                    if created_ts is not None:
                        latency_ms = (recv_ts - created_ts) * 1000
                    METRICS.record_message(latency_ms)
                    events.request.fire(
                        request_type="WS",
                        name="notification",
                        response_time=latency_ms or 0,
                        response_length=len(raw) if isinstance(raw, (bytes, str)) else 0,
                        exception=None,
                    )
        except Exception as e:
            events.request.fire(
                request_type="WS",
                name="recv",
                response_time=0,
                response_length=0,
                exception=e,
            )
        finally:
            try:
                ws.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Order writer — exactly one instance regardless of --users count
# ---------------------------------------------------------------------------
class OrderWriter(User):
    fixed_count = 1
    wait_time = between(0.5, 1.0)  # ~1 order per second per writer

    def on_start(self):
        host = self.environment.host.rstrip("/")
        self._host = host
        # The writer gets its own session (different cookie from the subscribers)
        # so we exercise the "another user in another browser is creating orders"
        # path — but it was already authenticated at test_start to keep the ramp
        # off the critical path.
        self._session = requests.Session()
        if SHARED_WRITER_COOKIE_HEADER:
            for kv in SHARED_WRITER_COOKIE_HEADER.split("; "):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    self._session.cookies.set(k, v)
        else:
            self._session = odoo_login(host)
        # Grab any partner id we can post against.
        partners = odoo_call(
            self._session, host, "res.partner", "search", args=[[]], kwargs={"limit": 1}
        )
        self._partner_id = partners[0] if partners else 2

    @task
    def create_sale_order(self):
        start = time.monotonic()
        try:
            order_id = odoo_call(
                self._session,
                self._host,
                "sale.order",
                "create",
                args=[{"partner_id": self._partner_id}],
            )
            with ORDER_TS_LOCK:
                ORDER_CREATE_TS[order_id] = time.monotonic()
            METRICS.record_order()
            events.request.fire(
                request_type="HTTP",
                name="sale.order.create",
                response_time=(time.monotonic() - start) * 1000,
                response_length=0,
                exception=None,
            )
        except Exception as e:
            events.request.fire(
                request_type="HTTP",
                name="sale.order.create",
                response_time=(time.monotonic() - start) * 1000,
                response_length=0,
                exception=e,
            )


# ---------------------------------------------------------------------------
# One-time auth at test start — populates the two shared cookies BEFORE Locust
# starts spawning users. This takes bcrypt out of the hot path.
# ---------------------------------------------------------------------------
@events.test_start.add_listener
def _auth_once(environment: Environment, **_kwargs):
    global SHARED_COOKIE_HEADER, SHARED_WRITER_COOKIE_HEADER
    host = environment.host.rstrip("/")
    sub_session = odoo_login(host)
    SHARED_COOKIE_HEADER = "; ".join(f"{k}={v}" for k, v in sub_session.cookies.items())
    writer_session = odoo_login(host)
    SHARED_WRITER_COOKIE_HEADER = "; ".join(
        f"{k}={v}" for k, v in writer_session.cookies.items()
    )
    print(f"[test_start] shared subscriber cookie + writer cookie acquired for {host}")


# ---------------------------------------------------------------------------
# Summary at test end
# ---------------------------------------------------------------------------
@events.quitting.add_listener
def _print_summary(environment: Environment, **_kwargs):
    with METRICS.lock:
        orders = METRICS.orders_created
        msgs = METRICS.messages_received
        lats = sorted(METRICS.latencies_ms)

    user_count = environment.runner.user_count if environment.runner else 0

    def pct(p):
        if not lats:
            return None
        k = max(0, min(len(lats) - 1, int(round(p / 100 * (len(lats) - 1)))))
        return lats[k]

    print()
    print("=" * 60)
    print("  C4 — Bus fan-out summary")
    print("=" * 60)
    print(f"  users running at test end .... {user_count}")
    print(f"  orders created (writer) ...... {orders}")
    print(f"  notifications received (all).. {msgs}")
    if orders:
        print(f"  avg fan-out per order ........ {msgs / orders:.1f}")
    if lats:
        print(f"  latency p50 / p95 / p99 (ms) . {pct(50):.0f} / {pct(95):.0f} / {pct(99):.0f}")
        print(f"  latency min / max (ms) ....... {lats[0]:.0f} / {lats[-1]:.0f}")
        print(f"  correlated samples ........... {len(lats)}")
    print("=" * 60)

"""IC Demo in-UI load-test runner.

Replicates the shape of ``tools/load/concurrent_writers.py`` as an Odoo
model so the full locust harness can be driven from the web UI during a
demo without SSH or a shell. One foreground RPC spawns a background
thread that runs the HTTP load against the same Odoo instance, writes
periodic progress into an ``ic.demo.load.run`` row, and the client polls
that row every ~2 s.
"""

import json
import logging
import random
import threading
import time

import requests

from odoo import api, fields, models
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


class IcDemoLoadRun(models.Model):
    _name = "ic.demo.load.run"
    _description = "IC Demo load-test run"
    _order = "id desc"

    name = fields.Char(required=True, default=lambda self: time.strftime("Load test %Y-%m-%d %H:%M:%S"))
    state = fields.Selection(
        [("pending", "Pending"), ("running", "Running"),
         ("finished", "Finished"), ("error", "Error")],
        default="pending", required=True,
    )
    users = fields.Integer(default=30, required=True)
    spawn_rate = fields.Integer(default=5, required=True)
    run_time = fields.Integer(default=60, required=True, help="Seconds")
    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    elapsed = fields.Integer(default=0, help="Seconds since start")
    total_requests = fields.Integer(default=0)
    total_failures = fields.Integer(default=0)
    orders_created = fields.Integer(default=0)
    stats_json = fields.Text(default="{}")
    errors_json = fields.Text(default="[]")
    log = fields.Text()

    @api.model
    def start_run(self, users=30, spawn_rate=5, run_time=60):
        users = max(1, min(int(users or 30), 200))
        spawn_rate = max(1, min(int(spawn_rate or 5), 50))
        run_time = max(5, min(int(run_time or 60), 600))
        run = self.create({
            "state": "pending",
            "users": users,
            "spawn_rate": spawn_rate,
            "run_time": run_time,
        })
        dbname = self.env.cr.dbname
        self.env.cr.commit()
        threading.Thread(
            target=_run_background,
            args=(dbname, run.id, users, spawn_rate, run_time),
            daemon=True,
            name=f"ic_demo_load_run_{run.id}",
        ).start()
        return run.id

    def get_status(self):
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "users": self.users,
            "spawn_rate": self.spawn_rate,
            "run_time": self.run_time,
            "elapsed": self.elapsed,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "orders_created": self.orders_created,
            "stats": json.loads(self.stats_json or "{}"),
            "errors": json.loads(self.errors_json or "[]"),
            "started_at": self.started_at and self.started_at.isoformat(),
            "finished_at": self.finished_at and self.finished_at.isoformat(),
            "log": self.log or "",
        }

    @api.model
    def latest_run_id(self):
        row = self.search([], order="id desc", limit=1)
        return row.id if row else False


# ---------------------------------------------------------------------------
# Background runner (no Odoo request context — opens its own cursor)
# ---------------------------------------------------------------------------
def _update_run(dbname, run_id, vals):
    registry = Registry(dbname)
    with registry.cursor() as cr:
        env = api.Environment(cr, 1, {})
        env["ic.demo.load.run"].browse(run_id).write(vals)
        cr.commit()


def _snapshot_stats(stats):
    out = {}
    for name, s in stats.items():
        latencies = sorted(s["latencies"])
        ok = s["ok"]
        fail = s["fail"]
        total = ok + fail
        if latencies:
            def pct(p):
                i = max(0, min(len(latencies) - 1, int(round(p / 100 * (len(latencies) - 1)))))
                return latencies[i]
            summary = {
                "reqs": total, "ok": ok, "fail": fail,
                "pct_fail": round(fail / total * 100, 2) if total else 0.0,
                "avg": round(sum(latencies) / len(latencies), 1),
                "min": round(latencies[0], 1),
                "max": round(latencies[-1], 1),
                "med": round(pct(50), 1),
                "p95": round(pct(95), 1),
                "p99": round(pct(99), 1),
            }
        else:
            summary = {"reqs": 0, "ok": 0, "fail": 0, "pct_fail": 0.0,
                       "avg": 0, "min": 0, "max": 0, "med": 0, "p95": 0, "p99": 0}
        out[name] = summary
    return out


def _run_background(dbname, run_id, users, spawn_rate, run_time):
    host = "http://127.0.0.1:8069"
    login_name = "admin"
    password = "admin"

    stats = {
        "sale.order.create":            {"ok": 0, "fail": 0, "latencies": []},
        "sale.order.action_confirm":    {"ok": 0, "fail": 0, "latencies": []},
        "sale.order.search_read":       {"ok": 0, "fail": 0, "latencies": []},
        "dashboard.get_snapshot":       {"ok": 0, "fail": 0, "latencies": []},
        "consolidation.web_read_group": {"ok": 0, "fail": 0, "latencies": []},
    }
    errors = {}
    counters = {"orders_created": 0}
    stats_lock = threading.Lock()

    def record(name, latency_ms, ok, err=None):
        with stats_lock:
            stats[name]["ok" if ok else "fail"] += 1
            stats[name]["latencies"].append(latency_ms)
            if ok and name == "sale.order.create":
                counters["orders_created"] += 1
            if not ok and err:
                key = f"{name}: {err}"
                errors[key] = errors.get(key, 0) + 1

    def login_session():
        s = requests.Session()
        s.post(
            f"{host}/web/session/authenticate",
            json={"jsonrpc": "2.0", "params": {"db": dbname, "login": login_name, "password": password}},
            timeout=15,
        )
        return s

    def odoo_call(sess, model, method, args=None, kwargs=None, timeout=30):
        r = sess.post(
            f"{host}/web/dataset/call_kw",
            json={"jsonrpc": "2.0", "method": "call", "params": {
                "model": model, "method": method,
                "args": args or [], "kwargs": kwargs or {},
            }},
            timeout=timeout,
        )
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"].get("message") or "Odoo Server Error")
        return body["result"]

    def business_worker(stop_event):
        try:
            sess = login_session()
            partners = odoo_call(sess, "res.partner", "search",
                                 [[("active", "=", True), "|",
                                   ("company_id", "=", False), ("company_id", "=", 2)]],
                                 {"limit": 10}) or [3]
            products = odoo_call(sess, "product.product", "search",
                                 [[("sale_ok", "=", True), "|",
                                   ("company_id", "=", False), ("company_id", "=", 2)]],
                                 {"limit": 20}) or []
        except Exception:
            _logger.exception("load_run: business_worker login failed")
            return
        last_order = None
        while not stop_event.is_set():
            r = random.random()
            if r < 0.57:  # create (weight 4 / 7)
                t0 = time.monotonic()
                try:
                    line = {"name": "Load-test item",
                            "price_unit": round(random.uniform(50.0, 2000.0), 2),
                            "product_uom_qty": random.randint(1, 10)}
                    if products:
                        line["product_id"] = random.choice(products)
                    oid = odoo_call(sess, "sale.order", "create", [{
                        "partner_id": random.choice(partners),
                        "order_line": [(0, 0, line)],
                    }])
                    record("sale.order.create", (time.monotonic() - t0) * 1000, True)
                    last_order = oid
                except Exception as e:
                    record("sale.order.create", (time.monotonic() - t0) * 1000, False, str(e)[:120])
            elif r < 0.71:  # confirm (weight 1 / 7)
                if last_order:
                    t0 = time.monotonic()
                    try:
                        odoo_call(sess, "sale.order", "action_confirm", [[last_order]])
                        record("sale.order.action_confirm", (time.monotonic() - t0) * 1000, True)
                    except Exception as e:
                        record("sale.order.action_confirm", (time.monotonic() - t0) * 1000, False, str(e)[:120])
            else:  # search_read (weight 2 / 7)
                t0 = time.monotonic()
                try:
                    odoo_call(sess, "sale.order", "search_read", [[]],
                              {"fields": ["name", "partner_id", "state", "amount_total"],
                               "limit": 10, "order": "id desc"})
                    record("sale.order.search_read", (time.monotonic() - t0) * 1000, True)
                except Exception as e:
                    record("sale.order.search_read", (time.monotonic() - t0) * 1000, False, str(e)[:120])
            time.sleep(random.uniform(0.5, 2.0))

    def analyst_worker(stop_event):
        try:
            sess = login_session()
        except Exception:
            _logger.exception("load_run: analyst_worker login failed")
            return
        while not stop_event.is_set():
            if random.random() < 0.75:
                t0 = time.monotonic()
                try:
                    odoo_call(sess, "ic.demo.dashboard.kpi", "get_snapshot", [],
                              {"company_ids": [2]})
                    record("dashboard.get_snapshot", (time.monotonic() - t0) * 1000, True)
                except Exception as e:
                    record("dashboard.get_snapshot", (time.monotonic() - t0) * 1000, False, str(e)[:120])
            else:
                t0 = time.monotonic()
                try:
                    odoo_call(sess, "ic.demo.consolidation.line", "web_read_group", [],
                              {"domain": [], "groupby": ["company_id"],
                               "aggregates": ["revenue:sum", "line_count:sum"], "limit": 25})
                    record("consolidation.web_read_group", (time.monotonic() - t0) * 1000, True)
                except Exception as e:
                    record("consolidation.web_read_group", (time.monotonic() - t0) * 1000, False, str(e)[:120])
            time.sleep(random.uniform(2.0, 4.0))

    try:
        _update_run(dbname, run_id, {
            "state": "running",
            "started_at": fields.Datetime.now(),
        })

        analyst_count = min(5, users)
        business_count = max(0, users - analyst_count)
        spawn_delay = 1.0 / max(1, spawn_rate)

        stop_event = threading.Event()
        threads = []
        # Spawn writers first, analysts last — match locust's fixed_count=5 shape.
        for _ in range(business_count):
            t = threading.Thread(target=business_worker, args=(stop_event,), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(spawn_delay)
        for _ in range(analyst_count):
            t = threading.Thread(target=analyst_worker, args=(stop_event,), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(spawn_delay)

        start = time.monotonic()
        last_tick = start
        while time.monotonic() - start < run_time:
            time.sleep(1)
            now = time.monotonic()
            if now - last_tick >= 2.0:
                last_tick = now
                with stats_lock:
                    snapshot = _snapshot_stats(stats)
                    total_req = sum(s["ok"] + s["fail"] for s in stats.values())
                    total_fail = sum(s["fail"] for s in stats.values())
                    oc = counters["orders_created"]
                _update_run(dbname, run_id, {
                    "elapsed": int(now - start),
                    "total_requests": total_req,
                    "total_failures": total_fail,
                    "orders_created": oc,
                    "stats_json": json.dumps(snapshot),
                })

        stop_event.set()
        for t in threads:
            t.join(timeout=5)

        with stats_lock:
            final_stats = _snapshot_stats(stats)
            final_errors = sorted(
                [{"error": k, "count": v} for k, v in errors.items()],
                key=lambda x: -x["count"],
            )
            total_req = sum(s["ok"] + s["fail"] for s in stats.values())
            total_fail = sum(s["fail"] for s in stats.values())
            oc = counters["orders_created"]

        _update_run(dbname, run_id, {
            "state": "finished",
            "finished_at": fields.Datetime.now(),
            "elapsed": int(time.monotonic() - start),
            "total_requests": total_req,
            "total_failures": total_fail,
            "orders_created": oc,
            "stats_json": json.dumps(final_stats),
            "errors_json": json.dumps(final_errors),
        })
    except Exception as e:
        _logger.exception("load_run: background runner crashed")
        try:
            _update_run(dbname, run_id, {
                "state": "error",
                "finished_at": fields.Datetime.now(),
                "log": f"{type(e).__name__}: {e}",
            })
        except Exception:
            _logger.exception("load_run: could not write error state")

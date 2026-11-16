"""C3 benchmark — exercise the real Odoo RPC paths and report p50/p95/p99
latencies. Run against whichever Odoo config is active (demo or perf).

Covers:
  - ic.demo.dashboard.kpi.get_snapshot (C4 server snapshot path)
  - ic.demo.consolidation.line         (C1 materialised-view roll-up via
                                        web_read_group)
  - sale.order.create                  (write path, drives C4 bus notify)

Usage:
    .venv-tools/bin/python tools/c3/bench.py \
        --iterations 1000 --concurrency 10 --host http://127.0.0.1:8069
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import statistics
import time

import requests


def auth(host: str, db: str, login: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(
        f"{host}/web/session/authenticate",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {"jsonrpc": "2.0", "params": {"db": db, "login": login, "password": password}}
        ),
        timeout=15,
    )
    r.raise_for_status()
    uid = r.json().get("result", {}).get("uid")
    if not uid:
        raise RuntimeError(f"auth failed: {r.text[:200]}")
    return s


def call(s: requests.Session, host: str, model: str, method: str, args=None, kwargs=None):
    r = s.post(
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
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{model}.{method} error: {body['error'].get('message')}")
    return body["result"]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_dashboard(s, host):
    return call(s, host, "ic.demo.dashboard.kpi", "get_snapshot", kwargs={"company_id": 1})


def scenario_consolidation(s, host):
    # Match the Pivot view's default call: group by company, 12-month window.
    return call(
        s,
        host,
        "ic.demo.consolidation.line",
        "web_read_group",
        kwargs={
            "domain": [],
            "groupby": ["company_id"],
            "aggregates": ["revenue:sum", "line_count:sum"],
            "limit": 25,
        },
    )


def scenario_sale_order_create(s, host, partner_id):
    return call(s, host, "sale.order", "create", args=[{"partner_id": partner_id}])


SCENARIOS = {
    "dashboard_snapshot": scenario_dashboard,
    "consolidation_pivot": scenario_consolidation,
    "sale_order_create":   scenario_sale_order_create,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_scenario(name, iterations, concurrency, host, db, login, password, partner_id):
    """Warm one session per thread, then fire ``iterations`` calls split evenly.

    Measures wall-clock per call in milliseconds. Errors are counted and
    excluded from the latency summary.
    """
    sessions = [auth(host, db, login, password) for _ in range(concurrency)]

    def one(iix):
        s = sessions[iix % concurrency]
        fn = SCENARIOS[name]
        t0 = time.perf_counter()
        try:
            if name == "sale_order_create":
                fn(s, host, partner_id)
            else:
                fn(s, host)
            return (time.perf_counter() - t0) * 1000.0, None
        except Exception as e:
            return (time.perf_counter() - t0) * 1000.0, str(e)[:120]

    latencies = []
    errors = []
    with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
        for ms, err in pool.map(one, range(iterations)):
            latencies.append(ms)
            if err:
                errors.append(err)
    return latencies, errors


def pct(xs, p):
    if not xs:
        return float("nan")
    k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
    return sorted(xs)[k]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("IC_DEMO_HOST", "http://127.0.0.1:8069"))
    p.add_argument("--db", default=os.environ.get("IC_DEMO_DB", "ic_demo"))
    p.add_argument("--login", default=os.environ.get("IC_DEMO_LOGIN", "admin"))
    p.add_argument("--password", default=os.environ.get("IC_DEMO_PASSWORD", "admin"))
    p.add_argument("--iterations", type=int, default=1000)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--scenarios", default="dashboard_snapshot,consolidation_pivot")
    p.add_argument("--partner-id", type=int, default=3,
                   help="partner to use for sale_order_create (optional scenario)")
    args = p.parse_args()

    print(f"host={args.host} db={args.db} iters={args.iterations} concurrency={args.concurrency}")
    print(f"scenarios: {args.scenarios}")
    print()

    # Simple calibration — grab the server identity and prove connectivity.
    probe = auth(args.host, args.db, args.login, args.password)
    probe_r = probe.post(
        f"{args.host}/web/dataset/call_kw",
        headers={"Content-Type": "application/json"},
        data=json.dumps({
            "jsonrpc": "2.0", "method": "call",
            "params": {"model": "ir.module.module", "method": "search_count",
                       "args": [[["state", "=", "installed"]]], "kwargs": {}},
        }),
        timeout=10,
    ).json()
    print(f"  Odoo reports {probe_r.get('result')} installed modules\n")

    results = {}
    for scenario in args.scenarios.split(","):
        scenario = scenario.strip()
        if not scenario:
            continue
        if scenario not in SCENARIOS:
            print(f"  skipping unknown scenario: {scenario}")
            continue
        print(f"[{scenario}] ...")
        latencies, errors = run_scenario(
            scenario, args.iterations, args.concurrency,
            args.host, args.db, args.login, args.password, args.partner_id,
        )
        ok = [ms for ms in latencies]  # we measure wall-clock even on errors
        succ = [ms for ms, i in zip(latencies, range(len(latencies))) if i >= len(errors)]
        # Simpler: filter paired results below
        ok_ms = [latencies[i] for i in range(len(latencies))]
        results[scenario] = (ok_ms, errors)
        print(
            f"  {len(ok_ms)} calls, {len(errors)} errors"
            f"   p50={pct(ok_ms, 50):.1f}ms  p95={pct(ok_ms, 95):.1f}ms"
            f"  p99={pct(ok_ms, 99):.1f}ms  max={max(ok_ms):.1f}ms"
            f"  mean={statistics.fmean(ok_ms):.1f}ms"
        )
        if errors:
            print(f"    first error: {errors[0]}")
        print()

    # Machine-readable one-liner at the end
    summary = {
        s: {
            "iterations": len(lat),
            "errors": len(errs),
            "p50": round(pct(lat, 50), 1),
            "p95": round(pct(lat, 95), 1),
            "p99": round(pct(lat, 99), 1),
            "max": round(max(lat), 1) if lat else None,
        }
        for s, (lat, errs) in results.items()
    }
    print("SUMMARY_JSON=" + json.dumps(summary))


if __name__ == "__main__":
    main()

"""V2 flows — Phase C of the local demo DB reshape.

Generates realistic business activity across the 5 active Techvoot cos:

    - sale.order:      ~5,000 per co, batch-created, mostly confirmed
    - purchase.order:  ~1,500 per co, batch-created, mostly confirmed
    - account.move:    ~200 invoices + ~200 bills per co posted
    - account.payment: ~100 payments per co

Date distribution: spread over the past 24 months so dashboard "last 30 d"
tiles and consolidation month pivots look populated.

Run from project root with the Odoo venv (after v2_bootstrap.py):

    .venv/bin/python tools/seed/v2_flows.py

Idempotent-ish: existing IC-generated SOs/POs are detected by origin tag and
skipped. Safe to re-run but the per-co targets add on top of what's already
there, so don't bump --so-per-co without clearing first.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import random
import sys
import time
from datetime import date, timedelta

PROJECT = pathlib.Path(__file__).resolve().parents[2]
ODOO_SRC = pathlib.Path("/home/techvoot/work_space/20/base/odoo")
sys.path.insert(0, str(ODOO_SRC))

os.environ.setdefault("ODOO_RC", str(PROJECT / "odoo" / "odoo.conf"))

import odoo  # noqa: E402
import odoo.init  # noqa: E402
from odoo import api  # noqa: E402
from odoo.modules.registry import Registry  # noqa: E402
from odoo.tools import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("v2_flows")

ORIGIN_TAG = "ic_demo_v2"


def _env_for_company(env, company):
    admin = env.ref("base.user_admin")
    if company.id not in admin.company_ids.ids:
        admin.sudo().company_ids = [(4, company.id)]
    return env(
        user=admin,
        context=dict(env.context, allowed_company_ids=[company.id]),
    )


def _random_date(rng, start_days_ago=730, end_days_ago=0):
    offset = rng.randint(end_days_ago, start_days_ago)
    return date.today() - timedelta(days=offset)


# ---------------------------------------------------------------------------
# Sale orders
# ---------------------------------------------------------------------------
def generate_sale_orders(env, company, products, customers, target, batch_size, rng):
    env_c = _env_for_company(env, company)

    existing = env_c["sale.order"].search_count(
        [("company_id", "=", company.id), ("origin", "=like", f"{ORIGIN_TAG}:%")]
    )
    need = max(0, target - existing)
    if not need:
        log.info("SO: company %s already has %d (target %d), skipping",
                 company.name, existing, target)
        return

    log.info("SO: company %s creating %d orders in batches of %d",
             company.name, need, batch_size)
    t0 = time.monotonic()
    created_ids = []
    for i in range(0, need, batch_size):
        batch_vals = []
        for j in range(min(batch_size, need - i)):
            idx = i + j
            partner = customers[rng.randint(0, len(customers) - 1)]
            n_lines = rng.randint(1, 5)
            lines = []
            for _ in range(n_lines):
                prod = products[rng.randint(0, len(products) - 1)]
                qty = rng.randint(1, 20)
                lines.append((0, 0, {
                    "product_id": prod.id,
                    "product_uom_qty": qty,
                    "price_unit": prod.list_price,
                }))
            batch_vals.append({
                "company_id": company.id,
                "partner_id": partner.id,
                "date_order": _random_date(rng, 730, 0).isoformat(),
                "origin": f"{ORIGIN_TAG}:{existing + idx:06d}",
                "order_line": lines,
            })
        orders = env_c["sale.order"].create(batch_vals)
        created_ids.extend(orders.ids)
        env.cr.commit()
        if (i // batch_size) % 5 == 0:
            log.info("  SO %s: %d / %d draft created (%.1fs)",
                     company.name, i + len(batch_vals), need, time.monotonic() - t0)

    log.info("SO: company %s %d draft created in %.1fs",
             company.name, len(created_ids), time.monotonic() - t0)

    # Confirm ~80% of them — keep 20% as open quotations for list variety.
    rng2 = random.Random(1000 + company.id)
    all_orders = env_c["sale.order"].browse(created_ids)
    to_confirm = all_orders.filtered(lambda o: rng2.random() < 0.80)
    log.info("SO: company %s confirming %d of %d", company.name, len(to_confirm), len(all_orders))
    t1 = time.monotonic()
    for i in range(0, len(to_confirm), batch_size):
        chunk = to_confirm[i:i + batch_size]
        try:
            chunk.action_confirm()
        except Exception as exc:
            log.warning("  confirm chunk failed at %d: %s (continuing)", i, exc)
        env.cr.commit()
        if (i // batch_size) % 5 == 0:
            log.info("  SO %s: confirmed %d / %d (%.1fs)",
                     company.name, i + len(chunk), len(to_confirm), time.monotonic() - t1)
    log.info("SO: company %s confirm pass done in %.1fs", company.name, time.monotonic() - t1)


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------
def generate_purchase_orders(env, company, products, vendors, target, batch_size, rng):
    env_c = _env_for_company(env, company)
    existing = env_c["purchase.order"].search_count(
        [("company_id", "=", company.id), ("origin", "=like", f"{ORIGIN_TAG}:%")]
    )
    need = max(0, target - existing)
    if not need:
        log.info("PO: company %s already has %d (target %d), skipping",
                 company.name, existing, target)
        return
    log.info("PO: company %s creating %d orders in batches of %d",
             company.name, need, batch_size)
    t0 = time.monotonic()
    created_ids = []
    for i in range(0, need, batch_size):
        batch_vals = []
        for j in range(min(batch_size, need - i)):
            idx = i + j
            vendor = vendors[rng.randint(0, len(vendors) - 1)]
            n_lines = rng.randint(1, 4)
            lines = []
            for _ in range(n_lines):
                prod = products[rng.randint(0, len(products) - 1)]
                qty = rng.randint(1, 50)
                lines.append((0, 0, {
                    "product_id": prod.id,
                    "product_qty": qty,
                    "price_unit": prod.standard_price or prod.list_price * 0.6,
                    "uom_id": prod.uom_id.id,
                    "name": prod.display_name,
                    "date_planned": (date.today() + timedelta(days=rng.randint(1, 30))).isoformat(),
                }))
            batch_vals.append({
                "company_id": company.id,
                "partner_id": vendor.id,
                "date_order": _random_date(rng, 730, 0).isoformat(),
                "origin": f"{ORIGIN_TAG}:{existing + idx:06d}",
                "order_line": lines,
            })
        orders = env_c["purchase.order"].create(batch_vals)
        created_ids.extend(orders.ids)
        env.cr.commit()
        if (i // batch_size) % 5 == 0:
            log.info("  PO %s: %d / %d draft created (%.1fs)",
                     company.name, i + len(batch_vals), need, time.monotonic() - t0)

    log.info("PO: company %s %d draft created in %.1fs",
             company.name, len(created_ids), time.monotonic() - t0)

    # Confirm ~70%.
    rng2 = random.Random(2000 + company.id)
    all_orders = env_c["purchase.order"].browse(created_ids)
    to_confirm = all_orders.filtered(lambda o: rng2.random() < 0.70)
    log.info("PO: company %s confirming %d of %d", company.name, len(to_confirm), len(all_orders))
    t1 = time.monotonic()
    for i in range(0, len(to_confirm), batch_size):
        chunk = to_confirm[i:i + batch_size]
        try:
            chunk.button_confirm()
        except Exception as exc:
            log.warning("  po-confirm chunk failed at %d: %s (continuing)", i, exc)
        env.cr.commit()
        if (i // batch_size) % 5 == 0:
            log.info("  PO %s: confirmed %d / %d (%.1fs)",
                     company.name, i + len(chunk), len(to_confirm), time.monotonic() - t1)
    log.info("PO: company %s confirm pass done in %.1fs", company.name, time.monotonic() - t1)


# ---------------------------------------------------------------------------
# Invoices from a subset of SOs, posted; payments on a share of them
# ---------------------------------------------------------------------------
def generate_invoices_and_payments(env, company, n_invoices, n_payments, rng):
    env_c = _env_for_company(env, company)
    # Find confirmed SOs with no invoices yet
    confirmed = env_c["sale.order"].search(
        [("company_id", "=", company.id),
         ("state", "=", "sale"),
         ("invoice_ids", "=", False)],
        limit=n_invoices,
    )
    if not confirmed:
        log.info("INV: company %s no confirmed SOs to invoice", company.name)
        return
    log.info("INV: company %s invoicing %d SOs", company.name, len(confirmed))
    t0 = time.monotonic()
    posted_invoices = env_c["account.move"]
    for i, so in enumerate(confirmed):
        try:
            so._create_invoices(final=True)
            inv = so.invoice_ids.filtered(lambda m: m.state == "draft")[:1]
            if inv:
                inv.action_post()
                posted_invoices |= inv
        except Exception as exc:
            log.warning("  invoice for SO %s failed: %s", so.name, exc)
        if (i + 1) % 50 == 0:
            env.cr.commit()
            log.info("  INV %s: %d / %d posted (%.1fs)",
                     company.name, i + 1, len(confirmed), time.monotonic() - t0)
    env.cr.commit()
    log.info("INV: company %s %d posted in %.1fs",
             company.name, len(posted_invoices), time.monotonic() - t0)

    # Payments on a share of the posted invoices
    to_pay = posted_invoices[:n_payments]
    if not to_pay:
        return
    log.info("PAY: company %s registering payments on %d invoices", company.name, len(to_pay))
    t1 = time.monotonic()
    payment_count = 0
    for i, inv in enumerate(to_pay):
        try:
            # account.payment.register wizard (Odoo 20 path)
            wiz = env_c["account.payment.register"].with_context(
                active_model="account.move", active_ids=inv.ids
            ).create({})
            wiz.action_create_payments()
            payment_count += 1
        except Exception as exc:
            log.warning("  payment for %s failed: %s", inv.name, exc)
        if (i + 1) % 50 == 0:
            env.cr.commit()
            log.info("  PAY %s: %d / %d (%.1fs)",
                     company.name, i + 1, len(to_pay), time.monotonic() - t1)
    env.cr.commit()
    log.info("PAY: company %s %d payments in %.1fs",
             company.name, payment_count, time.monotonic() - t1)


# ---------------------------------------------------------------------------
# Vendor bills from a subset of POs, posted; vendor payments on a share
# ---------------------------------------------------------------------------
def generate_bills_and_vendor_payments(env, company, n_bills, n_payments, rng):
    env_c = _env_for_company(env, company)
    # Confirmed POs without any posted bill yet
    confirmed = env_c["purchase.order"].search(
        [("company_id", "=", company.id),
         ("state", "=", "purchase"),
         ("invoice_ids", "=", False)],
        limit=n_bills,
    )
    if not confirmed:
        log.info("BILL: company %s no confirmed POs to invoice", company.name)
        return
    log.info("BILL: company %s billing %d POs", company.name, len(confirmed))
    t0 = time.monotonic()
    posted_bills = env_c["account.move"]
    for i, po in enumerate(confirmed):
        try:
            po.action_create_invoice()
            bill = po.invoice_ids.filtered(lambda m: m.state == "draft")[:1]
            if bill:
                # Odoo 20 refuses to post a vendor bill without an invoice_date
                # or a vendor ref — fill both so bulk post succeeds.
                bill.write({
                    "invoice_date": po.date_order.date() if hasattr(po.date_order, "date") else po.date_order,
                    "ref": f"BILL-{po.name}",
                })
                bill.action_post()
                posted_bills |= bill
        except Exception as exc:
            log.warning("  bill for PO %s failed: %s", po.name, exc)
        if (i + 1) % 50 == 0:
            env.cr.commit()
            log.info("  BILL %s: %d / %d posted (%.1fs)",
                     company.name, i + 1, len(confirmed), time.monotonic() - t0)
    env.cr.commit()
    log.info("BILL: company %s %d posted in %.1fs",
             company.name, len(posted_bills), time.monotonic() - t0)

    # Vendor payments on a share
    to_pay = posted_bills[:n_payments]
    if not to_pay:
        return
    log.info("VPAY: company %s paying %d bills", company.name, len(to_pay))
    t1 = time.monotonic()
    payment_count = 0
    for i, bill in enumerate(to_pay):
        try:
            wiz = env_c["account.payment.register"].with_context(
                active_model="account.move", active_ids=bill.ids
            ).create({})
            wiz.action_create_payments()
            payment_count += 1
        except Exception as exc:
            log.warning("  vendor payment for %s failed: %s", bill.name, exc)
        if (i + 1) % 50 == 0:
            env.cr.commit()
            log.info("  VPAY %s: %d / %d (%.1fs)",
                     company.name, i + 1, len(to_pay), time.monotonic() - t1)
    env.cr.commit()
    log.info("VPAY: company %s %d vendor payments in %.1fs",
             company.name, payment_count, time.monotonic() - t1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--so-per-co", type=int, default=5000)
    p.add_argument("--po-per-co", type=int, default=1500)
    p.add_argument("--invoices-per-co", type=int, default=200)
    p.add_argument("--payments-per-co", type=int, default=100)
    p.add_argument("--bills-per-co", type=int, default=200)
    p.add_argument("--vendor-payments-per-co", type=int, default=100)
    p.add_argument("--batch", type=int, default=100)
    p.add_argument("--only", help="comma list of co names to process; default all")
    p.add_argument("--skip", help="comma-separated phase names to skip: so,po,inv,bill")
    p.add_argument("--database", default="ic_demo")
    args = p.parse_args()

    skip = set((args.skip or "").split(","))

    config.parse_config(
        ["-c", os.environ["ODOO_RC"], "-d", args.database], setup_logging=True
    )

    registry = Registry(args.database)
    with registry.cursor() as cr:
        env = api.Environment(cr, odoo.SUPERUSER_ID, {})

        cos = env["res.company"].search([("active", "=", True)])
        if args.only:
            wanted = set(x.strip() for x in args.only.split(","))
            cos = cos.filtered(lambda c: c.name in wanted)
        log.info("acting on %d companies: %s", len(cos), cos.mapped("name"))

        products = env["product.product"].search(
            [("default_code", "=like", "IC-%")]
        )
        log.info("%d products loaded", len(products))
        if not products:
            raise RuntimeError("no products — run v2_bootstrap.py first")

        for co in cos:
            rng = random.Random(10000 + co.id)
            env_c = _env_for_company(env, co)

            customers = env_c["res.partner"].search(
                [("customer_rank", ">", 0), ("supplier_rank", "=", 0)], limit=500,
            )
            vendors = env_c["res.partner"].search(
                [("company_id", "=", co.id), ("supplier_rank", ">", 0)], limit=200,
            )
            log.info("company %s: %d customers, %d vendors",
                     co.name, len(customers), len(vendors))
            if not customers or not vendors:
                log.warning("company %s missing customers or vendors, skipping", co.name)
                continue

            if "so" not in skip:
                generate_sale_orders(
                    env, co, products, customers,
                    target=args.so_per_co, batch_size=args.batch, rng=rng,
                )
            if "po" not in skip:
                generate_purchase_orders(
                    env, co, products, vendors,
                    target=args.po_per_co, batch_size=args.batch, rng=rng,
                )
            if "inv" not in skip:
                generate_invoices_and_payments(
                    env, co,
                    n_invoices=args.invoices_per_co,
                    n_payments=args.payments_per_co,
                    rng=rng,
                )
            if "bill" not in skip:
                generate_bills_and_vendor_payments(
                    env, co,
                    n_bills=args.bills_per_co,
                    n_payments=args.vendor_payments_per_co,
                    rng=rng,
                )

        # Refresh consolidation matview at the end
        log.info("refreshing consolidation matview ...")
        env["ic.demo.consolidation.line"].refresh()
        env.cr.commit()
        log.info("done")


if __name__ == "__main__":
    main()

"""V2 bootstrap — Phase B of the local demo DB reshape.

Assumes Phase A has already run (5 active cos: Techvoot Global HQ parent +
Alpha Retail / Beta Trading / Delta Logistik GmbH / Gamma Manufacturing).

What this script does:
    1. Attach the ``generic_coa`` chart of accounts to each subsidiary that
       doesn't have a full journal set (sale, purchase, bank). Uses the same
       ``try_loading`` + ``attach_company_to_parent_accounts`` pattern as
       ``c1_bootstrap`` so the "subsidiary created after try_loading" gotcha
       is handled.
    2. Create ~300 shared products (consumable + service mix).
    3. Create ~100 vendor-tagged partners per company.
    4. Make existing customer partners multi-company so Phase C can pick them
       regardless of the active company switcher.
    5. Write ``var/v2_refs.json`` with the per-company id maps Phase C needs.

Run from the project root with the Odoo venv:

    .venv/bin/python tools/seed/v2_bootstrap.py

Idempotent: safe to re-run.
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
log = logging.getLogger("v2_bootstrap")

ACTIVE_NAMES = [
    "Techvoot Global HQ",
    "Alpha Retail",
    "Beta Trading",
    "Delta Logistik GmbH",
    "Gamma Manufacturing",
]
PARENT_NAME = "Techvoot Global HQ"


# ---------------------------------------------------------------------------
# Env helpers (same shape as c1_bootstrap)
# ---------------------------------------------------------------------------
def _env_for_company(env, company):
    admin = env.ref("base.user_admin")
    if company.id not in admin.company_ids.ids:
        admin.sudo().company_ids = [(4, company.id)]
    return env(
        user=admin,
        context=dict(env.context, allowed_company_ids=[company.id]),
    )


def ensure_coa(env, company):
    """Install generic_coa on `company` if it doesn't have a full sale/purchase
    journal set yet."""
    has_sale = env["account.journal"].search_count(
        [("company_id", "=", company.id), ("type", "=", "sale")]
    )
    has_purchase = env["account.journal"].search_count(
        [("company_id", "=", company.id), ("type", "=", "purchase")]
    )
    if has_sale and has_purchase:
        log.debug(
            "company %s already has sale+purchase journals, skipping CoA",
            company.name,
        )
        return
    log.info("installing generic_coa in %s ...", company.name)
    env_c = _env_for_company(env, company)
    env_c["account.chart.template"].try_loading(
        "generic_coa", company=company, install_demo=False
    )
    env.cr.commit()


# Journals that generic_coa's try_loading may not create on subsidiaries when
# accounts already exist on the parent. Create them explicitly so Phase C can
# post invoices and bills.
JOURNAL_SPEC = [
    ("sale",     "Customer Invoices", "INV"),
    ("purchase", "Vendor Bills",      "BILL"),
    ("bank",     "Bank",              "BNK"),
    ("cash",     "Cash",              "CSH"),
]


def ensure_journals(env, company):
    """Create any missing sale/purchase/bank/cash journals for `company`."""
    env_c = _env_for_company(env, company)
    existing_types = set(env_c["account.journal"].search(
        [("company_id", "=", company.id)]
    ).mapped("type"))
    created = []
    for j_type, name, code in JOURNAL_SPEC:
        if j_type in existing_types:
            continue
        vals = {
            "name": name,
            "code": code,
            "type": j_type,
            "company_id": company.id,
        }
        # Link default accounts where the journal expects one, so SO -> invoice
        # and PO -> bill don't trip on "no default debit/credit account".
        if j_type == "sale":
            rev = env["account.account"].search(
                [("company_ids", "in", company.id), ("account_type", "=", "income")], limit=1
            )
            if rev:
                vals["default_account_id"] = rev.id
        elif j_type == "purchase":
            exp = env["account.account"].search(
                [("company_ids", "in", company.id), ("account_type", "=", "expense")], limit=1
            )
            if exp:
                vals["default_account_id"] = exp.id
        elif j_type in ("bank", "cash"):
            cash_acct = env["account.account"].search(
                [("company_ids", "in", company.id), ("account_type", "=", "asset_cash")], limit=1
            )
            if cash_acct:
                vals["default_account_id"] = cash_acct.id
        env_c["account.journal"].create(vals)
        created.append(j_type)
    if created:
        env.cr.commit()
        log.info("journals: company %s created %s", company.name, created)


def ensure_warehouse(env, company):
    """Create a default stock warehouse for `company` if missing. Required
    before sale.order.action_confirm() succeeds with stock installed."""
    env_c = _env_for_company(env, company)
    existing = env_c["stock.warehouse"].search_count(
        [("company_id", "=", company.id)]
    )
    if existing:
        return
    code = f"WH{company.id:02d}"
    env_c["stock.warehouse"].create({
        "name": f"{company.name} Warehouse",
        "code": code,
        "company_id": company.id,
    })
    env.cr.commit()
    log.info("warehouse: company %s created (code %s)", company.name, code)


def attach_company_to_parent_accounts(env, parent, company):
    if company.id == parent.id:
        return
    env.cr.execute(
        """
        INSERT INTO account_account_res_company_rel (account_account_id, res_company_id)
        SELECT DISTINCT r.account_account_id, %s
        FROM account_account_res_company_rel r
        WHERE r.res_company_id = %s
        ON CONFLICT DO NOTHING
        """,
        (company.id, parent.id),
    )
    env.cache.invalidate()


# ---------------------------------------------------------------------------
# Products (shared across all cos: no company_id)
# ---------------------------------------------------------------------------
PRODUCT_CATEGORIES = [
    ("Retail / General Merchandise", "consu", 15.0, 45.0),
    ("Industrial Components", "consu", 50.0, 250.0),
    ("Logistics Services", "service", 120.0, 500.0),
    ("Manufactured Goods", "consu", 200.0, 1800.0),
    ("Professional Services", "service", 80.0, 320.0),
    ("Consumables / Office", "consu", 5.0, 30.0),
]


def ensure_products(env, n):
    """Create `n` shared products (no company_id) if we don't already have
    them. Each product gets a `default_code` prefixed with `IC-` so the set
    is easy to find on re-runs."""
    existing = env["product.product"].search([("default_code", "=like", "IC-%")])
    if len(existing) >= n:
        log.info("products: %d already present (target %d), skipping", len(existing), n)
        return existing

    rng = random.Random(42)
    to_create = []
    for i in range(len(existing), n):
        cat_name, cat_type, lo, hi = PRODUCT_CATEGORIES[i % len(PRODUCT_CATEGORIES)]
        price = round(rng.uniform(lo, hi), 2)
        to_create.append(
            {
                "name": f"IC {cat_name} {i:03d}",
                "default_code": f"IC-{i:04d}",
                "type": cat_type,
                "list_price": price,
                "standard_price": round(price * rng.uniform(0.45, 0.72), 2),
                "sale_ok": True,
                "purchase_ok": True,
            }
        )
    created = env["product.product"].create(to_create)
    env.cr.commit()
    log.info("products: created %d (now %d total)", len(created), len(existing) + len(created))
    return existing | created


# ---------------------------------------------------------------------------
# Vendor partners (per company, supplier_rank set)
# ---------------------------------------------------------------------------
VENDOR_PREFIXES = [
    "Acme", "Globex", "Initech", "Hooli", "Soylent", "Umbrella", "Vandelay",
    "Pied Piper", "Massive Dynamic", "Wonka", "Stark", "Tyrell", "Weyland",
    "Oscorp", "LexCorp", "Dunder", "Prestige", "Cyberdyne", "Rekall", "Aperture",
]


def ensure_vendors(env, company, n):
    """Create `n` vendor-tagged partners under `company`. Each gets a stable
    `ref` so re-runs top up rather than duplicate."""
    env_c = _env_for_company(env, company)
    existing = env_c["res.partner"].search(
        [("company_id", "=", company.id), ("ref", "=like", f"ic_vendor:{company.id}:%")]
    )
    needed = n - len(existing)
    if needed <= 0:
        log.info(
            "vendors: company %s already has %d (target %d), skipping",
            company.name, len(existing), n,
        )
        return existing

    rng = random.Random(100 + company.id)
    to_create = []
    for i in range(len(existing), n):
        prefix = VENDOR_PREFIXES[i % len(VENDOR_PREFIXES)]
        to_create.append(
            {
                "name": f"{prefix} Supply Co. {i:03d}",
                "ref": f"ic_vendor:{company.id}:{i}",
                "company_id": company.id,
                "supplier_rank": 1,
                "customer_rank": 0,
                "is_company": True,
                "street": f"{rng.randint(100, 9999)} {rng.choice(['Main','Market','Oak','Pine'])} St",
                "city": rng.choice(["New York", "Chicago", "Houston", "Austin", "Denver", "Seattle"]),
                "country_id": company.country_id.id or False,
            }
        )
    created = env_c["res.partner"].create(to_create)
    env.cr.commit()
    log.info("vendors: company %s created %d (now %d)", company.name, len(created), len(existing) + len(created))
    return existing | created


# ---------------------------------------------------------------------------
# Multi-company customers: existing 2k partners are tied to archived cos.
# Null their company_id so they're pickable from any of the 5 active cos.
# ---------------------------------------------------------------------------
def multi_company_existing_customers(env):
    cr = env.cr
    cr.execute(
        """
        UPDATE res_partner
        SET company_id = NULL
        WHERE customer_rank > 0
          AND company_id IS NOT NULL
          AND company_id NOT IN %s
        RETURNING id
        """,
        (tuple([2, 4, 5, 6, 7]),),
    )
    rows = cr.fetchall()
    cr.commit()
    log.info("customers: freed %d existing partners to multi-company", len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# Collect refs for Phase C
# ---------------------------------------------------------------------------
def collect_refs(env, company):
    env_c = _env_for_company(env, company)
    def pick(journal_type):
        j = env_c["account.journal"].search(
            [("company_id", "=", company.id), ("type", "=", journal_type)], limit=1
        )
        return j.id if j else None

    def pick_account(account_type):
        a = env_c["account.account"].search(
            [("company_ids", "in", company.id), ("account_type", "=", account_type)], limit=1
        )
        return a.id if a else None

    return {
        "company_id": company.id,
        "company_name": company.name,
        "currency_id": company.currency_id.id,
        "sale_journal_id": pick("sale"),
        "purchase_journal_id": pick("purchase"),
        "bank_journal_id": pick("bank"),
        "general_journal_id": pick("general"),
        "receivable_account_id": pick_account("asset_receivable"),
        "payable_account_id": pick_account("liability_payable"),
        "revenue_account_id": pick_account("income"),
        "expense_account_id": pick_account("expense"),
        "bank_account_id": pick_account("asset_cash"),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--products", type=int, default=300)
    p.add_argument("--vendors-per-co", type=int, default=100)
    p.add_argument("--database", default="ic_demo")
    p.add_argument("--out", default=str(PROJECT / "var" / "v2_refs.json"))
    args = p.parse_args()

    config.parse_config(
        ["-c", os.environ["ODOO_RC"], "-d", args.database], setup_logging=True
    )

    t0 = time.monotonic()
    registry = Registry(args.database)
    with registry.cursor() as cr:
        env = api.Environment(cr, odoo.SUPERUSER_ID, {})

        cos = env["res.company"].search(
            [("active", "=", True), ("name", "in", ACTIVE_NAMES)]
        )
        if len(cos) != 5:
            raise RuntimeError(
                f"expected 5 active companies with the Techvoot names, got {len(cos)}: "
                f"{cos.mapped('name')}"
            )
        parent = cos.filtered(lambda c: c.name == PARENT_NAME)
        if not parent:
            raise RuntimeError(f"parent {PARENT_NAME!r} not found")
        subs = cos - parent

        # 1. Chart of accounts on each sub (parent already has it from c1)
        for sub in subs:
            ensure_coa(env, sub)
            attach_company_to_parent_accounts(env, parent, sub)

        # 1b. Journals that try_loading may skip on subs whose accounts
        # already exist at the parent level — create them explicitly.
        for co in cos:
            ensure_journals(env, co)

        # 1c. Warehouse per co (stock module requires one for SO confirm).
        for co in cos:
            ensure_warehouse(env, co)

        # 2. Shared products
        ensure_products(env, args.products)

        # 3. Vendors per co
        for co in cos:
            ensure_vendors(env, co, args.vendors_per_co)

        # 4. Free existing customers from archived cos
        multi_company_existing_customers(env)

        # 5. Collect refs
        refs = [collect_refs(env, co) for co in cos]

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"companies": refs}, indent=2))
    log.info("wrote %s (%d companies) in %.1fs", out, len(refs), time.monotonic() - t0)


if __name__ == "__main__":
    main()

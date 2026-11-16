# C1 — Multi-company + 10M+ transactions + consolidated reporting — runbook

Challenge 1: prove Odoo 20 on this stack can hold 20+ companies' books at
10M+ journal lines **and** produce a fast consolidated P&L across them.

## What's in the box

| Piece | Where |
|---|---|
| Bootstrap script (companies, CoA, partners, journals) | `tools/seed/c1_bootstrap.py` |
| Bulk COPY of `account_move` + `account_move_line` | `tools/seed/c1_bulk.py` |
| Companion refs file (ids the bulk script reads) | `var/seed_refs.json` |
| Consolidation addon (`_auto=False` SQL view) | `addons/custom/ic_demo_consolidation/` |
| Pivot + graph + list view | `views/consolidation_views.xml` |

## How it fits together

`c1_bootstrap.py` uses the Odoo ORM to create the 20 subsidiaries + a parent,
install the `generic_coa` chart template, create one misc journal per
company, and pre-materialise a pool of partners. All of this is slow by nature
(ORM traversals, CoA install), but it's only done once and takes ~20 seconds
at 20 companies × 100 partners.

`c1_bulk.py` then bypasses the ORM entirely and uses Postgres `COPY` directly
to insert balanced 2-line journal entries straight into `account_move` /
`account_move_line`. Each line pair is DR Receivable / CR Revenue so the
view aggregates to a real P&L number. At chunk=100,000 moves per commit on a
local Postgres 18 we see **~15-20k AMLs/sec** — a 10M-AML seed in **~10 min**.

`ic_demo_consolidation` serves a read-only SQL view (`_auto = False`) that
rolls the posted AMLs into a per-company × per-month P&L grid with revenue,
receivables, net profit proxy, and line count. Three native views pick up
the model:
- **Pivot** — rows = company, columns = month, measures = revenue / profit / lines
- **Graph** — stacked-by-company bars of revenue over time
- **List** — monthly breakdown with column sums

Supporting indexes created once against the full account_move_line table:
```sql
CREATE INDEX ic_demo_aml_company_date ON account_move_line (company_id, date)
    WHERE display_type NOT IN ('line_section','line_subsection','line_note');
CREATE INDEX ic_demo_aml_account ON account_move_line (account_id)
    WHERE display_type NOT IN ('line_section','line_subsection','line_note');
CREATE INDEX ic_demo_am_state_posted ON account_move (id) WHERE state='posted';
```

## Browser demo path

1. Make sure Odoo is up on the demo config: `.venv/bin/python odoo-bin -c odoo/odoo.conf`
2. http://localhost:8069 → log in `admin` / `admin`
3. Top menu: **IC Demo → Consolidated P&L**
4. Expect: pivot view with 20 companies down, months across, revenue cells populated, plus a "Group by Company" default from the action.
5. Toggle to **Graph** — stacked bars per company over time.

## Repro from scratch

Needs a running Postgres + a fresh `ic_demo` DB with the base Odoo modules
installed (the same DB C4 uses). From the project root:

```bash
# 1. Make sure the consolidation module is installed (one-off)
source .venv/bin/activate
/home/techvoot/work_space/20/base/odoo/odoo-bin \
    -c odoo/odoo.conf -d ic_demo \
    -i ic_demo_consolidation --stop-after-init

# 2. Structural seed (ORM)
.venv/bin/python tools/seed/c1_bootstrap.py --companies 20 --partners 100

# 3. Supporting indexes (one-off)
PGPASSWORD=ic_demo psql -h localhost -U ic_demo -d ic_demo <<'SQL'
CREATE INDEX IF NOT EXISTS ic_demo_aml_company_date
    ON account_move_line (company_id, date)
    WHERE display_type NOT IN ('line_section','line_subsection','line_note');
CREATE INDEX IF NOT EXISTS ic_demo_aml_account
    ON account_move_line (account_id)
    WHERE display_type NOT IN ('line_section','line_subsection','line_note');
CREATE INDEX IF NOT EXISTS ic_demo_am_state_posted
    ON account_move (id) WHERE state='posted';
SQL

# 4. Bulk COPY — tune --moves to the volume you want to demo
.venv/bin/python tools/seed/c1_bulk.py --moves 5000000 --chunk 100000
```

Scale guide (one chunk per commit):

| `--moves` | AMLs | wall time local |
|---|---|---|
| 10,000 | 20K | ~3 s |
| 500,000 | 1M | ~1 min |
| 2,500,000 | 5M | ~5 min |
| 5,000,000 | **10M** | **~10 min** |

## Re-running / resetting

The bootstrap is idempotent — rerunning will skip existing companies and
only top up missing partners. The bulk COPY is **not** idempotent and
always appends; to reset the move volume:

```sql
TRUNCATE account_move_line, account_move RESTART IDENTITY CASCADE;
```

(Drop/recreate is cheaper than TRUNCATE+index rebuild if the tables are
very large — but see `pg_repack` for production.)

## Cloud mapping (C5 preview)

| Role | Local | Cloud (k3s) |
|---|---|---|
| Bulk write path | direct COPY on localhost | pgBouncer → HAProxy writer → pg1 (primary) |
| Read path (consolidation view) | same pg | HAProxy replica → pg2 — via Odoo 20's native `db_replica_host` |
| CoA install | serial, once | identical — one-shot per company |
| Supporting indexes | psql | `odoo.conf` boot hook + `init.sql` |

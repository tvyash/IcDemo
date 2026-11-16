# C2 — local failover demo — captured run

**Date:** 2026-11-16 13:00 local
**Target:** `tools/c2/local_cluster.sh demo` on this laptop
**Topology:** host Postgres 18 binaries, two independent clusters:
`var/ha/primary` on :5433, `var/ha/replica` on :5434, streaming replication
with `wal_level = replica` and `hot_standby = on`.

## Captured output

```text
==> C2 — automatic-failover shaped demo
primary|5433|f|0/30000A0|1
replica|5434|t|0/30000A0|0

==> 1) write to primary (the app connects to :5433)
CREATE TABLE
INSERT 0 2
 payments_on_primary
---------------------
                   2

==> 2) confirm replica is caught up and sees the same rows
 payments_on_replica | is_replica
---------------------+------------
                   2 | t

==> 3) kill the primary (simulating a node failure)

==> 4) promote the replica — Patroni would do this automatically after DCS leader-lock timeout
waiting for server to promote.... done
server promoted

==> 5) new leader accepting writes, old payments survive
INSERT 0 1
 id | amount |         written_at         |      written_on
----+--------+----------------------------+-----------------------
  1 |  100.0 | 2026-11-16 13:00:22.809488 | primary-pre-failover
  2 |  200.0 | 2026-11-16 13:00:22.809488 | primary-pre-failover
 34 |  300.0 | 2026-11-16 13:00:27.957715 | replica-post-failover

 still_replica_role
--------------------
 f

==> done — replica is now primary, and data is intact across the transition
```

## Status after failover

```
primary : DOWN
replica|5434|f|0/3040338|0
```

- primary postmaster is gone (SIGKILLed)
- replica is now `in_recovery = f` → it is the new primary
- LSN advanced from `0/30000A0` (last sync) to `0/3040338` after the post-failover write

## What this proves

1. **Zero data loss on the rows observed up to the moment of the kill.**
   The two rows written to the primary before the kill are present on the
   promoted node after the kill.
2. **The promoted node accepts writes immediately** without needing a
   restart or re-config of anything but `pg_ctl promote` — which is the one
   thing Patroni automates by holding a leader lock in etcd.
3. **`pg_is_in_recovery()` flips correctly**, so Odoo / applications
   watching for the role flag (e.g. a session bootstrap check) will detect
   the role change at the next query.

## What's different in the cloud

The local demo does the promotion by hand (`pg_ctl promote`) to isolate the
Postgres-side mechanics. Patroni's job is to make that same promotion
automatic by:
1. Holding a leader-lock key in etcd with a 30 s TTL.
2. Refreshing it every `loop_wait = 10 s`.
3. If the lock expires, the standby Patroni grabs it and runs
   `pg_ctl promote` itself.

HAProxy (ops node) health-checks each Patroni's REST `/primary` and
`/replica` endpoints every 3 s, so the write VIP (port 5000) flips to the
new primary within 3-6 s *after* the promotion completes. Total unavailability
for writes: roughly **30-40 s** on a hard crash, **3-5 s** on a graceful
`systemctl stop patroni`.

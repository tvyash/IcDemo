# C2 — Patroni + etcd + HAProxy cloud topology

**Target cluster:**

```
                                   +--------------------+
                                   |  ops.internal      |
                                   |   etcd  (quorum)   |
                                   |   HAProxy          |
                                   +---------+----------+
                                             |
          +-----------------+  2379/2380  +---+---+  2379/2380  +-----------------+
          | pg1.internal    |-------------|  etcd |-------------| pg2.internal    |
          |   Postgres 18   |   REST 8008 |cluster|  REST 8008  |   Postgres 18   |
          |   Patroni       |             +-------+             |   Patroni       |
          +-----------------+                                   +-----------------+
                    \                                                  /
                     \         replication, selected by Patroni       /
                      +---------->  streaming WAL (hot standby)  <---+

   Clients write at haproxy:5000 (routed to whichever node reports /primary)
   Clients read  at haproxy:5001 (round-robin across nodes reporting /replica)
```

**File → where it goes:**

| File | On |
|---|---|
| `patroni-pg1.yml` | `/etc/patroni/patroni.yml` on pg1 |
| `patroni-pg2.yml` | `/etc/patroni/patroni.yml` on pg2 |
| `etcd.conf.yml`   | `/etc/etcd/etcd.conf.yml` on pg1, pg2, ops (edit `name` + `advertise-*` per node) |
| `haproxy.cfg`     | `/etc/haproxy/haproxy.cfg` on ops |

## Bring-up order (per-node ansible / terraform-shell)

1. `apt install etcd-server patroni postgresql-18 haproxy` (haproxy only on ops)
2. Write `/etc/etcd/etcd.conf.yml` on each etcd node. `systemctl enable --now etcd`. Wait for all three to show as members: `etcdctl --endpoints=http://pg1:2379,http://pg2:2379,http://ops:2379 member list`.
3. Write `/etc/patroni/patroni.yml` on pg1 and pg2. **Stop and mask** the distro-managed Postgres (`systemctl stop postgresql; systemctl mask postgresql`) — Patroni manages the cluster, systemd must not. `systemctl enable --now patroni`. The first one up bootstraps and takes the leader lock; the second clones via `pg_basebackup`.
4. Confirm cluster health: `patronictl -c /etc/patroni/patroni.yml list` — one `Leader`, one `Replica`.
5. On ops: write `haproxy.cfg`, `systemctl enable --now haproxy`. Status page on `http://ops:7000`.

## What makes failover automatic

Patroni writes a leader lock into etcd with a 30 s TTL (configurable). The leader refreshes the lock every `loop_wait` (10 s). If the leader stops refreshing (node crash, network partition, Postgres hung), the lock expires in etcd. The other Patroni member sees the vacancy, runs `pg_rewind` if needed, promotes its Postgres, and claims the lock. HAProxy's `http-check GET /primary` flips to the new node within 3-6 s. In total the write channel is interrupted for the TTL + check interval, roughly **30-40 s**.

Clients see this as "connection dropped, reconnect, writes resume" — Odoo's connection pool retries idempotently. Reads continue on the replica side throughout if any replica remained up.

## Failure modes covered

- **PG primary crash** → replica auto-promotes; demonstrated locally at `tools/c2/local_cluster.sh demo` (manual promote stands in for the Patroni/etcd decision).
- **PG primary hang** → Patroni's own health check (PG `SELECT 1`) fails, Patroni stops the stuck PG and the DCS lock expires.
- **Patroni crash on leader** → leader lock expires in TTL, failover proceeds.
- **Single etcd member loss** → 2 of 3 is quorum, cluster carries on.
- **Network partition isolating the leader** → leader fences itself (can't refresh the lock), stops accepting writes, replica takes over.

## What's NOT covered by this setup

- Two etcd members down simultaneously → DCS stops accepting writes; no automatic promotion while quorum is lost. 3-node layout with members on pg1, pg2, and ops is the mitigation.
- Data-loss window equal to `maximum_lag_on_failover` (1 MiB here). For zero-RPO use `synchronous_mode: true` and accept the latency.

# C2 — Postgres HA with Patroni + automatic failover — runbook

Challenge 2: show Postgres surviving a primary-node failure with automated
promotion of the replica, data preserved across the transition.

## What's in the box

| Piece | Where | Target |
|---|---|---|
| Local live-failover demo (streaming replication, manual promote stands in for Patroni's decision) | `tools/c2/local_cluster.sh` | runs on this laptop |
| Patroni config for cloud node pg1 | `infra/patroni/patroni-pg1.yml` | DO droplet pg1 |
| Patroni config for cloud node pg2 | `infra/patroni/patroni-pg2.yml` | DO droplet pg2 |
| 3-node etcd config template | `infra/patroni/etcd.conf.yml` | pg1, pg2, ops |
| HAProxy routing | `infra/haproxy/haproxy.cfg` | ops |
| Topology + failure-mode notes | `infra/patroni/README.md` | reference |

## Local path — live failover on this laptop

Uses the host Postgres 18 binaries only. Spins up a separate primary on :5433
and a streaming replica on :5434 under `var/ha/` — your main PG on :5432
(holding the C1/C4 data) is untouched.

```bash
./tools/c2/local_cluster.sh init     # one-off: initdb + base backup
./tools/c2/local_cluster.sh start    # start both nodes
./tools/c2/local_cluster.sh status   # show role + LSN
./tools/c2/local_cluster.sh demo     # the failover demo below
./tools/c2/local_cluster.sh stop     # shut them down
./tools/c2/local_cluster.sh clean    # wipe (destructive)
```

What `demo` does, step by step:

1. Prints the current status — primary on 5433, replica on 5434, matching LSN.
2. Writes two `payments` rows to the primary.
3. Reads from the replica — rows are there (`pg_is_in_recovery = t`).
4. **Kills the primary with SIGKILL** (simulating a node failure).
5. Promotes the replica via `pg_ctl promote`. In cloud, Patroni + etcd would
   do this automatically once the leader-lock TTL expires.
6. Writes a third row to the promoted node, then reads all three back.
   `pg_is_in_recovery` is now `f` — the replica is the new primary.

Captured output lives at [`docs/C2-results/local-failover.md`](C2-results/local-failover.md).

## Cloud path — Patroni-managed, automatic

7 Ubuntu 24.04 droplets (from the project proposal):
- 2 × pg1/pg2 (4 vCPU / 8 GB) running Postgres 18 + Patroni + etcd
- 1 × ops (2 vCPU / 4 GB) running HAProxy + etcd (tie-breaker) + Prometheus
- 3 × k3s nodes for the Odoo workload (C5)
- 1 × monitoring (merged with ops in the current plan)

### Bring-up on pg1 / pg2 / ops

Each node, as root:

```bash
apt install -y etcd-server patroni postgresql-18 haproxy
# (haproxy only on ops)

install -m 0644 patroni.yml /etc/patroni/patroni.yml        # the right file per node
install -m 0644 etcd.conf.yml /etc/etcd/etcd.conf.yml       # edit name + advertise-* per node

systemctl mask postgresql                                   # Patroni manages PG
systemctl enable --now etcd
systemctl enable --now patroni                              # only on pg1 / pg2
systemctl enable --now haproxy                              # only on ops
```

### Verifying the cluster

```bash
# On any etcd node:
etcdctl --endpoints=http://pg1:2379,http://pg2:2379,http://ops:2379 member list

# On pg1:
patronictl -c /etc/patroni/patroni.yml list
#  + Cluster: ic-demo-pg -----------+------------------+----+-----------+
#  | Member | Host    | Role        | State            | TL | Lag in MB |
#  +--------+---------+-------------+------------------+----+-----------+
#  | pg1    | pg1:5432| Leader      | running          |  1 |           |
#  | pg2    | pg2:5432| Replica     | streaming        |  1 |         0 |
#  +--------+---------+-------------+------------------+----+-----------+
```

### Triggering a failover

Simulated node loss:
```bash
# On pg1 (current leader):
systemctl stop patroni     # Patroni releases the lock cleanly; pg2 takes over in <5s
```

Hard failure:
```bash
# On pg1:
systemctl kill -s SIGKILL patroni
kill -9 $(pgrep -f 'postgres: 18 main')
# pg2 waits for the leader-lock TTL (30s), then promotes.
```

Timings:
- **Graceful** (`systemctl stop patroni`) → failover in **3-5 s** (no TTL wait).
- **Hard crash** → failover in **30-40 s** (DCS TTL + HAProxy check interval).

### Odoo config pointing at the cluster

`odoo/odoo.conf` on the application nodes:
```ini
db_host         = haproxy.ic-demo.internal
db_port         = 5000     # writes
db_user         = ic_demo
db_password     = ***
db_replica_host = haproxy.ic-demo.internal
db_replica_port = 5001     # reads, round-robined across replicas
```

Odoo 20's native read-replica route (`db_replica_host`) sends read-mostly
cursors to :5001, write cursors to :5000. HAProxy's http-check against
Patroni's `/primary` + `/replica` endpoints keeps routing accurate as the
cluster topology changes.

## Failure modes the local demo stands in for

| Failure | Local demo | Cloud behaviour |
|---|---|---|
| Primary PG crash | `kill -9` on primary postmaster | Patroni detects dead PG, releases lock; replica promotes within TTL |
| Primary host crash | (equivalent — process gone) | Lock TTL expires; replica promotes |
| Primary hung (deadlock, OOM) | not simulated locally | Patroni's `SELECT 1` check fails, Patroni stops PG and releases the lock |
| Network partition isolating leader | not simulated locally | Leader can't refresh lock → fences itself → replica promotes |
| etcd minority down (1 of 3) | n/a | No effect — quorum holds |
| etcd majority down (2 of 3) | n/a | DCS stops accepting writes; no promotion decision possible until a member returns. Mitigation: 3-node layout across different AZ-shaped hosts. |

## Known gaps vs. production

- `synchronous_mode: false` in both configs. Trade-off: can lose up to
  `maximum_lag_on_failover` bytes on hard failover. For zero-RPO flip to
  `true` and accept `synchronous_commit = remote_apply` latency.
- Passwords are placeholders (`CHANGE_ME_*`). The cloud bring-up should
  pass these via Ansible vault or similar.
- `pg_rewind` enabled (`use_pg_rewind: true`) so a former leader can rejoin
  as a replica without a full base backup after a split-brain is resolved.

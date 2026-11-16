#!/usr/bin/env bash
# C2 — local 2-node Postgres streaming-replication cluster, no sudo.
#
# This is the *failover primitive* Patroni orchestrates — physical streaming
# replication with hot_standby. Patroni's own job is only to automate the
# promotion decision via a DCS (etcd); the on-disk mechanics are identical.
# Running this locally lets us demonstrate the failover in-session without
# needing etcd + haproxy. The cloud profile uses the configs under
# infra/patroni/ and infra/haproxy/.
#
# Usage:
#   ./tools/c2/local_cluster.sh init     # one-off: initdb primary + base_backup replica
#   ./tools/c2/local_cluster.sh start    # start both PGs
#   ./tools/c2/local_cluster.sh status   # show role + LSN on each
#   ./tools/c2/local_cluster.sh demo     # run the failover demo
#   ./tools/c2/local_cluster.sh stop     # stop both PGs
#   ./tools/c2/local_cluster.sh clean    # wipe data dirs (destructive)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PG_BIN=/usr/lib/postgresql/18/bin
P_DATA="$ROOT/var/ha/primary"
R_DATA="$ROOT/var/ha/replica"
P_PORT=5433
R_PORT=5434
REPL_USER="icd_repl"
REPL_PASS="icd_repl"
APP_USER="icd_app"
APP_PASS="icd_app"
APP_DB="icd_ha"
LOG_DIR="$ROOT/var/log"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

pg_running() { # $1 = data dir
    [ -f "$1/postmaster.pid" ] && kill -0 "$(head -1 "$1/postmaster.pid")" 2>/dev/null
}

wait_up() { # $1 = port
    for _ in $(seq 1 30); do
        if "$PG_BIN"/psql -h 127.0.0.1 -p "$1" -U postgres -d postgres \
                -tAc "SELECT 1" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

cmd_init() {
    mkdir -p "$LOG_DIR" "$ROOT/var/ha"
    if [ -d "$P_DATA" ]; then warn "primary data dir already exists: $P_DATA"; else
        say "initdb primary ($P_DATA on port $P_PORT)"
        "$PG_BIN"/initdb -D "$P_DATA" -U postgres --auth=trust --encoding=UTF8 --locale=C.UTF-8 >/dev/null

        cat >>"$P_DATA/postgresql.conf" <<EOF

# --- ic_demo C2 primary --------------------------------------------------
listen_addresses = '127.0.0.1'
port = $P_PORT
unix_socket_directories = '$ROOT/var/ha/primary/sock'
wal_level = replica
max_wal_senders = 8
wal_keep_size = '512MB'
hot_standby = on
synchronous_commit = on
EOF
        mkdir -p "$ROOT/var/ha/primary/sock"

        cat >>"$P_DATA/pg_hba.conf" <<EOF

# ic_demo C2
host replication $REPL_USER 127.0.0.1/32 md5
host all         $APP_USER  127.0.0.1/32 md5
host all         postgres   127.0.0.1/32 trust
EOF

        say "starting primary briefly to create roles + db"
        "$PG_BIN"/pg_ctl -D "$P_DATA" -l "$LOG_DIR/pg_primary.log" start
        wait_up "$P_PORT"
        "$PG_BIN"/psql -h 127.0.0.1 -p "$P_PORT" -U postgres -d postgres <<SQL
CREATE ROLE $REPL_USER WITH REPLICATION LOGIN PASSWORD '$REPL_PASS';
CREATE ROLE $APP_USER WITH LOGIN PASSWORD '$APP_PASS';
CREATE DATABASE $APP_DB OWNER $APP_USER;
SQL
        "$PG_BIN"/pg_ctl -D "$P_DATA" stop
    fi

    if [ -d "$R_DATA" ]; then warn "replica data dir already exists: $R_DATA"; else
        say "pg_basebackup replica from primary (requires primary briefly up)"
        "$PG_BIN"/pg_ctl -D "$P_DATA" -l "$LOG_DIR/pg_primary.log" start
        wait_up "$P_PORT"
        PGPASSWORD=$REPL_PASS "$PG_BIN"/pg_basebackup -h 127.0.0.1 -p "$P_PORT" -U "$REPL_USER" \
            -D "$R_DATA" -R -X stream -Fp -P
        "$PG_BIN"/pg_ctl -D "$P_DATA" stop

        # Swap in the replica's port + socket.
        sed -i "s/^port = $P_PORT/port = $R_PORT/" "$R_DATA/postgresql.conf"
        sed -i "s|primary/sock|replica/sock|" "$R_DATA/postgresql.conf"
        mkdir -p "$ROOT/var/ha/replica/sock"
    fi

    say "init done. Run: ./tools/c2/local_cluster.sh start"
}

cmd_start() {
    if pg_running "$P_DATA"; then warn "primary already running"; else
        say "start primary :$P_PORT"
        "$PG_BIN"/pg_ctl -D "$P_DATA" -l "$LOG_DIR/pg_primary.log" start
        wait_up "$P_PORT" || warn "primary didn't come up cleanly"
    fi
    if pg_running "$R_DATA"; then warn "replica already running"; else
        say "start replica :$R_PORT"
        "$PG_BIN"/pg_ctl -D "$R_DATA" -l "$LOG_DIR/pg_replica.log" start
        wait_up "$R_PORT" || warn "replica didn't come up cleanly"
    fi
    cmd_status
}

cmd_stop() {
    if pg_running "$R_DATA"; then "$PG_BIN"/pg_ctl -D "$R_DATA" stop || true; fi
    if pg_running "$P_DATA"; then "$PG_BIN"/pg_ctl -D "$P_DATA" stop || true; fi
    say "stopped"
}

cmd_status() {
    for pair in "primary:$P_PORT" "replica:$R_PORT"; do
        name=${pair%:*}; port=${pair#*:}
        if "$PG_BIN"/psql -h 127.0.0.1 -p "$port" -U postgres -d postgres -tAX <<SQL 2>/dev/null
SELECT '$name'                                      AS node,
       $port                                        AS port,
       pg_is_in_recovery()                          AS in_recovery,
       CASE WHEN pg_is_in_recovery()
            THEN pg_last_wal_replay_lsn()::text
            ELSE pg_current_wal_lsn()::text
        END                                         AS lsn,
       (SELECT COUNT(*) FROM pg_stat_replication)   AS replicas_connected;
SQL
        then :; else echo "$name : DOWN"; fi
    done
}

cmd_demo() {
    say "C2 — automatic-failover shaped demo"
    cmd_status
    say "1) write to primary (the app connects to :$P_PORT)"
    PGPASSWORD=$APP_PASS "$PG_BIN"/psql -h 127.0.0.1 -p "$P_PORT" -U "$APP_USER" -d "$APP_DB" <<SQL
CREATE TABLE IF NOT EXISTS payments (id bigserial PRIMARY KEY, amount numeric, written_at timestamp DEFAULT now(), written_on text);
INSERT INTO payments(amount, written_on) VALUES (100.0, 'primary-pre-failover'), (200.0, 'primary-pre-failover');
SELECT count(*) AS payments_on_primary FROM payments;
SQL
    say "2) confirm replica is caught up and sees the same rows"
    sleep 1
    PGPASSWORD=$APP_PASS "$PG_BIN"/psql -h 127.0.0.1 -p "$R_PORT" -U "$APP_USER" -d "$APP_DB" <<SQL
SELECT count(*) AS payments_on_replica, pg_is_in_recovery() AS is_replica FROM payments;
SQL

    say "3) kill the primary (simulating a node failure)"
    PRIMARY_PID=$(head -1 "$P_DATA/postmaster.pid" 2>/dev/null || echo "")
    kill -9 "$PRIMARY_PID" 2>/dev/null || true
    rm -f "$P_DATA/postmaster.pid"
    sleep 2

    say "4) promote the replica — Patroni would do this automatically after DCS leader-lock timeout"
    "$PG_BIN"/pg_ctl -D "$R_DATA" promote
    sleep 2

    say "5) new leader accepting writes, old payments survive"
    PGPASSWORD=$APP_PASS "$PG_BIN"/psql -h 127.0.0.1 -p "$R_PORT" -U "$APP_USER" -d "$APP_DB" <<SQL
INSERT INTO payments(amount, written_on) VALUES (300.0, 'replica-post-failover');
SELECT * FROM payments ORDER BY id;
SELECT pg_is_in_recovery() AS still_replica_role;
SQL
    say "done — replica is now primary, and data is intact across the transition"
}

cmd_clean() {
    cmd_stop || true
    rm -rf "$P_DATA" "$R_DATA"
    say "wiped $P_DATA and $R_DATA"
}

case "${1:-}" in
    init)   cmd_init ;;
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    demo)   cmd_demo ;;
    clean)  cmd_clean ;;
    *) echo "Usage: $0 {init|start|stop|status|demo|clean}"; exit 2 ;;
esac

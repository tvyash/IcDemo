# C5 — k3s deployment + monitoring — runbook

Challenge 5: ship everything built in C1-C4 onto a 7-droplet DO cluster
with k3s, pgBouncer, HAProxy/Patroni (from C2), ingress-nginx, and a
Prometheus+Grafana monitoring stack.

## Droplet layout (from the project proposal, locked in with the server team)

```
 ops.ic-demo.internal        (2 vCPU / 4 GB)   HAProxy + etcd + Prometheus + Grafana
 pg1.ic-demo.internal        (4 vCPU / 8 GB)   Postgres 18 + Patroni + etcd
 pg2.ic-demo.internal        (4 vCPU / 8 GB)   Postgres 18 + Patroni + etcd
 k3s-1.ic-demo.internal      (4 vCPU / 8 GB)   k3s server + agent
 k3s-2.ic-demo.internal      (4 vCPU / 8 GB)   k3s agent
 k3s-3.ic-demo.internal      (4 vCPU / 8 GB)   k3s agent
 (monitoring merged onto ops — one less droplet than the original plan)
```

Everything binds to the private network (10.10.0.0/16); the only public
address is the ingress on k3s-1.

## File layout

| Path | Target |
|---|---|
| `infra/k3s/install-server.sh` | k3s-1 (first) |
| `infra/k3s/install-agent.sh` | k3s-2, k3s-3 |
| `infra/patroni/patroni-pg1.yml` | pg1 `/etc/patroni/patroni.yml` |
| `infra/patroni/patroni-pg2.yml` | pg2 `/etc/patroni/patroni.yml` |
| `infra/patroni/etcd.conf.yml` | pg1/pg2/ops `/etc/etcd/etcd.conf.yml` (edit per node) |
| `infra/haproxy/haproxy.cfg` | ops `/etc/haproxy/haproxy.cfg` |
| `infra/k8s/00-namespace.yaml` | in-cluster |
| `infra/k8s/secrets/odoo-secret.yaml` | in-cluster (template — real values via kubectl create) |
| `infra/k8s/odoo/configmap-odoo-conf.yaml` | in-cluster |
| `infra/k8s/odoo/deploy-{web,bus,cron}.yaml` | in-cluster |
| `infra/k8s/odoo/pvc-filestore.yaml` | in-cluster |
| `infra/k8s/pgbouncer/pgbouncer.yaml` | in-cluster |
| `infra/k8s/ingress/ingress.yaml` | in-cluster |
| `infra/k8s/monitoring/prometheus.yaml` | in-cluster |
| `infra/k8s/monitoring/grafana.yaml` | in-cluster |

## Bring-up order

### 1. Base infrastructure (DO droplets up, firewall + DNS set)

- Every droplet: `apt install -y curl` and nothing else. The install scripts
  pull everything they need.

### 2. Postgres HA (C2)

- Per the `C2-runbook.md` — stand up etcd on pg1/pg2/ops, then Patroni on
  pg1/pg2, then HAProxy on ops.
- Seed the demo DB once (`createdb ic_demo` via HAProxy writer :5000, then
  `psql` to apply the C1 index SQL and the bootstrap/bulk scripts over
  this remote PG).

### 3. k3s cluster

```bash
# On k3s-1:
bash infra/k3s/install-server.sh
# Grab the token it prints, then on k3s-2 + k3s-3:
export K3S_URL=https://k3s-1.ic-demo.internal:6443
export K3S_TOKEN='<token-from-k3s-1>'
bash infra/k3s/install-agent.sh
# Back on k3s-1:
kubectl get nodes   # expect 3 Ready
```

### 4. ingress-nginx

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
    --namespace ingress-nginx --create-namespace \
    --set controller.service.type=LoadBalancer
```

### 5. Workload

```bash
kubectl apply -f infra/k8s/00-namespace.yaml

# Real secrets — don't use the template file:
kubectl -n ic-demo create secret generic odoo-secret \
    --from-literal=admin_passwd='<admin-passphrase>' \
    --from-literal=db_password='<ic_demo-pg-password>'

kubectl apply -f infra/k8s/odoo/configmap-odoo-conf.yaml
kubectl apply -f infra/k8s/pgbouncer/pgbouncer.yaml       # edit userlist.txt md5 first
kubectl apply -f infra/k8s/odoo/pvc-filestore.yaml        # needs an RWX storage class
kubectl apply -f infra/k8s/odoo/deploy-web.yaml
kubectl apply -f infra/k8s/odoo/deploy-bus.yaml
kubectl apply -f infra/k8s/odoo/deploy-cron.yaml
kubectl apply -f infra/k8s/ingress/ingress.yaml
```

### 6. Monitoring

```bash
kubectl apply -f infra/k8s/monitoring/prometheus.yaml
kubectl apply -f infra/k8s/monitoring/grafana.yaml
# Then: kubectl -n ic-demo-monitoring port-forward svc/grafana 3000:3000
```

Install `node_exporter` as a system package on all 6 non-k3s droplets
(`apt install prometheus-node-exporter`). Patroni's own exporter is
already exposed on `:8008/metrics` — Prometheus picks it up from the
scrape config.

## Three Odoo deployments, and why the split

| Deployment | Replicas | Role | Key difference |
|---|---|---|---|
| `odoo-web` | HPA 2-8 | HTTP preforked workers (serves `/`, `/web/*`, RPC) | `workers = 9`, listens on :8069 |
| `odoo-bus` | 1 | Websocket fan-out (gevent) | `odoo-bin gevent`, listens on :8072 |
| `odoo-cron` | 1 | Scheduled jobs | `--no-http --max-cron-threads=2` |

The ingress sends `/websocket` and `/longpolling` to `odoo-bus` and
everything else to `odoo-web`. `odoo-cron` isn't exposed.

## Image note

The manifests reference `odoo:20.0`. For the real deploy, bake a private
image on top of that base containing:
- `/opt/odoo/enterprise` (Odoo 20 Enterprise checkout)
- `/opt/odoo/addons/oca` (empty for now)
- `/opt/odoo/addons/custom` (`ic_demo_dashboard`, `ic_demo_consolidation`)
- the Python deps from `requirements.txt`

A small `Dockerfile` for that layering is next up; it's not yet in the repo
because the base `odoo:20.0` image needs licensed Enterprise bits and the
registry story is a server-team call.

## What monitoring covers out of the box

- **Postgres cluster** (Patroni exporter):
  `patroni_primary` (gauge, 0/1 per node), `patroni_is_running`,
  `patroni_master_lag_bytes`, `patroni_last_leader_operation`.
- **etcd** — `etcd_server_has_leader`, `etcd_server_leader_changes_seen_total`,
  commit latency.
- **HAProxy** — per-backend connect/active/error counters, `/metrics` endpoint.
- **Hosts** — node_exporter on each droplet (CPU, memory, disk, network).
- **k8s pods** — cAdvisor via kubelet proxy (CPU / memory / network per pod).

Grafana dashboards shipped as empty stubs under
`infra/k8s/monitoring/grafana.yaml → grafana-dashboards` ConfigMap — fill
them in after a first live run with queries like:

```
# Odoo RPC throughput via nginx-ingress
sum(rate(nginx_ingress_controller_requests{ingress="odoo-web"}[1m])) by (status)

# Postgres active connections
sum(pg_stat_database_numbackends{datname="ic_demo"})

# Patroni master lag
patroni_master_lag_bytes / 1024 / 1024
```

## Failure drills to rehearse before the client demo

1. **Pod-level**: `kubectl -n ic-demo rollout restart deploy/odoo-web`.
   Check live sessions survive (sticky-less thanks to Odoo's cookie-based
   session store on the shared PVC).
2. **Node-level**: `kubectl drain k3s-2 --ignore-daemonsets`. Pods
   reschedule; HPA may spin up on the remaining nodes.
3. **Postgres**: `systemctl stop patroni` on pg1 (current leader).
   HAProxy's `/primary` check flips to pg2 within 3-6 s after Patroni
   promotes. Odoo retries idempotently.
4. **Ingress**: `kubectl -n ingress-nginx delete pod -l app.kubernetes.io/name=ingress-nginx`.
   New pod comes up; existing ws connections will reconnect to the bus.

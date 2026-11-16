#!/usr/bin/env bash
# Bootstrap the first k3s server (control plane) on k3s-1.
#
# Ships with:
#   - Traefik DISABLED (we install ingress-nginx ourselves for sticky-sessions
#     support on the websocket route — Traefik can do it but ingress-nginx
#     is more boring in the Odoo ecosystem).
#   - ServiceLB kept (so Service type=LoadBalancer works on bare DO droplets).
#   - A longer token used below for the agent join.

set -euo pipefail

TOKEN=${K3S_TOKEN:-"$(openssl rand -hex 32)"}
echo "K3S_TOKEN=${TOKEN}" > /root/k3s-token.env
chmod 600 /root/k3s-token.env

curl -sfL https://get.k3s.io | \
    INSTALL_K3S_CHANNEL=stable \
    INSTALL_K3S_EXEC="server --disable traefik --write-kubeconfig-mode 644" \
    K3S_TOKEN="${TOKEN}" \
    sh -

echo
echo "k3s server is up. Save this token for the worker join:"
echo "  ${TOKEN}"
echo
echo "Worker command (run on k3s-2 and k3s-3):"
echo "  curl -sfL https://get.k3s.io | K3S_URL=https://$(hostname -I | awk '{print $1}'):6443 K3S_TOKEN='${TOKEN}' sh -"

#!/usr/bin/env bash
# Join a k3s worker (k3s-2, k3s-3) to the control plane on k3s-1.
#
# Env vars required:
#   K3S_URL=https://k3s-1.ic-demo.internal:6443
#   K3S_TOKEN=<from install-server.sh output>

set -euo pipefail

: "${K3S_URL:?set K3S_URL=https://k3s-1.ic-demo.internal:6443}"
: "${K3S_TOKEN:?set K3S_TOKEN=<the-token>}"

curl -sfL https://get.k3s.io | \
    INSTALL_K3S_CHANNEL=stable \
    K3S_URL="${K3S_URL}" \
    K3S_TOKEN="${K3S_TOKEN}" \
    sh -

echo "Agent joined. From k3s-1 run: kubectl get nodes"

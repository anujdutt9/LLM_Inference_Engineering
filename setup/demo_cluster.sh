#!/usr/bin/env bash
# GPU box, once per instance. Everything the demo needs, in one go:
#   1. model weights download (in the background, it's the slowest part)
#   2. k3s + HAMi
#   3. Prometheus (5 s scrape, admin API for snapshots) + Grafana + DCGM + dashboards
#   4. the fleet → gateway (setup/demo_apply.sh): two vLLM 9B replicas (Setup B, default), or with
#      FLEET=27b-9b the 27B and the 9B (Setup A)
#   bash setup/demo_cluster.sh                  # Setup B
#   FLEET=27b-9b bash setup/demo_cluster.sh     # Setup A
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_lambda_only.sh"
cd "$ROOT"
mkdir -p "$ROOT/results/_cluster"
T0=$(date +%s)

echo "== 1. model download (background, log: results/_cluster/models.log) =="
bash "$ROOT/setup/demo_models.sh" > "$ROOT/results/_cluster/models.log" 2>&1 &
MODELS=$!

echo "== 2. k3s + HAMi =="
bash "$ROOT/setup/k3s_hami.sh"

echo "== 3. observability =="
bash "$ROOT/setup/observability.sh"

echo "== waiting for the model download =="
if ! wait "$MODELS"; then
  tail -20 "$ROOT/results/_cluster/models.log"
  echo "Model download failed; fix and re-run: bash setup/demo_models.sh && bash setup/demo_apply.sh"
  exit 1
fi
tail -4 "$ROOT/results/_cluster/models.log"

echo "== 4. fleet =="
bash "$ROOT/setup/demo_apply.sh"

echo
echo "Cluster ready in $(( ($(date +%s) - T0) / 60 )) min."
echo "Mac, with the tunnel (bash setup/ssh.sh) open:"
echo "  bash experiments/exp0_check.sh"
echo "  Grafana  http://127.0.0.1:31495   Prometheus http://127.0.0.1:31490   UI http://127.0.0.1:8080/ui/"

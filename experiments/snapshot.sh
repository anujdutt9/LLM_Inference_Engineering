#!/usr/bin/env bash
# Mac. Save a raw record of the whole stack right now, for one experiment's record.
# Run it before and after each experiment; counters are cumulative, so the two /metrics
# snapshots give exact per-run deltas. Safe at any time; nothing on the box changes.
#   bash experiments/snapshot.sh exp1-sql-before
#   bash experiments/snapshot.sh exp1-sql-after
# Writes results/runs/<name>/<stamp>/:
#   pods.txt events.txt            kubectl get pods -o wide, events in time order
#   logs/<deploy>.log              full container logs (+ .previous.log after a restart)
#   nvidia-smi.txt                 the host's view, then each pod's (HAMi slice) view
#   metrics/<engine>.prom          vLLM /metrics (Setup B: qwen-9b-a :8001, qwen-9b-b :8002;
#                                  Setup A: qwen-27b :8000, qwen-9b :8001)
#   metrics/gateway.prom           orch-serve /metrics
#   gateway-config.json gateway-state.json   live config and state (mode, variant, lanes)
set -euo pipefail
source "$(dirname "$0")/_common.sh"
name="${1:?usage: snapshot.sh <name>   e.g. exp1-sql-before}"
out="results/runs/$name/$STAMP"
mkdir -p "$out/logs" "$out/metrics"
K='export KUBECONFIG=/etc/rancher/k3s/k3s.yaml;'
if [[ "$(box "cat results/_cluster/fleet" 2>/dev/null)" == 27b-9b ]]; then
  ENGINES="vllm-27b vllm-9b"; PORTS="qwen-27b:8000 qwen-9b:8001"
else
  ENGINES="vllm-9b-a vllm-9b-b"; PORTS="qwen-9b-a:8001 qwen-9b-b:8002"
fi

box "$K kubectl get pods -A -o wide" > "$out/pods.txt" 2>&1 || true
box "$K kubectl get events -A --sort-by=.lastTimestamp" > "$out/events.txt" 2>&1 || true
for d in $(box "$K kubectl get deploy -o name" 2>/dev/null | sed 's#deployment.apps/##'); do
  box "$K kubectl logs deploy/$d --all-containers --timestamps" > "$out/logs/$d.log" 2>&1 || true
  if box "$K kubectl logs deploy/$d --previous --timestamps" > "$out/logs/$d.previous.log" 2>/dev/null; then :; else rm -f "$out/logs/$d.previous.log"; fi
done
{
  echo "== host"; box "nvidia-smi"
  for d in $ENGINES; do
    echo "== pod $d (HAMi view; HAMi-core log lines dropped)"
    box "$K kubectl exec deploy/$d -- nvidia-smi --query-gpu=memory.total,memory.used,utilization.gpu --format=csv 2>/dev/null" || echo "(not running)"
  done
} > "$out/nvidia-smi.txt" 2>&1
for pair in $PORTS; do
  box "curl -sf http://127.0.0.1:${pair#*:}/metrics" > "$out/metrics/${pair%%:*}.prom" 2>/dev/null || rm -f "$out/metrics/${pair%%:*}.prom"
done
curl -sf "$ORCH/metrics" > "$out/metrics/gateway.prom" || echo "(gateway /metrics not reachable)" > "$out/metrics/gateway.prom"
curl -sf "$ORCH/admin/config" > "$out/gateway-config.json" || true
curl -sf "$ORCH/admin/state" > "$out/gateway-state.json" || true
echo "snapshot → $out ($(du -sh "$out" | cut -f1))"

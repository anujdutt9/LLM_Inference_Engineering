#!/usr/bin/env bash
# GPU box. Deploy a fleet, then the gateway. Removes the other fleet's engines first. Safe to re-run.
#
# Setup B (FLEET=two-9b, the default): two identical Qwen3.5-9B replicas (vllm-9b-a :8001, then
# vllm-9b-b :8002, each on a 38 GiB HAMi slice); gateway/config.yaml.
#   bash setup/demo_apply.sh                     # the whole fleet
#   ONLY=9b bash setup/demo_apply.sh             # restart both 9B replicas (A, then B)
#   MAX_SEQS=16 bash setup/demo_apply.sh         # slots per replica (--max-num-seqs; default 8), on both
#   KV_GIB=4.5 bash setup/demo_apply.sh          # the same fixed KV cache on both replicas (~18.2 KB per token:
#                                                # 4.5 GiB ≈ 265K tokens); default: what fits in the slice (~1.0M)
#
# Setup A (FLEET=27b-9b): Qwen3.8-27B-FP8 (vllm-27b :8000, 46 GiB slice), then Qwen3.5-9B (vllm-9b :8001,
# 30.5 GiB slice); gateway/config-27b-9b.yaml.
#   FLEET=27b-9b bash setup/demo_apply.sh
#   ONLY=27b / ONLY=9b bash setup/demo_apply.sh  # restart one engine
#   BATCHED_9B=2048 ONLY=9b bash setup/demo_apply.sh            # the 9B's chunked-prefill size (default 8192)
#   GPUCORES_27B=60 GPUCORES_9B=40 bash setup/demo_apply.sh     # HAMi compute caps (default: none)
#
# Both:
#   ONLY=gateway bash setup/demo_apply.sh        # restart only the gateway (new gateway config)
#   VLLM_IMAGE=vllm/vllm-openai:<tag> bash setup/demo_apply.sh
# The fleet is remembered (results/_cluster/fleet), so ONLY=... keeps the one deployed last.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_lambda_only.sh"
cd "$ROOT"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.30.0}"
ONLY="${ONLY:-all}"
mkdir -p "$ROOT/results/_cluster"
FLEET="${FLEET:-$(cat "$ROOT/results/_cluster/fleet" 2>/dev/null || echo two-9b)}"
case "$FLEET" in
  two-9b) GATEWAY_CONFIG=gateway/config.yaml ;;
  27b-9b) GATEWAY_CONFIG=gateway/config-27b-9b.yaml ;;
  *) echo "FLEET must be two-9b or 27b-9b (got '$FLEET')" >&2; exit 1 ;;
esac
echo "$FLEET" > "$ROOT/results/_cluster/fleet"

if [[ "$FLEET" == two-9b ]]; then
  RENDERED="$ROOT/results/_cluster/hami-demo.rendered.yaml"
  KV_LINE="# KV cache: what fits in the slice"
  if [[ -n "${KV_GIB:-}" ]]; then
    KV_LINE="- --kv-cache-memory-bytes\n            - \"$(python3 -c "print(int(float('$KV_GIB') * 2**30))")\""
  fi
  MAX_SEQS="${MAX_SEQS:-8}"
  sed -e "s|__VLLM_IMAGE__|$VLLM_IMAGE|g" -e "s|# __KV_CACHE__|$KV_LINE|" \
      -e "/- --max-num-seqs/{n;s|\"8\"|\"$MAX_SEQS\"|;}" "$ROOT/k8s-config/hami/hami-demo.yaml" > "$RENDERED"
  echo "fleet=two-9b  image=$VLLM_IMAGE  slots per replica: $MAX_SEQS  KV per replica: $([[ -n "${KV_GIB:-}" ]] && echo "$KV_GIB GiB" || echo "what fits in the slice")"
  OTHER=(vllm-27b vllm-9b)
else
  RENDERED="$ROOT/results/_cluster/hami-27b-9b.rendered.yaml"
  BATCHED_9B="${BATCHED_9B:-8192}"
  cores27="# no gpucores cap" cores9="# no gpucores cap"
  [[ -n "${GPUCORES_27B:-}" ]] && cores27="nvidia.com/gpucores: \"$GPUCORES_27B\""
  [[ -n "${GPUCORES_9B:-}" ]] && cores9="nvidia.com/gpucores: \"$GPUCORES_9B\""
  sed -e "s|__VLLM_IMAGE__|$VLLM_IMAGE|g" -e "s|__BATCHED_9B__|$BATCHED_9B|g" \
      -e "s|# __GPUCORES_27B__|$cores27|" -e "s|# __GPUCORES_9B__|$cores9|" \
      "$ROOT/k8s-config/hami/hami-27b-9b.yaml" > "$RENDERED"
  echo "fleet=27b-9b  image=$VLLM_IMAGE  9B max-num-batched-tokens=$BATCHED_9B  gpucores 27B=${GPUCORES_27B:-none} 9B=${GPUCORES_9B:-none}"
  OTHER=(vllm-9b-a vllm-9b-b)
fi

# The other fleet's engines would hold GPU memory and the same host ports.
for d in "${OTHER[@]}"; do
  kubectl delete deploy "$d" --ignore-not-found --wait=true >/dev/null
  kubectl delete svc "$d" --ignore-not-found >/dev/null
done

wait_ready() {
  echo "== waiting for $1 (weights load + CUDA graphs: up to 30 min) =="
  kubectl rollout status "deploy/$1" --timeout=30m
}

boot_numbers() {
  local log="$ROOT/results/_cluster/boot-$1.log"
  kubectl logs "deploy/$1" > "$log" 2>&1 || true
  echo "-- $1 boot log: memory and KV --"
  grep -E "KV cache|Maximum concurrency|model weights took|Available KV|GPU blocks|block size|mamba|CUDA graph" "$log" | tail -12 || true
}

engine() {  # apply one engine's Deployment + Service, restart it if it already ran, wait
  local existed=0
  kubectl get "deploy/$1" >/dev/null 2>&1 && existed=1
  awk -v want="$1" 'BEGIN{RS="---"} $0 ~ "name: " want "\n" {print "---\n" $0}' "$RENDERED" | kubectl apply -f -
  [[ $existed == 1 ]] && kubectl rollout restart "deploy/$1" >/dev/null
  wait_ready "$1"
  boot_numbers "$1"
}

# one at a time: an engine profiling its memory while the other is still loading would misjudge the free space
if [[ "$FLEET" == two-9b ]]; then
  if [[ "$ONLY" == all || "$ONLY" == 9b ]]; then
    engine vllm-9b-a
    engine vllm-9b-b
  fi
else
  if [[ "$ONLY" == all || "$ONLY" == 27b ]]; then engine vllm-27b; fi
  if [[ "$ONLY" == all || "$ONLY" == 9b ]]; then engine vllm-9b; fi
fi
if [[ "$ONLY" == all || "$ONLY" == gateway ]]; then
  sed -e "s|value: /app/gateway/config.yaml|value: /app/$GATEWAY_CONFIG|" "$ROOT/k8s-config/gateway/orch-serve.yaml" | kubectl apply -f -
  kubectl rollout restart deploy/orch-serve >/dev/null
  wait_ready orch-serve
fi

echo
echo "== GPU =="
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
nvidia-smi --query-compute-apps=pid,used_gpu_memory --format=csv,noheader || true
kubectl get pods -o wide
echo
echo "Fleet $FLEET up. Mac (tunnel open): bash experiments/exp0_check.sh"

#!/usr/bin/env bash
# Laptop, no GPU. The real gateway (gateway/serve.py) in front of fake vLLM engines (router/fake_vllm.py:
# the OpenAI streaming API, vLLM-shaped /metrics, slots, prefix cache and engine-like timing), so the
# UI, the modes, Locust and the experiment helpers can all be tried without the GPU box.
#
#   bash setup/local_fleet.sh                   # Setup B: two fake qwen-9b replicas (:18001, :18002)
#   FLEET=27b-9b bash setup/local_fleet.sh      # Setup A: fake qwen-27b (:18000) and qwen-9b (:18001)
#   SPEED=5 bash setup/local_fleet.sh           # engines 5x faster (quick rehearsals)
#
# Gateway on http://127.0.0.1:8080 (UI: /ui/), runs logged under results/live/. Ctrl-C stops everything.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PY:-$ROOT/.venv/bin/python}"
FLEET="${FLEET:-two-9b}"
SPEED="${SPEED:-1}"
LOGS="$ROOT/results/local"
mkdir -p "$LOGS"

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; echo; echo "stopped"; }
trap cleanup EXIT INT TERM

engine() {  # engine <model> <port> [extra args]
  local model="$1" port="$2"; shift 2
  "$PY" -m router.fake_vllm --model "$model" --port "$port" --speed "$SPEED" "$@" > "$LOGS/engine-$model-$port.log" 2>&1 &
  pids+=($!)
}

case "$FLEET" in
  two-9b)
    engine qwen-9b 18001
    engine qwen-9b 18002
    export ORCH_CONFIG=gateway/config.yaml POOL_QWEN_9B_URLS=http://127.0.0.1:18001,http://127.0.0.1:18002
    ;;
  27b-9b)
    engine qwen-27b 18000
    engine qwen-9b 18001 --no-think
    export ORCH_CONFIG=gateway/config-27b-9b.yaml POOL_QWEN_27B_URLS=http://127.0.0.1:18000 POOL_QWEN_9B_URLS=http://127.0.0.1:18001
    ;;
  *) echo "FLEET must be two-9b or 27b-9b" >&2; exit 1 ;;
esac

ORCH_RESULTS_DIR="$ROOT/results" SERVE_HOST=127.0.0.1 SERVE_PORT=8080 "$PY" -m gateway.serve > "$LOGS/gateway.log" 2>&1 &
pids+=($!)

for _ in $(seq 60); do
  curl -sf http://127.0.0.1:8080/healthz >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf http://127.0.0.1:8080/healthz >/dev/null || { echo "gateway did not start; see $LOGS/gateway.log" >&2; tail -20 "$LOGS/gateway.log" >&2; exit 1; }
echo "Local fleet $FLEET up (fake engines; logs in results/local/)."
echo "  UI:       http://127.0.0.1:8080/ui/"
echo "  models:   $(curl -sf http://127.0.0.1:8080/v1/models | "$PY" -c 'import json,sys; print(", ".join(m["id"] for m in json.load(sys.stdin)["data"]))')"
echo "  metrics:  http://127.0.0.1:8080/metrics"
echo "Ctrl-C to stop."
wait

# Sourced by the experiments/ scripts (Mac). The tunnel from setup/ssh.sh must be open.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi
ORCH="${ORCH:-http://127.0.0.1:8080}"
ORCH="${ORCH%/v1}"
PROM="${PROM:-http://127.0.0.1:31490}"
GRAFANA="${GRAFANA:-http://127.0.0.1:31495}"
PY="${PY:-$ROOT/.venv/bin/python}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

need_gateway() {
  if ! curl -sf "$ORCH/healthz" >/dev/null; then
    echo "orch-serve not reachable at $ORCH. Is the tunnel open (bash setup/ssh.sh)?" >&2
    exit 1
  fi
}

box() {  # run a command on the GPU box, in ~/llm-inference
  if [[ -z "${LAMBDA:-}" || -z "${LAMBDA_SSH_KEY:-}" ]]; then
    echo "Set LAMBDA and LAMBDA_SSH_KEY in .env" >&2
    exit 1
  fi
  ssh -i "$LAMBDA_SSH_KEY" -o StrictHostKeyChecking=accept-new "$LAMBDA" "cd ~/llm-inference && $*"
}

set_mode() {
  curl -sf -X POST "$ORCH/admin/mode" -H 'content-type: application/json' -d "{\"mode\": \"$1\"}"
  echo
}

annotate() {  # a Grafana marker (tags: run + the given tag); fine if Grafana isn't reachable
  local text="$1" tag="$2"
  curl -sf -X POST "$GRAFANA/api/annotations" -H 'content-type: application/json' \
    -u "admin:${GRAFANA_PASSWORD:-admin}" \
    -d "{\"time\": $(($(date +%s) * 1000)), \"tags\": [\"run\", \"$tag\"], \"text\": \"$text\"}" >/dev/null 2>&1 || true
}

need_idle() {  # refuse to start a measured run while any call is in flight or queued on the gateway
  local busy
  busy=$(curl -sf "$ORCH/admin/state" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(r.get("calls") or []) for r in d["replicas"]) + sum(l.get("depth", 0) for l in d["lanes"].values()))')
  if [[ "$busy" != 0 ]]; then
    echo "$busy calls in flight or queued on the gateway: stop every other session first." >&2
    exit 1
  fi
}

apply_variant() {  # apply_variant <variant id recorded on every call> <group>/<name> from experiments/variants.yaml
  local patch
  patch=$("$PY" -c 'import json,sys,yaml; g,n=sys.argv[1].split("/"); print(json.dumps(yaml.safe_load(open("experiments/variants.yaml"))[g][n]))' "$2")
  curl -sf -X POST "$ORCH/admin/config" -H 'content-type: application/json' -d "{\"variant\": \"$1\", \"patch\": $patch}" >/dev/null
}

cold_start() {  # every replica's prefix cache emptied (the gateway resets all it knows, in or out of the pool)
  if ! curl -sf -X POST "$ORCH/admin/reset_prefix_cache" >/dev/null; then
    echo "prefix-cache reset failed on some replica: stopping so no run starts warm." >&2
    curl -s -X POST "$ORCH/admin/reset_prefix_cache" >&2 || true
    exit 1
  fi
}

restore_default() {
  curl -sf -X POST "$ORCH/admin/config" -H 'content-type: application/json' -d '{"variant": "default", "patch": {}}' >/dev/null || true
}
